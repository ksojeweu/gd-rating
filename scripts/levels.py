"""Общие вещи про названия уровней (gd-rating, compare, add-video-links, sync-extras)."""
import re

# Особые названия: ID уровня в глобале -> как его называют в рейтинге и на листах.
# Нужны, когда у нескольких уровней одинаковое название или на листах уровень называют иначе, чем в API.
SPECIAL_LEVEL_NAMES = {
    "3299": "Sakupen circles (Nick24)",
    "3351": "Thinking Space II (Legacy)",
    "2234": "Deimos (Endlevel)",
}


def normalize(name):
    """Название для сравнения: без лишних пробелов и без учёта регистра."""
    return re.sub(r"\s+", " ", str(name or "").strip()).casefold()


def name_index(tp, levels):
    """Нормализованное название -> ключ уровня (ID глобала строкой или x... для уровней вне глобала).
    Порядок: levels_tp.json, особые названия, список глобала; побеждает первое найденное."""
    index = {}

    def add(name, key):
        n = normalize(name)
        if n and n not in index:
            index[n] = str(key)

    for key, e in tp.items():
        add(e.get("name"), key)
    for key, name in SPECIAL_LEVEL_NAMES.items():
        add(name, key)
    for l in levels:
        if l.get("id") is not None:
            add(SPECIAL_LEVEL_NAMES.get(str(l["id"]), l.get("name")), l["id"])
    return index
