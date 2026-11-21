"""Общий формат выходных файлов cbd/ (кроме cbd_videos.json): игрок -> ник и его записи.

  {"users": {
    "<ID>": [
      "ник",
      ["Название уровня", процент, "ссылка"],
      ...
    ]
  }}
"""
import json


def write_players(path, players):
    """players: {ключ игрока: (ник, [[название уровня, процент, ссылка], ...])}; порядок игроков сохраняется."""
    lines = ['{', '  "users": {']
    items = list(players.items())
    for i, (key, (nick, records)) in enumerate(items):
        lines.append(f'    {json.dumps(key, ensure_ascii=False)}: [')
        lines.append(f'      {json.dumps(nick, ensure_ascii=False)}' + ("," if records else ""))
        for j, r in enumerate(records):
            lines.append(f'      {json.dumps(r, ensure_ascii=False)}' + ("," if j < len(records) - 1 else ""))
        lines.append("    ]" + ("," if i < len(items) - 1 else ""))
    lines += ['  }', '}', '']
    path.write_text("\n".join(lines), "utf-8")
