"""Лист CoreBoard (coreboard.pythonanywhere.com): два борда, main (классический) и mobile.

Видео берём прямо со страницы борда /<борд>/board: в каждой строке игрок (ссылка /<борд>/board/player/<ID>),
страна и все его рекорды ссылками на видео. Если у игрока на борде рекордов не видно, открываем его профиль
/<борд>/board/player/<ID> (там тот же список в блоке Records).

ID аккаунта в links.json пишется строкой "<борд>:<ID>" ("main:1", "mobile:7"): у main и mobile ID свои.
Отдельных записей у CoreBoard нет: игрок, которого не удалось состыковать, это ошибка (links_coreboard_errors.txt).
Видео CoreBoard нужны только для привязки и каналы людям не добавляют (на CoreBoard ссылки бывают чужие).
"""
import html
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

from lists_core import Source, register

BASE = "https://coreboard.pythonanywhere.com"
BOARDS = ("main", "mobile")  # какие борды качаем; у каждого свои ID игроков
UA = {"User-Agent": "Mozilla/5.0 (compatible; gd-rating make-links)"}
# ВРЕМЕННО: борды, которые берём не с сайта, а из файла в репозитории (cbd-videos). Откат этого обновления вернёт сайт.
# Формат файла: {"<ID>": ["ник", ["уровень", процент, "ссылка на видео"], ...]}. Борд без записи здесь качается с сайта.
LOCAL_FILES = {"main": "cbd/cbd_videos.json"}
MAX_PAGES = 200  # на случай, если борд поделён на страницы (?page=N)


def http_get(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _split_record(text):
    """'Heliopolis 85%' -> ('Heliopolis', 85); без процента -> 100."""
    t = " ".join(html.unescape(text).split())
    m = re.search(r"\s(\d{1,3})%$", t)
    return (t[:m.start()], int(m.group(1))) if m else (t, 100)


class _Page(HTMLParser):
    """Разбирает и борд (строки <tr>), и профиль (блок <dd> после <dt>Records</dt>)."""

    def __init__(self, board):
        super().__init__(convert_charrefs=True)
        self.board = board
        self.rows, self.pages = [], set()
        self.cur = None      # строка борда, которую разбираем
        self.td = 0          # номер ячейки в строке
        self.in_player = False
        self.href = None     # ссылка <a>, внутри которой мы сейчас
        self.text = ""
        self.in_dt = False
        self.dt = ""
        self.want_dd = False
        self.in_rec = False  # внутри <dd> с рекордами (профиль)
        self.profile = []    # [(текст, ссылка)]
        self.country_buf = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href") and re.search(r"[?&]page=\d+", a["href"]) and "player" not in a["href"]:
            self.pages.add(a["href"])
        if tag == "tr" and self.board:
            self.cur, self.td = {"id": None, "nick": "", "cc": "", "recs": []}, 0
        elif tag == "td" and self.cur is not None:
            self.td += 1
            self.country_buf = ""
        elif tag == "a":
            self.href, self.text = a.get("href"), ""
            m = re.search(r"/player/(\d+)", self.href or "")
            if self.cur is not None and m and "player" in (a.get("class") or ""):
                self.cur["id"], self.in_player = m.group(1), True
        elif tag == "dt":
            self.in_dt, self.dt = True, ""
        elif tag == "dd" and self.want_dd:
            self.in_rec, self.want_dd = True, False

    def handle_endtag(self, tag):
        if tag == "a":
            if self.in_player:
                self.cur["nick"] = " ".join(self.text.split())
                self.in_player = False
            elif self.cur is not None and self.td >= 4 and self.text.strip():
                self.cur["recs"].append((self.text, self.href))
            elif self.in_rec and self.text.strip():
                self.profile.append((self.text, self.href))
            self.href = None
        elif tag == "td" and self.cur is not None and self.td == 3:
            self.cur["cc"] = self.country_buf
        elif tag == "tr" and self.cur is not None:
            if self.cur["id"]:
                self.rows.append(self.cur)
            self.cur = None
        elif tag == "dt":
            self.in_dt = False
            self.want_dd = self.dt.strip().lower() == "records"
        elif tag == "dd":
            self.in_rec = False

    def handle_data(self, data):
        if self.href is not None:
            self.text += data
        if self.in_dt:
            self.dt += data
        if self.cur is not None and self.td == 3 and self.href is None:
            self.country_buf += data


def _country(s):
    """'🇺🇸United States' -> 'United States' (флаг-эмодзи отбрасываем)."""
    return re.sub(r"^[\U0001F1E6-\U0001F1FF\s]+", "", s or "").strip() or None


def parse_board(text):
    p = _Page(True)
    p.feed(text)
    return p.rows, p.pages


def parse_profile(text):
    p = _Page(False)
    p.feed(text)
    return p.profile


def _records(pairs):
    out = []
    for txt, href in pairs:
        name, pct = _split_record(txt)
        out.append((name, pct, (href or "").strip() or None))
    return out


class CoreBoard(Source):
    name = "coreboard"
    prefix = "cb"     # игрок без пары: ключ cb<борд>:<ID>, например cbmain:1
    tag = "CB"
    short = "cb"
    fetchable = True
    strict = True     # свои записи не создаёт, видео не участвуют в поиске каналов, ошибки привязки идут в links_coreboard_errors.txt

    pause = 0.3
    max_profiles = 300  # сколько профилей максимум открывать для игроков без рекордов в строке борда

    def _local(self, mode, path):
        """Борд из файла репозитория: [(id, ник, страна, [(уровень, процент, видео)])]."""
        import json
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        for p in (path, os.path.join(here, "..", path), os.path.join(here, path)):
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    raw = json.load(f)
                break
        else:
            raise FileNotFoundError(path)
        out = []
        for pid, items in raw.items():
            nick = next((x for x in items if isinstance(x, str)), "")
            recs = []
            for x in items:
                if isinstance(x, list) and x:
                    pct = x[1] if len(x) > 1 and isinstance(x[1], (int, float)) else 100
                    url = x[2] if len(x) > 2 and isinstance(x[2], str) and x[2].strip() else None
                    recs.append((str(x[0]), int(pct), url))
            out.append((str(pid), nick, None, recs))
        out.sort(key=lambda r: (not r[0].isdigit(), int(r[0]) if r[0].isdigit() else 0, r[0]))
        return out

    def _board(self, mode, log):
        """[(id, ник, страна, [(уровень, процент, видео)])] всего борда или None."""
        rows, seen, queue = [], set(), [f"{BASE}/{mode}/board"]
        while queue and len(seen) < MAX_PAGES:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            text = http_get(url)
            got, pages = parse_board(text)
            rows += got
            time.sleep(self.pause)
            for href in sorted(pages):
                full = href if href.startswith("http") else BASE + (href if href.startswith("/") else f"/{mode}/board{href}")
                if f"/{mode}/" in full and full not in seen:
                    queue.append(full)
        out, ids = [], set()
        for r in rows:
            if r["id"] in ids:
                continue
            ids.add(r["id"])
            out.append((r["id"], r["nick"], _country(r["cc"]), _records(r["recs"])))
        return out

    def fetch(self, store, workers=3, log=print):
        players, records = {}, []
        for mode in BOARDS:
            old = store.get(mode)
            if mode in LOCAL_FILES:  # временно: данные из файла репозитория, сайт не трогаем
                try:
                    rows = self._local(mode, LOCAL_FILES[mode])
                except Exception as e:
                    log(f"  {self.name}/{mode}: файл {LOCAL_FILES[mode]} не прочитался ({e}), борд пропускаю")
                    continue
                log(f"  {self.name}/{mode}: из файла {LOCAL_FILES[mode]}")
                for rank, (pid, nick, cc, recs) in enumerate(rows, 1):
                    acc = f"{mode}:{pid}"
                    players[acc] = {"n": nick, "r": None, "c": cc}
                    for lvl, pct, video in recs:
                        records.append({"a": acc, "l": lvl, "p": pct, "v": video, "i": None, "pos": None})
                log(f"  {self.name}/{mode}: игроков {len(rows)}")
                continue
            try:
                rows = self._board(mode, log)
                if not rows:
                    raise RuntimeError("на странице борда нет игроков")
                # профили для строк без рекордов
                empty = [r for r in rows if not r[3]][:self.max_profiles]
                if empty:
                    def prof(r):
                        try:
                            return r[0], _records(parse_profile(http_get(f"{BASE}/{mode}/board/player/{r[0]}")))
                        except Exception:
                            return r[0], []
                        finally:
                            time.sleep(self.pause)
                    with ThreadPoolExecutor(max(1, workers)) as ex:
                        filled = dict(ex.map(prof, empty))
                    rows = [(i, n, c, recs or filled.get(i, [])) for i, n, c, recs in rows]
                store.put(mode, {"at": time.time(), "rows": rows})
                store.save()
            except Exception as e:
                if old and old.get("rows"):
                    log(f"  {self.name}/{mode}: борд не скачался ({e}), беру прошлую копию")
                    rows = old["rows"]
                else:
                    log(f"  {self.name}/{mode}: борд не скачался ({e}), его пропускаю")
                    continue
            for rank, (pid, nick, cc, recs) in enumerate(rows, 1):
                acc = f"{mode}:{pid}"
                players[acc] = {"n": nick, "r": rank, "c": cc}
                for lvl, pct, video in recs:
                    records.append({"a": acc, "l": lvl, "p": pct, "v": video, "i": None, "pos": None})
            log(f"  {self.name}/{mode}: игроков {len(rows)}")
        if not records:
            log(f"  {self.name}: записей нет, лист пропускаю")
            return None
        log(f"  {self.name}: игроков {len(players)}, записей {len(records)}")
        return {"players": players, "records": records}


register(CoreBoard())
