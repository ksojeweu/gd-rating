#!/usr/bin/env python3
"""Скачивает оба топа CoreBoard (основной /main/board и Топ РК /main/board/rc), объединяет игроков и пишет:
  cbd.json      полный разбор;
  cbd.txt       строки в формате gd-rating, их читает compare.py: '5) 🇵🇱 dovvin (ORBIT, Thinking Space II 88%) - 14.41';
  cbd_ids.json  ник -> ID игрока (только если в ссылках на игроков уже стоят числовые ID; иначе файл удаляется).
Скачанные страницы лежат рядом (cbd_w.html, cbd_rc.html) и считаются свежими час; -r скачивает заново.

  python coreboard-parse.py [-r]
"""
import argparse
import gzip
import json
import re
import ssl
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://coreboard.pythonanywhere.com/"
HERE = Path(__file__).resolve().parent
TTL = 3600
PAGES = (("world", "main/board", "w"), ("rc", "main/board/rc", "rc"))  # (топ, адрес, часть имени файла)

ap = argparse.ArgumentParser()
ap.add_argument("-r", "--refresh", action="store_true", help="скачать заново, даже если файл свежий")
args = ap.parse_args()

NO_LEVEL = "No"  # пустой слот рекорда (только точно так; всё остальное, даже UNKNOWN, это название уровня)
PCT = re.compile(r"^(.*?)(?:\s+(\d+(?:\.\d+)?)%)?$")


class Rows(HTMLParser):
    """Собирает строки таблиц: в каждой ячейки с классом, текстом и ссылкой."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = {"cls": (a.get("class") or "").split(), "text": "", "href": None, "link_cls": [],
                         "head": tag == "th"}
        elif tag == "a" and self.cell is not None:
            self.cell["href"] = a.get("href")
            self.cell["link_cls"] = (a.get("class") or "").split()

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.cell["text"] = self.cell["text"].strip()
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def split_country(s):
    """'🇺🇸United States' -> ('United States', 'US'); без флага -> (текст, '')."""
    s = s.strip()
    flag = ""
    while s and 0x1F1E6 <= ord(s[0]) <= 0x1F1FF:
        flag += chr(ord("A") + ord(s[0]) - 0x1F1E6)
        s = s[1:]
    return s.strip(), flag


def parse_records(raw):
    # Названия уровней могут содержать запятую, поэтому рядом сохраняется и исходная строка "raw"
    out = []
    for part in raw.split(", "):
        part = part.strip()
        if not part or part == NO_LEVEL:
            continue
        m = PCT.match(part)
        out.append({"level": m.group(1).strip(), "percent": float(m.group(2)) if m.group(2) else 100})
    return out


def id_from_href(href):
    """Ссылка вида .../player/123 -> "123"; ссылка по нику (.../player/SerVax) -> None."""
    if not href:
        return None
    path = href.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    last = path.rsplit("/", 1)[-1]
    return last if last.isdigit() else None


def to_float(s):
    try:
        return float(s.replace(",", ".").strip())
    except ValueError:
        return None


def parse_page(page):
    p = Rows()
    p.feed(page)
    players = []
    for row in p.rows:
        if any(c["head"] for c in row) or len(row) < 4:
            continue
        by_cls = lambda name: next((c for c in row if name in c["cls"]), None)
        rank_c = by_cls("rank") or row[0]
        player_c = next((c for c in row if "player" in c["link_cls"]), None) or \
            next((c for c in row if c["href"]), None) or row[1]
        pts_c = by_cls("pts") or row[3]
        rec_c = by_cls("rec") or row[-1]
        used = {id(rank_c), id(player_c), id(pts_c), id(rec_c)}
        country_c = next((c for c in row if id(c) not in used), None)
        rank = to_float(rank_c["text"])
        points = to_float(pts_c["text"])
        if rank is None or points is None:
            continue
        cname, flag = split_country(country_c["text"]) if country_c else ("", "")
        raw = re.sub(r"\s+", " ", rec_c["text"]).strip()
        raw = ", ".join(x for x in raw.split(", ") if x.strip() != NO_LEVEL)
        players.append({
            "rank": int(rank), "id": id_from_href(player_c["href"]),
            "name": player_c["text"], "href": player_c["href"],
            "country": cname, "flag": flag, "points": points,
            "records": parse_records(raw), "raw": raw,
        })
    return players


_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://coreboard.pythonanywhere.com/",
}


def get_html(path, short):
    cache = HERE / f"cbd_{short}.html"
    if not args.refresh and cache.exists() and time.time() - cache.stat().st_mtime < TTL:
        print(f"{path}: беру сохранённую страницу {cache.name} (свежая)")
        return cache.read_text("utf-8")
    print(f"{path}: скачиваю {BASE + path}")
    with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=_HEADERS), timeout=60, context=_CTX) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":  # сервер может отдавать gzip
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8")
    cache.write_text(text, "utf-8")
    time.sleep(1)  # не нагружаем бесплатный хостинг
    return text


def merge(world, rc):
    """Один список: каждый игрок один раз, с местом в каждом из топов."""
    people = {}
    for src_name, rows in (("world", world), ("rc", rc)):
        for r in rows:
            p = people.get(r["name"].lower())
            if p is None:
                p = {"name": r["name"], "id": r.get("id"), "country": r["country"], "flag": r["flag"],
                     "points": r["points"],
                     "world_rank": None, "rc_rank": None, "records": r["records"], "raw": r["raw"]}
                people[r["name"].lower()] = p
            p[src_name + "_rank"] = r["rank"]
            if not p.get("id") and r.get("id"):
                p["id"] = r["id"]
            if src_name == "rc" and p["world_rank"] is not None:
                # игрок уже взят из основного топа: если в Топе РК данные другие, отмечаем
                if abs(p["points"] - r["points"]) > 0.005:
                    p["points_rc"] = r["points"]
                if p["raw"] != r["raw"]:
                    p["raw_rc"] = r["raw"]
    return sorted(people.values(), key=lambda p: (p["points"], p["world_rank"] or 10 ** 9))


def main():
    world, rc = (parse_page(get_html(path, short)) for _, path, short in PAGES)
    if not world and not rc:
        print("ни одной строки не найдено, разметка страницы другая")
        return
    rows = merge(world, rc)
    (HERE / "cbd.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
    lines = []
    for i, r in enumerate(rows, 1):
        flag = r.get("flag") or ""
        emoji = "".join(chr(0x1F1E6 + ord(c) - 65) for c in flag) if len(flag) == 2 else ""
        lines.append(f"{r.get('rank', i)}) {emoji + ' ' if emoji else ''}{r['name']} ({r['raw']}) - {r['points']:.2f}")
    (HERE / "cbd.txt").write_text("\n".join(lines), "utf-8")
    ids = {r["name"]: r["id"] for r in rows if r.get("id")}
    ids_file = HERE / "cbd_ids.json"
    if ids:
        ids_file.write_text(json.dumps(ids, ensure_ascii=False, indent=1), "utf-8")
    elif ids_file.exists():
        ids_file.unlink()  # ID пропали (сайт вернул ссылки по нику): старый файл не оставляем
    print(f"Объединено: игроков {len(rows)} (основной топ {len(world)}, Топ РК {len(rc)})")


main()
