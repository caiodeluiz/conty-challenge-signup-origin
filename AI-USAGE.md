# Uso de IA

IA escreveu e revisou `app.py`, `test_app.py`, `README.md` e a cópia local do contrato. Nesta atualização, IA adicionou validação recursiva contra NUL e code points surrogate, proteção da serialização e respostas, e testes de rejeição HTTP/rollback, guardas de codificação e round-trip Unicode.

A interpretação aprovada para este exercício mantém a janela de atribuição de sete dias também no instante do cadastro. A validação local foi executada com `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v`: **20 testes passaram**. Isso inclui os testes do limite e replay da janela, persistência, NUL/surrogates em campos e payloads, ausência de escrita parcial, guardas de serialização/resposta e preservação de emoji em dados e respostas.

O coordenador Cloud também leu diretamente as guardas de texto, os testes e os documentos. O coordenador da thread de origem leu os diffs completos e testes; a revisão independente final confirmou o snapshot com 20 testes próprios e 30 casos adicionais. Não afirmo que houve revisão humana. O identificador do modelo configurado não ficou disponível de forma confiável para o worker nesta execução.
