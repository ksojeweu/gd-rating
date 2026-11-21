#!/usr/bin/env python3
"""Ищет видео для достижений из <out-dir>/missing_achievements.json и собирает команды /fulladd и /try.
Вызывается из cbd-update.py:  add-video-links.py --out-dir DIR --id-map coreboard_id_map.json --prev-players cbd/missing_players.json

Читает (из DIR):  missing_achievements.json, missing_achievements_with_nicks.json, missing_players.json.
Пишет (в DIR):    commands.txt, ссылки в оба missing_achievements*.json и в первую запись каждого игрока из
                  missing_players.json, а для отладки missing_video_links.txt, video_link_sources.txt,
                  missing_players_video_links.txt.
Видео ищутся в GlobalList (все аккаунты человека) и в Pointercrate.
"""
import argparse
import html
import json
import re
import time
from pathlib import Path

import requests

import levels as levelsearch
import lists_core as L

parser = argparse.ArgumentParser()
parser.add_argument("--out-dir", required=True, help="папка cbd-update")
parser.add_argument("--id-map", required=True, help="coreboard_id_map.json: в командах вместо нашего ID пишется ник CoreBoard")
parser.add_argument("--prev-players", required=True,
                    help="прошлый опубликованный cbd/missing_players.json: ссылки, уже найденные в нём, заново не ищутся")
args = parser.parse_args()

OUT_DIR = Path(args.out_dir)
INPUT_FILE = OUT_DIR / "missing_achievements.json"
NICKS_FILE = OUT_DIR / "missing_achievements_with_nicks.json"
NOT_FOUND_FILE = OUT_DIR / "missing_video_links.txt"
SOURCES_FILE = OUT_DIR / "video_link_sources.txt"
COMMANDS_FILE = OUT_DIR / "commands.txt"
PLAYERS_FILE = OUT_DIR / "missing_players.json"
PLAYERS_NOT_FOUND_FILE = OUT_DIR / "missing_players_video_links.txt"
ID_MAP_FILE = Path(args.id_map)
LEVELS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"
LINKS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/refs/heads/main/links.json"
FIX_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/fix.json"  # склейки аккаунтов глобала (merges)
GLOBAL_API = "https://api.demonlist.org"
DEMONLIST_LEVEL_URL = "https://demonlist.org/demonlist/level/{level_id}"
POINTERCRATE_API = "https://pointercrate.com/api/v1"
POINTERCRATE_DEMONS_API = "https://pointercrate.com/api/v2/demons"

SPECIAL_LEVEL_NAMES = levelsearch.SPECIAL_LEVEL_NAMES
LIMIT = 100
DELAY = 0.15
TIMEOUT = 30
TRIES = 5        # сколько раз пробовать запрос, если сервер не отвечает
RETRY_PAUSE = 2  # базовая пауза между попытками (секунд), растёт с каждой попыткой
RECHECK_ROUNDS = 3  # сколько раз перепроверять запись, если видео не нашлось из-за сбоя запросов
RECHECK_PAUSE = 15  # базовая пауза между такими проходами (секунд): 15, 30, 45

session = requests.Session()
session.headers.update({"User-Agent": "GD-Video-Link-Finder/1.0", "Accept": "application/json"})

global_records_cache = {}
links_dl_extra = {}   # ID человека из links.json -> его дополнительные аккаунты на глобале
fix_absorbed = None   # ID главного аккаунта -> ID аккаунтов, склеенных в него (fix.json); None пока не загружен
global_level_cache = {}
pointer_demon_cache = {}
pointer_records_cache = {}
level_name_cache = {}    # ID уровня -> название
name_failed_levels = []  # уровни, название которых не нашлось нигде

# Ровно 11 символов после начала ссылки: youtu.be/ID, youtube.com/watch?v=ID, /shorts/, /live/, /embed/
YOUTUBE_ID_PATTERN = re.compile(
    r'(?:youtu\.be/'
    r'|youtube\.com/(?:watch\?(?:[^\s]*?&)?v=|shorts/|live/|embed/|v/))'
    r'([A-Za-z0-9_-]{11})'
)
# "first beaten by <a href="/demonlist/player/3988">ikkyseen</a>" на странице уровня demonlist.org
FIRST_BEATEN_RE = re.compile(r'first beaten by\s*<a[^>]*href="/demonlist/player/(\d+)"[^>]*>(.*?)</a>', re.S | re.I)


def get_json(url, params=None):
    """Запрос -> (json, response). Обрыв связи, таймаут, 429 и 5xx повторяем (TRIES раз, пауза растёт), остальное нет."""
    last_error = None
    for attempt in range(1, TRIES + 1):
        try:
            response = session.get(url, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            return response.json(), response
        except (requests.RequestException, ValueError) as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is not None and status < 500 and status != 429:
                raise
            last_error = error
            if attempt < TRIES:
                pause = RETRY_PAUSE * attempt
                print(f"    Повтор {attempt}/{TRIES - 1} через {pause} с: {error}")
                time.sleep(pause)
    raise last_error


def get_video(record):
    for field in ("video_url", "video", "video_link"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_percent(value):
    """Процент -> int. Пустое значение = 100. None, если не разобрать."""
    text = str(value if value is not None else "").strip().replace("%", "")
    if text == "":
        return 100
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return None


def record_percent(record, *fields):
    """Процент из записи API (в GlobalList percent, в Pointercrate progress)."""
    for field in fields:
        value = record.get(field)
        if value is not None:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
    return None


def player_id_from_key(key):
    return str(key).split(" — ", 1)[0].strip()


def normalize_id(player_id):
    return str(player_id).strip().casefold()


def normalize_link(link):
    """YouTube-ссылку приводит к виду https://www.youtube.com/watch?v=ID. Остальное и пустое остаётся как есть."""
    link = str(link).strip()
    match = YOUTUBE_ID_PATTERN.search(link)
    return f"https://www.youtube.com/watch?v={match.group(1)}" if match else link


def get_links():
    """links.json -> {ID глобала: {лист: [ID аккаунтов]}}, например {"123": {"pointercrate": ["7"], "wml": ["5"]}}."""
    data, _ = get_json(LINKS_URL)
    links = {}
    list_names = set(L.list_names())
    links_dl_extra.clear()
    for global_id, entry in data.items():
        if isinstance(entry, dict):
            links[str(global_id)] = L.accounts_of(entry)
            # Доп. аккаунты того же человека на глобале: в links.json под сайтом, в названии которого есть demonlist/global
            blocks = [{k: v for k, v in entry.items() if k not in ("_name", "channels", L.OTHER_BLOCK)}]
            if isinstance(entry.get(L.OTHER_BLOCK), dict):
                blocks.append(entry[L.OTHER_BLOCK])
            for block in blocks:
                for site, ids in block.items():
                    if site in list_names or not any(w in str(site).lower() for w in ("demonlist", "global")):
                        continue
                    ids = ids if isinstance(ids, list) else [ids]
                    bucket = links_dl_extra.setdefault(str(global_id), [])
                    bucket.extend(str(x) for x in ids if x is not None and str(x) not in bucket)
    return links


# ---------------- GLOBAL DEMONLIST ----------------

def get_global_records(user_id):
    user_id = int(user_id)
    if user_id in global_records_cache:
        return global_records_cache[user_id]

    records, offset = [], 0
    while True:
        data, _ = get_json(f"{GLOBAL_API}/user/record/list", {"user_id": user_id, "limit": 50, "offset": offset})
        if data.get("message") != "success":
            raise RuntimeError(f"Global API error: {data}")
        payload = data.get("data", {})
        page = payload.get("records", [])
        records.extend(page)
        if not page or len(records) >= payload.get("total_count", len(records)):
            break
        offset += len(page)
        time.sleep(DELAY)

    global_records_cache[user_id] = records
    return records


def get_global_level(level_id):
    level_id = int(level_id)
    if level_id not in global_level_cache:
        data, _ = get_json(f"{GLOBAL_API}/level/classic/get", {"id": level_id})
        if data.get("message") != "success":
            raise RuntimeError(f"Global level API error: {data}")
        global_level_cache[level_id] = data.get("data", {})
    return global_level_cache[level_id]


def get_first_beaten_by(level_id):
    """Игрок «first beaten by» со страницы уровня на demonlist.org: (player_id, nick) или (None, None)."""
    try:
        response = session.get(DEMONLIST_LEVEL_URL.format(level_id=level_id), timeout=TIMEOUT)
        response.raise_for_status()
        page = response.text
    except Exception as error:
        print(f"    Не удалось загрузить страницу уровня {level_id}: {error}")
        return None, None

    m = FIRST_BEATEN_RE.search(page)
    if not m:
        return None, None
    return m.group(1), html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()


def find_global_video(user_id, level_id, percent):
    """Видео рекорда на этот уровень И с этим процентом: (video, status). Принятый (accepted) лучше ожидающего
    (waiting); отклонённые (rejected) пропускаются."""
    waiting = None
    for record in get_global_records(user_id):
        if str((record.get("level") or {}).get("id")) != str(level_id):
            continue
        if record_percent(record, "percent") != percent:
            continue
        status = str(record.get("status") or "").strip().lower()
        if status == "rejected":
            continue
        video = get_video(record)
        if not video:
            continue
        if status == "waiting":
            if waiting is None:
                waiting = (video, "waiting")
            continue
        return video, status or "accepted"
    return waiting if waiting else ("", "")


def load_fix_absorbed():
    """fix.json -> {главный ID: [ID склеенных в него аккаунтов]}. При сбое не запоминаем (попробуем снова)."""
    global fix_absorbed
    if fix_absorbed is not None:
        return fix_absorbed
    try:
        data, _ = get_json(FIX_URL)
    except Exception as error:
        print(f"    fix.json не загрузился: {error}")
        return {}
    absorbed = {}
    for merge in (data or {}).get("merges", []):
        keep = str(merge.get("keep"))
        for item in merge.get("absorb", []):
            absorbed.setdefault(keep, []).append(str(item.get("id")))
    fix_absorbed = absorbed
    return absorbed


def global_accounts(player_id):
    """Все аккаунты человека на глобале: главный, склеенные в него по fix.json (и по цепочке) и дополнительные
    из links.json. Видео рекорда может лежать на любом из них."""
    absorbed = load_fix_absorbed()
    group, queue = [], [str(player_id)]
    while queue:
        account = queue.pop(0)
        if account in group:
            continue
        group.append(account)
        queue.extend(absorbed.get(account, []))
    result = []
    for account in group:
        for item in [account] + links_dl_extra.get(account, []):
            if item not in result:
                result.append(item)
    return result


def find_global_video_any(player_id, level_id, percent):
    """Видео на глобале у любого аккаунта человека. Порядок: принятый рекорд, видео верификации уровня, рекорд в
    ожидании. Если видео нет, а часть запросов упала, поднимаем ошибку: «не нашли» и «не смогли проверить» разные вещи."""
    accounts = global_accounts(player_id)
    waiting, first_error = None, None

    for account in accounts:
        try:
            video, status = find_global_video(account, level_id, percent)
        except Exception as error:
            first_error = first_error or error
            continue
        if video and status != "waiting":
            return video, status
        if video and waiting is None:
            waiting = (video, status)

    if percent == 100:
        try:
            verification = get_global_level(level_id).get("verification") or {}
            video = get_video(verification)
            if video and str(verification.get("user_id")) in accounts:
                return video, "accepted"
        except Exception as error:
            first_error = first_error or error

    # Дополнительная попытка: рекорд игрока из «first beaten by» на странице уровня
    fb_id, fb_nick = get_first_beaten_by(level_id)
    if fb_id:
        try:
            for account in global_accounts(fb_id):
                video, status = find_global_video(account, level_id, percent)
                if video:
                    return video, f"GlobalList (first beaten by {fb_nick})"
        except Exception as error:
            first_error = first_error or error

    if waiting:
        return waiting
    if first_error is not None:
        raise first_error
    return "", ""


# ---------------- POINTERCRATE ----------------

def get_pointer_demon(ingame_id):
    ingame_id = int(ingame_id)
    if ingame_id in pointer_demon_cache:
        return pointer_demon_cache[ingame_id]

    data, _ = get_json(f"{POINTERCRATE_DEMONS_API}/", {"level_id": ingame_id, "limit": LIMIT})
    demons = data.get("data", data.get("demons", [])) if isinstance(data, dict) else data
    if not isinstance(demons, list):
        demons = []

    found = None
    for demon in demons:
        level_id = demon.get("level_id")
        if level_id is None and isinstance(demon.get("level"), dict):
            level_id = demon["level"].get("id")
        if level_id is None:
            level_id = demon.get("ingame_id")
        if str(level_id) == str(ingame_id):
            found = demon
            break
    if found is None and len(demons) == 1:  # фильтр level_id уже ограничивает результат этим уровнем
        found = demons[0]

    pointer_demon_cache[ingame_id] = found
    return found


def get_pointer_demon_records(demon_id):
    demon_id = int(demon_id)
    if demon_id in pointer_records_cache:
        return pointer_records_cache[demon_id]

    records, after = [], None
    while True:
        params = {"demon_id": demon_id, "limit": LIMIT}
        if after is not None:
            params["after"] = after
        data, _ = get_json(f"{POINTERCRATE_API}/records/", params)
        page = data.get("data", data.get("records", [])) if isinstance(data, dict) else data
        if not isinstance(page, list) or not page:
            break
        records.extend(page)
        if len(page) < LIMIT:
            break
        last_id = page[-1].get("id")
        if last_id is None or last_id == after:
            break
        after = last_id
        time.sleep(DELAY)

    pointer_records_cache[demon_id] = records
    return records


def find_pointer_video(pointer_player_id, ingame_id, percent):
    demon = get_pointer_demon(ingame_id)
    if not demon or demon.get("id") is None:
        return ""
    for record in get_pointer_demon_records(demon["id"]):
        player = record.get("player") or {}
        record_player_id = player.get("id") if isinstance(player, dict) else player
        if str(record_player_id) != str(pointer_player_id):
            continue
        if record_percent(record, "progress", "percent") != percent:
            continue
        video = get_video(record)
        if video:
            return video
    return ""


# ---------------- ПОИСК ВИДЕО ----------------

def accounts_of_key_player(player_id, key_src, key_rest, links):
    """Аккаунты игрока без глобала (ключ вида pc40667 / wml5). Ключ называет только ОДИН аккаунт, а у человека их
    может быть несколько, и нужное видео лежит на другом: добавляем всё, что links.json знает о человеке."""
    accounts = {key_src.name: [key_rest]}
    wanted = str(player_id).casefold()
    for link_key, per_list in links.items():
        if not (str(link_key).casefold() == wanted or key_rest in (per_list.get(key_src.name) or [])):
            continue
        for list_name, ids in per_list.items():
            bucket = accounts.setdefault(list_name, [])
            for account in ids:
                if account not in bucket:
                    bucket.append(account)
    return accounts


def find_video(player_id, level_id, percent, links):
    """(видео, источник). Если видео нет, источник объясняет почему."""
    request_errors = []  # отличаем "видео нет" от "не смогли проверить из-за сбоя"

    # Ключ вида pc7 / wml5 (игрок без глобала): лист и аккаунт названы самим ключом, глобал не нужен.
    key_src, key_rest = L.key_parts(str(player_id).casefold())
    if key_src is not None:
        accounts = accounts_of_key_player(player_id, key_src, key_rest, links)
    else:
        accounts = links.get(str(player_id), {})
        try:
            video, status = find_global_video_any(player_id, level_id, percent)
            if video:
                return video, "GlobalList (статус: waiting)" if status == "waiting" else "GlobalList"
        except Exception as error:
            print(f"    GlobalList API error: {error}")
            request_errors.append("GlobalList")

    # Для Pointercrate нужен ingame_id уровня из Global API.
    ingame_note, ingame_id = "", None
    try:
        ingame_id = get_global_level(level_id).get("ingame_id")
        if ingame_id is None:
            ingame_note = "у уровня отсутствует ingame_id"
    except Exception as error:
        ingame_note = f"ошибка получения ingame_id: {error}"
        request_errors.append("ingame_id")

    if ingame_id is not None:
        for pointer_id in accounts.get("pointercrate", []):
            try:
                video = find_pointer_video(pointer_id, ingame_id, percent)
                if video:
                    return video, f"Pointercrate {pointer_id}"
            except Exception as error:
                print(f"    Pointercrate {pointer_id}: {error}")
                request_errors.append(f"Pointercrate {pointer_id}")
            time.sleep(DELAY)

    if request_errors:  # видео могло существовать, но проверить не удалось: стоит запустить ещё раз
        return "", f"НЕ ПРОВЕРЕНО из-за ошибок запросов ({', '.join(request_errors)}), запустите ещё раз"
    if ingame_note:
        return "", ingame_note
    return "", f"видео с {percent}% не найдено"


def find_video_retry(player_id, level_id, percent, links):
    """find_video + дополнительные проходы: если видео не нашлось из-за ошибок запросов, ждём и спрашиваем снова."""
    video, source = find_video(player_id, level_id, percent, links)
    for n in range(1, RECHECK_ROUNDS + 1):
        if video or not source.startswith("НЕ ПРОВЕРЕНО"):
            break
        pause = RECHECK_PAUSE * n
        print(f"    сбой запросов, перепроверка {n}/{RECHECK_ROUNDS} через {pause} с")
        time.sleep(pause)
        video, source = find_video(player_id, level_id, percent, links)
    return video, source


# ---------------- ВЫВОД ----------------

def save_json(users, filename):
    with open(filename, "w", encoding="utf-8") as file:
        file.write('{\n  "users": {\n')
        items = list(users.items())
        for user_index, (player, achievements) in enumerate(items):
            file.write("    " + json.dumps(player, ensure_ascii=False) + ": [\n")
            for index, achievement in enumerate(achievements):
                file.write("      " + json.dumps(achievement, ensure_ascii=False))
                if index < len(achievements) - 1:
                    file.write(",")
                file.write("\n")
            file.write("    ]")
            if user_index < len(items) - 1:
                file.write(",")
            file.write("\n")
        file.write("  }\n}\n")


def save_players_json(players, filename):
    with open(filename, "w", encoding="utf-8") as file:
        file.write('{\n  "users": {\n')
        items = list(players.items())
        for index, (player, info) in enumerate(items):
            file.write("    " + json.dumps(player, ensure_ascii=False) + ": " + json.dumps(info, ensure_ascii=False))
            file.write(",\n" if index < len(items) - 1 else "\n")
        file.write("  }\n}\n")


def load_id_to_nick():
    """coreboard_id_map.json ({ID CoreBoard: {nick, gdr_id}}) -> {наш ID (gdr_id): ник CoreBoard}."""
    try:
        with open(ID_MAP_FILE, encoding="utf-8") as file:
            id_map = json.load(file)
    except FileNotFoundError:
        print(f"ВНИМАНИЕ: не найден {ID_MAP_FILE}, в командах вместо ников будут ID")
        return {}
    id_to_nick = {}
    for row in id_map.values():
        if isinstance(row, dict) and row.get("gdr_id") and row.get("nick"):
            id_to_nick.setdefault(normalize_id(row["gdr_id"]), row["nick"])  # у одного ID несколько ников: первый
    return id_to_nick


def load_level_names():
    """levels_tp.json с GitHub -> {ID уровня: название} (обычные ID числами, "x..." строками)."""
    try:
        levels_data = get_json(LEVELS_URL)[0]["levels"]
    except Exception as error:
        print(f"ВНИМАНИЕ: не удалось загрузить levels_tp.json с GitHub ({error}), названия уровней возьмутся из API")
        return {}
    names = {}
    for level_id, data in levels_data.items():
        name = data.get("name")
        if name:
            names.setdefault(str(level_id) if str(level_id).startswith("x") else int(level_id), name)
    return names


def get_level_name(level_id, level_names):
    """Название уровня по ID: особые названия, levels_tp.json, API."""
    key = str(level_id)
    if key in level_name_cache:
        return level_name_cache[key]

    name = SPECIAL_LEVEL_NAMES.get(key)
    if not name:
        name = level_names.get(level_id) or level_names.get(key)
        if not name and key.isdigit():
            name = level_names.get(int(key))
    if not name and key.isdigit():  # в API есть только обычные числовые ID
        try:
            name = get_global_level(level_id).get("name")
        except Exception as error:
            print(f"    Не удалось получить название уровня {level_id}: {error}")
    if not name:
        name_failed_levels.append(key)
        name = f"УРОВЕНЬ {key}"

    level_name_cache[key] = name
    return name


def site_of(source_name):
    """Как подписывать источник в команде («Видео: ...»): Pointercrate одним словом, без ID аккаунта."""
    return "Pointercrate" if source_name.startswith("Pointercrate") else source_name


def build_commands(command_records, id_to_nick, level_names):
    """command_records: (ключ игрока, ID игрока, ID уровня, процент, ссылка, источник). На каждую запись 2 команды,
    между записями пустая строка; если видео найдено, перед командами строка «Видео: источник»."""
    blocks = []
    for player_key, player_id, level_id, percent, link, site in command_records:
        # Ник: сначала из coreboard_id_map.json, потом из ключа "ID — Ник", потом сам ID.
        nick = id_to_nick.get(normalize_id(player_id))
        if not nick and " — " in str(player_key):
            nick = str(player_key).split(" — ", 1)[1].strip()
        if not nick:
            nick = player_id

        level_name = get_level_name(level_id, level_names)
        link = normalize_link(link)
        if percent != 100:
            level_name = f"{level_name} {percent}%"

        lines = []
        if link and site:
            lines.append(f"Видео: {site}")
        lines.append(f"/fulladd {nick}, {level_name}, {link}".rstrip())
        lines.append(f"/try {nick}, {level_name}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) + ("\n" if blocks else "")


def add_links_to_file(command_records, path, index):
    """Записывает в файл ссылку на видео (ту же, что в /fulladd); не нашлась - "".
      index=3: missing_achievements_with_nicks.json -> [ID уровня, процент, название, ссылка]
      index=2: missing_achievements.json            -> [ID уровня, процент, ссылка]
    Записи ищутся по (игрок, уровень, процент)."""
    link_map = {(normalize_id(player_id), str(level_id), percent): normalize_link(link)
                for _key, player_id, level_id, percent, link, _site in command_records}

    data = json.loads(path.read_text("utf-8"))
    users = data.get("users", data)
    for player_key, records in users.items():
        player_id = normalize_id(player_id_from_key(player_key))
        for record in records:
            while len(record) < index:
                record.append("")
            link = link_map.get((player_id, str(record[0]), parse_percent(record[1])), "")
            if len(record) > index:
                record[index] = link
            else:
                record.append(link)
    save_json(users, path)


def load_previous_players_links():
    """Ссылки из прошлого опубликованного missing_players.json: (игрок, уровень, процент) -> ссылка."""
    cache = {}
    try:
        data = json.loads(Path(args.prev_players).read_text("utf-8"))
    except (OSError, ValueError):
        return cache
    for key, info in (data.get("users", data) or {}).items():
        # Ключ файла может быть ID CoreBoard; наш ID лежит в gdr_id (в старых файлах ключ и есть наш ID)
        owner = normalize_id((info.get("gdr_id") if isinstance(info, dict) else None) or key)
        for record in (info.get("records", []) if isinstance(info, dict) else []):
            if len(record) >= 3 and record[2]:
                cache[(owner, str(record[0]), parse_percent(record[1]))] = record[2]
    return cache


def add_videos_to_players(links):
    """Дописывает ссылку в missing_players.json только у ПЕРВОЙ записи каждого игрока (у остальных пусто, видео для
    них не ищутся). Ссылки, найденные в прошлом опубликованном файле, заново не ищутся."""
    if not PLAYERS_FILE.exists():
        print(f"Нет {PLAYERS_FILE}: новых игроков нет, пропускаю")
        return

    data = json.loads(PLAYERS_FILE.read_text("utf-8"))
    users = data.get("users", data)
    previous = load_previous_players_links()
    not_found = []
    for key, info in users.items():
        # Ключ - ID на CoreBoard (или наш); видео ищем по нашему ID из gdr_id
        player_id = info.get("gdr_id") or player_id_from_key(key)
        print(f"\nНовый игрок: {key} ({info.get('nick')}), наш ID {player_id}")

        for record_index, record in enumerate(info.get("records", [])):
            while len(record) < 3:
                record.append("")
            if record_index > 0:  # видео только у первой записи
                record[2] = ""
                continue
            level_id, raw_percent = record[0], record[1]
            percent = parse_percent(raw_percent)

            cached = previous.get((normalize_id(player_id), str(level_id), percent))
            if cached:
                record[2] = cached
                continue

            video, source_name = "", ""
            try:
                if percent is not None and str(level_id).strip().isdigit():
                    video, source_name = find_video_retry(player_id, level_id, percent, links)
                    time.sleep(DELAY)
                else:
                    source_name = "непонятный процент или ID уровня"
            except Exception as error:
                source_name = f"ОШИБКА: {error}"

            record[2] = normalize_link(video) if video else ""
            if record[2]:
                print(f"  {level_id} {raw_percent}%: найдено ({source_name})")
            else:
                not_found.append(f"{key} | {info.get('nick')} | level_id={level_id} | {raw_percent}% | {source_name}")
                print(f"  {level_id} {raw_percent}%: не найдено ({source_name})")

    save_players_json(users, PLAYERS_FILE)
    PLAYERS_NOT_FOUND_FILE.write_text("\n".join(not_found) + ("\n" if not_found else ""), encoding="utf-8")


def main():
    users = json.loads(INPUT_FILE.read_text("utf-8")).get("users", {})
    links = get_links()

    not_found = []
    global_lines, pointer_lines = [], []  # найденные видео по источникам (для video_link_sources.txt)
    command_records = []  # всё, из чего потом сделаем команды /fulladd и /try
    total = sum(len(records) for records in users.values())
    processed = 0

    for player_key, achievements in users.items():
        player_id = player_id_from_key(player_key)
        print(f"\nИгрок: {player_key}")

        for achievement in achievements:
            processed += 1
            level_id, raw_percent = achievement[0], achievement[1]
            level_name = achievement[2] if len(achievement) > 2 else ""  # в missing_achievements.json тут пусто
            percent = parse_percent(raw_percent)
            label = f"[{processed}/{total}] {level_name}"
            prefix = f"{player_key} | {level_name} | level_id={level_id}"

            if percent is None:
                not_found.append(f"{prefix} | {raw_percent} | не удалось разобрать процент")
                print(f"  {label}: непонятный процент")
                continue

            if not str(level_id).strip().isdigit():
                not_found.append(f"{prefix} | {percent}% | нечисловой ID уровня")
                command_records.append((player_key, player_id, level_id, percent, "", ""))
                print(f"  {label}: нечисловой ID")
                continue

            link, site = "", ""  # если видео не найдём, в команде ссылка будет пустой
            try:
                video, source_name = find_video_retry(player_id, level_id, percent, links)
                if video:
                    link, site = video, site_of(source_name)
                    line = (f"{player_key} | level_id={level_id} | {level_name or '-'} | {percent}% | "
                            f"{video} | {source_name}")
                    (global_lines if source_name.startswith("GlobalList") else pointer_lines).append(line)
                    print(f"  {label}: найдено ({source_name})")
                else:
                    not_found.append(f"{prefix} | {percent}% | {source_name}")
                    print(f"  {label}: не найдено ({source_name})")
            except Exception as error:
                not_found.append(f"{prefix} | {percent}% | ОШИБКА: {error}")
                print(f"  {label}: ОШИБКА {error}")

            command_records.append((player_key, player_id, level_id, percent, link, site))
            time.sleep(DELAY)

    with open(SOURCES_FILE, "w", encoding="utf-8") as file:
        file.write(f"=== GlobalList ({len(global_lines)}) ===\n")
        for line in global_lines:
            file.write(line + "\n")
        file.write(f"\n=== Pointercrate ({len(pointer_lines)}) ===\n")
        for line in pointer_lines:
            file.write(line + "\n")

    COMMANDS_FILE.write_text(build_commands(command_records, load_id_to_nick(), load_level_names()), encoding="utf-8")
    if name_failed_levels:
        print("Название не нашлось ни в levels_tp.json, ни в API для уровней: " + ", ".join(sorted(set(name_failed_levels))))
    add_links_to_file(command_records, NICKS_FILE, 3)
    add_links_to_file(command_records, INPUT_FILE, 2)
    NOT_FOUND_FILE.write_text("\n".join(not_found) + ("\n" if not_found else ""), encoding="utf-8")
    print(f"\nЗаписей {total}, видео найдено {len(global_lines) + len(pointer_lines)}, не найдено / ошибки {len(not_found)}")

    add_videos_to_players(links)


main()
