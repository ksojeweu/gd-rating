"""Общий формат выходных файлов cbd/ (кроме cbd_videos.json): игрок -> ник и его записи.

  {"users": {
    "<ID>": [
      "ник",
      ["Название уровня", процент, "ссылка"],
      ...
    ]
  }}

У новых игроков (missing_players*.json) вторым элементом идёт страна: код из 2 букв или null:
    "<ID>": ["ник", "PL", ["Название уровня", процент, "ссылка"], ...]
Читатели записей должны брать только элементы-списки (строка/null это не запись).
"""
import json
import re


def write_players(path, players):
    """players: {ключ игрока: (ник, [[название уровня, процент, ссылка], ...])}; порядок игроков сохраняется.
    Кортеж из 3 элементов (ник, записи, страна): страна пишется вторым элементом (код из 2 букв или null)."""
    lines = ['{', '  "users": {']
    items = list(players.items())
    for i, (key, value) in enumerate(items):
        nick, records = value[0], value[1]
        lines.append(f'    {json.dumps(key, ensure_ascii=False)}: [')
        if len(value) > 2:
            code = str(value[2] or "").strip().upper()
            code = code if len(code) == 2 and code.isalpha() else None
            lines.append(f'      {json.dumps(nick, ensure_ascii=False)},')
            lines.append(f'      {json.dumps(code)}' + ("," if records else ""))
        else:
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
