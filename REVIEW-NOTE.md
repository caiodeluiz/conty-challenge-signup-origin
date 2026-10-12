# Nota de revisão: validade da origem no cadastro

O contrato 06 pede uma janela de validade da origem medida desde a primeira abertura do app e descreve como o cadastro consome essa origem. O coordenador da thread de origem e seu revisor independente concordaram em exigir validade também no instante da conversão. A implementação foi alinhada a essa decisão: prazo de sete dias inclusivo para receber e consumir a origem.

## Problema reproduzido e correção

Antes da correção, um toque do dia zero ainda atribuía um cadastro no dia 30. O ajuste verifica `registered_at > first_open_at + 7 dias` antes de permitir um vencedor. Nesse cenário, a resposta atual informa:

- `registered_at`: `2026-10-31T12:00:00.000000Z`;
- janela: `2026-10-01T12:00:00.000000Z` até `2026-10-08T12:00:00.000000Z`, inclusiva;
- atribuição: `status: organic`, campos de origem nulos, `reason: attribution_window_expired_before_signup`;
- toque: recebido no dia 0, `outcome: expired`, `reason: origin_expired_before_signup`.

No limite exato, a origem ainda pode vencer; no primeiro microssegundo seguinte, expira. Link inválido conserva seu motivo individual, e toque recebido após a janela continua `outside_attribution_window`. O motivo global de expiração vence em cadastro tardio mesmo sem toques. Essa prioridade está explícita no README.

## Validação e limites

Os 20 testes próprios cobrem limite exato, primeiro instante posterior, cadastro no dia 30, replay orgânico após reinício e replay de cadastro originalmente atribuído depois do prazo. A correção posterior de texto rejeita NUL e surrogates com HTTP400, preserva Unicode válido e impede escrita parcial, inclusive na primeira abertura com link inválido. A revisão independente final confirmou 30 casos adicionais; os coordenadores também leram diretamente os diffs completos, testes e documentos. Não houve revisão humana declarada.

O ajuste não recalcula cadastros já persistidos nem migra dados anteriores. Um replay continua devolvendo a decisão original; esta limitação é preservada para manter idempotência e histórico auditável. Não houve integração externa ou revisão humana confirmada.
