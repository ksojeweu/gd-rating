"""ID CoreBoard берутся из links.json (other_lists.coreboard.<борд>), отдельного coreboard_id_map.json больше нет.

В links.json у человека (ключ = наш ID):
  "other_lists": {"coreboard": {"main": {"id": 1, "nick": "Zoink"}, "mobile": {"id": 4, "nick": "Kristage"}}}
Один человек = один ID на борд. Допускается и короткая запись {"main": 12}.

load_id_map() отдаёт карту в прежнем виде, чтобы остальные скрипты читали её как раньше:
  {"<ID CoreBoard>": {"nick": ник на CoreBoard (если нет, _name), "gdr_id": наш ID}}
"""
from pathlib import Path

from jsonfmt import parse_lenient

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _links():
    for p in (ROOT / "links.json", HERE / "links.json"):
        if p.exists():
            return parse_lenient(p.read_text("utf-8"), p.name)
    print("ВНИМАНИЕ: links.json не найден, карта ID CoreBoard пустая")
    return {}


def load_id_map(board="main"):
    out, owner = {}, {}
    for gdr_id, entry in _links().items():
        if not isinstance(entry, dict):
            continue
        block = (entry.get("other_lists") or {}).get("coreboard")
        acc = block.get(board) if isinstance(block, dict) else None
        if isinstance(acc, list):           # на всякий случай: старый формат со списком
            acc = acc[0] if acc else None
        nick = None
        if isinstance(acc, dict):
            acc, nick = acc.get("id"), acc.get("nick")
        cb_id = str(acc).strip() if acc is not None else ""
        if not cb_id.isdigit():
            continue
        if cb_id in out:
            print(f"ВНИМАНИЕ: ID CoreBoard {board}:{cb_id} у двух людей ({owner[cb_id]} и {gdr_id}), беру первого")
            continue
        owner[cb_id] = gdr_id
        out[cb_id] = {"nick": nick or entry.get("_name") or "", "gdr_id": str(gdr_id)}
    return out
