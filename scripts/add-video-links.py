#!/usr/bin/env python3
"""Ищет видео для достижений из <out-dir>/missing_achievements.json и собирает команды /fulladd и /try.
Вызывается из cbd-update.py:  add-video-links.py --out-dir DIR --prev-players cbd/missing_players.json

Читает (из DIR):  missing_achievements.json, missing_achievements_with_nicks.json, missing_players.json.
Пишет (в DIR):    commands.txt, ссылки в оба missing_achievements*.json и в первую запись каждого игрока из
                  missing_players.json, а для отладки missing_video_links.txt, video_link_sources.txt,
                  missing_players_video_links.txt.
Видео ищутся в GlobalList (все аккаунты человека) и в Pointercrate.
"""
import argparse
import json
import time
from pathlib import Path

import levels as levelsearch
from videos import (DELAY, find_video_retry, get_global_level, get_json, get_links, normalize_id, normalize_link,
                    parse_percent, player_id_from_key)

parser = argparse.ArgumentParser()
parser.add_argument("--out-dir", required=True, help="папка cbd-update")
parser.add_argument("--id-map", default=None, help="не используется (ID CoreBoard берутся из links.json через idmap.py), оставлен для совместимости")
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
from idmap import load_id_map
LEVELS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"
LEVELS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"

SPECIAL_LEVEL_NAMES = levelsearch.SPECIAL_LEVEL_NAMES
level_name_cache = {}    # ID уровня -> название
name_failed_levels = []  # уровни, название которых не нашлось нигде

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
    """links.json (other_lists.coreboard.main) -> {наш ID (gdr_id): ник CoreBoard}."""
    id_map = load_id_map()
    if not id_map:
        print("ВНИМАНИЕ: в links.json нет ID CoreBoard, в командах вместо ников будут ID")
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
        # Ник: сначала из links.json, потом из ключа "ID — Ник", потом сам ID.
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
