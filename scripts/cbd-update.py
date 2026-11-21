#!/usr/bin/env python3
"""
Находит то, что есть в нашем рейтинге (gdr), а в CoreBoard (cbd) нет или там процент ниже, ищет на это видео и
собирает команды /fulladd и /try. Обратная сторона sync-extras.py (my_extras.json этот скрипт не трогает).

Шаги:
  1. coreboard-parse.py -> cbd.txt (если sync-extras только что скачал страницы, они берутся из кеша, а не качаются заново);
  2. gd-rating.py --src all -> gdr.txt (наш рейтинг, ID игроков в [скобках]);
  3. compare.py in-gdr-not-in-cbd -> missing_achievements*.json, missing_players.json;
  4. add-video-links.py -> видео (GlobalList, Pointercrate) и commands.txt;
  5. с --commit копирует в cbd/ missing_achievements.json, missing_achievements_with_nicks.json, commands.txt,
     missing_players.json и missing_players_with_waiting.json (игроки, у которых меньше 250 очков только с waiting-записями;
     ID продолжаются после последнего в missing_players.json), коммитит и пушит.

ID CoreBoard: из cbd_ids.json (с сайта, главнее) и coreboard_id_map.json в корне репозитория. В missing_achievements*.json
вместо наших ID стоят ID CoreBoard, новые игроки получают ID по порядку после самого большого.
Рабочая папка с полными результатами и отладочными файлами: scripts/cbd-update/.

  python scripts/cbd-update.py [--commit]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # scripts/
ROOT = HERE.parent                              # корень репозитория
CBD_DIR = ROOT / "cbd"                          # сюда копируются результаты при --commit
OUT = HERE / "cbd-update"
PUBLISHED = ("missing_achievements.json", "missing_achievements_with_nicks.json", "commands.txt", "missing_players.json")
PUBLISHED_OPTIONAL = ("missing_players_with_waiting.json",)   # только если файл появился в этом запуске
WAITING_NEW = HERE / "waiting_new_players.json"   # игроки, у которых меньше 250 очков только с waiting (наши ID)

ap = argparse.ArgumentParser()
ap.add_argument("--commit", action="store_true", help="скопировать результаты в cbd/, закоммитить и запушить")
args = ap.parse_args()


def run(cmd, what):
    print(f"\n=== {what} ===\n$ {' '.join(map(str, cmd))}", flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=HERE)
    if r.returncode != 0:
        sys.exit(f"Шаг «{what}» завершился с ошибкой (код {r.returncode}).")


def git(*a, check=True):
    r = subprocess.run(["git", *a], cwd=ROOT, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(a)} не удался:\n{r.stdout}{r.stderr}")
    return r


RECORDS_FILES = ("missing_achievements.json", "missing_achievements_with_nicks.json")


def convert_records(src, dst, mapping):
    """missing_achievements*.json: наши ID игроков -> ID CoreBoard (ключ 'ID' или 'ID — ник').
    Игрока, которого в карте нет, оставляем под нашим ID, чтобы не потерять записи (и предупреждаем)."""
    users = json.loads(src.read_text("utf-8")).get("users", {})
    converted, kept = {}, []
    for key, records in users.items():
        our_id, sep, rest = key.partition(" — ")
        cb_id = mapping.get(our_id.strip().casefold())
        if cb_id is None:
            kept.append(key)
            converted[key] = records
        else:
            converted[cb_id + sep + rest] = records
    with open(dst, "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        items = list(converted.items())  # порядок как в compare: по очкам игрока
        for i, (key, records) in enumerate(items):
            f.write(f'    {json.dumps(key, ensure_ascii=False)}: [\n')
            for j, record in enumerate(records):
                f.write(f'      {json.dumps(record, ensure_ascii=False)}{"," if j < len(records) - 1 else ""}\n')
            f.write(f'    ]{"," if i < len(items) - 1 else ""}\n')
        f.write('  }\n}\n')
    if kept:
        print(f"{src.name}: нет ID CoreBoard у {len(kept)} игроков, оставил наши ID: {', '.join(kept[:5])}"
              + (" ..." if len(kept) > 5 else ""))


def publish(out, message):
    """Копирует файлы из рабочей папки в cbd/ (записи переводит на ID CoreBoard), коммитит и пушит."""
    CBD_DIR.mkdir(exist_ok=True)
    mapping_file = out / "gdr_to_cb_ids.json"
    mapping = json.loads(mapping_file.read_text("utf-8")) if mapping_file.exists() else {}
    rels = []
    for name in PUBLISHED:
        src, dst = out / name, CBD_DIR / name
        if not src.exists():
            sys.exit(f"Нет {src}, нечего публиковать.")
        if mapping and name in RECORDS_FILES:
            convert_records(src, dst, mapping)
        else:
            shutil.copyfile(src, dst)
        rels.append(str(dst.relative_to(ROOT)))
    for name in PUBLISHED_OPTIONAL:
        src, dst = out / name, CBD_DIR / name
        if src.exists():
            shutil.copyfile(src, dst)
            rels.append(str(dst.relative_to(ROOT)))

    git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "github-actions[bot]"))
    git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL",
                                               "41898282+github-actions[bot]@users.noreply.github.com"))
    git("add", *rels)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("Нечего коммитить: файлы в cbd/ не изменились.")
        return
    git("commit", "-m", message)
    for attempt in range(1, 4):  # параллельно может пушить другой шаг или workflow
        git("pull", "--rebase", "--autostash", check=False)
        r = git("push", check=False)
        if r.returncode == 0:
            print("Запушено.")
            return
        print(f"push не прошёл (попытка {attempt}/3): {r.stderr.strip()}")
    sys.exit("Не удалось запушить за 3 попытки. Коммит остался локально.")


CLAN_TAG_RE = re.compile(r"\[[^\]]*\]")


def strip_clan_tags(nick):
    """'[OX] zsakos' -> 'zsakos' (как в compare.py). Если остался бы пустой ник, не трогаем."""
    return re.sub(r"\s+", " ", CLAN_TAG_RE.sub("", nick)).strip() or nick


def make_players_with_waiting(out, cbd_ids, id_map):
    """missing_players_with_waiting.json: игроки, которые набирают меньше 250 очков ТОЛЬКО с waiting-записями.
    Берёт scripts/waiting_new_players.json (его пишет gd-rating --waiting), выбрасывает тех, кто уже есть на
    CoreBoard, выдаёт ID, продолжая после последнего ID в missing_players.json, и делает ники уникальными."""
    if not WAITING_NEW.exists():
        print("\nwaiting_new_players.json нет (gd-rating --waiting в этом запуске не работал): "
              "missing_players_with_waiting.json не обновляю.")
        return
    waiting = json.loads(WAITING_NEW.read_text("utf-8")).get("users", {})
    regular = json.loads((out / "missing_players.json").read_text("utf-8")).get("users", {})
    map_data = json.loads(id_map.read_text("utf-8")) if id_map.exists() else {}
    site_ids = json.loads(cbd_ids.read_text("utf-8")) if cbd_ids.exists() else {}
    mapping_file = out / "gdr_to_cb_ids.json"   # наш ID -> ID CoreBoard у тех, кто уже есть на CoreBoard
    on_cbd = {k.casefold() for k in json.loads(mapping_file.read_text("utf-8"))} if mapping_file.exists() else set()

    # Продолжаем нумерацию после самого большого ID: он стоит последним в missing_players.json (новые ID идут
    # по порядку после максимума из карты и сайта). Нет ни карты, ни ID с сайта: compare.py оставил наши ID,
    # и здесь тоже остаются наши.
    use_cb_ids = bool(map_data or site_ids)
    all_ids = [k for k in (*regular, *map_data, *site_ids.values()) if str(k).isdigit()] if use_cb_ids else []
    next_id = max(map(int, all_ids), default=0) + 1

    taken = {strip_clan_tags(i["nick"]).casefold() for i in regular.values() if i.get("nick")}
    taken |= {str(n).casefold() for n in site_ids}
    taken |= {str(v.get("nick", "")).casefold() for v in map_data.values()}

    result, already = {}, 0
    for gdr_id, info in waiting.items():
        if gdr_id.casefold() in on_cbd:   # игрок уже на CoreBoard: ему нужны не новый ID, а обычные добавления
            already += 1
            continue
        base = strip_clan_tags(info["nick"])
        nick, n = base, 1
        while nick.casefold() in taken:
            n += 1
            nick = f"{base}{n}"
        taken.add(nick.casefold())
        if use_cb_ids:
            key, next_id = str(next_id), next_id + 1
        else:
            key = gdr_id
        result[key] = {**info, "nick": nick}

    with open(out / "missing_players_with_waiting.json", "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        items = list(result.items())
        for i, (key, info) in enumerate(items):
            f.write(f"    {json.dumps(key, ensure_ascii=False)}: {json.dumps(info, ensure_ascii=False)}"
                    + (",\n" if i < len(items) - 1 else "\n"))
        f.write("  }\n}\n")
    print(f"\nmissing_players_with_waiting.json: игроков {len(result)} (уже на CoreBoard, пропущено: {already})"
          + (f", ID с {next_id - len(result)} по {next_id - 1}" if result and use_cb_ids else ""))


def main():
    cbd_txt, gdr_txt = HERE / "cbd.txt", HERE / "gdr.txt"
    run([sys.executable, HERE / "coreboard-parse.py"], "CoreBoard: оба топа -> cbd.txt")
    run([sys.executable, HERE / "gd-rating.py", "--src", "all", "--out", gdr_txt.name], "Наш рейтинг -> gdr.txt")
    for f in (cbd_txt, gdr_txt):
        if not f.exists() or f.stat().st_size == 0:
            sys.exit(f"Нет файла {f.name} (или он пустой). Останавливаюсь.")

    cbd_ids = HERE / "cbd_ids.json"   # ник -> ID с сайта; делает coreboard-parse, когда в ссылках появятся ID
    id_map = next((p for p in (ROOT / "coreboard_id_map.json", HERE / "coreboard_id_map.json") if p.exists()),
                  ROOT / "coreboard_id_map.json")

    shutil.rmtree(OUT, ignore_errors=True)   # результаты прошлого запуска убираем, чтобы старые команды не приняли за новые
    out = OUT
    out.mkdir(parents=True)

    run([sys.executable, HERE / "compare.py", "in-gdr-not-in-cbd", "--cbd", cbd_txt, "--gdr", gdr_txt, "--out-dir", out],
        "compare: in-gdr-not-in-cbd")
    for name in ("missing_achievements.json", "missing_achievements_with_nicks.json", "missing_players.json"):
        if not (out / name).exists():
            sys.exit(f"compare.py не создал {name}.")
    found = json.loads((out / "missing_achievements.json").read_text("utf-8")).get("users", {})
    total = sum(len(v) for v in found.values())
    new_players = len(json.loads((out / "missing_players.json").read_text("utf-8")).get("users", {}))
    print(f"\ncompare нашёл: игроков {len(found)}, записей {total}; новых игроков (нет на CoreBoard) {new_players}")

    if total or new_players:
        # --prev-players: ссылки, уже найденные в опубликованном cbd/missing_players.json, заново не ищутся
        run([sys.executable, HERE / "add-video-links.py", "--out-dir", out, "--id-map", id_map,
             "--prev-players", CBD_DIR / "missing_players.json"], "Видео и команды (add-video-links)")
        if not (out / "commands.txt").exists():
            sys.exit("add-video-links.py не создал commands.txt.")
    else:
        print("В CoreBoard нечего добавлять.")
        (out / "commands.txt").write_text("", "utf-8")

    make_players_with_waiting(out, cbd_ids, id_map)
    if args.commit:
        publish(out, f"cbd: {len(found)} игроков, {total} записей, новых игроков {new_players} (авто, gdr-not-in-cbd)")


main()
