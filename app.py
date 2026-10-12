"""Small, auditable signup-attribution API using only Python's standard library."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import unquote, urlsplit


WINDOW = timedelta(days=7)
MAX_BODY_BYTES = 64 * 1024
ALLOWED_SOURCE_TYPES = {"campaign", "referral"}
_IDENTIFIER_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]+$")


class AttributionError(Exception):
    """An expected API-level attribution error."""

    def __init__(self, status: int, reason: str, message: str):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message


def _validate_identifier(value: Any, field: str) -> str:
    _validate_unicode_tree(value)
    if not isinstance(value, str) or not value or value.strip() != value:
        raise AttributionError(400, f"invalid_{field}", f"{field} must be a non-empty string without surrounding whitespace")
    if len(value) > 256 or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise AttributionError(400, f"invalid_{field}", f"{field} must be at most 256 characters and contain no control characters")
    return value


def _contains_invalid_unicode(value: Any) -> bool:
    if isinstance(value, str):
        return any(
            character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    if isinstance(value, dict):
        return any(
            _contains_invalid_unicode(key) or _contains_invalid_unicode(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_invalid_unicode(item) for item in value)
    return False


def _validate_unicode_tree(value: Any) -> None:
    if _contains_invalid_unicode(value):
        raise AttributionError(
            400,
            "invalid_text_encoding",
            "text fields cannot contain NUL or surrogate code points",
        )


def _canonical_json(value: Any) -> str:
    _validate_unicode_tree(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(value: str) -> Any:
    return json.loads(value)


def _format_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError(f"stored timestamp is not UTC: {value}")
    return datetime.fromisoformat(value[:-1] + "+00:00")


class AttributionStore:
    """SQLite-backed attribution state for one local API process."""

    def __init__(
        self,
        database_path: str = "attribution.sqlite3",
        *,
        clock: Callable[[], datetime] | None = None,
        window: timedelta = WINDOW,
    ):
        if window < timedelta(0):
            raise ValueError("window cannot be negative")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._window = window
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        if database_path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _initialize(self) -> None:
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS installations (
                    installation_id TEXT PRIMARY KEY,
                    first_open_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS touches (
                    touch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    installation_id TEXT NOT NULL REFERENCES installations(installation_id),
                    click_id TEXT,
                    source_type TEXT,
                    source_id TEXT,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    is_valid INTEGER NOT NULL,
                    validation_reason TEXT,
                    outcome TEXT NOT NULL DEFAULT 'pending',
                    reason TEXT NOT NULL DEFAULT 'awaiting_registration',
                    UNIQUE (installation_id, click_id)
                );

                CREATE INDEX IF NOT EXISTS touches_by_installation_and_time
                    ON touches(installation_id, received_at, click_id);

                CREATE TABLE IF NOT EXISTS registrations (
                    installation_id TEXT PRIMARY KEY REFERENCES installations(installation_id),
                    registration_id TEXT NOT NULL UNIQUE,
                    registered_at TEXT NOT NULL,
                    cutoff_touch_id INTEGER NOT NULL,
                    attribution_status TEXT NOT NULL,
                    source_type TEXT,
                    source_id TEXT,
                    click_id TEXT,
                    winner_touch_id INTEGER,
                    decision_reason TEXT NOT NULL
                );
                """
            )

    def app_open(self, installation_id: Any, *, link_present: bool = False, link: Any = None) -> dict[str, Any]:
        installation_id = _validate_identifier(installation_id, "installation_id")
        now = _format_time(self._clock())
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO installations(installation_id, first_open_at) VALUES (?, ?)",
                (installation_id, now),
            )
            first_open_recorded = cursor.rowcount == 1
            installation = self._db.execute(
                "SELECT first_open_at FROM installations WHERE installation_id = ?", (installation_id,)
            ).fetchone()
            result: dict[str, Any] = {
                "installation_id": installation_id,
                "first_open_at": installation["first_open_at"],
                "first_open_recorded": first_open_recorded,
            }
            if link_present:
                touch, created = self._record_touch(installation_id, link, now)
                result["touch"] = {**touch, "duplicate": not created}
            return result

    def record_touch(self, installation_id: Any, link: Any) -> dict[str, Any]:
        installation_id = _validate_identifier(installation_id, "installation_id")
        now = _format_time(self._clock())
        with self._lock, self._db:
            if self._db.execute(
                "SELECT 1 FROM installations WHERE installation_id = ?", (installation_id,)
            ).fetchone() is None:
                raise AttributionError(
                    409,
                    "first_open_required",
                    "record /v1/app-open before recording signup touches",
                )
            touch, created = self._record_touch(installation_id, link, now)
            return {**touch, "duplicate": not created}

    def _record_touch(self, installation_id: str, link: Any, received_at: str) -> tuple[dict[str, Any], bool]:
        is_valid, validation_reason, click_id, source_type, source_id = self._validate_link(link)
        payload_json = _canonical_json(link)

        if click_id is not None:
            existing = self._db.execute(
                "SELECT * FROM touches WHERE installation_id = ? AND click_id = ?",
                (installation_id, click_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload_json:
                    raise AttributionError(
                        409,
                        "click_id_payload_conflict",
                        "this click_id is already stored with a different link payload",
                    )
                return self._touch_dict(existing), False

        registration = self._db.execute(
            "SELECT 1 FROM registrations WHERE installation_id = ?", (installation_id,)
        ).fetchone()
        if registration is not None:
            outcome, reason = "ignored", "after_signup"
        elif not is_valid:
            outcome, reason = "invalid", "invalid_link"
        else:
            outcome, reason = "pending", "awaiting_registration"

        cursor = self._db.execute(
            """
            INSERT INTO touches(
                installation_id, click_id, source_type, source_id, received_at,
                payload_json, is_valid, validation_reason, outcome, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                installation_id,
                click_id,
                source_type,
                source_id,
                received_at,
                payload_json,
                int(is_valid),
                validation_reason,
                outcome,
                reason,
            ),
        )
        row = self._db.execute("SELECT * FROM touches WHERE touch_id = ?", (cursor.lastrowid,)).fetchone()
        return self._touch_dict(row), True

    @staticmethod
    def _validate_link(link: Any) -> tuple[bool, str | None, str | None, str | None, str | None]:
        if not isinstance(link, dict):
            return False, "link_not_object", None, None, None
        _validate_unicode_tree(link)

        click_id_value = link.get("click_id")
        source_type_value = link.get("source_type")
        source_id_value = link.get("source_id")

        click_id = (
            click_id_value
            if isinstance(click_id_value, str)
            and click_id_value
            and click_id_value.strip() == click_id_value
            and len(click_id_value) <= 256
            and _IDENTIFIER_PATTERN.fullmatch(click_id_value)
            else None
        )
        source_type = source_type_value if isinstance(source_type_value, str) else None
        source_id = source_id_value if isinstance(source_id_value, str) else None

        if click_id is None:
            return False, "missing_or_invalid_click_id", None, source_type, source_id
        if not isinstance(source_type_value, str) or not source_type_value:
            return False, "missing_or_invalid_source_type", click_id, source_type, source_id
        if source_type_value not in ALLOWED_SOURCE_TYPES:
            return False, "unsupported_source_type", click_id, source_type, source_id
        if (
            not isinstance(source_id_value, str)
            or not source_id_value
            or source_id_value.strip() != source_id_value
            or len(source_id_value) > 256
            or not _IDENTIFIER_PATTERN.fullmatch(source_id_value)
        ):
            return False, "missing_or_invalid_source_id", click_id, source_type, source_id
        if set(link) != {"source_type", "source_id", "click_id"}:
            return False, "unexpected_link_fields", click_id, source_type, source_id
        return True, None, click_id, source_type, source_id

    def register(self, installation_id: Any, registration_id: Any) -> tuple[dict[str, Any], bool]:
        installation_id = _validate_identifier(installation_id, "installation_id")
        registration_id = _validate_identifier(registration_id, "registration_id")
        now = _format_time(self._clock())

        with self._lock, self._db:
            prior = self._db.execute(
                "SELECT * FROM registrations WHERE installation_id = ?", (installation_id,)
            ).fetchone()
            if prior is not None:
                if prior["registration_id"] != registration_id:
                    raise AttributionError(
                        409,
                        "registration_id_conflict",
                        "this installation already has a registration; retry with its original registration_id",
                    )
                return self._audit_dict(installation_id, replayed=True), False

            installation = self._db.execute(
                "SELECT first_open_at FROM installations WHERE installation_id = ?", (installation_id,)
            ).fetchone()
            if installation is None:
                raise AttributionError(
                    409,
                    "first_open_required",
                    "record /v1/app-open before registering",
                )

            first_open_at = installation["first_open_at"]
            first_open = _parse_time(first_open_at)
            registered_at = now
            signup_time = _parse_time(registered_at)
            deadline = first_open + self._window
            window_expired = signup_time > deadline
            cutoff_row = self._db.execute(
                "SELECT COALESCE(MAX(touch_id), 0) AS cutoff FROM touches WHERE installation_id = ?",
                (installation_id,),
            ).fetchone()
            cutoff_touch_id = int(cutoff_row["cutoff"])
            touches = self._db.execute(
                "SELECT * FROM touches WHERE installation_id = ? AND touch_id <= ? ORDER BY touch_id",
                (installation_id, cutoff_touch_id),
            ).fetchall()

            eligible: list[sqlite3.Row] = []
            classifications: dict[int, tuple[str, str]] = {}
            for touch in touches:
                if not touch["is_valid"]:
                    classifications[touch["touch_id"]] = ("invalid", "invalid_link")
                    continue
                received = _parse_time(touch["received_at"])
                if received < first_open:
                    classifications[touch["touch_id"]] = ("ignored", "before_first_open")
                elif received > signup_time:
                    classifications[touch["touch_id"]] = ("ignored", "received_after_signup_time")
                elif received > deadline:
                    classifications[touch["touch_id"]] = ("expired", "outside_attribution_window")
                elif window_expired:
                    classifications[touch["touch_id"]] = ("expired", "origin_expired_before_signup")
                else:
                    eligible.append(touch)

            winner: sqlite3.Row | None = None
            if eligible:
                latest_time = max(_parse_time(touch["received_at"]) for touch in eligible)
                same_time = [touch for touch in eligible if _parse_time(touch["received_at"]) == latest_time]
                winner = min(same_time, key=lambda touch: touch["click_id"])

            for touch in eligible:
                if winner is not None and touch["touch_id"] == winner["touch_id"]:
                    classifications[touch["touch_id"]] = ("winner", "last_touch_before_signup")
                elif winner is not None and _parse_time(touch["received_at"]) == _parse_time(winner["received_at"]):
                    classifications[touch["touch_id"]] = ("eligible_not_selected", "same_time_tie_lost_by_click_id")
                else:
                    classifications[touch["touch_id"]] = ("eligible_not_selected", "superseded_by_later_eligible_touch")

            if winner is None:
                status = "organic"
                source_type = source_id = click_id = None
                winner_touch_id = None
                decision_reason = ("attribution_window_expired_before_signup" if window_expired
                                   else "no_valid_touch_in_window_before_signup")
            else:
                status = "attributed"
                source_type = winner["source_type"]
                source_id = winner["source_id"]
                click_id = winner["click_id"]
                winner_touch_id = int(winner["touch_id"])
                decision_reason = "last_touch_before_signup"

            try:
                self._db.execute(
                    """
                    INSERT INTO registrations(
                        installation_id, registration_id, registered_at, cutoff_touch_id,
                        attribution_status, source_type, source_id, click_id,
                        winner_touch_id, decision_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        installation_id,
                        registration_id,
                        registered_at,
                        cutoff_touch_id,
                        status,
                        source_type,
                        source_id,
                        click_id,
                        winner_touch_id,
                        decision_reason,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise AttributionError(409, "registration_id_conflict", "registration_id is already in use") from error

            for touch_id, (outcome, reason) in classifications.items():
                self._db.execute(
                    "UPDATE touches SET outcome = ?, reason = ? WHERE touch_id = ?",
                    (outcome, reason, touch_id),
                )

            return self._audit_dict(installation_id, replayed=False), True

    def get_registration(self, installation_id: Any) -> dict[str, Any]:
        installation_id = _validate_identifier(installation_id, "installation_id")
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM registrations WHERE installation_id = ?", (installation_id,)
            ).fetchone()
            if row is None:
                raise AttributionError(404, "registration_not_found", "no registration exists for this installation")
            return self._audit_dict(installation_id, replayed=False)

    def _audit_dict(self, installation_id: str, *, replayed: bool) -> dict[str, Any]:
        registration = self._db.execute(
            "SELECT * FROM registrations WHERE installation_id = ?", (installation_id,)
        ).fetchone()
        installation = self._db.execute(
            "SELECT first_open_at FROM installations WHERE installation_id = ?", (installation_id,)
        ).fetchone()
        deadline = _format_time(_parse_time(installation["first_open_at"]) + self._window)
        rows = self._db.execute(
            "SELECT * FROM touches WHERE installation_id = ? ORDER BY received_at, click_id, touch_id",
            (installation_id,),
        ).fetchall()
        return {
            "registration": {
                "registration_id": registration["registration_id"],
                "installation_id": installation_id,
                "registered_at": registration["registered_at"],
            },
            "attribution": {
                "status": registration["attribution_status"],
                "source_type": registration["source_type"],
                "source_id": registration["source_id"],
                "click_id": registration["click_id"],
                "reason": registration["decision_reason"],
            },
            "window": {
                "starts_at": installation["first_open_at"],
                "ends_at_inclusive": deadline,
                "duration_seconds": int(self._window.total_seconds()),
            },
            "touches": [self._touch_dict(row) for row in rows],
            "replayed": replayed,
        }

    @staticmethod
    def _touch_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "touch_id": int(row["touch_id"]),
            "click_id": row["click_id"],
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "received_at": row["received_at"],
            "valid": bool(row["is_valid"]),
            "outcome": row["outcome"],
            "reason": row["reason"],
            "validation_reason": row["validation_reason"],
            "submitted_link": _parse_json(row["payload_json"]),
        }


class AttributionRequestHandler(BaseHTTPRequestHandler):
    store: AttributionStore
    server_version = "SignupAttribution/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep local use quiet; API results and errors are returned as JSON.
        return

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        if _contains_invalid_unicode(body):
            status = 500
            body = {
                "error": "invalid_response_text",
                "message": "response contains unsupported text",
            }
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise AttributionError(400, "invalid_content_length", "Content-Length must be an integer") from error
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            raise AttributionError(400, "invalid_body_size", f"JSON body must be between 1 and {MAX_BODY_BYTES} bytes")
        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AttributionError(400, "invalid_json", "request body must be valid UTF-8 JSON") from error
        if not isinstance(body, dict):
            raise AttributionError(400, "invalid_body", "request body must be a JSON object")
        _validate_unicode_tree(body)
        if self._contains_client_time(body):
            raise AttributionError(
                400,
                "client_time_not_accepted",
                "timestamps supplied by clients are not accepted; the server records receipt time",
            )
        return body

    @classmethod
    def _contains_client_time(cls, value: Any) -> bool:
        if isinstance(value, dict):
            forbidden = {"timestamp", "received_at", "occurred_at", "client_time", "event_time"}
            return bool(forbidden.intersection(value)) or any(cls._contains_client_time(item) for item in value.values())
        if isinstance(value, list):
            return any(cls._contains_client_time(item) for item in value)
        return False

    def _handle_error(self, error: AttributionError) -> None:
        self._send_json(error.status, {"error": error.reason, "message": error.message})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler interface
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        prefix = "/v1/registrations/"
        if path.startswith(prefix):
            try:
                result = self.store.get_registration(unquote(path[len(prefix) :]))
                self._send_json(200, result)
            except AttributionError as error:
                self._handle_error(error)
            return
        self._send_json(404, {"error": "not_found", "message": "unknown endpoint"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler interface
        path = urlsplit(self.path).path
        try:
            body = self._read_json()
            if path == "/v1/app-open":
                result = self.store.app_open(
                    body.get("installation_id"),
                    link_present="link" in body,
                    link=body.get("link"),
                )
                self._send_json(201 if result["first_open_recorded"] else 200, result)
            elif path == "/v1/touches":
                result = self.store.record_touch(body.get("installation_id"), body.get("link"))
                self._send_json(201 if not result["duplicate"] else 200, {"touch": result})
            elif path == "/v1/registrations":
                result, created = self.store.register(body.get("installation_id"), body.get("registration_id"))
                self._send_json(201 if created else 200, result)
            else:
                self._send_json(404, {"error": "not_found", "message": "unknown endpoint"})
        except AttributionError as error:
            self._handle_error(error)


def make_server(port: int, database_path: str = "attribution.sqlite3") -> ThreadingHTTPServer:
    store = AttributionStore(database_path)
    handler = type("BoundAttributionHandler", (AttributionRequestHandler,), {"store": store})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server.store = store  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Local signup attribution API (loopback only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default="attribution.sqlite3", help="SQLite file path (default: ./attribution.sqlite3)")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    server = make_server(args.port, args.db)
    address, port = server.server_address
    print(f"Signup attribution API listening on http://{address}:{port} (SQLite: {args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.store.close()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
