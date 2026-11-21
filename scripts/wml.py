"""World Mobile List: скачивание списка уровней и разбор страницы уровня (нужно list_wml.py)."""
import html
import gzip
import json
import re
import ssl
import time
import urllib.error
import urllib.request

SITE = "https://worldmobilelist.com"
# В адресе стоит МЕСТО уровня в списке (/demonlist/3 = третий), а не его внутренний ID на сайте
LEVEL_URL = SITE + "/demonlist/{pos}"
API_TRIES = 4

# Сертификат не проверяем (как раньше). Контекст создаём один раз, а не на каждый запрос.
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
# Язык принудительно английский: заголовки блоков разбираются по ним
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://worldmobilelist.com/",
}


def http_get(url, timeout=30):
    """GET с повторами (до API_TRIES раз). 404/403 и подобное не повторяются. Возвращает текст ответа."""
    last = None
    for attempt in range(1, API_TRIES + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS),
                                        timeout=timeout, context=_CTX) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":  # сервер может отдавать gzip
                raw = gzip.decompress(raw)
            return raw.decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:
            last = e
        print(f"    попытка {attempt}/{API_TRIES} не удалась: {last}")
        if attempt < API_TRIES:
            time.sleep(2 * attempt)
    raise last


def normalize(value):
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def strip_clan(nick):
    """'[67] Kristage' -> 'Kristage'."""
    return re.sub(r"^\[[^\]]*\]\s*", "", nick.strip())


def fetch_wml_level_list():
    """Список уровней WML со страницы /demonlist/ в порядке списка: [(wml_level_id, название), ...]."""
    page = http_get(SITE + "/demonlist/")
    m = re.search(r'id="list-data" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        print("  Не найден #list-data на странице списка WML")
        return []
    return [(str(e[0]), e[2]) for e in json.loads(m.group(1)).get("levels", [])]


# Записи уровня лежат в <ol id="records"> целиком: те, что скрыты за «Show all», тоже есть в HTML
LEVEL_ID = re.compile(r'id="copy-id"[^>]*data-copy="(\d+)"')
LEVEL_RECORDS = re.compile(r'<ol[^>]*id="records"[^>]*>(.*?)</ol>', re.S)
LEVEL_ROW = re.compile(r'<li class="r rec[^"]*"[^>]*>(.*?)</li>', re.S)
ROW_PLAYER = re.compile(r'<a class="r-main" href="/demonlist/player/(\d+)"[^>]*>\s*<b>(.*?)</b>', re.S)
ROW_PERCENT = re.compile(r'<b class="tn">(\d+)%</b>')
ROW_VIDEO = re.compile(r'<a class="rec-video" href="([^"]*)"')
ROW_COUNTRY = re.compile(r'data-cc="([A-Z]{2})"')
PAGE_TITLE = re.compile(r'<title>(.*?)</title>', re.S)
LEVEL_FIRST = re.compile(r'id="by"[^>]*>.*?first beaten by\s*<a href="/demonlist/player/(\d+)"[^>]*>(.*?)</a>', re.S)


def parse_level_page(page):
    """Страница уровня WML -> {"title", "ingame": ID уровня в игре или None, "first": {"id", "nick"} или None,
    "records": [{"id", "nick", "cc", "percent", "video"}]}. None, если это не страница уровня."""
    m_id = LEVEL_ID.search(page)
    m_rec = LEVEL_RECORDS.search(page)
    if not m_id and not m_rec:
        return None

    records = []
    for row in LEVEL_ROW.finditer(m_rec.group(1) if m_rec else ""):
        body = row.group(1)
        pl = ROW_PLAYER.search(body)
        if not pl:
            continue
        pct = ROW_PERCENT.search(body)
        vid = ROW_VIDEO.search(body)
        cc = ROW_COUNTRY.search(body)
        records.append({
            "id": pl.group(1),
            "nick": html.unescape(pl.group(2)).strip(),
            "cc": cc.group(1) if cc else None,
            "percent": int(pct.group(1)) if pct else 100,
            "video": html.unescape(vid.group(1)).strip() if vid else None,
        })

    first = LEVEL_FIRST.search(page)
    title = PAGE_TITLE.search(page)
    return {
        "title": html.unescape(title.group(1)).strip() if title else "",
        "ingame": m_id.group(1) if m_id else None,
        "first": {"id": first.group(1), "nick": html.unescape(first.group(2)).strip()} if first else None,
        "records": records,
    }
