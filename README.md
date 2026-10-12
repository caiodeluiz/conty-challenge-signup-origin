# Origem do cadastro no app

API local de atribuição de cadastro, feita com Python 3.12, `http.server` e SQLite. O link carrega os campos explícitos `source_type`, `source_id` e `click_id`; a solução não usa `Referer` nem outro referrer do navegador/webview.

## Onde começar

Para avaliar, comece por `app.py`, em `AttributionStore.register()` e `_audit_dict()`, e depois rode `python3 -m unittest -v`. `test_app.py` cobre as regras de escolha, a auditoria e um fluxo HTTP real em loopback.

O contrato completo do desafio está copiado em [`CONTRACT.md`](CONTRACT.md).

## Rodar

Não há dependências externas nem instalação de pacotes.

```sh
python3.12 app.py --db ./attribution.sqlite3 --port 8765
```

O servidor aceita conexões somente em `127.0.0.1`. A base SQLite permanece no caminho informado; use outro arquivo para começar uma demonstração limpa. O endpoint `GET /health` responde `{ "status": "ok" }`.

## Contrato do link e fluxo

Um sistema de links ou redirecionamento deve preencher um `click_id` novo para cada clique, preservando o tipo e o identificador da origem. Exemplos de links:

```text
https://app.conty.com/signup?source_type=campaign&source_id=creator-week-2026&click_id=clk_7f3a
https://app.conty.com/signup?source_type=referral&source_id=creator_42&click_id=clk_b981
```

`source_type` aceita `campaign` ou `referral`; `source_id` identifica a campanha ou quem indicou; `click_id` identifica o clique. O app lê esses três valores da URL de entrada e os envia explicitamente na abertura ou em um toque posterior. Na primeira abertura, grave a instalação e, se a abertura veio de um link, o toque no mesmo pedido:

```sh
curl -s http://127.0.0.1:8765/v1/app-open \
  -H 'Content-Type: application/json' \
  -d '{"installation_id":"device-001","link":{"source_type":"campaign","source_id":"creator-week-2026","click_id":"clk_7f3a"}}'
```

Todo texto recebido é verificado recursivamente antes de qualquer escrita, incluindo nomes de campos e payloads de links inválidos que seriam mantidos em `submitted_link`. NUL e code points surrogate isolados retornam HTTP 400 com `invalid_text_encoding`; a verificação cobre IDs, campos do link e texto arbitrário aninhado. Unicode válido, inclusive emoji, continua permitido e é preservado no SQLite e nas respostas JSON.

Se outro link for aberto antes do cadastro, envie-o como novo toque:

```sh
curl -s http://127.0.0.1:8765/v1/touches \
  -H 'Content-Type: application/json' \
  -d '{"installation_id":"device-001","link":{"source_type":"referral","source_id":"creator_42","click_id":"clk_b981"}}'
```

Ao concluir o cadastro, envie um `registration_id` estável para que uma repetição da mesma requisição seja idempotente:

```sh
curl -s http://127.0.0.1:8765/v1/registrations \
  -H 'Content-Type: application/json' \
  -d '{"installation_id":"device-001","registration_id":"signup-8842"}'
```

`POST /v1/registrations` retorna o cadastro completo e a auditoria. `GET /v1/registrations/{installation_id}` recupera a mesma atribuição persistida e os toques já recebidos. Se surgir um toque depois do cadastro, ele fica gravado com `outcome: "ignored"` e `reason: "after_signup"`; a atribuição original não é recalculada.

## Regra de atribuição

- A primeira chamada a `/v1/app-open` persiste `first_open_at`. A abertura posterior, inclusive a abertura de outro link, nunca reinicia esse instante.
- A validade é uma janela fixa de sete dias desde `first_open_at`, inclusiva no limite final. Não é contada desde o último clique nem desde o cadastro.
- A origem também precisa estar válida no instante do cadastro. Cadastro no limite exato pode receber o último toque elegível; cadastro um microssegundo após o limite é salvo como orgânico com `reason: "attribution_window_expired_before_signup"`, mesmo que exista um toque recebido no dia zero. A validade não se renova a cada toque.
- A API usa o horário UTC de recebimento no servidor para primeira abertura, toque e cadastro. Não aceita `timestamp`, `received_at`, `occurred_at`, `client_time` nem `event_time` enviados pelo cliente. Assim, o aparelho não consegue fazer um toque parecer futuro ou retroagir sua data. A hora do servidor é a referência operacional; sincronização incorreta do próprio servidor continua sendo uma limitação.
- Entre toques válidos recebidos dentro da janela e antes do cadastro, vence o mais recente (last touch). Se os horários forem iguais, vence o menor `click_id` em ordem lexical, comparada como string Unicode. A regra dá o mesmo resultado em uma repetição e após reiniciar o processo.
- Toques elegíveis que perdem ficam com `eligible_not_selected` e uma razão de desempate ou substituição. Toque depois do cadastro fica `ignored/after_signup`; toque recebido após os sete dias fica `expired/outside_attribution_window`. Um toque válido recebido dentro da janela perde por `expired/origin_expired_before_signup` quando o cadastro ocorre após o limite. Link inválido fica `invalid/invalid_link` e inclui `validation_reason` e `submitted_link` para auditoria.
- Cadastro feito dentro da janela e sem toque válido anterior recebe `status: "organic"` e `reason: "no_valid_touch_in_window_before_signup"`. Cadastro após o limite recebe `attribution_window_expired_before_signup`, inclusive sem toques: a expiração da janela tem precedência no motivo global. Os motivos individuais de links inválidos e de toques fora da janela permanecem na auditoria. Ausência de origem não fica silenciosa.
- Um `click_id` repetido na mesma instalação com o mesmo conteúdo retorna o toque original, sem atualizar horário nem criar uma segunda origem. O mesmo ID com conteúdo diferente responde HTTP 409 (`click_id_payload_conflict`). Um toque inválido sem `click_id` utilizável é registrado, mas não pode ser deduplicado pelo protocolo; por isso tem um `touch_id` interno.
- Uma instalação tem um único cadastro atribuído. Repetir o mesmo `registration_id` devolve a atribuição originalmente salva. Outro `registration_id` nessa instalação responde 409 para não sobrescrever o cadastro.

As razões são persistidas em SQLite junto de cada toque, e a decisão do cadastro (orgânico ou atribuído, origem vencedora, motivo e limite da janela) fica gravada separadamente. A resposta inclui todos os toques disponíveis no momento, com resultado individual e razão.

## Formato de resposta auditável

Um cadastro atribuído retorna uma estrutura deste tipo (os horários e IDs são exemplos):

```json
{
  "registration": {
    "registration_id": "signup-8842",
    "installation_id": "device-001",
    "registered_at": "2026-10-01T14:00:00.000000Z"
  },
  "attribution": {
    "status": "attributed",
    "source_type": "referral",
    "source_id": "creator_42",
    "click_id": "clk_b981",
    "reason": "last_touch_before_signup"
  },
  "window": {
    "starts_at": "2026-10-01T12:00:00.000000Z",
    "ends_at_inclusive": "2026-10-08T12:00:00.000000Z",
    "duration_seconds": 604800
  },
  "touches": [
    {"click_id": "clk_7f3a", "outcome": "eligible_not_selected", "reason": "superseded_by_later_eligible_touch"},
    {"click_id": "clk_b981", "outcome": "winner", "reason": "last_touch_before_signup"}
  ],
  "replayed": false
}
```

Na resposta real, cada toque também contém `touch_id`, `source_type`, `source_id`, `received_at`, `valid`, `validation_reason` e `submitted_link`. Um cadastro orgânico conserva `source_type`, `source_id` e `click_id` nulos e traz a razão orgânica no campo `attribution.reason`.

## Escopo e limites

Esta entrega cobre o contrato de transporte de origem, a janela, a escolha do toque, idempotência, persistência e auditoria. Não inclui autenticação, assinatura/verificação de links, interface do app, validação de campanhas ou pessoas em um catálogo externo, associação com uma conta de produção, limpeza/expurgo, analytics ou deployment. A API é uma demonstração local, sem hardening para exposição pública.

Para `n` toques de uma instalação, o cadastro classifica e seleciona em `O(n)`; montar a auditoria ordenada custa `O(n log n)`. O armazenamento é `O(n)` por instalação e mantém os registros para que uma reinicialização não perca a justificativa.

## Testes executados

Comando: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v` — **20 testes passaram**, incluindo o fluxo HTTP, o cadastro no limite exato, o instante posterior, o cadastro no dia 30, replay após expiração e persistência após reinício. As regressões de texto verificam rejeição HTTP 400 sem escrita parcial para NUL e surrogate isolado, inclusive num primeiro `/v1/app-open` com link inválido; também exercitam as guardas de serialização e resposta e a preservação de emoji em IDs e no payload auditado de link inválido.

## Uso de IA

IA escreveu e revisou o código, os testes e esta documentação, e executou os testes descritos acima. Esta declaração não afirma revisão humana. Veja também [`AI-USAGE.md`](AI-USAGE.md).
