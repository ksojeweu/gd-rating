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
import re


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


def parse_lenient(text, name="файл"):
    """json.loads, но с прощением лишних запятых перед ] и } (частая ошибка при ручной правке): на такой случай
    печатается предупреждение. Если и так не разобралось, исключение json.JSONDecodeError с исходной причиной."""
    try:
        return json.loads(text)
    except ValueError as first:
        try:
            data = json.loads(re.sub(r",(\s*[\]}])", r"\1", text))
        except ValueError:
            raise first
        print(f"ВНИМАНИЕ: {name}: лишние запятые перед ] или }} (разобрал, но исправь файл): {first}")
        return data
