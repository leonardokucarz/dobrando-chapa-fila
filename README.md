# Fila de publicação — @dobrando_chapa

Fila temporária de Reels aprovados para o Instagram @dobrando_chapa.

- `fila.json`: os posts aprovados, com horário, legenda e situação.
- `m/<post>/`: vídeo, capa e story de cada post, servidos pelo GitHub Pages para o Instagram baixar na hora de publicar.

As rotinas de publicação (12h00 e 19h30, horário de Brasília) leem esta fila e publicam pelo Windsor.ai. Depois que o post sai, os arquivos dele são apagados daqui.
