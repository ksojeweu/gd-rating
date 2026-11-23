#!/usr/bin/env python3
"""
Дописывает в my_extras.json то, что compare.py находит командой in-cbd-not-in-gdr: достижения, которые есть в
CoreBoard, а в нашем рейтинге их нет или процент ниже. Файл ПОЛНОСТЬЮ перезаписывается найденным (сохраняются только
ссылки на видео), записи с CoreBoard, которые появились у нас или пропали у них, удаляются.

Шаги:
  1. levels_tp.json: список уровней CoreBoard (/main/levels) -> levels_tp.json в корне репозитория;
  2. coreboard-parse.py -> cbd.txt; gd-rating.py --src pointercrate,wml --waiting -> gdr_gl_pc_wml.txt;
  3. compare.py in-cbd-not-in-gdr -> найденное;
  4. my_extras.json и missing_achievements.json (корень репозитория: [ID уровня, процент, ""]), waiting_records.json -> cbd/;
  5. с --commit коммитит и пушит.

  python scripts/sync-extras.py --commit [--dry-run] [--force] [--max N]
  --dry-run  ничего не писать в репозиторий, только показать
  --max N    защита: если изменений (новых + повышенных + пониженных + убранных) больше N (по умолчанию 300), ничего
             не пишем: огромное число почти всегда значит сбой парсинга
  --force    игнорировать --max

levels_tp.json: основные уровни (таблица <tr id="level-N">) и специальные (алиасы, ключ "xИмя"). ID уровней - это ID
глобала; на странице CoreBoard их нет: известные берутся из старого файла (с GitHub) и СВЕРЯЮТСЯ с глобалом по названию,
новые ищутся по имени в api.demonlist.org; неоднозначные и не найденные пропускаются с предупреждением.
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent          # scripts/
import levels as levelsearch
from idmap import load_id_map
from jsonfmt import write_players

ROOT = HERE.parent                              # корень репозитория
EXTRAS = ROOT / "my_extras.json"
LEVELS = ROOT / "levels_tp.json"
MISSING = ROOT / "missing_achievements.json"
WAITING = ROOT / "cbd" / "waiting_records.json"                  # делает gd-rating --waiting
OLD_FILES = ["cbd/waiting_records_with_nicks.json"]  # файлы, которых в репозитории больше быть не должно

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--commit", action="store_true")
ap.add_argument("--max", type=int, default=300)
ap.add_argument("--force", action="store_true")
args = ap.parse_args()

LEVELS_URL = "https://coreboard.pythonanywhere.com/main/levels"
LEVELS_UA = "gl-ext-top6"
LEVELS_RAW = "https://raw.githubusercontent.com/gl-ext/gl-ext/main/levels_tp.json"
GLOBAL_API = "https://api.demonlist.org/level/classic/list"


def run(cmd, what):
    print(f"\n=== {what} ===\n$ {' '.join(map(str, cmd))}", flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=HERE)
    if r.returncode != 0:
        sys.exit(f"Шаг «{what}» завершился с ошибкой (код {r.returncode}). my_extras.json не тронут.")


def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def fetch_levels_html():
    req = urllib.request.Request(LEVELS_URL, headers={"User-Agent": LEVELS_UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        sys.exit(f"Не удалось скачать {LEVELS_URL}: {e}. levels_tp.json не тронут.")


# Особые названия уровней (ID в глобале -> как он называется у нас) и сравнение названий лежат в levels.py,
# общем для всех скриптов: дописали название там, и оно работает здесь.
norm = levelsearch.normalize
SPECIAL_LEVEL_NAMES = levelsearch.SPECIAL_LEVEL_NAMES


def fetch_global_levels():
    """Список уровней глобала (api.demonlist.org), топ с запасом.
    Возвращает (id2name, name2ids, id2place, place2id): ID -> наше название (с учётом SPECIAL_LEVEL_NAMES),
    нормализованное название -> список ID, ID -> место в глобале и место -> ID. При сбое возвращает None."""
    id2name, name2ids, id2place, place2id, offset = {}, {}, {}, {}, 0
    try:
        while True:
            req = urllib.request.Request(f"{GLOBAL_API}?limit=100&offset={offset}",
                                         headers={"User-Agent": LEVELS_UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                arr = ((json.load(r).get("data") or {}).get("levels")) or []
            if not arr:
                break
            for l in arr:
                lid = str(l["id"])
                if lid in id2name:
                    continue
                name = SPECIAL_LEVEL_NAMES.get(lid, str(l["name"]).strip())
                id2name[lid] = name
                name2ids.setdefault(norm(name), []).append(lid)
                pl = int(float(l["placement"]))
                id2place[lid] = pl
                place2id.setdefault(pl, lid)
            if float(arr[-1]["placement"]) > 600:
                break
            offset += 100
    except Exception as e:
        print(f"ВНИМАНИЕ: не удалось получить список уровней глобала ({e}). "
              f"ID уровней не проверены, новые уровни будут пропущены.")
        return None
    if len(id2name) < 300:
        print(f"ВНИМАНИЕ: из глобала пришло только {len(id2name)} уровней - список неполный, "
              f"ID уровней не проверены, новые уровни будут пропущены.")
        return None
    return id2name, name2ids, id2place, place2id


def parse_levels(page, old):
    """Разбирает страницу /main/levels (новая версия CoreBoard - таблицы). Возвращает список (key, запись) в порядке файла.

    Основные уровни: таблица <tr id="level-N"><td class="rank">N</td><td>Name</td><td>List%</td><td>Ending%</td></tr>
    Специальные уровни: вторая таблица <tr><td>Special name</td><td class="rank">N</td><td>Level</td><td>List%</td><td>Ending%</td></tr>
    ID уровня на странице нет (ключ - ID уровня в глобале, api.demonlist.org). Для каждого уровня:
      1) ищем в глобале уровень с ТАКИМ ЖЕ НАЗВАНИЕМ. Он один - берём его ID (если старый ID был другим,
         пишем в лог, что исправили). Само по себе место для этого не используется: стоит в глобале
         вставить или убрать уровень, и все места после него съедут на одну позицию;
      2) таких названий несколько (одноимённые уровни) - выбираем того, чьё место в глобале равно месту
         на CoreBoard;
      3) такого названия в глобале нет (уровень переименован) - оставляем старый ID, только если он же стоит
         в глобале на этом месте (два независимых признака); иначе ID остаётся с пометкой "не подтверждён";
      4) старого ID нет и найти не удалось - уровень пропускаем (выдумывать ID нельзя).
    Если глобал недоступен, сверки нет: ID берутся из старого файла как есть.
    """
    old_levels = old.get("levels", {})
    name2id = {v["name"]: k for k, v in old_levels.items() if not k.startswith("x")}
    gl = fetch_global_levels()
    id2name, name2ids, id2place, place2id = gl if gl else ({}, {}, {}, {})

    main = []  # (place, name, T, t)
    # Новый формат: <tr id="level-N"><td class="rank">N</td><td>Name</td><td>List%</td><td>Ending%</td></tr>
    for m in re.finditer(r'<tr id="level-(\d+)">(.*?)</tr>', page, re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', m.group(2), re.S)
        if len(cells) < 4:
            continue
        place = int(cells[0].strip())
        name = _clean(cells[1])
        list_pct = _clean(cells[2])
        ending_pct = _clean(cells[3])
        # Столбец List percent (первый) = t, Ending percent (второй) = T. Формула требует T > t: на T% запись стоит 2h, на t% 4h.
        try:
            t = float(list_pct)
            T = float(ending_pct)
        except ValueError:
            continue
        main.append((place, name, T, t))
    main.sort()
    name2place = {name: place for place, name, _, _ in main}

    rows, new_names, skipped, fixed, unverified, renamed, used = [], [], [], [], [], [], {}
    for place, name, T, t in main:
        key = name2id.get(name)
        weak = False  # ID не подтверждён глобалом: при споре за ID уступает подтверждённым
        if gl:
            cands = name2ids.get(norm(name), [])
            if len(cands) > 1:  # одноимённые уровни: выбираем по месту
                same = [c for c in cands if id2place.get(c) == place]
                if len(same) == 1:
                    cands = same
            if len(cands) == 1:
                if key is None:
                    new_names.append(f"{place}. {name} -> id {cands[0]}")
                elif key != cands[0]:
                    fixed.append(f"{place}. {name}: был ID {key} "
                                 f"({id2name.get(key, 'нет в глобале')!r}), стал {cands[0]}")
                key = cands[0]
            elif key is not None:
                if not cands and place2id.get(place) == key:
                    renamed.append(f"{place}. {name}: в глобале этот ID называется {id2name.get(key)!r}")
                else:
                    why = "в глобале несколько уровней с таким названием" if cands else "в глобале нет такого названия"
                    unverified.append(f"{place}. {name}: ID {key} ({id2name.get(key, 'нет в глобале')!r}) - {why}")
                    weak = True
        if key is None:
            skipped.append(f"{place}. {name}")
            continue
        rows.append((place, name, T, t, key, weak))

    # Один ID - один уровень. Подтверждённые уровни занимают ID первыми, неподтверждённые уступают.
    accepted = set()
    for weak_pass in (False, True):
        for i, (place, name, T, t, key, weak) in enumerate(rows):
            if weak != weak_pass:
                continue
            if key in used:
                skipped.append(f"{place}. {name}: ID {key} уже занят уровнем «{used[key]}»")
                if weak:
                    unverified[:] = [u for u in unverified if not u.startswith(f"{place}. {name}:")]
                continue
            used[key] = name
            accepted.add(i)
    out = [(key, {"place": place, "name": name, "T": T, "t": t})
           for i, (place, name, T, t, key, weak) in enumerate(rows) if i in accepted]

    # Специальные уровни (алиасы): вторая таблица
    # <tr><td>Special name</td><td class="rank">N</td><td>Level</td><td>List%</td><td>Ending%</td></tr>
    n_alias = 0
    special_table = re.search(r'<h2>Special levels</h2>.*?(<table>.*?</table>)', page, re.S)
    if special_table:
        table_html = special_table.group(1)
        for m in re.finditer(r'<tr>(.*?)</tr>', table_html, re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', m.group(1), re.S)
            if len(cells) < 5:
                continue
            special_name = _clean(cells[0])
            place = int(_clean(cells[1]))
            level_name = _clean(cells[2])
            list_pct = _clean(cells[3])
            ending_pct = _clean(cells[4])
            if level_name in name2place:
                try:
                    t = float(list_pct)      # как в основной таблице: List percent = t, Ending percent = T
                    T = float(ending_pct)
                except ValueError:
                    continue
                out.append(("x" + special_name, {"name": special_name, "like": level_name, "place": name2place[level_name],
                                                  "T": T, "t": t}))
                n_alias += 1

    if gl:
        confirmed = len(out) - len(fixed) - len(new_names) - len(unverified) - len(renamed)
        print(f"ID сверены с глобалом: подтверждено {confirmed}, исправлено {len(fixed)}, новых {len(new_names)}, "
              f"переименованных {len(renamed)}, не подтверждено {len(unverified)}")
    if renamed:
        print("Название на CoreBoard отличается от глобала (ID подтверждён по месту):")
        for s in renamed:
            print("  " + s)
    if fixed:
        print("ИСПРАВЛЕНЫ ID (старый ID относился к другому уровню):")
        for s in fixed:
            print("  " + s)
    if new_names:
        print("Новые уровни (ID найден в глобале по имени):")
        for s in new_names:
            print("  " + s)
    if unverified:
        print("Не подтверждены глобалом (ID оставлен как был, проверь вручную):")
        for s in unverified:
            print("  " + s)
    if skipped:
        print("ВНИМАНИЕ: эти уровни НЕ записаны (нет ID или ID занят другим уровнем; допиши вручную):")
        for s in skipped:
            print("  " + s)
    return out, len(main), n_alias


def save_levels(entries, path):
    """Формат как в исходном файле: одна запись на строку."""
    lines = ['{', '  "levels": {']
    for i, (k, v) in enumerate(entries):
        lines.append(f'    {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}'
                     + ("," if i < len(entries) - 1 else ""))
    lines += ['  }', '}', '']
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text("\n".join(lines), "utf-8")
    os.replace(tmp, path)


def update_levels(target):
    """Скачивает список уровней CoreBoard и пишет его в target. Возвращает True, если файл изменился."""
    print(f"\n=== CoreBoard: список уровней -> {target.name} ===", flush=True)
    # прежний список берём с GitHub, а не из папки
    try:
        req = urllib.request.Request(LEVELS_RAW, headers={"User-Agent": LEVELS_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            before = r.read().decode("utf-8")
        old = json.loads(before)
    except Exception as e:
        sys.exit(f"Не могу загрузить levels_tp.json с GitHub ({e}). Чтобы не затереть его, останавливаюсь.")
    entries, n_main, n_alias = parse_levels(fetch_levels_html(), old)
    old_main = sum(1 for k in old.get("levels", {}) if not k.startswith("x"))
    if n_main < 100 or (old_main and n_main < old_main * 0.8):
        sys.exit(f"Со страницы разобрано только {n_main} уровней (в файле было {old_main}) - "
                 f"похоже на сбой парсинга. {LEVELS.name} не тронут.")
    save_levels(entries, target)
    changed = target.read_text("utf-8") != before
    print(f"Уровней: {n_main}, алиасов: {n_alias}. " + ("Есть изменения." if changed else "Без изменений."))
    return changed


def load_extras():
    if not EXTRAS.exists():
        return {"users": {}}
    try:
        data = json.loads(EXTRAS.read_text("utf-8"))
    except Exception as e:
        sys.exit(f"Не могу прочитать {EXTRAS.name} ({e}). Чтобы не затереть его, останавливаюсь.")
    if not isinstance(data, dict) or not isinstance(data.get("users", {}), dict):
        sys.exit(f"В {EXTRAS.name} ожидался объект вида {{\"users\": {{...}}}}. Останавливаюсь.")
    data.setdefault("users", {})
    return data


def replace_found(extras, found):
    """ПОЛНОСТЬЮ заменяет содержимое extras результатом compare (на месте).

    my_extras.json теперь всегда равен тому, что compare нашёл прямо сейчас:
      - рекорд на CoreBoard упал       -> процент понижается;
      - рекорд появился в нашем рейтинге -> запись пропадает из my_extras (она там больше не нужна);
      - рекорда нет на CoreBoard        -> запись пропадает.
    Единственное, что переносится из старых записей, - ссылка на видео (3-й элемент),
    если запись с тем же игроком и уровнем осталась.
    Возвращает (новых, повышенных, понижённых, удалённых, подробности)."""
    old_users = extras["users"]
    new_users = {}
    added = improved = lowered = removed = 0
    details = []
    found_ids = {str(u) for u in found}

    for uid, rows in found.items():
        uid = str(uid)
        # старые записи этого игрока: ID уровня -> запись
        old_index = {}
        for r in old_users.get(uid, []):
            if isinstance(r, list) and len(r) >= 2:
                old_index[str(r[0])] = r
        cur = []
        for r in rows:
            lvl, pct = r[0], float(r[1])
            pct = int(pct) if pct == int(pct) else pct
            old = old_index.pop(str(lvl), None)  # pop: что останется в old_index, то нужно удалить
            if old is None:
                cur.append([lvl, pct, ""])
                details.append(f"  {uid}: уровень {lvl}: новая запись {pct}%")
                added += 1
            else:
                if float(old[1]) < pct:
                    details.append(f"  {uid}: уровень {lvl}: {old[1]}% -> {pct}%")
                    improved += 1
                elif float(old[1]) > pct:
                    details.append(f"  {uid}: уровень {lvl}: ПОНИЖЕН {old[1]}% -> {pct}%")
                    lowered += 1
                link = old[2] if len(old) > 2 and isinstance(old[2], str) else ""
                cur.append([lvl, pct, link])
        for old in old_index.values():  # были раньше, а compare их больше не находит
            details.append(f"  {uid}: уровень {old[0]}: запись {old[1]}% убрана")
            removed += 1
        if cur:
            new_users[uid] = cur

    for uid, rows in old_users.items():  # игроков, которых compare вообще не вернул, убираем целиком
        if str(uid) in found_ids:
            continue
        for r in rows:
            if isinstance(r, list) and len(r) >= 2:
                details.append(f"  {uid}: уровень {r[0]}: запись {r[1]}% убрана")
                removed += 1

    extras["users"] = new_users
    return added, improved, lowered, removed, details


def dump_users(users, path):
    """Формат как у compare.py: игрок -> записи по одной на строку (маленькие диффы в git). Атомарная запись."""
    items = sorted(users.items(), key=lambda kv: (not kv[0].isdigit(), int(kv[0]) if kv[0].isdigit() else 0, kv[0]))
    out = ['{', '  "users": {']
    for i, (uid, rows) in enumerate(items):
        out.append(f'    {json.dumps(uid, ensure_ascii=False)}: [')
        for j, r in enumerate(rows):
            out.append(f'      {json.dumps(r, ensure_ascii=False)}' + ("," if j < len(rows) - 1 else ""))
        out.append('    ]' + ("," if i < len(items) - 1 else ""))
    out += ['  }', '}', '']
    text = "\n".join(out)
    if path.exists() and path.read_text("utf-8") == text:
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


def save_missing(found):
    """missing_achievements.json в корень репозитория: игрок -> [[ID уровня, процент, ""], ...] (ссылки на видео не ищем)."""
    users = {}
    for uid, rows in found.items():
        recs = [[r[0], int(float(r[1])) if float(r[1]) == int(float(r[1])) else float(r[1]), ""] for r in rows]
        if recs:
            users[str(uid)] = recs
    dump_users(users, MISSING)


def cb_ids():
    """Откуда берутся ID CoreBoard: (наш ID -> ID CoreBoard, ник -> ID CoreBoard) из links.json (other_lists.coreboard.main)
    ({"<ID CoreBoard>": {"nick", "gdr_id"}}) и cbd_ids.json (ник -> ID с сайта; главнее, если есть)."""
    by_gdr, by_nick = {}, {}
    for cb_id, row in load_id_map().items():
        if row.get("gdr_id"):
            by_gdr[str(row["gdr_id"]).casefold()] = str(cb_id)
        if row.get("nick"):
            by_nick.setdefault(str(row["nick"]).casefold(), str(cb_id))
    site = HERE / "cbd_ids.json"
    if site.exists():
        for nick, cb_id in json.loads(site.read_text("utf-8")).items():
            if str(cb_id).isdigit():
                by_nick[str(nick).casefold()] = str(cb_id)
    return by_gdr, by_nick


def publish_waiting():
    """waiting_records.json из scripts/ (ключ - наш ID) -> cbd/ с ключами ID CoreBoard."""
    src, dst = HERE / WAITING.name, WAITING
    if not src.exists():
        return
    by_gdr, by_nick = cb_ids()
    users = json.loads(src.read_text("utf-8")).get("users", {})
    out, kept = {}, []
    for gdr_id, (nick, *records) in users.items():
        cb_id = by_gdr.get(gdr_id.casefold()) or by_nick.get(nick.casefold())
        if cb_id is None:
            kept.append(f"{gdr_id} ({nick})")
            cb_id = gdr_id
        out[cb_id] = (nick, records)
    if kept:
        print(f"waiting_records: нет ID CoreBoard у {len(kept)} игроков, оставил наши ID: {', '.join(kept[:5])}"
              + (" ..." if len(kept) > 5 else ""))
    dst.parent.mkdir(exist_ok=True)
    write_players(dst, out)


def git(*a, check=True):
    r = subprocess.run(["git", *a], cwd=ROOT, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(a)} не удался:\n{r.stdout}{r.stderr}")
    return r


def commit_and_push(message, files):
    git("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "github-actions[bot]"))
    git("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL",
                                               "41898282+github-actions[bot]@users.noreply.github.com"))
    for f in files:
        if f.exists():
            git("add", str(f.relative_to(ROOT)))  # путь от корня репозитория: нужен для файлов в подпапках (cbd/...)
    git("rm", "-q", "--ignore-unmatch", "--", *OLD_FILES)  # старые файлы убираем из репозитория
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("Нечего коммитить: файлы не изменились.")
        return
    git("commit", "-m", message)
    for attempt in range(1, 4):  # параллельно может пушить другой workflow (make_links)
        git("pull", "--rebase", "--autostash", check=False)
        r = git("push", check=False)
        if r.returncode == 0:
            print("Запушено.")
            return
        print(f"push не прошёл (попытка {attempt}/3): {r.stderr.strip()}")
    sys.exit("Не удалось запушить за 3 попытки. Коммит остался локально.")


def main():
    work = Path(tempfile.mkdtemp(prefix="extras_sync_"))
    cbd_txt, gdr_txt = HERE / "cbd.txt", HERE / "gdr_gl_pc_wml.txt"

    # 1. Сначала levels_tp.json (в репозиторий). gd-rating и compare сами берут его с GitHub, поэтому свежая правка
    #    станет видна им только после push и истечения кеша raw (~5 минут)
    if args.dry_run:
        levels_changed = update_levels(work / "levels_tp.json")
    else:
        levels_changed = update_levels(LEVELS)

    # 2. Оба рейтинга
    run([sys.executable, HERE / "coreboard-parse.py", "-r"], "CoreBoard: оба топа -> cbd.txt")
    run([sys.executable, HERE / "gd-rating.py", "--src", "pointercrate,wml", "--out", gdr_txt.name, "--waiting"],
        "Наш рейтинг (глобал + pointercrate + wml) -> gdr_gl_pc_wml.txt")
    for f in (cbd_txt, gdr_txt):
        if not f.exists() or f.stat().st_size == 0:
            sys.exit(f"Нет файла {f.name} (или он пустой). Останавливаюсь.")

    # 3. Сравнение. --include-absent: игроки CoreBoard, которых нет в gdr совсем, но чей ID находится через
    #    links.json, сравниваются с пустым gdr: их записи не пропадают из my_extras при пересборке
    run([sys.executable, HERE / "compare.py", "in-cbd-not-in-gdr", "--cbd", cbd_txt, "--gdr", gdr_txt,
         "--out-dir", work, "--include-absent"], "compare: in-cbd-not-in-gdr")
    found_file = work / "missing_achievements.json"
    if not found_file.exists():
        sys.exit("compare.py не создал missing_achievements.json.")
    found = json.loads(found_file.read_text("utf-8")).get("users", {})

    if not args.dry_run:
        publish_waiting()
    extras = load_extras()
    added, improved, lowered, removed, details = replace_found(extras, found)
    changes = added + improved + lowered + removed
    print(f"{EXTRAS.name}: новых {added}, повышено {improved}, понижено {lowered}, убрано {removed}")
    for line in details[:50]:
        print(line)
    if len(details) > 50:
        print(f"  ... и ещё {len(details) - 50}")

    if not changes:
        if not args.dry_run:
            save_missing(found)
            if args.commit:
                commit_and_push("levels_tp / missing_achievements: обновление (авто, CoreBoard)",
                                [LEVELS, MISSING, WAITING])
        return
    # защита: файл перезаписывается целиком, поэтому считаем ВСЕ изменения, а не только новые
    if changes > args.max and not args.force:
        if levels_changed:
            # levels_tp.json не зависит от my_extras: коммитим его отдельно, иначе правка T/t никогда не попадёт на GitHub
            # и лимит срабатывал бы на каждом запуске.
            if args.commit and not args.dry_run:
                print("my_extras.json упёрся в лимит, но levels_tp.json обновлён: коммичу только его.")
                commit_and_push("levels_tp обновлён (авто, CoreBoard); my_extras не тронут: лимит --max", [LEVELS])
        sys.exit(f"Изменений {changes}, больше лимита --max {args.max}. my_extras.json не записан. "
                 f"Если это нормально, запусти с --force (или подними --max).")
    if args.dry_run:
        print("\n--dry-run: файл не записан.")
        return

    dump_users(extras["users"], EXTRAS)
    save_missing(found)
    if args.commit:
        msg = f"my_extras: +{added} новых, {improved} повышено, {lowered} понижено, {removed} убрано (авто, CoreBoard)"
        if levels_changed:
            msg += "; levels_tp обновлён"
        commit_and_push(msg, [EXTRAS, LEVELS, MISSING, WAITING])


main()
