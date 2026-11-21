#!/usr/bin/env python3
"""Сравнение рейтингов CoreBoard (cbd.txt, ники без ID) и нашего (gdr.txt, ID в [скобках]).

  compare.py in-cbd-not-in-gdr --cbd cbd.txt --gdr gdr.txt --out-dir DIR [--include-absent]
      достижения, которые есть в cbd, а в gdr их нет (или в cbd процент выше) -> missing_achievements.json
  compare.py in-gdr-not-in-cbd --cbd cbd.txt --gdr gdr.txt --out-dir DIR
      наоборот -> missing_achievements.json, missing_achievements_with_nicks.json, missing_players.json
      (новые игроки: есть у нас с очками меньше 250, на CoreBoard их нет), gdr_to_cb_ids.json
      и отладочные файлы (invalid_*_lines.txt, *_not_in_*.txt, missing_players_skipped.txt).

Игроки cbd сопоставляются с gdr через ID с сайта (cbd_ids.json рядом с cbd.txt) и coreboard_id_map.json
(корень репозитория или рядом со скриптом). ID CoreBoard главнее наших: JSON выходят с ID CoreBoard,
новые игроки получают ID по порядку после самого большого. Нет ни ID с сайта, ни карты: ключи наши.
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
import levels as levelsearch
import lists_core as L  # метки листов ([PC], [WML]) у ников игроков, которых нет в глобале

MODES = ("in-cbd-not-in-gdr", "in-gdr-not-in-cbd")
parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=MODES)
parser.add_argument("--cbd", required=True)
parser.add_argument("--gdr", required=True)
parser.add_argument("--out-dir", required=True)
parser.add_argument("--include-absent", action="store_true",
                    help="in-cbd-not-in-gdr: игроки CoreBoard, которых НЕТ в gdr совсем, но чей ID нашёлся "
                         "(coreboard_id_map.json), сравниваются с пустым gdr: все их записи попадают в результат")
args = parser.parse_args()

TO_CBD = args.mode == "in-gdr-not-in-cbd"  # True: ищем то, что есть у нас и нет на CoreBoard
NEW_MAX_SCORE = 250.0                       # новые игроки: очков МЕНЬШЕ этого числа
SAME_LEVELS_MIN = 4                         # столько общих уровней с несопоставленным игроком cbd = тот же человек?
API_TRIES = 4
LEVELS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"
GLOBAL_LIST_URL = "https://api.demonlist.org/level/classic/list?limit={limit}&offset={offset}"
NICK_KEY_PREFIX = "nick:"  # временный ключ игрока, у которого ник есть, а ID не нашёлся

# Внутри скрипта 1-й файл это ИСТОЧНИК (из него берём достижения), 2-й это ЭТАЛОН (с ним сравниваем).
if TO_CBD:
    NAME1, FILE1, NAME2, FILE2 = "gdr", args.gdr, "cbd", args.cbd
else:
    NAME1, FILE1, NAME2, FILE2 = "cbd", args.cbd, "gdr", args.gdr
FILE1_HAS_ID = NAME1 == "gdr"  # ID записан в [скобках] только в gdr
FILE2_HAS_ID = NAME2 == "gdr"

OUT_DIR = args.out_dir
os.makedirs(OUT_DIR, exist_ok=True)


def out_path(name):
    return os.path.join(OUT_DIR, name)


SPECIAL_LEVEL_NAMES = levelsearch.SPECIAL_LEVEL_NAMES
normalize = levelsearch.normalize

FLAG_RE = re.compile(r'^(?:[\U0001F1E6-\U0001F1FF]{2}|[\U0001F3F3\U0001F3F4]\uFE0F?)\s*')
LINE_FLAG_RE = re.compile(r'^\s*\d+\)\s*([\U0001F1E6-\U0001F1FF]{2})')
CLAN_TAG_RE = re.compile(r"\[[^\]]*\]")


def normalize_nick(nick):
    return normalize(FLAG_RE.sub('', nick.strip()))


def normalize_id(player_id):
    return str(player_id).strip().casefold()


def split_line_parts(line):
    """'17) 🇷🇺 ROZE (Sakupen circles (Nick24), Tidal Wave 87%) - 22.47 [12345]' -> (ник, строка достижений, очки).
    Ищет закрывающую ')' перед '- очки' и идёт влево по балансу скобок до её парной '('."""
    tail_match = re.search(r'\)\s*-\s*(\d+(?:\.\d+)?)(?:\s+\[[^\]]+\])?\s*$', line)
    if not tail_match:
        return None, None, None

    score = float(tail_match.group(1))
    close_paren_idx = tail_match.start()
    depth = 0
    open_paren_idx = None
    for idx in range(close_paren_idx, -1, -1):
        ch = line[idx]
        if ch == ')':
            depth += 1
        elif ch == '(':
            depth -= 1
            if depth == 0:
                open_paren_idx = idx
                break
    if open_paren_idx is None:
        return None, None, score

    achievements_str = line[open_paren_idx + 1:close_paren_idx]
    head_match = re.match(r'^\s*\d+\)\s*(.*?)\s*$', line[:open_paren_idx])
    if not head_match:
        return None, achievements_str, score
    nick = FLAG_RE.sub('', head_match.group(1).strip())
    return (nick or None), achievements_str, score


def parse_flag(line):
    """Код страны (US, RU...) из флага-эмодзи в начале строки рейтинга; нет флага - пустая строка."""
    match = LINE_FLAG_RE.match(line)
    if not match:
        return ""
    return "".join(chr(ord("A") + ord(ch) - 0x1F1E6) for ch in match.group(1))


def parse_id(line):
    match = re.search(r'\[([^\]]+)\]\s*$', line)
    return normalize_id(match.group(1)) if match else None


def parse_achievements(achievements_str):
    achievements = {}
    for item in (achievements_str or "").split(","):
        item = item.strip()
        if not item or item == "No":  # No: пустой слот рекорда (только точно так)
            continue
        percent_match = re.search(r'\s+(\d+)%$', item)
        if percent_match:
            percent = int(percent_match.group(1))
            name = item[:percent_match.start()].strip()
        else:
            percent = 100
            name = item
        achievements[normalize(name)] = {"name": name, "percent": percent}
    return achievements


def save_json(filename, result):
    """{"users": {ключ: [записи]}}: игрок, а внутри записи по одной на строку (порядок задан при сборке)."""
    users = list(result.items())
    with open(filename, "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        for i, (player_id, records) in enumerate(users):
            f.write(f'    {json.dumps(player_id, ensure_ascii=False)}: [\n')
            for j, record in enumerate(records):
                f.write(f'      {json.dumps(record, ensure_ascii=False)}{"," if j < len(records) - 1 else ""}\n')
            f.write(f'    ]{"," if i < len(users) - 1 else ""}\n')
        f.write('  }\n}\n')


def fetch_json(url):
    """GET с повторными попытками (до API_TRIES раз)."""
    last_error = None
    for attempt in range(1, API_TRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
            time.sleep(0.2)  # небольшая пауза, чтобы не заспамить сервер
            return payload
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429:  # 404, 403 и подобные повторять бессмысленно
                raise
            last_error = error
        except Exception as error:
            last_error = error
        print(f"  Запрос не удался (попытка {attempt}/{API_TRIES}): {last_error}")
        if attempt < API_TRIES:
            time.sleep(2 * attempt)
    raise last_error


# =========================
# ID игроков на CoreBoard (ID с сайта главнее coreboard_id_map.json)
# =========================
cb_id_by_nick = {}    # нормализованный ник cbd -> ID CoreBoard (cbd_ids.json, делает coreboard-parse)
gdr_by_cb_id = {}     # ID CoreBoard -> наш ID (карта)
gdr_by_map_nick = {}  # нормализованный ник из карты -> наш ID
map_nicks = {}        # нормализованный ник из карты -> множество наших ID (уникальность ников новых игроков)
cb_id_by_gdr = {}     # наш ID (нормализованный) -> ID CoreBoard (карта)
max_cb_id = 0

cb_ids_file = os.path.join(os.path.dirname(os.path.abspath(args.cbd)), "cbd_ids.json")
if os.path.exists(cb_ids_file):
    try:
        with open(cb_ids_file, encoding="utf-8") as f:
            cb_id_by_nick = {normalize_nick(nick): str(cb_id).strip()
                             for nick, cb_id in json.load(f).items() if str(cb_id).strip().isdigit()}
    except (OSError, ValueError, AttributeError) as error:
        print(f"{cb_ids_file}: не удалось прочитать ({error}), ID с сайта не используются")
if cb_id_by_nick:
    max_cb_id = max(int(v) for v in cb_id_by_nick.values())

id_map_file = next((p for p in (os.path.join(os.path.dirname(HERE), "coreboard_id_map.json"),
                                os.path.join(HERE, "coreboard_id_map.json")) if os.path.exists(p)),
                   os.path.join(os.path.dirname(HERE), "coreboard_id_map.json"))
if os.path.exists(id_map_file):
    try:
        with open(id_map_file, encoding="utf-8") as f:
            for cb_id, row in json.load(f).items():
                cb_id = str(cb_id).strip()
                if not cb_id.isdigit():
                    continue
                max_cb_id = max(max_cb_id, int(cb_id))
                gdr_id = row.get("gdr_id") if isinstance(row, dict) else None
                if gdr_id:
                    if row.get("nick"):
                        gdr_by_map_nick.setdefault(normalize_nick(str(row["nick"])), normalize_id(gdr_id))
                        map_nicks.setdefault(normalize_nick(str(row["nick"])), set()).add(normalize_id(gdr_id))
                    gdr_by_cb_id[cb_id] = normalize_id(gdr_id)
                    cb_id_by_gdr[normalize_id(gdr_id)] = cb_id
    except (OSError, ValueError, AttributeError) as error:
        sys.exit(f"{id_map_file}: не удалось прочитать ({error}). Останавливаюсь.")
if not (cb_id_by_nick or cb_id_by_gdr):
    print("ID CoreBoard не найдены (нет ни cbd_ids.json, ни coreboard_id_map.json): ключи остаются нашими")


# =========================
# Уровни: levels_tp.json ТОЛЬКО с GitHub, потом особые названия, потом глобал
# =========================
try:
    levels_data = fetch_json(LEVELS_URL)["levels"]
except Exception as error:
    sys.exit(f"levels_tp.json: не удалось загрузить с GitHub ({error}). Останавливаюсь.")
if not isinstance(levels_data, dict) or not levels_data:
    sys.exit("levels_tp.json: на GitHub пустой список уровней. Останавливаюсь.")

level_names = {}  # название (нормализованное) -> ID уровня
for level_id, data in levels_data.items():
    name = data.get("name")
    if not name:
        continue
    level_names.setdefault(normalize(name), str(level_id) if str(level_id).startswith("x") else int(level_id))

special_name_to_id = {normalize(name): int(level_id) for level_id, name in SPECIAL_LEVEL_NAMES.items()}
global_levels_by_name = None  # {нормализованное название: ID}; загружается один раз и только если понадобится


def load_global_levels():
    global global_levels_by_name
    if global_levels_by_name is not None:
        return global_levels_by_name
    result, offset = {}, 0
    try:
        while True:
            levels = (fetch_json(GLOBAL_LIST_URL.format(limit=100, offset=offset)).get("data") or {}).get("levels") or []
            if not levels:
                break
            added = 0
            for level in levels:
                name, level_id = level.get("name"), level.get("id")
                if not name or level_id is None:
                    continue
                key = normalize(name)
                if key not in result:
                    result[key] = int(level_id)
                    added += 1
            if added == 0:  # защита от бесконечного цикла
                break
            offset += 100
    except Exception as error:
        print(f"  Не удалось загрузить список уровней из глобала: {error}")
    global_levels_by_name = result  # запоминаем результат (даже неполный), чтобы не качать заново
    return result


def find_level_id(normalized_name):
    """ID уровня по названию: levels_tp.json, особые названия, глобал. Не нашёлся - None."""
    level_id = level_names.get(normalized_name)
    if level_id is None:
        level_id = special_name_to_id.get(normalized_name)
    if level_id is None:
        level_id = load_global_levels().get(normalized_name)
    return level_id


# =========================
# Чтение рейтингов
# =========================
def read_players(filename, id_in_line, invalid_filename=None):
    """Игроки файла: {ID: {nick, score, achievements, order, line}}.
    id_in_line=True: ID из [скобок] (gdr). False (cbd): ID по нику через ID с сайта и coreboard_id_map.json;
    если ника там нет, игрок сохраняется с ключом "nick:<ник>", который ни с кем не совпадёт."""
    players, invalid_lines = {}, []
    with open(filename, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    for order, line in enumerate(lines):
        nick, achievements_str, score = split_line_parts(line)
        reasons = []

        if id_in_line:
            player_id = parse_id(line)
            if not player_id:
                reasons.append("нет ID в [скобках]")
        elif not nick:
            player_id = None
            reasons.append("не удалось найти ник")
        else:
            site_id = cb_id_by_nick.get(normalize_nick(nick))
            player_id = gdr_by_cb_id.get(site_id) if site_id else None
            if not player_id:
                player_id = gdr_by_map_nick.get(normalize_nick(nick))
            if not player_id:  # игрока нет в coreboard_id_map.json: это не ошибка строки
                player_id = NICK_KEY_PREFIX + normalize_nick(nick)

        if score is None:
            reasons.append("не удалось найти очки")
        if reasons:
            invalid_lines.append(f"[{', '.join(reasons)}] {line}")
            continue

        player_id = normalize_id(player_id)
        if player_id in players:  # при повторе остаётся позиция первой встреченной строки
            order = players[player_id]["order"]
        players[player_id] = {"nick": nick, "score": round(score, 2),
                              "achievements": parse_achievements(achievements_str), "order": order, "line": line}

    if invalid_filename:
        with open(invalid_filename, "w", encoding="utf-8") as f:
            f.write("\n".join(invalid_lines))
    return players


players1 = read_players(FILE1, FILE1_HAS_ID, out_path(f"invalid_{NAME1}_lines.txt") if TO_CBD else None)
players2 = read_players(FILE2, FILE2_HAS_ID, out_path(f"invalid_{NAME2}_lines.txt") if TO_CBD else None)

common_ids = set(players1) & set(players2)
ids_only_in_1 = set(players1) - set(players2)


def players_in_file_order(ids, players):
    return sorted(ids, key=lambda player_id: players[player_id]["order"])


# =========================
# Сравнение достижений
# =========================
# Игрок есть в обоих файлах (совпал по ID): очки в 1-м файле меньше, чем во 2-м; из достижений 1-го берём те,
# которых нет во 2-м или у которых процент в 1-м выше. Игроков, которых нет в одном из файлов, не трогаем
# (кроме --include-absent). Уровень должен найтись.
result = {}
result_with_nicks = {}
unknown_levels = set()

# Игроки cbd с известным ID, которых нет в gdr совсем: сравниваем с пустым gdr, чтобы все их записи попали
# в результат (иначе они пропадали бы из my_extras при пересборке). Игроки без найденного ID не трогаются.
absent_ids = set()
if args.include_absent and not TO_CBD:
    absent_ids = {i for i in ids_only_in_1 if not i.startswith(NICK_KEY_PREFIX)}


def get_nick(player_id):
    """Ник берём в первую очередь из cbd, потом из gdr."""
    for players in (players2, players1):  # TO_CBD: 2-й файл это cbd
        player = players.get(player_id)
        if player and player["nick"]:
            return player["nick"]
    return "?"


for player_id in sorted(common_ids | absent_ids, key=lambda i: (players1[i]["score"], players1[i]["order"])):
    p1 = players1[player_id]
    # у отсутствующего в gdr игрока очки считаем бесконечными, чтобы проверка "очки меньше" его пропустила
    p2 = players2.get(player_id) or {"score": float("inf"), "achievements": {}}
    if p1["score"] >= p2["score"]:
        continue

    records, records_with_names = [], []
    improvements = [(name, a1) for name, a1 in p1["achievements"].items()
                    if p2["achievements"].get(name) is None or a1["percent"] > p2["achievements"][name]["percent"]]
    for achievement_name, achievement in sorted(improvements, key=lambda x: x[0]):
        level_id = find_level_id(achievement_name)
        if level_id is None:
            unknown_levels.add(achievement["name"])
            continue
        records.append([level_id, achievement["percent"], ""])
        records_with_names.append([level_id, achievement["percent"], achievement["name"]])

    if records:
        result[player_id] = records
        if TO_CBD:
            result_with_nicks[f"{player_id} — {get_nick(player_id)}"] = records_with_names


# =========================
# Новые игроки (только in-gdr-not-in-cbd): есть в gdr, нет в cbd, очков меньше 250. Результат: missing_players.json
#   {"users": {"<ID>": {"nick", "country_code", "gdr_id": "<наш ID>", "records": [[название уровня, процент, ссылка]]}}}
# Ключ: ID CoreBoard (НОВЫЕ ID по порядку после самого большого) или наш ID, если ID CoreBoard нет.
# Игрок НЕ попадает в файл (а пишется в missing_players_skipped.txt с причиной), если его ник совпал с ником
# несопоставленного игрока cbd, у них почти одни и те же уровни или хотя бы один его уровень не нашёлся.
# =========================
def strip_clan_tags(nick):
    """'[OX] zsakos' -> 'zsakos'. Если остался бы пустой ник, не трогаем."""
    return re.sub(r"\s+", " ", CLAN_TAG_RE.sub("", nick)).strip() or nick


def clean_nick(player_id, nick):
    """Ник без метки листа: 'Name [WML]' -> 'Name'."""
    nick = (nick or "").strip()
    tag = L.tag_of(player_id).strip()
    if tag and nick.endswith(tag):
        nick = nick[: -len(tag)].strip()
    return nick


def record_cost(record):
    """Стоимость записи f как в gd-rating (меньше = сложнее). Нет T/t у уровня или запись не даёт очков: в конец."""
    lv = levels_data.get(str(record[0])) or {}
    try:
        place, T, t, c = float(lv["place"]), float(lv["T"]), float(lv["t"]), float(record[1])
        if c >= 100:
            f = place
        elif not (t < T) or c < t or c <= 0:
            return (float("inf"), place)
        else:
            f = place * 2 * (T / c) ** (math.log(4 / 2) / math.log(T / t))
        return (f, place)
    except (KeyError, TypeError, ValueError):
        return (float("inf"), float("inf"))


def level_name_for_missing_player(level_id):
    """Название уровня для missing_players.json вместо ID, включая особые названия."""
    level_id = str(level_id)
    special_name = SPECIAL_LEVEL_NAMES.get(level_id)
    if special_name:
        return special_name
    level = levels_data.get(level_id)
    if level and level.get("name"):
        return level["name"]
    for name, global_id in (global_levels_by_name or {}).items():  # уровень найден только в глобале
        if str(global_id) == level_id:
            return name
    return level_id


if TO_CBD:
    taken_nicks = {normalize_nick(p["nick"]) for p in players2.values() if p["nick"]}

    def unique_nick(player_id, nick):
        """Ник без клан-тегов; если он уже есть на CoreBoard, в карте (у другого игрока) или встречался раньше,
        в конец ставится число: Nejou -> Nejou2 -> Nejou3."""
        base = strip_clan_tags(nick)
        own = normalize_id(player_id)
        candidate, n = base, 1
        while True:
            key = normalize_nick(candidate)
            if key not in taken_nicks and not (map_nicks.get(key, set()) - {own}):
                break
            n += 1
            candidate = f"{base}{n}"
        taken_nicks.add(normalize_nick(candidate))
        return candidate

    new_players, skipped_players = {}, []
    next_cb_id = max_cb_id + 1  # новым игрокам CoreBoard-ID по порядку (в порядке рейтинга gdr)
    unmapped_cbd = [p for pid, p in players2.items() if pid.startswith(NICK_KEY_PREFIX)]
    unmapped_cbd_nicks = {normalize_nick(p["nick"]) for p in unmapped_cbd if p["nick"]}

    for player_id in players_in_file_order(ids_only_in_1, players1):
        p1 = players1[player_id]
        if p1["score"] >= NEW_MAX_SCORE:
            continue

        nick = clean_nick(player_id, p1["nick"])
        names = set(p1["achievements"])

        if normalize_nick(nick) in unmapped_cbd_nicks or normalize_nick(strip_clan_tags(nick)) in unmapped_cbd_nicks:
            skipped_players.append(f"{player_id} | {nick} | {p1['score']} | такой ник уже есть в cbd, но не сопоставился по coreboard_id_map.json")
            continue
        twin = next((p for p in unmapped_cbd if len(names & set(p["achievements"])) >= SAME_LEVELS_MIN), None)
        if twin is not None:
            skipped_players.append(f"{player_id} | {nick} | {p1['score']} | похож на игрока cbd {twin['nick']} (общих уровней: "
                                   f"{len(names & set(twin['achievements']))}), возможно тот же человек под другим ником")
            continue

        records, unresolved = [], []
        for achievement_name, achievement in p1["achievements"].items():
            level_id = find_level_id(achievement_name)
            if level_id is None:
                unresolved.append(achievement["name"])
                unknown_levels.add(achievement["name"])
                continue
            records.append([level_id, achievement["percent"], ""])

        if unresolved or not records:
            skipped_players.append(f"{player_id} | {nick} | {p1['score']} | не найден уровень: {', '.join(unresolved) or 'нет записей'}")
            continue
        records.sort(key=record_cost)  # сначала самые сложные: первая запись получит ссылку на видео
        if cb_id_by_gdr or cb_id_by_nick:
            key = cb_id_by_gdr.get(normalize_id(player_id))  # игрок уже в карте (а на CoreBoard его нет): свой ID
            if key is None or key in new_players:
                key = str(next_cb_id)
                next_cb_id += 1
        else:
            key = player_id
        new_players[key] = {
            "nick": unique_nick(player_id, nick),
            "country_code": parse_flag(p1["line"]) or None,
            "gdr_id": player_id,  # наш ID: по нему add-video-links ищет видео
            "records": records,
        }

    # Только в missing_players.json ID уровней заменяются названиями
    for player in new_players.values():
        for record in player["records"]:
            record[0] = level_name_for_missing_player(record[0])
    with open(out_path("missing_players.json"), "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        items = list(new_players.items())
        for i, (pid, info) in enumerate(items):
            f.write("    " + json.dumps(pid, ensure_ascii=False) + ": " + json.dumps(info, ensure_ascii=False))
            f.write(",\n" if i < len(items) - 1 else "\n")
        f.write("  }\n}\n")

    # Наш ID -> ID на CoreBoard у игроков из обоих рейтингов (по нему cbd-update переводит записи на ID CoreBoard).
    # Нет ID CoreBoard: файла нет, переводить нечего.
    gdr_to_cb = {}
    for pid in common_ids:
        cb_id = cb_id_by_nick.get(normalize_nick(players2[pid]["nick"] or "")) or cb_id_by_gdr.get(normalize_id(pid))
        if cb_id:
            gdr_to_cb[pid] = cb_id
    if gdr_to_cb:
        with open(out_path("gdr_to_cb_ids.json"), "w", encoding="utf-8") as f:
            json.dump(gdr_to_cb, f, ensure_ascii=False, indent=1)
    with open(out_path("missing_players_skipped.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(skipped_players) + ("\n" if skipped_players else ""))

    # отладочные файлы (cbd-update кладёт их в артефакт debug-files)
    for filename, ids, players in ((out_path("gdr_not_in_cbd.txt"), ids_only_in_1, players1),
                                   (out_path("cbd_not_in_gdr.txt"), set(players2) - set(players1), players2)):
        with open(filename, "w", encoding="utf-8") as f:
            for player_id in players_in_file_order(ids, players):
                f.write(players[player_id]["line"] + "\n")

    save_json(out_path("missing_achievements_with_nicks.json"), result_with_nicks)

save_json(out_path("missing_achievements.json"), result)

print(f"compare {args.mode}: игроков в обоих рейтингах {len(common_ids)}, с недостающими достижениями {len(result)}, "
      f"записей {sum(map(len, result.values()))}"
      + (f", новых игроков {len(new_players)} (пропущено {len(skipped_players)})" if TO_CBD else ""))
if unknown_levels:
    print("Уровни, которых нет ни в levels_tp.json, ни в глобале: " + ", ".join(sorted(unknown_levels)))
