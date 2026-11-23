#!/usr/bin/env python3
"""
Находит то, что есть в нашем рейтинге (gdr), а в CoreBoard (cbd) нет или там процент ниже, ищет на это видео и
собирает команды /fulladd и /try. Обратная сторона sync-extras.py (my_extras.json этот скрипт не трогает).

Шаги:
  1. coreboard-parse.py -> cbd.txt (если sync-extras только что скачал страницы, они берутся из кеша, а не качаются заново);
  2. gd-rating.py --src all -> gdr.txt (наш рейтинг, ID игроков в [скобках]);
  3. compare.py in-gdr-not-in-cbd -> missing_achievements*.json, missing_players.json;
  4. add-video-links.py -> видео (GlobalList, Pointercrate) и commands.txt;
  5. с --commit кладёт в cbd/ missing_achievements.json, missing_players.json и missing_players_with_waiting.json
     (игроки, у которых меньше 250 очков только с waiting-записями; ID продолжаются после последнего в
     missing_players.json) в формате jsonfmt.py, коммитит и пушит. commands.txt остаётся только в scripts/cbd-update/.

ID CoreBoard: из cbd_ids.json (с сайта, главнее) и links.json (other_lists.coreboard.main), см. idmap.py. В missing_achievements*.json
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
import time
from pathlib import Path

from idmap import load_id_map
from jsonfmt import write_players

HERE = Path(__file__).resolve().parent          # scripts/
ROOT = HERE.parent                              # корень репозитория
CBD_DIR = ROOT / "cbd"                          # сюда копируются результаты при --commit
OUT = HERE / "cbd-update"
OLD_FILES = ["cbd/commands.txt", "cbd/missing_achievements_with_nicks.json"]  # в репозитории их больше быть не должно
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


def players_achievements(src, mapping):
    """missing_achievements_with_nicks.json ('наш ID — ник': [[ID уровня, процент, название, ссылка]]) ->
    {ID CoreBoard: (ник, [[название, процент, ссылка]])}. Игрока, которого в карте нет, оставляем под нашим ID,
    чтобы не потерять записи (и предупреждаем)."""
    users = json.loads(src.read_text("utf-8")).get("users", {})
    out, kept = {}, []
    for key, records in users.items():
        our_id, _, nick = key.partition(" — ")
        cb_id = mapping.get(our_id.strip().casefold())
        if cb_id is None:
            kept.append(key)
            cb_id = our_id.strip()
        out[cb_id] = (nick, [[r[2], r[1], r[3] if len(r) > 3 else ""] for r in records])
    if kept:
        print(f"{src.name}: нет ID CoreBoard у {len(kept)} игроков, оставил наши ID: {', '.join(kept[:5])}"
              + (" ..." if len(kept) > 5 else ""))
    return out


def players_new(src):
    """missing_players*.json ({ключ: {"nick", "records", ...}}) -> {ключ: (ник, записи)}."""
    users = json.loads(src.read_text("utf-8")).get("users", {})
    return {key: (info["nick"], info["records"]) for key, info in users.items()}


def publish(out, message):
    """Копирует файлы из рабочей папки в cbd/ (записи переводит на ID CoreBoard), коммитит и пушит."""
    CBD_DIR.mkdir(exist_ok=True)
    mapping_file = out / "gdr_to_cb_ids.json"
    mapping = json.loads(mapping_file.read_text("utf-8")) if mapping_file.exists() else {}
    rels = []
    jobs = {"missing_achievements.json": lambda: players_achievements(out / "missing_achievements_with_nicks.json", mapping),
            "missing_players.json": lambda: players_new(out / "missing_players.json")}
    if (out / "missing_players_with_waiting.json").exists():
        jobs["missing_players_with_waiting.json"] = lambda: players_new(out / "missing_players_with_waiting.json")
    for name, build in jobs.items():
        if not (out / name).exists():
            sys.exit(f"Нет {out / name}, нечего публиковать.")
        write_players(CBD_DIR / name, build())
        rels.append(str(CBD_DIR.joinpath(name).relative_to(ROOT)))

    git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "github-actions[bot]"))
    git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL",
                                               "41898282+github-actions[bot]@users.noreply.github.com"))
    git("add", *rels)
    git("rm", "-q", "--ignore-unmatch", "--", *OLD_FILES)  # старые файлы убираем из репозитория
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


def previous_waiting_links():
    """Ссылки из прошлого опубликованного cbd/missing_players_with_waiting.json: (ник, уровень, процент) -> ссылка."""
    cache = {}
    try:
        users = json.loads((CBD_DIR / "missing_players_with_waiting.json").read_text("utf-8")).get("users", {})
    except (OSError, ValueError):
        return cache
    for info in users.values():
        if isinstance(info, list) and info:
            for r in info[1:]:
                if isinstance(r, list) and len(r) >= 3 and r[2]:
                    cache[(str(info[0]).casefold(), str(r[0]).casefold(), str(r[1]))] = r[2]
    return cache


def fill_missing_links(result):
    """У записей без ссылки ищет видео: GlobalList (все аккаунты человека) и Pointercrate. gd-rating кладёт ссылку только
    у waiting-записей, а принятые рекорды игрока приходят пустыми, хотя видео у них есть на глобале / Pointercrate."""
    todo = [(info, i) for info in result.values() for i, r in enumerate(info["records"]) if not r[2]]
    if not todo:
        return
    try:
        from videos import DELAY, find_video_retry, get_links, normalize_link, parse_percent
        links = get_links()
    except Exception as error:
        print(f"Ссылки для missing_players_with_waiting.json не ищу: не загрузились links.json / videos.py ({error})")
        return
    previous, found, missing = previous_waiting_links(), 0, []
    for info, i in todo:
        record = info["records"][i]
        name, pct = record[0], record[1]
        cached = previous.get((info["nick"].casefold(), str(name).casefold(), str(pct)))
        if cached:
            record[2], found = cached, found + 1
            continue
        level_ids = info.get("level_ids") or []
        level_id = level_ids[i] if i < len(level_ids) else None
        percent = parse_percent(pct)
        if percent is None or not str(level_id).isdigit():
            missing.append(f"{info.get('gdr_id')} | {info['nick']} | {name} {pct}% | " +
                           ("непонятный процент" if percent is None else f"уровень {level_id} не из глобала"))
            continue
        try:
            video, source = find_video_retry(info["gdr_id"], level_id, percent, links)
            time.sleep(DELAY)
        except Exception as error:
            video, source = "", f"ОШИБКА: {error}"
        if video:
            record[2], found = normalize_link(video), found + 1
        else:
            missing.append(f"{info.get('gdr_id')} | {info['nick']} | {name} {pct}% | {source}")
    (OUT / "missing_players_with_waiting_video_links.txt").write_text("\n".join(missing) + ("\n" if missing else ""), "utf-8")
    print(f"missing_players_with_waiting.json: ссылок найдено {found} из {len(todo)}, "
          f"без ссылки {len(missing)} (причины в missing_players_with_waiting_video_links.txt)")


def make_players_with_waiting(out, cbd_ids):
    """missing_players_with_waiting.json: игроки, которые набирают меньше 250 очков ТОЛЬКО с waiting-записями.
    Берёт scripts/waiting_new_players.json (его пишет gd-rating --waiting), выбрасывает тех, кто уже есть на
    CoreBoard, выдаёт ID, продолжая после последнего ID в missing_players.json, и делает ники уникальными."""
    if not WAITING_NEW.exists():
        print("\nwaiting_new_players.json нет (gd-rating --waiting в этом запуске не работал): "
              "missing_players_with_waiting.json не обновляю.")
        return
    waiting = json.loads(WAITING_NEW.read_text("utf-8")).get("users", {})
    regular = json.loads((out / "missing_players.json").read_text("utf-8")).get("users", {})
    map_data = load_id_map()
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

    fill_missing_links(result)

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
        run([sys.executable, HERE / "add-video-links.py", "--out-dir", out,
             "--prev-players", CBD_DIR / "missing_players.json"], "Видео и команды (add-video-links)")
        if not (out / "commands.txt").exists():
            sys.exit("add-video-links.py не создал commands.txt.")
    else:
        print("В CoreBoard нечего добавлять.")
        (out / "commands.txt").write_text("", "utf-8")

    make_players_with_waiting(out, cbd_ids)
    if args.commit:
        publish(out, f"cbd: {len(found)} игроков, {total} записей, новых игроков {new_players} (авто, gdr-not-in-cbd)")


main()
