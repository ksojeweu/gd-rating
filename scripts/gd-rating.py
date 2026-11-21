#!/usr/bin/env python3
"""Рейтинг по геометрическому среднему 6 лучших записей (меньше = лучше).
100% = h (место уровня); иначе f = h*a*(T/c)^(ln(b/a)/ln(T/t)), T и t из levels_tp.json (только с GitHub).
Источники: глобал (всегда), pointercrate, другие листы из реестра (сейчас wml), my_extras.

  gd-rating.py --src pointercrate,wml --out gdr_gl_pc_wml.txt --waiting     (sync-extras)
  gd-rating.py --src all --out gdr.txt                                       (cbd-update)

Результат: <out> (рейтинг строками '5) 🇵🇱 dovvin (ORBIT, Thinking Space II 88%) - 14.18 [ID]').
С --waiting ещё два файла: waiting_records.json (формат jsonfmt.py) и waiting_new_players.json.
Кеш запросов: levelcache.json рядом со скриптом (его сохраняет и восстанавливает workflow).
"""
import argparse
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import levels as levelsearch
import lists_core as L
from jsonfmt import write_players

HERE = Path(__file__).resolve().parent
DL = "https://api.demonlist.org/"
PC = "https://pointercrate.com/api/"
RAW = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/"
OTHER = "other_lists"  # блок аккаунтов человека на других листах в links.json
A, B = 2, 4
N = 6                    # сколько лучших записей берём
CAP = 300.0              # стоимость записи строго меньше этого значения; недостающие добираются им
CAP_PLACE = 300.0        # проверяем уровни выше этого места (само место не берётся)
CONC = 20                # параллельных запросов
TTL = 3600               # секунд, пока кеш уровня свежий
WAITING_TTL = 3 * 3600   # секунд, пока список waiting-записей игрока в кеше свежий
WAITING_MAX_SCORE = 250.0  # в waiting-файлы попадают игроки, у которых ПОСЛЕ принятия всех waiting-записей очков меньше
PC_CONC = 20             # запросов к Pointercrate одновременно
LIST_CONC = 3            # страниц других листов качать одновременно
LIST_TTL = 600           # страницы уровней листов, скачанные недавно (в этом же запуске workflow), заново не качаем
SAVE_EVERY = 60          # как часто (секунд) сбрасывать кеш на диск во время работы
WAITING_STATUSES = {"waiting"}
SPECIAL_LEVEL_NAMES = levelsearch.SPECIAL_LEVEL_NAMES

LIST_SOURCES = [s.name for s in L.sources()]  # включённые листы, которые умеют качать данные сами
ALL_SOURCES = {"pointercrate", "my_extras", *LIST_SOURCES}

p = argparse.ArgumentParser()
p.add_argument("--src", required=True, help="pointercrate, " + ", ".join(LIST_SOURCES) + ", my_extras или all (через запятую)")
p.add_argument("--out", required=True, help="имя файла рейтинга рядом со скриптом")
p.add_argument("--waiting", action="store_true",
               help="записать waiting_records*.json и waiting_new_players.json: непринятые записи глобала (статус waiting), "
                    "которые вошли бы в топ-N игрока")
args = p.parse_args()

wanted = [s.strip() for s in args.src.split(",") if s.strip()]
SOURCES = set(ALL_SOURCES) if "all" in wanted else set(wanted)
SOURCES.discard("global")


def get_json(url, tries=8):
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


def load_paged(path, key, step, stop=None):
    """Грузит список постранично. stop(arr) -> True, когда дальше качать не нужно."""
    out, offset = [], 0
    while True:
        d = get_json(f"{DL}{path}?limit={step}&offset={offset}")
        arr = (d.get("data") or {}).get(key) or []
        if not arr:
            return out
        out += arr
        if stop and stop(arr):
            return out
        offset += step


def levels_past_cap(arr):
    """Страница отсортирована по месту и её последний уровень уже ниже порога - дальше не нужно."""
    pl = [float(l["placement"]) for l in arr]
    return pl == sorted(pl) and pl[-1] >= CAP_PLACE


# ---------- формула ----------
def score(h, c, T, t):
    if c >= 100:
        return h
    if not (t < T) or c < t or c <= 0:
        return None
    return h * A * (T / c) ** (math.log(B / A) / math.log(T / t))


def geo_mean(values):
    return math.exp(sum(math.log(x) for x in values) / len(values))


# ---------- файлы репозитория ----------
def load_repo_json(name):
    f = HERE / name
    try:
        if f.exists():
            return json.loads(f.read_text("utf-8"))
        return get_json(RAW + name)  # raw GitHub кешируется до ~5 минут после правки
    except Exception as e:
        print(f"{name}: {e}")
        return {}


def load_tp():
    """levels_tp.json берём ТОЛЬКО с GitHub. Без него T/t были бы запасными и рейтинг неверным, поэтому при сбое выходим."""
    try:
        levels = get_json(RAW + "levels_tp.json").get("levels", {})
    except Exception as e:
        raise SystemExit(f"levels_tp.json: не удалось загрузить с GitHub ({e}). Останавливаюсь.")
    if not levels:
        raise SystemExit("levels_tp.json: на GitHub пустой список уровней. Останавливаюсь.")
    return levels


is_global = L.is_global  # аккаунт глобала: ID начинается с цифры (pc7, wml5 и т.п. глобалом не считаются)


def link_sites(entry):
    """{"pointercrate": [...], "wml": [...], ...} из записи links.json (блок other_lists; старый формат тоже понимаем:
    списки прямо в записи или голый список - это Pointercrate)."""
    if not isinstance(entry, dict):
        return {"pointercrate": entry}
    sources = [{k: v for k, v in entry.items() if k not in ("_name", "channels", OTHER)}]
    if isinstance(entry.get(OTHER), dict):
        sources.append(entry[OTHER])
    sites = {}
    for src in sources:
        for site, ids in src.items():
            ids = ids if isinstance(ids, list) else [ids]
            sites.setdefault(site, []).extend(x for x in ids if x is not None)
    return sites


def load_links():
    """Возвращает:
    owner(site, acc_id) - ID аккаунта на листе -> главный ID человека (цифры, pc..., wml...) или None;
    canon(uid)          - любой ID человека -> главный ID (склейки fix.json + доп. аккаунты из links.json);
    removals            - видео, которые у игрока не учитываются;
    accounts(uid)       - все аккаунты человека: {"dl": [...], "pointercrate": [...], "wml": [...], ...}."""
    links = load_repo_json("links.json")
    fix = load_repo_json("fix.json")
    absorb = {}
    for m in fix.get("merges", []):
        for a in m.get("absorb", []):
            absorb[str(a["id"])] = str(m["keep"])

    def canon0(i):
        i, seen = str(i), set()
        while i in absorb and i not in seen:  # цепочки склеек A->B->C
            seen.add(i)
            i = absorb[i]
        return i

    names = L.list_names()
    site_map = {n: {} for n in names}  # лист -> {ID аккаунта на листе: главный ID человека}
    alias = {}

    def empty():
        return {"dl": [], **{n: [] for n in names}}

    people = {}  # главный ID -> {"dl": [аккаунты глобала, главный первым], "pointercrate": [...], "wml": [...]}

    def grp(main):
        return people.setdefault(main, empty())

    def add_uniq(lst, x):
        x = str(x)
        if x not in lst:
            lst.append(x)

    for main_id, entry in links.items():
        main = canon0(main_id)
        g = grp(main)
        if is_global(main):
            add_uniq(g["dl"], main)
        if is_global(main_id):
            add_uniq(g["dl"], main_id)
        for site, ids in link_sites(entry).items():
            for x in ids:
                if site in site_map:
                    site_map[site][str(x)] = main
                    add_uniq(g[site], x)
                elif any(s in str(site).lower() for s in ("demonlist", "global")):
                    alias[str(x)] = main  # доп. аккаунты того же человека на Demonlist
                    add_uniq(g["dl"], x)
    for main_id in links:
        alias.setdefault(str(main_id), canon0(main_id))
    for a in absorb:  # аккаунты, склеенные через fix.json, тоже аккаунты этого человека
        main = canon0(a)
        g = grp(main)
        add_uniq(g["dl"], main)
        add_uniq(g["dl"], a)

    def canon(uid):
        uid = str(uid)
        return canon0(alias.get(uid, uid))

    def accounts(uid):
        """Все известные аккаунты человека (главный аккаунт глобала первым). Если человека нет в links.json,
        ключи вида pc<id>, wml<id> и т.п. понимаем сами."""
        c = canon(uid)
        if c in people:
            return people[c]
        src, rest = L.key_parts(c)
        acc = empty()
        if src:
            acc[src.name] = [rest]
        else:
            acc["dl"] = [c]
        return acc

    def owner(site, acc_id):
        return site_map.get(site, {}).get(str(acc_id))

    # removals: ключ - ID глобала, videos - ID ютуб-видео, записи с ними у этого игрока не учитываются
    removals = {}
    for r in fix.get("removals", []):
        removals.setdefault(str(r["user"]), []).extend(str(v) for v in r.get("videos", []) if v)

    if not links:
        print("ВНИМАНИЕ: links.json пустой или не загрузился - склейки аккаунтов не будет!")
    elif not any(site_map.values()):
        print("ВНИМАНИЕ: в links.json не нашлось ни одного аккаунта на других листах - "
              "проверь формат файла (ожидается блок other_lists).")
    return owner, canon, removals, accounts


def is_removed(removals, uid, video):
    """True, если видео записи входит в removals именно этого игрока."""
    vids = removals.get(str(uid))
    return bool(vids and video and any(v in str(video) for v in vids))


def merge_accounts(best_by, info, canon):
    """Склеивает все ID одного человека в один: лучший процент по каждому уровню."""
    new_best, new_info = {}, {}
    for uid, m in best_by.items():
        cu = canon(uid)
        nb = new_best.setdefault(cu, {})
        for k, v in m.items():
            nb[k] = max(nb.get(k, -1), v)
        n = info.get(uid)
        if not n:
            continue
        if cu not in new_info or uid == cu:
            new_info[cu] = n
        elif n[1] and not new_info[cu][1]:
            new_info[cu] = (new_info[cu][0], n[1])
    for uid in new_best:
        new_info.setdefault(uid, ("#" + uid, None))
    return new_best, new_info


def load_extras():
    """my_extras.json -> {ID игрока: {ID уровня: процент}} (только если my_extras среди источников)."""
    merged = {}
    if "my_extras" not in SOURCES:
        return merged
    try:
        raw = get_json(RAW + "my_extras.json").get("users", {})
    except Exception as e:
        print(f"my_extras.json: {e}")
        return merged
    for uid, rows in raw.items():
        m = merged.setdefault(str(uid), {})
        for r in rows:
            if isinstance(r, list) and len(r) >= 2:
                m[str(r[0])] = max(m.get(str(r[0]), -1), float(r[1]))
    return merged


def load_my_extras_always(owner_of, canon):
    """my_extras.json с GitHub ВСЕГДА, независимо от --src. Нужен для waiting: запись не считается новой, если у игрока
    уже есть такая же или лучшая в экстрасах. Возвращает {главный ID человека: {ID уровня: процент}}."""
    try:
        raw = get_json(RAW + "my_extras.json").get("users", {})
    except Exception as e:
        print(f"my_extras.json (для waiting): {e}")
        return {}
    out = {}
    for uid, rows in raw.items():
        src, rest = L.key_parts(uid)
        if src and owner_of(src.name, rest):
            uid = owner_of(src.name, rest)
        m = out.setdefault(canon(uid), {})
        for r in rows:
            if isinstance(r, list) and len(r) >= 2:
                m[str(r[0])] = max(m.get(str(r[0]), -1), float(r[1]))
    return out


def load_board_records(owner_of, canon):
    """Записи, которые уже лежат на корборде: missing_achievements.json. Нужны для waiting: если запись (или лучшая)
    уже на корборде, в waiting её не пишем. Возвращает {главный ID человека: {ID уровня: процент}}."""
    raw = load_repo_json("missing_achievements.json").get("users", {})
    out = {}
    for uid, rows in raw.items():
        uid = str(uid).split(" — ")[0].strip()
        src, rest = L.key_parts(uid)
        if src and owner_of(src.name, rest):
            uid = owner_of(src.name, rest)
        m = out.setdefault(canon(uid), {})
        for r in rows:
            if isinstance(r, list) and len(r) >= 2:
                try:
                    m[str(r[0])] = max(m.get(str(r[0]), -1), float(r[1]))
                except (TypeError, ValueError):
                    pass
    return out


def apply_corrections(best_by, info, canon):
    """Правки из corrections.json (после склейки аккаунтов, при любом --src).
    Формат: {"delete": {"users": {ID: [[уровень, процент, ""], ...]}}, "add": {"users": {...}}}
    delete - убрать запись, если у игрока по этому уровню лучший процент совпадает.
    add    - ИСПРАВЛЕНИЕ завышенной записи: добавляется, только если у этого игрока по этому уровню запись реально
             была удалена через delete. Всё, что не сработало, печатается с причиной - так видно, где опечатка."""
    data = load_repo_json("corrections.json")
    if not data:
        print("corrections.json: пусто или не загрузился (см. строку с ошибкой выше) - правок НЕ будет")
        return

    def users_of(section):
        sec = data.get(section) or {}
        return sec.get("users", sec) if isinstance(sec, dict) else {}  # допускаем и {"users": {...}}, и сразу {ID: [...]}

    deleted_keys = set()  # пары (главный ID игрока, ID уровня), которые реально удалены
    for uid, rows in users_of("delete").items():
        cu = canon(uid)
        mine = best_by.get(cu)
        for r in rows:
            if not isinstance(r, list) or len(r) < 2:
                continue
            k = str(r[0])
            if (cu, k) in deleted_keys:
                continue  # этот рекорд уже удалён через другой аккаунт того же человека
            if mine is None:
                print(f"  delete {uid} (-> {cu}): игрока нет в топе")
            elif k not in mine:
                print(f"  delete {uid} (-> {cu}): уровня {k} у него нет")
            elif abs(mine[k] - float(r[1])) > 1e-9:
                print(f"  delete {uid} (-> {cu}) уровень {k}: у него {mine[k]:g}%, а в файле {float(r[1]):g}% - не совпало")
            else:
                del mine[k]
                deleted_keys.add((cu, k))
    for uid, rows in users_of("add").items():
        cu = canon(uid)
        for r in rows:
            if not isinstance(r, list) or len(r) < 2:
                continue
            k = str(r[0])
            if (cu, k) not in deleted_keys:
                print(f"  add {uid} (-> {cu}) уровень {k}: пропущено - у него не удалялась запись по этому уровню")
                continue
            mine = best_by.setdefault(cu, {})
            mine[k] = max(mine.get(k, -1), float(r[1]))
            info.setdefault(cu, ("#" + cu, None))


# ---------- кеш ----------
CACHE_FILE = HERE / "levelcache.json"
cache = json.loads(CACHE_FILE.read_text("utf-8")) if CACHE_FILE.exists() else {}
lock = threading.Lock()
waiting_raw = {}  # ID уровня -> [(ID аккаунта, процент, видео), ...] (записи waiting; заполняет load_waiting)
_last_save = [time.time()]


def save_cache():
    """Запись через временный файл; под блокировкой, чтобы потоки не мешали друг другу."""
    with lock:
        data = json.dumps(cache)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(data, "utf-8")
        os.replace(tmp, CACHE_FILE)
    _last_save[0] = time.time()


def level_records(lvl_id):
    def page(offset):
        d = get_json(f"{DL}level/classic/record/list?level_id={lvl_id}&limit=50&offset={offset}")["data"]
        return d.get("records") or [], int(d.get("total_count") or 0)

    first, total = page(0)
    c = cache.get(str(lvl_id))
    # len(x) >= 5: в старом кеше не было ссылки на видео - такие уровни перекачиваем
    if c and c["total"] == total and time.time() - c["at"] < TTL and all(len(x) >= 5 for x in c["recs"]):
        return c["recs"]
    records = list(first)
    while len(records) < total:
        more, _ = page(len(records))
        if not more:
            break
        records += more
    out = []
    for r in records:
        u = r.get("user") or {}
        if str(r.get("status") or "accepted").lower() != "accepted" or u.get("id") is None:
            continue
        pct = r.get("percent")
        out.append([str(u["id"]), u.get("username"), u.get("country"),
                    float(pct if pct is not None else 100), r.get("video_url")])
    with lock:
        cache[str(lvl_id)] = {"total": total, "at": time.time(), "recs": out}
    return out


def user_waiting(uid):
    """Записи игрока в статусе waiting: GET user/record/list?user_id=<id>. Возвращает [[ID уровня, процент, видео], ...].
    Кешируется на WAITING_TTL секунд."""
    key = "w" + str(uid)
    c = cache.get(key)
    if c and time.time() - c["at"] < WAITING_TTL:
        return c["wait"]
    wait, offset = [], 0
    while True:
        try:
            d = get_json(f"{DL}user/record/list?user_id={uid}&limit=50&offset={offset}").get("data") or {}
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):  # такого игрока на глобале нет
                break
            raise
        recs = d.get("records") or []
        for r in recs:
            lvl = (r.get("level") or {}).get("id")
            if str(r.get("status") or "accepted").lower() in WAITING_STATUSES and lvl is not None:
                pct = r.get("percent")
                wait.append([str(lvl), float(pct if pct is not None else 100), r.get("video_url")])
        offset += len(recs)
        if not recs or offset >= int(d.get("total_count") or 0):
            break
    with lock:
        cache[key] = {"at": time.time(), "wait": wait}
    return wait


def load_waiting(result, accounts_of):
    """Качает waiting-записи по каждому аккаунту глобала у игроков рейтинга и кладёт их в waiting_raw."""
    uids = sorted({str(a) for r in result for a in accounts_of(r["id"])["dl"]})
    print(f"Записи waiting: запрашиваю аккаунтов глобала {len(uids)}")
    for (uid,), wait in run_tasks([(u,) for u in uids], lambda it: user_waiting(it[0]), "игроков (waiting)"):
        for lvl, pct, video in wait or []:
            waiting_raw.setdefault(lvl, []).append((uid, pct, video))


def verifier_of(lvl):
    v = lvl.get("verification")
    if not v:
        key = "v" + str(lvl["id"])
        c = cache.get(key)
        if c and time.time() - c["at"] < 7 * 86400:
            v = c["v"]
        else:
            v = get_json(f"{DL}level/classic/get?id={lvl['id']}")["data"].get("verification")
            with lock:
                cache[key] = {"at": time.time(), "v": v}
    if v and v.get("user_id") is not None:
        return [str(v["user_id"]), v.get("username"), v.get("video_url")]
    return None


def clean_country(c):
    """'Unknown' и пустое значение на глобале означают 'страны нет'."""
    c = (c or "").strip()
    return None if c.lower() in ("", "unknown") else c


def dl_profile(uid):
    """Профиль игрока на глобале: (ник, страна) или None, если профиля нет. Кешируется на 30 дней."""
    key = "u" + str(uid)
    c = cache.get(key)
    if c and time.time() - c["at"] < 30 * 86400 and c.get("n"):
        return (c["n"], clean_country(c.get("c")))
    try:
        d = get_json(f"{DL}user/get?id={uid}").get("data") or {}
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):  # такого игрока на глобале нет
            return None
        raise
    name = d.get("username")
    if not name:
        return None
    country = d.get("country")
    with lock:
        cache[key] = {"at": time.time(), "n": name, "c": country}
    return (name, clean_country(country))


# ---------- Pointercrate ----------
def pc_demons():
    out, seen, after = [], set(), 0
    while True:
        arr = get_json(f"{PC}v2/demons/?limit=100&after={after}")
        if not isinstance(arr, list) or not arr:
            return out
        new = [d for d in arr if d["id"] not in seen]
        for d in new:
            seen.add(d["id"])
        out += new
        last = max(int(d["id"]) for d in arr)
        if not new or len(arr) < 100 or last <= after:
            return out
        after = last


def pc_records(demon_id):
    key = "pc" + str(demon_id)
    c = cache.get(key)
    if c and time.time() - c["at"] < TTL and all(len(x) >= 4 for x in c["recs"]):
        return c["recs"]
    out, after = [], 0
    while True:
        arr = get_json(f"{PC}v1/records/?demon_id={demon_id}&limit=100&after={after}")
        if not isinstance(arr, list) or not arr:
            break
        for r in arr:
            st = str(r.get("status") or "APPROVED").upper()
            pl = r.get("player") or {}
            if st == "APPROVED" and pl.get("id") is not None and r.get("progress") is not None:
                out.append([str(pl["id"]), pl.get("name"), float(r["progress"]), r.get("video")])
        last = max(int(r["id"]) for r in arr)
        if len(arr) < 100 or last <= after:
            break
        after = last
    with lock:
        cache[key] = {"at": time.time(), "recs": out}
    return out


pc_sem = threading.Semaphore(PC_CONC)  # не заваливаем Pointercrate запросами
pc_style = ["/"]  # какой вариант URL работает: со слэшем в конце или без


def pc_fetch(url, tries=4):
    """GET к Pointercrate: (HTTP-код, текст ответа). 429 и 5xx повторяем (с учётом Retry-After), остальные коды
    возвращаем как есть. Исключение - только если сеть не отвечала все попытки."""
    last = None
    for i in range(tries):
        try:
            with pc_sem:
                req = urllib.request.Request(url, headers={"User-Agent": "gl-ext-top6", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:  # сюда попадаем уже ВНЕ семафора
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if e.code == 429 or e.code >= 500:
                last = (e.code, body)
                try:
                    wait = float(e.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    wait = 3 * (i + 1)
                time.sleep(min(wait, 60))
                continue
            return e.code, body
        except Exception as e:
            last = e
            time.sleep(min(2 ** i, 10))
    if isinstance(last, tuple):
        return last
    raise last


def pc_player_country(pid):
    """Код страны (ISO-2) игрока Pointercrate или None. Кешируется на 30 дней.
    Если запрос не удался - бросает исключение (и ничего не кеширует)."""
    pid = str(pid)
    key = "pcn3" + pid  # новый ключ: старые pcn/pcn2 могли хранить "страны нет" от кривого ответа
    c = cache.get(key)
    if c and time.time() - c["at"] < 30 * 86400:
        return c["c"]

    variants = [f"{PC}v1/players/{pid}/", f"{PC}v1/players/{pid}"]
    if pc_style[0] == "":
        variants.reverse()
    status, body, errs = None, "", []
    for u in variants:
        try:
            st, bd = pc_fetch(u)
        except Exception as e:
            errs.append(f"{u} -> сеть: {str(e)[:60]}")
            status = None
            continue
        errs.append(f"{u} -> HTTP {st}")
        status, body = st, bd
        if st == 200:
            pc_style[0] = "/" if u.endswith("/") else ""
            break
    if status != 200:
        raise RuntimeError("; ".join(errs))

    try:
        d = json.loads(body)
    except ValueError:
        raise RuntimeError("ответ не JSON")
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        d = d["data"]
    if not isinstance(d, dict) or "id" not in d:
        return None  # структура не та - НЕ кешируем, чтобы не запомнить ложное "страны нет"
    nat = d.get("nationality")
    code = nat.get("country_code") if isinstance(nat, dict) else None
    with lock:
        cache[key] = {"at": time.time(), "c": code}
    return code


def run_tasks(items, fn, label, workers=None):
    """Выполняет fn для каждого элемента в потоках. Возвращает [(элемент, результат или None)].
    Упавшие задачи повторяются по одной (до 3 кругов с паузой); кеш сохраняется при завершении и при прерывании."""
    total = len(items)
    state = {"done": 0}
    errors = []
    failed = set()  # id() задач, которые не удались
    step = max(1, total // 10)  # прогресс печатаем примерно каждые 10%

    def item_name(item):
        return str(item[0] if isinstance(item, tuple) else item.get("name", item))

    def wrap(item):
        res = None
        try:
            res = fn(item)
        except Exception as e:
            with lock:
                errors.append((item_name(item), str(e)[:80]))
                failed.add(id(item))
        with lock:
            state["done"] += 1
            d = state["done"]
        if d % step == 0:
            print(f"  {label} {d}/{total}  ошибок: {len(errors)}", flush=True)
        if time.time() - _last_save[0] >= SAVE_EVERY:
            save_cache()
        return item, res

    ex = ThreadPoolExecutor(workers or CONC)
    try:
        results = list(ex.map(wrap, items))
        ex.shutdown()
    except KeyboardInterrupt:
        print("\nОстановлено. Сохраняю кеш...")
        ex.shutdown(wait=False, cancel_futures=True)
        save_cache()
        raise SystemExit
    finally:
        save_cache()

    for rnd in range(1, 4):  # автоповтор упавших задач (обрывы SSL, 5xx и т.п.)
        bad = [i for i, (it, _) in enumerate(results) if id(it) in failed]
        if not bad:
            break
        print(f"  Повтор {rnd}/3: неудачных {len(bad)}, пауза 5с...")
        time.sleep(5)
        errors.clear()
        for i in bad:
            it = results[i][0]
            try:
                results[i] = (it, fn(it))
                failed.discard(id(it))
            except Exception as e:
                errors.append((item_name(it), str(e)[:80]))
        save_cache()

    if errors:
        print(f"  Не скачались {len(errors)} (запусти ещё раз, докачает только их):")
        for name, msg in errors[:10]:
            print(f"    {name}: {msg}")
    return results


# ---------- вывод ----------
COUNTRY = {
    "unitedstates": "US", "usa": "US", "newzealand": "NZ", "russia": "RU", "armenia": "AM", "finland": "FI",
    "spain": "ES", "southkorea": "KR", "kazakhstan": "KZ", "brazil": "BR", "poland": "PL", "portugal": "PT",
    "moldova": "MD", "canada": "CA", "germany": "DE", "peru": "PE", "chile": "CL", "unitedkingdom": "GB",
    "australia": "AU", "japan": "JP", "romania": "RO", "norway": "NO", "denmark": "DK", "netherlands": "NL",
    "austria": "AT", "ukraine": "UA", "israel": "IL", "estonia": "EE", "uzbekistan": "UZ", "serbia": "RS",
    "france": "FR", "italy": "IT", "belgium": "BE", "china": "CN", "colombia": "CO", "argentina": "AR",
    "philippines": "PH", "panama": "PA", "albania": "AL", "czechrepublic": "CZ", "czechia": "CZ",
    "lithuania": "LT", "sweden": "SE", "hungary": "HU", "belarus": "BY", "mexico": "MX", "india": "IN",
    "venezuela": "VE", "pakistan": "PK", "greece": "GR", "ireland": "IE", "unitedarabemirates": "AE",
    "uae": "AE", "indonesia": "ID", "georgia": "GE", "nigeria": "NG", "paraguay": "PY", "iceland": "IS",
    "costarica": "CR", "slovakia": "SK", "uruguay": "UY", "turkey": "TR", "bosniaandherzegovina": "BA",
    "kosovo": "XK", "laos": "LA", "southafrica": "ZA", "latvia": "LV", "bulgaria": "BG", "ecuador": "EC",
    "vietnam": "VN", "cambodia": "KH", "iran": "IR", "dominicanrepublic": "DO", "malta": "MT",
    "palestine": "PS", "turkmenistan": "TM", "algeria": "DZ", "cuba": "CU", "egypt": "EG", "taiwan": "TW",
    "azerbaijan": "AZ", "tunisia": "TN", "kyrgyzstan": "KG", "croatia": "HR", "morocco": "MA",
    "bolivia": "BO", "switzerland": "CH", "hongkong": "HK", "slovenia": "SI", "iraq": "IQ", "thailand": "TH",
    "nicaragua": "NI", "jamaica": "JM", "luxembourg": "LU", "guatemala": "GT", "ghana": "GH",
    "puertorico": "PR", "mongolia": "MN", "montenegro": "ME", "elsalvador": "SV", "syria": "SY",
    "somalia": "SO", "greenland": "GL", "saudiarabia": "SA", "malaysia": "MY", "afghanistan": "AF",
    "northmacedonia": "MK", "macedonia": "MK", "bahrain": "BH", "maldives": "MV", "libya": "LY",
    "senegal": "SN", "capeverde": "CV", "cyprus": "CY", "honduras": "HN", "singapore": "SG", "yemen": "YE",
    "lebanon": "LB", "oman": "OM", "kenya": "KE", "kuwait": "KW", "jordan": "JO", "ethiopia": "ET",
    "myanmar": "MM", "bangladesh": "BD", "qatar": "QA", "tajikistan": "TJ", "bahamas": "BS", "mali": "ML",
    "togo": "TG", "zambia": "ZM", "gabon": "GA", "andorra": "AD", "guam": "GU", "haiti": "HT",
    "england": "GB", "scotland": "GB", "wales": "GB", "canaryislands": "ES",
}


def flag(country):
    """Принимает либо название страны (Demonlist), либо готовый ISO-код из 2 букв (Pointercrate)."""
    raw = str(country or "").strip()
    if len(raw) == 2 and raw.isalpha():
        code = raw.upper()
    else:
        code = COUNTRY.get("".join(ch for ch in raw.lower() if ch.isalpha()))
    if not code or len(code) != 2:
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code)


def line_short(i, r):
    """5) 🇵🇱 dovvin (ORBIT, Thinking Space II 88%, ...) - 14.18 [id]"""
    parts = []
    for rec in r["records"]:
        if rec["place"] is None:
            continue
        parts.append(rec["level"] + ("" if rec["percent"] >= 100 else f" {rec['percent']:g}%"))
    return f"{i}) {flag(r['country'])} {r['name']} ({', '.join(parts)}) - {r['score']:.2f} [{r['id']}]"


def save_waiting(meta, best_by, info, result, canon_id, removals, owner_of):
    """waiting_records.json: для каждого человека с аккаунтом глобала (аккаунты из links.json и fix.json считаются его)
    все непринятые записи (статус waiting), которые, будь они приняты, попали бы в его топ-N лучших записей.
    Запись подходит, если она лучше принятой у человека на этом уровне (или принятой нет) и остаётся в его топ-N после
    добавления ВСЕХ его waiting-записей сразу. Записи группируются по аккаунту глобала, с которого они поданы.
    Сравнение идёт со ВСЕМИ записями человека: глобал, Pointercrate, другие листы, my_extras и то, что уже на корборде.
    Файлы: waiting_records.json (ID человека -> ник и записи [название уровня, процент, ссылка]) и
    waiting_new_players.json (игроки, у которых меньше порога очков ТОЛЬКО с waiting-записями)."""

    def top_of(best):
        recs = []
        for k, pct in best.items():
            lv = meta.get(k)
            if not lv:
                continue
            f = score(lv["place"], pct, lv["T"], lv["t"])
            if f is not None and f < CAP:  # ровно cap (например, уровень на месте 300 со 100%) не учитывается
                recs.append((f, k.startswith("x"), k, pct))
        recs.sort(key=lambda r: (r[0], r[1]))
        return recs[:N]

    def person_score(best):
        """Очки человека по набору его лучших записей: как в result (топ-N, недостающие добираются cap)."""
        fs = [r[0] for r in top_of(best)]
        fs += [CAP] * (N - len(fs))
        return geo_mean(fs)

    by_person = {}  # главный ID человека -> {ID аккаунта глобала: [(ID уровня, процент, видео)]}
    for lvl_id, recs in waiting_raw.items():
        if lvl_id not in meta:
            continue
        for uid, pct, video in recs:
            if is_removed(removals, uid, video):
                continue
            by_person.setdefault(canon_id(uid), {}).setdefault(uid, []).append((lvl_id, pct, video))

    extras_all = load_my_extras_always(owner_of, canon_id)
    for person_, recs_ in load_board_records(owner_of, canon_id).items():  # то, что на корборде, тоже считается имеющимся
        m_ = extras_all.setdefault(person_, {})
        for k_, pct_ in recs_.items():
            m_[k_] = max(m_.get(k_, -1), pct_)
    ranks = {r["id"]: r["rank"] for r in result}

    def fmt_pct(x):
        return int(x) if x == int(x) else x

    def pick_best(items):
        """Записи одного человека (с разных его аккаунтов) -> по одной на уровень: больший процент, при равном - с видео."""
        best = {}
        for lvl_id, pct, video in items:
            link = video if isinstance(video, str) and video.startswith("http") else ""
            cur = best.get(lvl_id)
            if cur is None or (pct, bool(link)) > (cur[0], bool(cur[1])):
                best[lvl_id] = (pct, link)
        return best

    people = {}        # очков меньше порога и БЕЗ waiting -> waiting_records*.json
    only_waiting = {}  # меньше порога только С waiting (без него >= порога) -> waiting_new_players.json
    for person, accs in by_person.items():
        base = dict(best_by.get(person, {}))
        for k, pct in extras_all.get(person, {}).items():  # лучшее из всего, включая my_extras
            base[k] = max(base.get(k, -1), pct)
        new = []
        for items in accs.values():
            for lvl_id, pct, video in items:
                best = base.get(lvl_id)
                if best is not None and pct <= best:  # равна или меньше того, что уже есть: ничего не добавляет
                    continue
                if any(t[2] == lvl_id and t[3] == pct for t in top_of({**base, lvl_id: pct})):  # попадает в топ-N
                    new.append((lvl_id, pct, video))
        if not new:
            continue
        # Очки, которые получатся, если принять ВСЕ waiting-записи сразу (лучшая запись на уровень)
        final_best = dict(base)
        for lvl_id, (pct, _link) in pick_best(new).items():
            final_best[lvl_id] = max(final_best.get(lvl_id, -1), pct)
        # Каждая запись проверялась ОТДЕЛЬНО. Если принять все сразу, часть вытеснят друг друга: оставляем
        # только те, что реально войдут в итоговый топ-N.
        final_top = {(t[2], t[3]) for t in top_of(final_best)}  # (ID уровня, процент)
        new = [x for x in new if (x[0], x[1]) in final_top]
        if not new:
            continue
        final_score = person_score(final_best)
        if final_score >= WAITING_MAX_SCORE:
            continue
        entry = {"nick": (info.get(person) or (None, None))[0] or person, "rank": ranks.get(person), "new": new,
                 "final_best": final_best, "final_score": final_score}
        if person_score(base) < WAITING_MAX_SCORE:
            people[person] = entry
        else:
            only_waiting[person] = entry

    order = sorted(people, key=lambda u: (people[u]["rank"] is None, people[u]["rank"] or 0, u))

    # Записи внутри игрока по названию уровня; ссылка есть у записей, где в waiting было видео
    out = {}
    for u in order:
        best = pick_best(people[u]["new"])
        if not best:
            continue
        rows = sorted(best.items(), key=lambda kv: str(meta[kv[0]]["name"] or "").casefold())
        out[u] = (people[u]["nick"], [[meta[lvl]["name"], fmt_pct(pct), link] for lvl, (pct, link) in rows])
    write_players(HERE / "waiting_records.json", out)

    # waiting_new_players.json: формат как у missing_players.json, но ключ пока наш ID: ID CoreBoard и уникальные
    # ники выдаёт cbd-update.py. Записи: итоговый топ-N игрока, сначала самые сложные; ссылка есть у waiting-записей.
    new_players = {}
    for person in sorted(only_waiting, key=lambda u: (only_waiting[u]["final_score"], u)):
        e = only_waiting[person]
        links = pick_best(e["new"])  # уровень -> (процент, ссылка)
        records = []
        for _f, _special, k, pct in top_of(e["final_best"]):
            w = links.get(k)
            records.append([meta[k]["name"], fmt_pct(pct), w[1] if w and w[0] == pct else ""])
        new_players[person] = {"nick": e["nick"], "country_code": (info.get(person) or (None, None))[1] or None,
                               "gdr_id": person, "records": records}
    lines = ['{', '  "users": {']
    items = list(new_players.items())
    for i, (key, val) in enumerate(items):
        lines.append(f"    {json.dumps(key, ensure_ascii=False)}: {json.dumps(val, ensure_ascii=False)}"
                     + ("," if i < len(items) - 1 else ""))
    lines += ['  }', '}', '']
    (HERE / "waiting_new_players.json").write_text("\n".join(lines), "utf-8")
    print(f"waiting: игроков {len(people)}, только с waiting (новые игроки) {len(new_players)}")


def main():
    print("Источники: global" + "".join(" + " + s for s in sorted(SOURCES)))
    owner_of, canon_id, removals, accounts_of = load_links()
    # качаем только до CAP_PLACE (топ-300), остальные уровни нигде не используются
    levels = load_paged("level/classic/list", "levels", 100, stop=levels_past_cap)
    extras = load_extras()
    tp = load_tp()

    meta, fallback = {}, 0
    for l in levels:
        k = str(l["id"])
        e = tp.get(k) or {}
        if e.get("T") and e.get("t"):
            T, t = float(e["T"]), float(e["t"])
        else:
            T, t = 100.0, float(l["list_percent"])
            fallback += 1
        # Особое название (если есть) берётся вместо названия из API
        meta[k] = {"name": SPECIAL_LEVEL_NAMES.get(k, l["name"]), "place": float(l["placement"]), "T": T, "t": t}
    for k, e in tp.items():
        if k.startswith("x") and e.get("T") and e.get("t"):
            meta[k] = {"name": e["name"], "place": float(e["place"]), "T": float(e["T"]), "t": float(e["t"])}
    if fallback:
        print(f"Уровней без T/t в levels_tp.json (запасные T=100, list_percent): {fallback}")

    top_levels = sorted((l for l in levels if float(l["placement"]) < CAP_PLACE), key=lambda l: float(l["placement"]))

    def fetch_dl(l):
        recs = level_records(l["id"])
        ver = verifier_of(l)
        if ver:
            recs = [r for r in recs if r[0] != ver[0]] + [[ver[0], ver[1], None, 100.0, ver[2]]]
        return recs

    print(f"Demonlist: уровней {len(top_levels)}")
    best_by, info = {}, {}
    dl_seen = set()  # игроки, чей ник и страна уже пришли из записей глобала
    for l, recs in run_tasks(top_levels, fetch_dl, "уровней"):
        k = str(l["id"])
        for uid, name, country, pct, video in recs or []:
            country = clean_country(country)
            if is_removed(removals, uid, video):
                continue
            m = best_by.setdefault(uid, {})
            m[k] = max(m.get(k, -1), pct)
            if country:
                dl_seen.add(uid)
            if uid not in info or country:
                info[uid] = (name, country)

    if "pointercrate" in SOURCES:
        by_ingame = {str(l["ingame_id"]): l for l in top_levels}
        try:
            demons = pc_demons()
        except Exception as e:
            print("Не удалось загрузить демонов Pointercrate:", e)
            demons = []
        targets = [(d, by_ingame[str(d["level_id"])]) for d in demons
                   if d.get("level_id") is not None and str(d["level_id"]) in by_ingame]
        print(f"Pointercrate: демонов, найденных в глобале (по игровому ID): {len(targets)}")
        for (_, d, l), recs in run_tasks([(f"{d['name']}", d, l) for d, l in targets],
                                         lambda it: pc_records(it[1]["id"]), "демонов"):
            k = str(l["id"])
            for pid, pname, pct, video in recs or []:
                main_id = owner_of("pointercrate", pid)
                if main_id and is_removed(removals, main_id, video):
                    continue
                uid = main_id or "pc" + pid  # человек из links.json (цифры, pc..., wml...) или игрок без линка
                if is_global(uid):
                    info.setdefault(uid, (pname, None))
                else:
                    info.setdefault(uid, (pname or "?", None))
                m = best_by.setdefault(uid, {})
                m[k] = max(m.get(k, -1), pct)

    # Другие листы из реестра (wml и т.д.): данные качает адаптер листа, уровни ищет levels.py
    list_countries = {}  # лист -> {ID аккаунта: страна}
    wanted_lists = [src for src in L.sources() if src.name in SOURCES]
    if wanted_lists:
        names = levelsearch.name_index(tp, levels)  # нормализованное название уровня -> ключ уровня
        by_ingame_id = {str(l["ingame_id"]): str(l["id"]) for l in levels if l.get("ingame_id") is not None}
    for src in wanted_lists:
        print(f"{src.name}:")
        src.ttl = LIST_TTL
        data = src.fetch(L.DictStore(cache, lock, save_cache), workers=LIST_CONC)
        if not data:
            print(f"Лист {src.name} недоступен, источник в этом запуске пропускаю")
            continue
        countries = list_countries.setdefault(src.name, {})
        for aid, pl in data["players"].items():
            main_id = owner_of(src.name, aid)
            uid = main_id or src.prefix + aid  # человек из links.json или игрок листа без линка
            nick = src.display_nick(pl["n"]) or pl["n"]
            countries[aid] = pl.get("c")
            if main_id:
                info.setdefault(uid, (nick, None))
            else:
                info[uid] = (nick, pl.get("c"))  # метку [тег] добавим при выводе
        found = {}  # (название, ID уровня листа) -> ключ уровня или None; одно название может быть у разных уровней
        for r in data["records"]:
            aid = str(r["a"])
            main_id = owner_of(src.name, aid)
            uid = main_id or src.prefix + aid
            iid = r.get("i")
            ck = (r["l"], str(iid) if iid is not None else None)
            if ck not in found:
                k = names.get(levelsearch.normalize(r["l"]))
                if iid is not None:
                    # Сравнение по ID: ingame_id уровня листа есть среди уровней глобала -> это он, название не важно.
                    # Иначе название годится, только если оно ведёт на уровень вне глобала (ключ x...); если ведёт
                    # на уровень глобала (цифры), это ДРУГОЙ уровень с тем же названием.
                    by_id = by_ingame_id.get(str(iid))
                    k = by_id if by_id is not None else (None if k is None or k.isdigit() else k)
                found[ck] = k
            k = found[ck]
            if k is None:
                continue
            if main_id and is_removed(removals, main_id, r.get("v")):
                continue
            m = best_by.setdefault(uid, {})
            m[k] = max(m.get(k, -1), float(r["p"]))

    for uid, m in extras.items():
        # my_extras мог записать игрока как "pc<ID Pointercrate>", "wml<ID WML>" и т.п., когда линка ещё не было.
        # Если линк в links.json с тех пор появился, переводим на его главный ID.
        src, rest = L.key_parts(uid)
        if src and owner_of(src.name, rest):
            uid = owner_of(src.name, rest)
        b = best_by.setdefault(uid, {})
        for k, pct in m.items():
            b[k] = max(b.get(k, -1), pct)
        info.setdefault(uid, ("#" + uid, None))

    # Склеиваем аккаунты одного человека (links.json + fix.json) после сбора всех источников
    best_by, info = merge_accounts(best_by, info, canon_id)
    apply_corrections(best_by, info, canon_id)  # правки из corrections.json: всегда, при любом источнике

    # Вся информация об игроке (ник, страна) - с глобала, если он там есть. Из Pointercrate остаются только те, кого на глобале нет.
    need = [u for u in best_by if is_global(u) and u not in dl_seen]
    if need:
        got = 0
        for (u,), prof in run_tasks([(u,) for u in need], lambda it: dl_profile(it[0]), "профилей"):
            if prof:
                info[u] = prof
                got += 1
        print(f"Профиль на глобале найден у {got} из {len(need)}")

    # Если у главного аккаунта страны нет - ищем её у остальных аккаунтов человека: сначала по всем аккаунтам
    # Demonlist (главный первым), затем Pointercrate, затем по данным других листов. Белый флаг остаётся, только если страны нет нигде.
    def find_country(uid):
        acc = accounts_of(uid)
        for d in acc["dl"]:
            if not is_global(d):
                continue
            try:
                prof = dl_profile(d)
            except Exception:
                continue
            if prof and clean_country(prof[1]):
                return prof[1]
        for pid in acc["pointercrate"]:
            try:
                code = pc_player_country(pid)
            except Exception:
                continue
            if code:
                return code
        for lname, countries in list_countries.items():  # данные других листов уже скачаны выше
            for aid in acc.get(lname, []):
                if countries.get(str(aid)):
                    return countries[str(aid)]
        return None

    no_flag = [u for u in best_by if flag((info.get(u) or ("", None))[1]) == "🏳️"]
    if no_flag:
        got = 0
        for (u,), code in run_tasks([(u,) for u in no_flag], lambda it: find_country(it[0]), "игроков (страна)"):
            if code:
                info[u] = (info[u][0], code)
                got += 1
        print(f"Страна найдена у {got} из {len(no_flag)}")

    result = []
    for uid, best in best_by.items():
        recs = []
        for k, pct in best.items():
            lv = meta.get(k)
            if not lv:
                continue
            f = score(lv["place"], pct, lv["T"], lv["t"])
            if f is not None and f < CAP:  # ровно cap не учитывается
                recs.append({"level": lv["name"], "place": lv["place"], "percent": pct, "f": f, "special": k.startswith("x")})
        if not recs:
            continue
        recs.sort(key=lambda r: (r["f"], r["special"]))
        top = recs[:N]
        while len(top) < N:
            top.append({"level": "-", "place": None, "percent": None, "f": CAP, "special": False})
        name, country = info[uid]
        name = f"{name}{L.tag_of(uid)}"  # человека нет в глобале: помечаем, откуда он ([PC], [WML], ...)
        result.append({"id": uid, "name": name, "country": country, "score": geo_mean([r["f"] for r in top]), "records": top})

    result.sort(key=lambda r: r["score"])
    for i, r in enumerate(result, 1):
        r["rank"] = i  # место во всём рейтинге

    if args.waiting:
        load_waiting(result, accounts_of)
        save_waiting(meta, best_by, info, result, canon_id, removals, owner_of)

    (HERE / args.out).write_text("\n".join(line_short(r["rank"], r) for r in result), "utf-8")
    print(f"Рейтинг: игроков {len(result)} -> {args.out}")


main()
