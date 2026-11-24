#!/usr/bin/env python3
"""
Рекорды CoreBoard, которые больше месяца не принимают ни на одном листе -> cbd/unaccepted_records.json.

Берёт то, что есть на CoreBoard, а у нас нет (root/missing_achievements.json, его только что записал sync-extras:
CoreBoard минус глобал accepted, Pointercrate и WML), и выбрасывает записи, которые ждут проверки на глобале (статус
waiting у любого аккаунта человека, процент не ниже). Для каждой оставшейся записи:

  1. видео: ссылка на видео рекорда с CoreBoard (scripts/cbd.json, его пишет coreboard-parse.py);
  2. дата видео: когда оно загружено на YouTube (ключ YOUTUBE_API_KEY, если есть, иначе страница видео);
  3. дата уровня: date_created в глобале (api.demonlist.org/level/classic/get);
  4. берётся самая поздняя из двух дат; если с неё прошло больше --days дней (по умолчанию 30), запись попадает в файл.

Формат как у остальных файлов cbd/ (jsonfmt.py): {"users": {"<ID CoreBoard>": ["ник", ["Уровень", процент, "ссылка"], ...]}}.
Запись, у которой нет ссылки на видео или чью дату не удалось узнать, в файл НЕ попадает (рисковать ложным срабатыванием
нельзя): они перечислены в scripts/cbd-stale/report.txt вместе с причиной.

Кеш: scripts/levelcache.json (общий с gd-rating): "w<ID>" списки waiting игрока, "ld<ID уровня>" дата уровня,
"yt<ID видео>" дата видео.

  python scripts/cbd-stale.py [--commit] [--days 30]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import levels as levelsearch
from idmap import load_id_map, load_links
from jsonfmt import parse_lenient, write_players

HERE = Path(__file__).resolve().parent          # scripts/
ROOT = HERE.parent                              # корень репозитория
CBD_DIR = ROOT / "cbd"
OUT_FILE = CBD_DIR / "unaccepted_records.json"
WORK = HERE / "cbd-stale"                       # отладочные файлы (report.txt)
MISSING = ROOT / "missing_achievements.json"    # его пишет sync-extras
CACHE_FILE = HERE / "levelcache.json"           # общий с gd-rating

DL = "https://api.demonlist.org/"
WAITING_TTL = 3 * 3600          # как в gd-rating
LEVEL_TTL = 30 * 86400          # date_created уровня не меняется
VIDEO_OK_TTL = 3650 * 86400     # дата загрузки видео не меняется
VIDEO_FAIL_TTL = 86400          # не нашли дату: повторяем завтра
WORKERS = 8
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

ap = argparse.ArgumentParser()
ap.add_argument("--commit", action="store_true", help="закоммитить и запушить cbd/unaccepted_records.json")
ap.add_argument("--days", type=int, default=30, help="сколько дней должно пройти с самой поздней из двух дат (30)")
args = ap.parse_args()

lock = threading.Lock()
cache = {}
norm = levelsearch.normalize

YT = re.compile(
    r"(?:(?:youtu\.be|youtube\.be|yotu\.be)/(?:watch\?(?:.*&)?v=)?"
    r"|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/))([A-Za-z0-9_-]{11})", re.I)
YT_DATE = [re.compile(r'itemprop="uploadDate"\s+content="([^"]+)"'),
           re.compile(r'"uploadDate":"([^"]+)"'),
           re.compile(r'"publishDate":"([^"]+)"')]


# ---------- сеть и кеш ----------
def get_json(url, tries=6):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gl-ext-top6"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 and e.code < 500:
                break
        except Exception as e:
            last = e
        time.sleep(min(2 ** i, 30) + 1)
    raise last


def load_cache():
    global cache
    try:
        cache = json.loads(CACHE_FILE.read_text("utf-8")) if CACHE_FILE.exists() else {}
    except (OSError, ValueError):
        cache = {}


def save_cache():
    """Кеш общий с gd-rating: перечитываем файл и дописываем только свои ключи, чтобы не затереть чужие."""
    with lock:
        try:
            disk = json.loads(CACHE_FILE.read_text("utf-8")) if CACHE_FILE.exists() else {}
        except (OSError, ValueError):
            disk = {}
        for k, v in cache.items():
            if k[:2] in ("ld", "yt") or k[:1] == "w":
                disk[k] = v
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(disk), "utf-8")
        os.replace(tmp, CACHE_FILE)


def parse_date(s):
    """ISO-дата (с поясом или без) -> datetime в UTC. None, если не разобралась."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def pmap(fn, items, workers=WORKERS):
    """Результаты fn(item) по порядку; сбой одной задачи даёт исключение в её месте, а не обрыв всего."""
    def wrap(it):
        try:
            return fn(it)
        except Exception as e:
            return e
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(wrap, items))


# ---------- аккаунты человека на глобале (как accounts_of в gd-rating) ----------
def read_fix():
    for base in (ROOT, HERE):
        p = base / "fix.json"
        if p.exists():
            return parse_lenient(p.read_text("utf-8"), p.name)
    return {}


def build_accounts(links, fix):
    """наш ID -> все его аккаунты глобала (главный первым): сам ID, аккаунты Demonlist из links.json и склеенные через fix.json."""
    absorbed = {}
    for m in fix.get("merges", []):
        for a in m.get("absorb", []):
            absorbed.setdefault(str(m["keep"]), []).append(str(a["id"]))

    def accounts(uid):
        uid, out = str(uid), []

        def add(x):
            x = str(x)
            if x.isdigit() and x not in out:
                out.append(x)
        add(uid)
        entry = links.get(uid)
        if isinstance(entry, dict):
            blocks = [entry] + ([entry["other_lists"]] if isinstance(entry.get("other_lists"), dict) else [])
            for b in blocks:
                for site, ids in b.items():
                    if site in ("_name", "channels", "other_lists") or not any(s in str(site).lower() for s in ("demonlist", "global")):
                        continue
                    for x in (ids if isinstance(ids, list) else [ids]):
                        if x is not None:
                            add(x)
        for a in absorbed.get(uid, []):
            add(a)
        return out
    return accounts


# ---------- данные из глобала ----------
def user_waiting(uid):
    """[[ID уровня, процент], ...] записей игрока в статусе waiting (тот же кеш «w<ID>», что у gd-rating)."""
    key = "w" + str(uid)
    c = cache.get(key)
    if c and time.time() - c["at"] < WAITING_TTL:
        return [[w[0], w[1]] for w in c["wait"]]
    wait, offset = [], 0
    while True:
        try:
            d = get_json(f"{DL}user/record/list?user_id={uid}&limit=50&offset={offset}").get("data") or {}
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):    # такого игрока на глобале нет
                break
            raise
        recs = d.get("records") or []
        for r in recs:
            lvl = (r.get("level") or {}).get("id")
            if str(r.get("status") or "accepted").lower() == "waiting" and lvl is not None:
                pct = r.get("percent")
                wait.append([str(lvl), float(pct if pct is not None else 100), r.get("video_url")])
        offset += len(recs)
        if not recs or offset >= int(d.get("total_count") or 0):
            break
    with lock:
        cache[key] = {"at": time.time(), "wait": wait}
    return [[w[0], w[1]] for w in wait]


def level_date(lvl_id):
    """date_created уровня в глобале (datetime UTC) или None."""
    key = "ld" + str(lvl_id)
    c = cache.get(key)
    if c and time.time() - c["at"] < (LEVEL_TTL if c.get("d") else VIDEO_FAIL_TTL):
        return parse_date(c.get("d"))
    d = (get_json(f"{DL}level/classic/get?id={lvl_id}").get("data") or {}).get("date_created")
    with lock:
        cache[key] = {"at": time.time(), "d": d}
    return parse_date(d)


# ---------- дата загрузки видео ----------
def yt_id(url):
    m = YT.search(str(url or ""))
    return m.group(1) if m else None


def _cache_video(vid, date):
    with lock:
        cache["yt" + vid] = {"at": time.time(), "d": date}


def _video_cached(vid):
    """(есть ли свежая запись, дата)."""
    c = cache.get("yt" + vid)
    if c and time.time() - c["at"] < (VIDEO_OK_TTL if c.get("d") else VIDEO_FAIL_TTL):
        return True, parse_date(c.get("d"))
    return False, None


def yt_api_dates(ids, key):
    """YouTube Data API: до 50 видео за запрос, 1 единица квоты. Удалённые и закрытые видео в ответе не приходят."""
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(
            {"part": "snippet", "id": ",".join(chunk), "key": key, "maxResults": 50})
        got = {it["id"]: (it.get("snippet") or {}).get("publishedAt") for it in get_json(url).get("items", [])}
        for vid in chunk:
            _cache_video(vid, got.get(vid))


def yt_page_date(vid):
    """Дата загрузки со страницы видео (ключ API не нужен). None, если страница не отдала дату."""
    for i in range(3):
        try:
            req = urllib.request.Request(
                "https://www.youtube.com/watch?v=" + vid,
                headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9",
                         "Cookie": "CONSENT=YES+1; SOCS=CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"})
            with urllib.request.urlopen(req, timeout=30) as r:
                buf = b""
                while len(buf) < 1_500_000:
                    part = r.read(65536)
                    if not part:
                        break
                    buf += part
                    text = buf.decode("utf-8", "replace")
                    for rx in YT_DATE:
                        m = rx.search(text)
                        if m:
                            return m.group(1)
            return None
        except urllib.error.HTTPError as e:
            if e.code in (412, 429) or e.code >= 500:
                time.sleep(2 ** i)
                continue
            return None     # 401/403/404: видео нет
        except Exception:
            time.sleep(2 ** i)
    return None


def fill_video_dates(ids):
    """Узнаёт даты ещё не закешированных видео YouTube. Возвращает {ID видео: datetime или None}."""
    need = [v for v in dict.fromkeys(ids) if not _video_cached(v)[0]]
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if need and key:
        try:
            yt_api_dates(need, key)
            need = []
        except Exception as e:
            print(f"YouTube API не ответил ({e}), беру даты со страниц видео")
    if need:
        print(f"Даты видео: открываю страницы YouTube ({len(need)})")

        def one(v):
            d = yt_page_date(v)
            _cache_video(v, d)
            time.sleep(0.3)
        pmap(one, need, workers=3)
    return {v: _video_cached(v)[1] for v in ids}


# ---------- вход: что не принято ----------
def read_json(path, what):
    if not path.exists():
        sys.exit(f"Нет {path} ({what}). Сначала должны отработать sync-extras.py и coreboard-parse.py.")
    return parse_lenient(path.read_text("utf-8"), path.name)


def level_names(tp):
    """ID уровня (в том числе алиас x...) -> (ID глобала, название на CoreBoard)."""
    by_name = {norm(e.get("name")): k for k, e in tp.items() if str(k).isdigit()}
    out = {}
    for k, e in tp.items():
        if str(k).isdigit():
            out[str(k)] = (str(k), e.get("name"))
        else:
            like = e.get("like")
            gid = by_name.get(norm(like))
            if gid:
                out[str(k)] = (gid, e.get("name") or like)
    return out


def main():
    load_cache()
    tp = read_json(ROOT / "levels_tp.json", "список уровней").get("levels", {})
    lv = level_names(tp)
    missing = read_json(MISSING, "что есть на CoreBoard, а у нас нет").get("users", {})
    board = read_json(HERE / "cbd.json", "полный разбор CoreBoard")
    links = load_links()
    accounts = build_accounts(links, read_fix())

    id_map = load_id_map()      # {ID CoreBoard: {"nick", "gdr_id"}}
    cb_by_gdr = {str(v["gdr_id"]).casefold(): k for k, v in id_map.items() if v.get("gdr_id")}
    rows_by_id = {str(r["id"]): r for r in board if r.get("id")}
    rows_by_nick = {str(r["name"]).casefold(): r for r in board}

    report = []     # (строка отчёта)
    cand = []       # [{uid, cb_id, nick, row, lvl_key, gid, name, pct}]
    for key, recs in missing.items():
        uid, _, nick = str(key).partition(" — ")
        uid = uid.strip()
        cb_id = cb_by_gdr.get(uid.casefold())
        row = rows_by_id.get(cb_id) if cb_id else None
        if row is None and nick:
            row = rows_by_nick.get(nick.strip().casefold())
            cb_id = cb_id or (str(row["id"]) if row and row.get("id") else None)
        for r in recs:
            lvl_key, pct = str(r[0]), float(r[1])
            gid, name = lv.get(lvl_key, (None, None))
            cand.append({"uid": uid, "cb_id": cb_id or uid, "row": row, "nick": (row or {}).get("name") or nick.strip()
                         or (id_map.get(cb_id or "") or {}).get("nick") or uid,
                         "lvl_key": lvl_key, "gid": gid, "name": name, "pct": pct})
    print(f"Не принятых записей CoreBoard (missing_achievements.json): {len(cand)}")

    # 1. waiting на глобале: у любого аккаунта человека есть такая же или лучшая запись
    uids = sorted({a for c in cand for a in accounts(c["uid"])})
    print(f"Проверяю waiting у аккаунтов глобала: {len(uids)}")
    waiting, failed = {}, set()
    for uid, res in zip(uids, pmap(user_waiting, uids)):
        if isinstance(res, Exception):
            failed.add(uid)
        else:
            waiting[uid] = res

    alive = []
    for c in cand:
        label = f"{c['cb_id']} | {c['nick']} | {c['name'] or c['lvl_key']} {c['pct']:g}%"
        if not c["gid"]:
            report.append(f"{label} | пропущено: у уровня нет ID глобала в levels_tp.json")
            continue
        accs = accounts(c["uid"])
        if any(a in failed for a in accs):
            report.append(f"{label} | пропущено: не удалось проверить waiting (ошибка запроса к глобалу)")
            continue
        if any(w[0] == c["gid"] and w[1] >= c["pct"] for a in accs for w in waiting.get(a, [])):
            report.append(f"{label} | пропущено: ждёт проверки на глобале (waiting)")
            continue
        alive.append(c)

    # 2. видео с CoreBoard
    todo = []
    for c in alive:
        label = f"{c['cb_id']} | {c['nick']} | {c['name']} {c['pct']:g}%"
        vids = (c["row"] or {}).get("videos") or []
        names = {norm(c["name"]), norm((tp.get(c["gid"]) or {}).get("name"))}   # алиас: ещё и название самого уровня
        exact = [v for v in vids if norm(v["level"]) in names and abs(float(v["percent"]) - c["pct"]) < 0.01]
        same = [v for v in vids if norm(v["level"]) in names]
        pick = (exact or same or [None])[0]
        if not pick:
            report.append(f"{label} | пропущено: на CoreBoard нет ссылки на видео этого рекорда")
            continue
        c["video"], c["label"] = pick["video"], label
        todo.append(c)

    # 3. даты: уровня и видео
    gids = sorted({c["gid"] for c in todo})
    print(f"Даты уровней: {len(gids)}")
    ldates = {}
    for g, res in zip(gids, pmap(level_date, gids)):
        ldates[g] = None if isinstance(res, Exception) else res
    vdates = fill_video_dates([v for v in (yt_id(c["video"]) for c in todo) if v])

    now = datetime.now(timezone.utc)
    limit = timedelta(days=args.days)
    result, kept = {}, 0
    for c in todo:
        ld, vid = ldates.get(c["gid"]), yt_id(c["video"])
        vd = vdates.get(vid) if vid else None
        if ld is None:
            report.append(f"{c['label']} | пропущено: не удалось узнать date_created уровня {c['gid']}")
            continue
        if vd is None:
            why = "видео не с YouTube" if not vid else "YouTube не отдал дату видео (удалено, закрыто или сбой)"
            report.append(f"{c['label']} | пропущено: дата видео неизвестна ({why}): {c['video']}")
            continue
        latest = max(ld, vd)
        age = now - latest
        if age <= limit:
            report.append(f"{c['label']} | рано: самая поздняя дата {latest:%Y-%m-%d} "
                          f"({'видео' if vd >= ld else 'уровень'}), прошло {age.days} дн.")
            continue
        pct = int(c["pct"]) if c["pct"] == int(c["pct"]) else c["pct"]
        nick, recs = result.setdefault(c["cb_id"], (c["nick"], []))
        recs.append([c["name"], pct, c["video"]])
        kept += 1
        report.append(f"{c['label']} | В ФАЙЛ: видео {vd:%Y-%m-%d}, уровень {ld:%Y-%m-%d}, прошло {age.days} дн.")

    result = dict(sorted(result.items(), key=lambda kv: (not kv[0].isdigit(), int(kv[0]) if kv[0].isdigit() else 0, kv[0])))
    for _, recs in result.values():
        recs.sort(key=lambda r: (-float(r[1]), r[0]))
    CBD_DIR.mkdir(exist_ok=True)
    write_players(OUT_FILE, result)
    WORK.mkdir(exist_ok=True)
    (WORK / "report.txt").write_text("\n".join(sorted(report)) + "\n", "utf-8")
    save_cache()
    print(f"\nunaccepted_records.json: игроков {len(result)}, записей {kept} (причины по остальным: scripts/cbd-stale/report.txt)")

    if args.commit:
        publish(f"cbd: не принятые больше {args.days} дн. {kept} записей у {len(result)} игроков (авто)")


# ---------- git ----------
def git(*a, check=True):
    r = subprocess.run(["git", *a], cwd=ROOT, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(a)} не удался:\n{r.stdout}{r.stderr}")
    return r


def publish(message):
    git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "github-actions[bot]"))
    git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com"))
    git("add", str(OUT_FILE.relative_to(ROOT)))
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("Нечего коммитить: файл не изменился.")
        return
    git("commit", "-m", message)
    for attempt in range(1, 4):
        git("pull", "--rebase", "--autostash", check=False)
        r = git("push", check=False)
        if r.returncode == 0:
            print("Запушено.")
            return
        print(f"push не прошёл (попытка {attempt}/3): {r.stderr.strip()}")
    sys.exit("Не удалось запушить за 3 попытки. Коммит остался локально.")


main()
