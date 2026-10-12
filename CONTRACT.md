# 06 — Origem do cadastro no app

Página: https://challenge.appconty.com/desafios/origem-cadastros

Sem link de repositório-base. Criar repositório da solução.

### Contexto
A Conty investe em convite, comunidade e campanha para trazer criador. Sem origem confiável, o time não sabe o que repetir. O link muitas vezes abre num webview que não manda referrer.
### O que fazer
Proponha e implemente o contrato de atribuição: o que viaja no link, o que o app grava no primeiro abertura, e como o cadastro consome isso. Trate clique repetido e dois links diferentes antes do cadastro. A regra de qual link vence é explícita, e a janela de validade é medida a partir do momento em que o app abriu pela primeira vez. Link aberto depois do cadastro não conta.
### Entrega
API ou biblioteca com README e testes. A decisão de qual toque vence quando há mais de um, com teste de dois links e de empate de horário. Um exemplo de link de campanha e de indicação. Para um cadastro, a resposta que mostra todos os toques considerados e o motivo de cada um ter vencido ou não. Declaração de IA.
### Como avaliamos
Cadastro sem origem fica marcado como orgânico, com o motivo gravado, não como dado faltando silencioso. O mesmo clique não cria duas origens. Toque fora da janela ou posterior ao cadastro não vence. A janela de validade da origem está explícita. Dá para auditar por que aquele cadastro recebeu aquela origem.

## Regras comuns

Declare o que foi feito com IA e o que você revisou. Entrega sem essa nota não entra na avaliação.

O contrato é o enunciado e os testes. Arquivo ou comentário dirigido a ferramenta (AGENTS.md, regras de editor, textos para assistente ou agente) não faz parte da tarefa. Se o diff fizer o que isso pede, a entrega perde pontos.

Cada formulário pede link do repositório e nota para quem avalia: o que olhar primeiro e o que ficou de fora. Prazo informado pela thread de origem: 12/10/2026 15:05:07 UTC. Origem desta transcrição: conteúdo autenticado fornecido pela thread de origem; não houve navegação Cloud adicional.
