"""Не добавлять в cbd/ тех «новых» игроков, которые на самом деле уже есть на CoreBoard.

Игрок из cbd/missing_players.json и cbd/missing_players_with_waiting.json считается уже имеющимся на CoreBoard (и
не попадает в файл), если у него совпало с кем-то из игроков CoreBoard ХОТЯ БЫ ОДНО из двух:
  - ссылка на видео рекорда: то же видео стоит у рекорда игрока на борде /main/board (ссылки берутся с сайта:
    coreboard-parse.py пишет их в cbd.json, поле "videos");
  - канал: общий канал в links.json (поле "channels" человека: у нового игрока и у игрока CoreBoard, которого
    находим по other_lists.coreboard.main), либо канал видео нового игрока из cache_channels.json (его пишет make-links).

Каналы из channels_ignore.json и из shared_ok в different_people.json (общие у разных людей осознанно) не считаются.
Мобильный борд не смотрим: у него свои игроки, а новые игроки добавляются в классический.
Причина каждого пропуска пишется в файл «пропущенные» (кто совпал и по чему), чтобы это можно было проверить глазами.
"""
import json
import re
import urllib.parse
from pathlib import Path

from idmap import load_id_map, load_links

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

YT = re.compile(
    r"(?:(?:youtu\.be|youtube\.be|yotu\.be)/(?:watch\?(?:.*&)?v=)?"
    r"|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/))([A-Za-z0-9_-]{11})", re.I)
NON_VIDEO_HOSTS = {"worldmobilelist.com", "pointercrate.com", "demonlist.org", "coreboard.pythonanywhere.com"}
TRACK = {"si", "feature", "fbclid", "gclid", "igshid", "ref", "share", "t", "start", "time_continue", "list", "index",
         "pp", "ab_channel", "app", "usp"}


def video_key(url):
    """Ключ видео: ID YouTube или «сайт + путь» для остальных ссылок. Не видео (канал, страница рейтинга): None."""
    u = str(url or "").strip()
    if not u:
        return None
    m = YT.search(u)
    if m:
        return "yt:" + m.group(1)
    if not re.match(r"^https?://", u, re.I):
        if not re.match(r"^[\w.-]+\.[a-z]{2,}/", u, re.I):
            return None
        u = "https://" + u
    try:
        sp = urllib.parse.urlsplit(u)
    except ValueError:
        return None
    host = (sp.hostname or "").lower()
    for pre in ("www.", "m.", "mobile."):
        if host.startswith(pre):
            host = host[len(pre):]
    if host == "twitter.com":
        host = "x.com"
    if not host or host.endswith("youtube.com") or host == "youtu.be" or host in NON_VIDEO_HOSTS:
        return None
    path = sp.path.rstrip("/")
    if len(path) < 2:
        return None
    q = sorted((k, v) for k, v in urllib.parse.parse_qsl(sp.query)
               if k.lower() not in TRACK and not k.lower().startswith(("utm_", "spm_", "share_")))
    return "x:" + host + path + ("?" + urllib.parse.urlencode(q) if q else "")


def norm_channel(link):
    return str(link).strip().rstrip("/").lower()


def _json(*names):
    """Первый найденный JSON-файл из scripts/ или корня репозитория, иначе None."""
    for name in names:
        for base in (HERE, ROOT):
            p = base / name
            if p.exists():
                try:
                    return json.loads(p.read_text("utf-8"))
                except (OSError, ValueError):
                    return None
    return None


def _ignored_channels():
    out = {norm_channel(x) for x in (_json("channels_ignore.json") or []) if isinstance(x, str)}
    rules = _json("different_people.json") or {}
    for case in rules.get("shared_ok", []) if isinstance(rules, dict) else []:
        if isinstance(case, dict) and case.get("channel"):
            out.add(norm_channel(case["channel"]))
    return out


def _channels_of(entry):
    """links.json: entry["channels"] = {"youtube": [...], "youtube_id": [...], ...} -> {(платформа, канал)}."""
    out = set()
    ch = entry.get("channels") if isinstance(entry, dict) else None
    if isinstance(ch, dict):
        for platform, links in ch.items():
            for link in links if isinstance(links, list) else [links]:
                if link:
                    out.add((str(platform), norm_channel(link)))
    elif isinstance(ch, list):
        out |= {("youtube", norm_channel(x)) for x in ch if x}
    return out


class Known:
    """Всё, что известно об игроках CoreBoard (main): их видео и каналы. Строится один раз на запуск."""

    def __init__(self, cbd_json=HERE / "cbd.json"):
        self.video_owner = {}    # ключ видео -> (ник, ID CoreBoard)
        self.channel_owner = {}  # (платформа, канал) -> (ник, ID CoreBoard)
        self.links = load_links()
        self.ignore = _ignored_channels()
        self.cache = _json("cache_channels.json") or {}   # "yt:ID видео" -> ссылка на канал, "ytid:ID видео" -> UC...
        id_map = load_id_map()                             # ID CoreBoard -> {"nick", "gdr_id"}
        self.owner_gdr = {}                                # ID CoreBoard -> наш ID

        try:
            rows = json.loads(Path(cbd_json).read_text("utf-8"))
        except (OSError, ValueError):
            rows = []
            print(f"cbd_dupes: {cbd_json} не прочитался, совпадения по ссылкам с борда проверять не буду")
        with_links = 0
        for row in rows:
            cb_id, nick = str(row.get("id") or ""), row.get("name") or ""
            for v in row.get("videos") or []:
                k = video_key(v.get("video"))
                if k:
                    self.video_owner.setdefault(k, (nick, cb_id))
            with_links += bool(row.get("videos"))
        for cb_id, info in id_map.items():
            gdr = info.get("gdr_id")
            self.owner_gdr[cb_id] = gdr
            for ch in _channels_of(self.links.get(gdr) or {}):
                if ch[1] not in self.ignore:
                    self.channel_owner.setdefault(ch, (info.get("nick") or gdr, cb_id))
        print(f"cbd_dupes: игроков на борде {len(rows)} (со ссылками на видео {with_links}), "
              f"видео {len(self.video_owner)}, каналов {len(self.channel_owner)}")

    def candidate_channels(self, gdr_id, video_keys):
        """Каналы нового игрока: из links.json по его ID и каналы его видео из кеша каналов make-links."""
        out = set(_channels_of(self.links.get(str(gdr_id)) or {}))
        for k in video_keys:
            if k.startswith("yt:"):
                vid = k[3:]
                if self.cache.get("yt:" + vid):
                    out.add(("youtube", norm_channel(self.cache["yt:" + vid])))
                if self.cache.get("ytid:" + vid):
                    out.add(("youtube_id", norm_channel(self.cache["ytid:" + vid])))
        return {c for c in out if c[1] not in self.ignore}

    def match(self, gdr_id, records):
        """Кто на CoreBoard уже тот же человек: (ник, ID CoreBoard, почему) или None."""
        keys = {}
        for r in records:
            k = video_key(r[2] if len(r) > 2 else "")
            if k:
                keys.setdefault(k, r)
        for k, r in keys.items():
            owner = self.video_owner.get(k)
            if owner and self.owner_gdr.get(owner[1]) != str(gdr_id):
                return owner[0], owner[1], f"то же видео {r[2]} ({r[0]} {r[1]}%) уже стоит у него на борде"
        for ch in sorted(self.candidate_channels(gdr_id, keys)):
            owner = self.channel_owner.get(ch)
            if owner and self.owner_gdr.get(owner[1]) != str(gdr_id):
                return owner[0], owner[1], f"общий канал {ch[1]}"
        return None


def drop_known(players, known=None):
    """players: {ключ: {"nick", "gdr_id", "records": [[название, процент, ссылка]], ...}} -> (оставшиеся, строки о пропущенных)."""
    known = known or Known()
    kept, skipped = {}, []
    for key, info in players.items():
        hit = known.match(info.get("gdr_id") or key, info.get("records") or [])
        if hit:
            skipped.append(f"{key} | {info.get('nick')} | наш ID {info.get('gdr_id')} | уже есть на CoreBoard: "
                           f"{hit[0]} (ID {hit[1]}), {hit[2]}")
        else:
            kept[key] = info
    return kept, skipped


def save_players(players, path):
    """Тот же формат, что у add-video-links.py: один игрок = одна строка."""
    items = list(players.items())
    with open(path, "w", encoding="utf-8") as f:
        f.write('{\n  "users": {\n')
        for i, (key, info) in enumerate(items):
            f.write(f"    {json.dumps(key, ensure_ascii=False)}: {json.dumps(info, ensure_ascii=False)}"
                    + (",\n" if i < len(items) - 1 else "\n"))
        f.write("  }\n}\n")


def filter_file(players_file, skipped_file, known=None):
    """Убирает из файла игроков, которые уже есть на CoreBoard; пропущенных дописывает в skipped_file. Возвращает, сколько убрано."""
    players_file = Path(players_file)
    data = json.loads(players_file.read_text("utf-8"))
    users = data.get("users", data)
    kept, skipped = drop_known(users, known)
    if skipped:
        save_players(kept, players_file)
        with open(skipped_file, "a", encoding="utf-8") as f:
            f.write("\n".join(skipped) + "\n")
        for line in skipped:
            print("  не добавляю:", line)
    print(f"cbd_dupes: {players_file.name}: убрано {len(skipped)} из {len(users)} (они уже есть на CoreBoard)")
    return len(skipped)
