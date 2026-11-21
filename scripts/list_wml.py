"""Лист World Mobile List («по уровням»): у WML нет API, поэтому идём от уровней.

1. со страницы /demonlist/ берём список уровней (первые TOP_LEVELS);
2. у каждого уровня открываем /demonlist/<место> и берём ID уровня в игре (ingame_id), все записи
   и того, кто прошёл уровень первым;
3. отдаём данные в едином виде; "i" у записи = ingame_id уровня, gd-rating сверяет его с ingame_id глобала.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import wml as site
from lists_core import Source, register


class WML(Source):
    name = "wml"
    prefix = "wml"    # игрок без глобала получает ключ wml<id>
    tag = "WML"       # метка [WML] в рейтинге
    short = "wml"
    fetchable = True

    TOP_LEVELS = 100  # сколько верхних уровней WML обходим
    pause = 0.4       # пауза после каждого запроса, секунд
    max_failed = 10   # сколько страниц уровней можно не скачать и всё равно продолжить
    rev = 5           # ревизия разбора страницы: поправил парсер, увеличь число, и кеш перекачается сам
    ttl = 0           # сколько секунд страница уровня в кеше считается свежей (0 = качаем каждый раз; gd-rating ставит своё)

    def display_nick(self, nick):
        return site.strip_clan(nick) or nick  # '[67] Kristage' -> 'Kristage'

    def _key(self, level_id):
        return f"{self.name}:L{level_id}"

    def fetch(self, store, workers=3, log=print):
        try:
            entries = site.fetch_wml_level_list()  # [(id уровня на WML, название)] в порядке списка
        except Exception as e:
            log(f"  {self.name}: список уровней не скачался: {e}")
            return None
        if not entries:
            log(f"  {self.name}: список уровней пустой, лист пропускаю")
            return None
        entries = entries[:self.TOP_LEVELS]

        def job(wid, name, pos):
            key = self._key(wid)
            old = store.get(key)
            if old and old.get("rev") == self.rev and time.time() - old.get("at", 0) < self.ttl:
                return wid, old  # страница скачана совсем недавно (в этом же запуске workflow)
            try:
                page = site.parse_level_page(site.http_get(site.LEVEL_URL.format(pos=pos)))
                if page is None:
                    raise RuntimeError("в ответе нет страницы уровня")
                # Защита от сдвига списка и от ошибки в адресе: название из списка должно быть в заголовке страницы
                if page["title"] and site.normalize(name) not in site.normalize(page["title"]):
                    raise RuntimeError(f"на месте #{pos} другой уровень (заголовок {page['title']!r})")
                entry = {"at": time.time(), "rev": self.rev, "name": name, "ingame": page["ingame"],
                         "first": page["first"], "recs": page["records"]}
                store.put(key, entry)
                return wid, entry
            except Exception as e:
                if old and old.get("rev") == self.rev:  # сайт не ответил: берём прошлую копию уровня (даже старую)
                    return wid, old
                log(f"    уровень {name!r} (WML {wid}): не скачался: {e}")
                return wid, None
            finally:
                time.sleep(self.pause)

        got, failed = {}, []
        names = dict(entries)
        ex = ThreadPoolExecutor(max(1, workers))
        try:
            futs = [ex.submit(job, wid, name, pos) for pos, (wid, name) in enumerate(entries, 1)]
            for fut in as_completed(futs):
                wid, entry = fut.result()
                if entry is None:
                    failed.append(wid)
                else:
                    got[wid] = entry
        except KeyboardInterrupt:
            ex.shutdown(wait=False, cancel_futures=True)
            store.save()
            raise SystemExit(1)
        ex.shutdown()
        store.save()

        if len(failed) > self.max_failed:
            log(f"  {self.name}: не скачано уровней: {len(failed)}. Лист в этом запуске пропускаю.")
            return None
        if failed:
            log(f"  {self.name}: не скачано {len(failed)} уровней (повторю в следующий раз): "
                + ", ".join(f"{names[w]} ({w})" for w in failed))

        players, records, no_ingame = {}, [], []
        for pos, (wid, name) in enumerate(entries, 1):  # pos = место уровня в списке WML
            e = got.get(wid)
            if e is None:
                continue
            iid = e.get("ingame")
            if not iid:
                no_ingame.append(name)  # без ID сравнить с глобалом нельзя, а гадать по названию не будем
                continue

            best = {}  # ID игрока на WML -> запись (по уровню у игрока остаётся лучший процент)
            for r in e.get("recs") or []:
                pid = str(r["id"])
                if pid not in best or r["percent"] > best[pid]["percent"]:
                    best[pid] = r
                players.setdefault(pid, {"n": r["nick"], "r": None, "c": r.get("cc")})
                if not players[pid].get("c") and r.get("cc"):
                    players[pid]["c"] = r["cc"]

            first = e.get("first")  # прошёл первым: в таблице записей его может не быть
            if first and str(first["id"]) not in best:
                pid = str(first["id"])
                best[pid] = {"id": pid, "nick": first["nick"], "percent": 100, "video": None}
                players.setdefault(pid, {"n": first["nick"], "r": None, "c": None})

            for pid, r in best.items():
                records.append({"a": pid, "l": name, "p": r["percent"], "v": r.get("video"),
                                "i": str(iid), "pos": pos})

        if no_ingame:
            log(f"  {self.name}: у {len(no_ingame)} уровней нет ID в игре, их записи пропущены: "
                + ", ".join(no_ingame[:10]))
        if not records:
            log(f"  {self.name}: записей нет, лист пропускаю")
            return None
        log(f"  {self.name}: уровней {len(got)}, игроков {len(players)}, записей {len(records)}")
        return {"players": players, "records": records}


register(WML())
