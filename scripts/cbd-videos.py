#!/usr/bin/env python3
"""
Ссылки на видео для ВСЕХ рекордов всех игроков CoreBoard -> cbd/cbd_videos.json:

  {"users": {
    "<ID CoreBoard>": [
      ["Название уровня как на CoreBoard", процент, "ссылка"],
      ...
    ],
    ...
  }}

Шаги:
  1. coreboard-parse.py -> cbd.json (оба топа CoreBoard);
  2. ID игрока: с сайта (cbd_ids.json) или из coreboard_id_map.json (по нику); игрок без ID пропускается с предупреждением;
  3. для каждого рекорда: если в уже лежащем cbd/cbd_videos.json есть тот же игрок + уровень + ПРОЦЕНТ, берётся его ссылка
     (в том числе вписанная вручную: скрипт её не трогает); если процент изменился или рекорда не было, ссылка
     ищется заново (GlobalList, Pointercrate), не нашлась - "";
  4. с --commit коммитит и пушит cbd/cbd_videos.json.

Ссылку можно вписать вручную прямо в cbd/cbd_videos.json: пока процент рекорда тот же, она сохраняется.
Не нашедшиеся ссылки перепроверяются не чаще раза в RETRY_DAYS дней (кеш scripts/videocache.json).
Первый запуск ищет видео для тысяч рекордов, поэтому есть лимит времени --budget-min (по умолчанию 40 минут):
что не успели, дойдёт в следующем запуске.

  python scripts/cbd-videos.py [--commit] [--budget-min 40]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import levels as levelsearch
from videos import find_video_retry, get_json, get_links, normalize_link

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT_FILE = ROOT / "cbd" / "cbd_videos.json"
CACHE_FILE = HERE / "videocache.json"   # "ключ записи" -> когда в последний раз искали и не нашли
LEVELS_URL = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"
RETRY_DAYS = 3

ap = argparse.ArgumentParser()
ap.add_argument("--commit", action="store_true")
ap.add_argument("--budget-min", type=float, default=40, help="сколько минут максимум искать видео")
args = ap.parse_args()
deadline = time.time() + args.budget_min * 60


def git(*a, check=True):
    r = subprocess.run(["git", *a], cwd=ROOT, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(a)} не удался:\n{r.stdout}{r.stderr}")
    return r


def fmt_pct(x):
    x = float(x)
    return int(x) if x == int(x) else x


def lvl_key(name):
    return levelsearch.normalize(name)


def load_existing():
    """cbd/cbd_videos.json -> {ключ игрока: {(уровень, процент): ссылка}}."""
    try:
        users = json.loads(OUT_FILE.read_text("utf-8")).get("users", {})
    except (OSError, ValueError):
        return {}
    out = {}
    for key, rows in users.items():
        m = out.setdefault(str(key), {})
        for r in rows:
            if isinstance(r, list) and len(r) >= 3 and r[2]:
                m[(lvl_key(r[0]), fmt_pct(r[1]))] = str(r[2])
    return out


def save_users(users):
    items = sorted(users.items(), key=lambda kv: int(kv[0]))
    lines = ['{', '  "users": {']
    for i, (key, rows) in enumerate(items):
        lines.append(f'    {json.dumps(key, ensure_ascii=False)}: [')
        for j, r in enumerate(rows):
            lines.append(f'      {json.dumps(r, ensure_ascii=False)}' + ("," if j < len(rows) - 1 else ""))
        lines.append("    ]" + ("," if i < len(items) - 1 else ""))
    lines += ['  }', '}', '']
    OUT_FILE.parent.mkdir(exist_ok=True)
    tmp = OUT_FILE.with_suffix(".json.tmp")
    tmp.write_text("\n".join(lines), "utf-8")
    os.replace(tmp, OUT_FILE)


def commit_and_push(message):
    git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "github-actions[bot]"))
    git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com"))
    git("add", str(OUT_FILE.relative_to(ROOT)))
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("Нечего коммитить: файл не изменился.")
        return
    git("commit", "-m", message)
    for attempt in range(1, 4):  # параллельно может пушить другой workflow
        git("pull", "--rebase", "--autostash", check=False)
        r = git("push", check=False)
        if r.returncode == 0:
            print("Запушено.")
            return
        print(f"push не прошёл (попытка {attempt}/3): {r.stderr.strip()}")
    sys.exit("Не удалось запушить за 3 попытки. Коммит остался локально.")


def main():
    r = subprocess.run([sys.executable, HERE / "coreboard-parse.py"], cwd=HERE)
    if r.returncode != 0:
        sys.exit("coreboard-parse.py завершился с ошибкой.")
    rows = json.loads((HERE / "cbd.json").read_text("utf-8"))
    if not rows:
        sys.exit("cbd.json пустой. Останавливаюсь.")

    # ID CoreBoard и наш ID (по нему ищем видео) из coreboard_id_map.json: {"<ID CoreBoard>": {"nick", "gdr_id"}}
    id_map_file = next((p for p in (ROOT / "coreboard_id_map.json", HERE / "coreboard_id_map.json") if p.exists()), None)
    id_map = json.loads(id_map_file.read_text("utf-8")) if id_map_file else {}
    cb_by_nick = {str(v.get("nick", "")).casefold(): str(k) for k, v in id_map.items()
                  if isinstance(v, dict) and str(k).isdigit()}

    try:
        tp = get_json(LEVELS_URL)[0]["levels"]
    except Exception as e:
        sys.exit(f"levels_tp.json: не удалось загрузить с GitHub ({e}). Останавливаюсь.")
    level_index = levelsearch.name_index(tp, [])  # название -> ключ уровня (ID глобала или x...)

    existing = load_existing()
    cache = json.loads(CACHE_FILE.read_text("utf-8")) if CACHE_FILE.exists() else {}
    links = get_links()

    players = []  # (ID CoreBoard, наш ID или None, [(название, процент)])
    no_id = []
    for row in rows:
        cb_id = row.get("id") or cb_by_nick.get(row["name"].casefold())
        if not cb_id:
            no_id.append(row["name"])
            continue
        key = str(cb_id)
        gdr_id = (id_map.get(key) or {}).get("gdr_id")
        recs = [(r["level"], fmt_pct(r["percent"])) for r in row["records"]]
        players.append((key, str(gdr_id) if gdr_id else None, recs))

    if no_id:
        print(f"ВНИМАНИЕ: нет ID CoreBoard у {len(no_id)} игроков, пропущены: {', '.join(no_id[:10])}")
    found = {}  # (ключ игрока, уровень, процент) -> ссылка, найденная в этом запуске
    searched = skipped = out_of_time = 0
    now = time.time()
    for key, gdr_id, recs in players:
        old = existing.get(key, {})
        for level, pct in recs:
            if (lvl_key(level), pct) in old:
                continue  # рекорд не изменился: ссылка (в том числе ручная) остаётся
            level_id = level_index.get(lvl_key(level))
            if not gdr_id or not level_id or not level_id.isdigit():
                continue  # искать негде: нет нашего ID игрока или уровня нет в глобале
            ck = f"{gdr_id}|{level_id}|{pct}"
            if now - cache.get(ck, 0) < RETRY_DAYS * 86400:
                skipped += 1
                continue
            if time.time() > deadline:
                out_of_time += 1
                continue
            try:
                video, source = find_video_retry(gdr_id, level_id, int(pct), links)
            except Exception as e:
                print(f"  {key} {level} {pct}%: ОШИБКА {e}")
                continue
            searched += 1
            if video:
                found[(key, lvl_key(level), pct)] = normalize_link(video)
            elif not source.startswith("НЕ ПРОВЕРЕНО"):
                cache[ck] = time.time()  # видео реально нет: не ищем снова несколько дней
            if searched % 50 == 0:
                print(f"  поиск: {searched}, найдено {len(found)}", flush=True)
                CACHE_FILE.write_text(json.dumps(cache), "utf-8")
    CACHE_FILE.write_text(json.dumps(cache), "utf-8")

    if args.commit:  # забираем чужие правки (ручные ссылки), пока мы искали
        git("pull", "--rebase", "--autostash", check=False)
    existing = load_existing()

    users = {}
    for key, _gdr, recs in players:
        old = existing.get(key, {})
        out = []
        for level, pct in recs:
            link = old.get((lvl_key(level), pct)) or found.get((key, lvl_key(level), pct), "")
            out.append([level, pct, link])
        users[key] = out
    save_users(users)
    with_link = sum(1 for v in users.values() for r in v if r[2])
    total = sum(len(v) for v in users.values())
    print(f"Игроков {len(users)}, записей {total}, со ссылкой {with_link}; искали {searched}, найдено {len(found)}, "
          f"пропущено из-за недавней неудачи {skipped}, не хватило времени {out_of_time}")
    if args.commit:
        commit_and_push(f"cbd_videos: {with_link}/{total} ссылок, найдено новых {len(found)} (авто)")


main()
