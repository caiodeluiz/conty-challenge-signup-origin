from __future__ import annotations

import json
import io
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import (
    AttributionError,
    AttributionRequestHandler,
    AttributionStore,
    _canonical_json,
)


class ManualClock:
    def __init__(self, current: datetime):
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class AttributionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.temp_dir.name}/attribution.sqlite3"
        self.clock = ManualClock(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))
        self.store = AttributionStore(self.db_path, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    @staticmethod
    def link(click_id: str, source_id: str, source_type: str = "campaign") -> dict[str, str]:
        return {"source_type": source_type, "source_id": source_id, "click_id": click_id}

    def app_open(self, install_id: str = "install-1", link: dict[str, str] | None = None) -> dict:
        return self.store.app_open(install_id, link_present=link is not None, link=link)

    def register(self, install_id: str = "install-1", registration_id: str = "signup-1") -> dict:
        return self.store.register(install_id, registration_id)[0]

    def test_two_links_choose_last_touch_and_explain_both(self) -> None:
        self.app_open(link=self.link("click-a", "launch"))
        self.clock.advance(timedelta(hours=2))
        self.store.record_touch("install-1", self.link("click-b", "referrer-7", "referral"))

        audit = self.register()

        self.assertEqual(audit["attribution"]["source_type"], "referral")
        self.assertEqual(audit["attribution"]["source_id"], "referrer-7")
        by_click = {touch["click_id"]: touch for touch in audit["touches"]}
        self.assertEqual(by_click["click-b"]["outcome"], "winner")
        self.assertEqual(by_click["click-a"]["reason"], "superseded_by_later_eligible_touch")

    def test_equal_timestamps_choose_lexically_smallest_click_id(self) -> None:
        self.app_open()
        self.store.record_touch("install-1", self.link("z-click", "z-origin"))
        self.store.record_touch("install-1", self.link("a-click", "a-origin", "referral"))

        audit = self.register()

        self.assertEqual(audit["attribution"]["click_id"], "a-click")
        by_click = {touch["click_id"]: touch for touch in audit["touches"]}
        self.assertEqual(by_click["z-click"]["reason"], "same_time_tie_lost_by_click_id")

    def test_duplicate_click_is_idempotent_and_conflicting_payload_is_409(self) -> None:
        self.app_open()
        first = self.store.record_touch("install-1", self.link("click-1", "campaign-a"))
        self.clock.advance(timedelta(minutes=1))
        repeated = self.store.record_touch("install-1", self.link("click-1", "campaign-a"))
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(first["touch_id"], repeated["touch_id"])
        self.assertEqual(first["received_at"], repeated["received_at"])

        with self.assertRaises(AttributionError) as error:
            self.store.record_touch("install-1", self.link("click-1", "campaign-b"))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(error.exception.reason, "click_id_payload_conflict")

    def test_first_open_never_moves_when_a_later_link_is_opened(self) -> None:
        initial = self.app_open(link=self.link("click-first", "first"))
        self.clock.advance(timedelta(days=4))
        second = self.store.app_open(
            "install-1", link_present=True, link=self.link("click-second", "second")
        )

        self.assertTrue(initial["first_open_recorded"])
        self.assertFalse(second["first_open_recorded"])
        self.assertEqual(initial["first_open_at"], second["first_open_at"])
        self.assertEqual(second["touch"]["received_at"], self.clock().isoformat(timespec="microseconds").replace("+00:00", "Z"))

    def test_window_boundary_is_inclusive(self) -> None:
        self.app_open()
        self.clock.advance(timedelta(days=7))
        self.store.record_touch("install-1", self.link("at-boundary", "boundary"))

        audit = self.register()

        self.assertEqual(audit["attribution"]["click_id"], "at-boundary")
        self.assertEqual(audit["window"]["duration_seconds"], 7 * 24 * 60 * 60)

    def test_signup_after_window_expires_touch_received_on_day_zero(self) -> None:
        opened = self.app_open(link=self.link("day-zero", "campaign-day-zero"))
        self.clock.advance(timedelta(days=30))

        audit = self.register(registration_id="signup-day-30")

        self.assertEqual(audit["registration"]["registered_at"], "2026-10-31T12:00:00.000000Z")
        self.assertEqual(audit["window"]["starts_at"], opened["first_open_at"])
        self.assertEqual(audit["window"]["ends_at_inclusive"], "2026-10-08T12:00:00.000000Z")
        self.assertEqual(audit["attribution"]["status"], "organic")
        self.assertIsNone(audit["attribution"]["click_id"])
        self.assertIsNone(audit["attribution"]["source_type"])
        self.assertIsNone(audit["attribution"]["source_id"])
        self.assertEqual(audit["attribution"]["reason"], "attribution_window_expired_before_signup")
        self.assertEqual(audit["touches"][0]["received_at"], opened["first_open_at"])
        self.assertEqual(audit["touches"][0]["outcome"], "expired")
        self.assertEqual(audit["touches"][0]["reason"], "origin_expired_before_signup")

    def test_day_zero_touch_can_attribute_signup_at_exact_window_end(self) -> None:
        self.app_open(link=self.link("day-zero", "launch"))
        self.clock.advance(timedelta(days=7))

        audit = self.register()

        self.assertEqual(audit["registration"]["registered_at"], audit["window"]["ends_at_inclusive"])
        self.assertEqual(audit["attribution"]["status"], "attributed")
        self.assertEqual(audit["attribution"]["click_id"], "day-zero")
        self.assertEqual(audit["touches"][0]["outcome"], "winner")

    def test_signup_one_microsecond_after_window_is_organic_and_replay_stable(self) -> None:
        self.app_open(link=self.link("day-zero", "launch"))
        self.clock.advance(timedelta(days=7, microseconds=1))

        audit = self.register()

        self.assertEqual(audit["registration"]["registered_at"], "2026-10-08T12:00:00.000001Z")
        self.assertEqual(audit["attribution"]["status"], "organic")
        self.assertEqual(audit["attribution"]["reason"], "attribution_window_expired_before_signup")
        self.assertEqual(audit["touches"][0]["reason"], "origin_expired_before_signup")
        self.clock.advance(timedelta(days=30))
        self.store.close()
        self.store = AttributionStore(self.db_path, clock=self.clock)
        replay, created = self.store.register("install-1", "signup-1")
        self.assertFalse(created)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["registration"], audit["registration"])
        self.assertEqual(replay["attribution"], audit["attribution"])
        self.assertEqual(replay["touches"], audit["touches"])

    def test_replay_after_expiry_keeps_prior_attributed_signup(self) -> None:
        self.app_open(link=self.link("in-time", "launch"))
        self.clock.advance(timedelta(days=7))
        original = self.register()
        self.clock.advance(timedelta(microseconds=1))

        replay, created = self.store.register("install-1", "signup-1")

        self.assertFalse(created)
        self.assertEqual(replay["attribution"], original["attribution"])
        self.assertEqual(replay["registration"], original["registration"])
        self.assertEqual(replay["attribution"]["status"], "attributed")

    def test_touch_outside_window_is_expired_and_does_not_win(self) -> None:
        self.app_open(link=self.link("inside", "campaign"))
        self.clock.advance(timedelta(days=7, microseconds=1))
        self.store.record_touch("install-1", self.link("expired", "late"))

        audit = self.register()

        by_click = {touch["click_id"]: touch for touch in audit["touches"]}
        self.assertEqual(audit["attribution"]["status"], "organic")
        self.assertIsNone(audit["attribution"]["click_id"])
        self.assertEqual(audit["attribution"]["reason"], "attribution_window_expired_before_signup")
        self.assertEqual(by_click["inside"]["outcome"], "expired")
        self.assertEqual(by_click["inside"]["reason"], "origin_expired_before_signup")
        self.assertEqual(by_click["expired"]["outcome"], "expired")
        self.assertEqual(by_click["expired"]["reason"], "outside_attribution_window")

    def test_touch_after_signup_is_persisted_as_ignored_and_does_not_change_winner(self) -> None:
        self.app_open(link=self.link("before", "campaign"))
        first_audit = self.register()
        self.clock.advance(timedelta(hours=1))
        late_touch = self.store.record_touch("install-1", self.link("after", "late", "referral"))
        second_audit = self.register()

        self.assertEqual(first_audit["attribution"]["click_id"], "before")
        self.assertEqual(second_audit["attribution"]["click_id"], "before")
        self.assertEqual(late_touch["outcome"], "ignored")
        self.assertEqual(late_touch["reason"], "after_signup")
        by_click = {touch["click_id"]: touch for touch in second_audit["touches"]}
        self.assertEqual(by_click["after"]["reason"], "after_signup")

    def test_organic_reason_is_saved_and_repeated_registration_replays_it(self) -> None:
        self.app_open()
        first, created = self.store.register("install-1", "signup-organic")
        replay, replay_created = self.store.register("install-1", "signup-organic")

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first["attribution"]["status"], "organic")
        self.assertEqual(first["attribution"]["reason"], "no_valid_touch_in_window_before_signup")
        self.assertEqual(replay["attribution"], first["attribution"])
        self.assertTrue(replay["replayed"])

    def test_registration_survives_store_restart(self) -> None:
        self.app_open(link=self.link("persist-me", "campaign-persist"))
        expected = self.register()
        self.store.close()
        self.store = AttributionStore(self.db_path, clock=self.clock)

        actual = self.store.get_registration("install-1")

        self.assertEqual(actual["registration"], expected["registration"])
        self.assertEqual(actual["attribution"], expected["attribution"])
        self.assertEqual(actual["touches"], expected["touches"])

    def test_invalid_link_reason_is_audited(self) -> None:
        self.app_open(link={"source_type": "coupon", "source_id": "x", "click_id": "invalid-1"})

        audit = self.register()

        self.assertEqual(audit["attribution"]["status"], "organic")
        self.assertEqual(audit["touches"][0]["outcome"], "invalid")
        self.assertEqual(audit["touches"][0]["validation_reason"], "unsupported_source_type")
        self.assertEqual(audit["touches"][0]["reason"], "invalid_link")

    def test_invalid_unicode_in_direct_store_open_rolls_back_installation(self) -> None:
        bad_link = {
            "source_type": "campaign",
            "source_id": "campaign-\ud800",
            "click_id": "click-valid",
        }

        with self.assertRaises(AttributionError) as error:
            self.store.app_open("direct-install", link_present=True, link=bad_link)

        self.assertEqual(error.exception.status, 400)
        self.assertEqual(error.exception.reason, "invalid_text_encoding")
        row = self.store._db.execute(
            "SELECT 1 FROM installations WHERE installation_id = ?", ("direct-install",)
        ).fetchone()
        self.assertIsNone(row)

        with self.assertRaises(AttributionError) as error:
            self.store.app_open("another-install", link_present=True, link={"opaque": "bad\x00text"})
        self.assertEqual(error.exception.reason, "invalid_text_encoding")
        row = self.store._db.execute(
            "SELECT 1 FROM installations WHERE installation_id = ?", ("another-install",)
        ).fetchone()
        self.assertIsNone(row)

    def test_touch_before_first_open_is_retained_but_not_eligible(self) -> None:
        self.app_open()
        self.clock.advance(timedelta(seconds=-1))
        self.store.record_touch("install-1", self.link("backwards-clock", "x"))

        audit = self.register()

        self.assertEqual(audit["attribution"]["status"], "organic")
        self.assertEqual(audit["touches"][0]["reason"], "before_first_open")


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AttributionStore(f"{self.temp_dir.name}/api.sqlite3")
        handler = type("BoundHandler", (AttributionRequestHandler,), {"store": self.store})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp_dir.cleanup()

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_http_flow_and_client_timestamp_rejection(self) -> None:
        status, opened = self.post("/v1/app-open", {"installation_id": "http-install", "link": {
            "source_type": "campaign", "source_id": "launch", "click_id": "http-click"
        }})
        self.assertEqual(status, 201)
        self.assertTrue(opened["first_open_recorded"])

        status, bad_timestamp = self.post("/v1/touches", {
            "installation_id": "http-install",
            "link": {"source_type": "referral", "source_id": "person", "click_id": "http-next"},
            "occurred_at": "2999-01-01T00:00:00Z",
        })
        self.assertEqual(status, 400)
        self.assertEqual(bad_timestamp["error"], "client_time_not_accepted")

        status, signup = self.post("/v1/registrations", {
            "installation_id": "http-install", "registration_id": "http-signup"
        })
        self.assertEqual(status, 201)
        self.assertEqual(signup["attribution"]["click_id"], "http-click")

    def test_http_rejects_nul_and_surrogates_before_persisting_any_part(self) -> None:
        valid_link = {
            "source_type": "campaign",
            "source_id": "source-ok",
            "click_id": "click-ok",
        }
        bad_open_bodies = [
            {"installation_id": "install-\ud800", "link": valid_link},
            {"installation_id": "install-\x00nul"},
            {
                "installation_id": "link-source-surrogate",
                "link": {**valid_link, "source_id": "source-\udfff"},
            },
            {
                "installation_id": "payload-surrogate",
                "link": {**valid_link, "metadata": {"note": "bad-\ud800"}},
            },
            {
                "installation_id": "payload-key-surrogate",
                "link": {**valid_link, "\ud800": "bad key"},
            },
            {
                "installation_id": "payload-nul",
                "link": {**valid_link, "metadata": {"note": "bad\x00text"}},
            },
        ]

        for body in bad_open_bodies:
            with self.subTest(installation_id=repr(body["installation_id"])):
                status, response = self.post("/v1/app-open", body)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"], "invalid_text_encoding")
                installation_id = body["installation_id"]
                if "\x00" in installation_id or any(
                    0xD800 <= ord(character) <= 0xDFFF for character in installation_id
                ):
                    self.assertEqual(
                        self.store._db.execute("SELECT COUNT(*) FROM installations").fetchone()[0],
                        0,
                    )
                    continue
                row = self.store._db.execute(
                    "SELECT 1 FROM installations WHERE installation_id = ?",
                    (installation_id,),
                ).fetchone()
                self.assertIsNone(row)

        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM installations").fetchone()[0], 0)
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM touches").fetchone()[0], 0)

        status, opened = self.post("/v1/app-open", {"installation_id": "safe-install"})
        self.assertEqual(status, 201)
        self.assertTrue(opened["first_open_recorded"])

        invalid_touch_links = [
            {**valid_link, "click_id": "click-\ud800"},
            {**valid_link, "click_id": "click-\x00"},
            {**valid_link, "source_type": "type-\x00nul"},
            {**valid_link, "source_type": "type-\ud800"},
            {**valid_link, "source_id": "source-\ud800"},
            {**valid_link, "source_id": "source-\x00"},
        ]
        for link in invalid_touch_links:
            with self.subTest(link=repr(link)):
                status, response = self.post(
                    "/v1/touches", {"installation_id": "safe-install", "link": link}
                )
                self.assertEqual(status, 400)
                self.assertEqual(response["error"], "invalid_text_encoding")
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM touches").fetchone()[0], 0)

        for registration_id in ("signup-\ud800", "signup-\x00"):
            with self.subTest(registration_id=repr(registration_id)):
                status, response = self.post(
                    "/v1/registrations",
                    {"installation_id": "safe-install", "registration_id": registration_id},
                )
                self.assertEqual(status, 400)
                self.assertEqual(response["error"], "invalid_text_encoding")
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0], 0)

    def test_valid_unicode_and_emoji_survive_sqlite_and_audit_response(self) -> None:
        installation_id = "install-🧑‍🚀"
        registration_id = "signup-🥳"
        valid_link = {
            "source_type": "campaign",
            "source_id": "campaign-🎨",
            "click_id": "click-🧪",
        }
        status, opened = self.post(
            "/v1/app-open", {"installation_id": installation_id, "link": valid_link}
        )
        self.assertEqual(status, 201)
        self.assertEqual(opened["installation_id"], installation_id)
        self.assertEqual(opened["touch"]["submitted_link"], valid_link)

        # The extra field makes this link invalid by contract, but its valid
        # Unicode payload is retained for audit rather than lost or mangled.
        invalid_but_unicode_link = {
            "source_type": "referral",
            "source_id": "referrer-🌱",
            "click_id": "click-🦊",
            "note": {"label": "emoji-👋"},
        }
        status, touch = self.post(
            "/v1/touches",
            {"installation_id": installation_id, "link": invalid_but_unicode_link},
        )
        self.assertEqual(status, 201)
        self.assertEqual(touch["touch"]["submitted_link"], invalid_but_unicode_link)
        self.assertEqual(touch["touch"]["validation_reason"], "unexpected_link_fields")

        status, audit = self.post(
            "/v1/registrations",
            {"installation_id": installation_id, "registration_id": registration_id},
        )
        self.assertEqual(status, 201)
        self.assertEqual(audit["registration"]["registration_id"], registration_id)
        self.assertEqual(audit["attribution"]["source_id"], "campaign-🎨")
        by_click = {item["click_id"]: item for item in audit["touches"]}
        self.assertEqual(by_click["click-🧪"]["outcome"], "winner")
        self.assertEqual(by_click["click-🦊"]["submitted_link"], invalid_but_unicode_link)

    def test_canonical_and_response_json_guards_do_not_encode_surrogates(self) -> None:
        with self.assertRaises(AttributionError) as error:
            _canonical_json({"submitted_link": {"note": "bad-\ud800"}})
        self.assertEqual(error.exception.reason, "invalid_text_encoding")

        class ResponseRecorder:
            def __init__(self) -> None:
                self.status = None
                self.headers = []
                self.wfile = io.BytesIO()

            def send_response(self, status: int) -> None:
                self.status = status

            def send_header(self, name: str, value: str) -> None:
                self.headers.append((name, value))

            def end_headers(self) -> None:
                return

        recorder = ResponseRecorder()
        AttributionRequestHandler._send_json(
            recorder, 200, {"touch": {"submitted_link": "bad-\ud800"}}
        )
        self.assertEqual(recorder.status, 500)
        self.assertEqual(
            json.loads(recorder.wfile.getvalue()),
            {
                "error": "invalid_response_text",
                "message": "response contains unsupported text",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
