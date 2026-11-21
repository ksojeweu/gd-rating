"""Реестр листов-источников (Pointercrate, WML и будущие) и общие правила ключей.

Новый лист: файл list_<название>.py рядом (образец list_wml.py): класс на основе Source с полями
name/prefix/tag/short и методом fetch(), внизу register(Класс()). Он подхватится сам.

Выключить лист без правки кода: enabled = False в его классе или переменная окружения GL_LISTS_OFF=wml (или wml,xyz).
Выключенный лист не качается, но его ключи (wml5) и записи в links.json по-прежнему распознаются.

Ключи записей: с цифры = аккаунт глобала; pc7, wml5 ... = человек без глобала (префикс задаёт лист).
"""
import glob
import importlib
import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # чтобы находились list_*.py и их зависимости


class Source:
    name = None        # название листа: ключ в other_lists и значение для --src
    prefix = None      # префикс ключа записи без глобала: wml -> wml5. Один префикс не должен начинаться с другого
    tag = None         # метка в рейтинге у игроков без глобала: [WML]
    short = None       # короткое имя листа
    fetchable = False  # умеет ли сам скачивать данные (Pointercrate скрипты качают по-своему)
    enabled = True

    def display_nick(self, nick):
        """Ник для показа (без клана и т.п.)."""
        return nick

    def fetch(self, store, workers=3, log=print):
        """None (лист недоступен) или {"players": {id: {"n": ник, "r": место, "c": страна}},
        "records": [{"a": id игрока, "l": название уровня, "p": процент, "v": видео, "i": игровой ID уровня или None,
        "pos": место уровня в листе или None}]}."""
        raise NotImplementedError


class JsonStore:
    """Кеш в json-файле (его использует make-links)."""

    def __init__(self, path):
        self.path, self.lock, self.data = path, threading.Lock(), {}
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        with self.lock:
            self.data[key] = value

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)


class DictStore:
    """Обёртка над уже существующим кешем-словарём (levelcache.json у gd-rating).
    save - функция скрипта, которая сама сохраняет кеш на диск."""

    def __init__(self, data, lock, save):
        self.data, self.lock, self._save = data, lock, save

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        with self.lock:
            self.data[key] = value

    def save(self):
        self._save()


_registry = []
_loaded = False


def register(src):
    """Добавляет лист. Проверяет, что название и префикс не пересекаются с уже добавленными."""
    for other in _registry:
        if src.name == other.name:
            raise ValueError(f"лист {src.name!r} уже зарегистрирован")
        if src.prefix.startswith(other.prefix) or other.prefix.startswith(src.prefix):
            raise ValueError(f"префиксы листов {src.name!r} ({src.prefix}) и {other.name!r} ({other.prefix}) "
                             f"пересекаются: ключи записей станет не отличить")
    if src.prefix[:1].isdigit():
        raise ValueError(f"префикс {src.prefix!r} не должен начинаться с цифры: так выглядят аккаунты глобала")
    _registry.append(src)
    return src


def _ensure():
    """Один раз подхватывает все list_*.py рядом с этим файлом."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for path in sorted(glob.glob(os.path.join(HERE, "list_*.py"))):
        mod = os.path.splitext(os.path.basename(path))[0]
        try:
            importlib.import_module(mod)
        except Exception as e:  # сломанный лист не должен ронять остальные
            print(f"Лист {mod}: не удалось подключить ({e}), пропускаю")


class _Pointercrate(Source):
    name, prefix, tag, short = "pointercrate", "pc", "PC", "pc"


register(_Pointercrate())


def all_lists():
    """Все листы (включая Pointercrate), в порядке регистрации."""
    _ensure()
    return list(_registry)


def is_enabled(src):
    off = os.environ.get("GL_LISTS_OFF", "").replace(";", ",").replace(" ", ",")
    return bool(src.enabled) and src.name.lower() not in {x.strip().lower() for x in off.split(",") if x.strip()}


def sources():
    """Включённые листы, которые умеют сами скачивать данные (всё, кроме Pointercrate)."""
    return [s for s in all_lists() if s.fetchable and is_enabled(s)]


def list_names():
    return tuple(s.name for s in all_lists())


def is_global(key):
    """Аккаунт глобала: ключ начинается с цифры."""
    return str(key)[:1].isdigit()


def source_of_key(key):
    """Лист, которому принадлежит ключ вида pc7 / wml5, или None."""
    k = str(key)
    return next((s for s in all_lists() if k.startswith(s.prefix)), None)


def tag_of(key):
    """Метка для вывода: ' [WML]' у человека без глобала, иначе пусто."""
    s = source_of_key(key)
    return f" [{s.tag}]" if s and not is_global(key) else ""


def key_parts(key):
    """'wml5' -> (лист WML, '5'). Для глобала и незнакомых ключей (None, ключ)."""
    s = source_of_key(key)
    return (s, str(key)[len(s.prefix):]) if s and not is_global(key) else (None, str(key))


OTHER_BLOCK = "other_lists"  # блок аккаунтов человека на других листах в записи links.json


def accounts_of(entry):
    """Аккаунты человека на всех листах из записи links.json: {лист: [ID строками]}.
    Читает и новый формат (блок other_lists), и старый (списки прямо в записи)."""
    out = {}
    if not isinstance(entry, dict):
        return out
    block = entry.get(OTHER_BLOCK) if isinstance(entry.get(OTHER_BLOCK), dict) else {}
    for s in all_lists():
        vals = block.get(s.name)
        if not isinstance(vals, (list, dict)):
            vals = entry.get(s.name)
        if isinstance(vals, dict):  # coreboard: {"main": 12, "mobile": 34} -> ["main:12", "mobile:34"]
            vals = [f"{m}:{i}" for m, ids in vals.items() for i in (ids if isinstance(ids, list) else [ids])]
        out[s.name] = [str(v).strip() for v in vals if str(v).strip()] if isinstance(vals, list) else []
    return out
