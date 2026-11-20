"""Лист World Mobile List. Данные берутся со страниц УРОВНЕЙ (места 1..300), страницы игроков и топ игроков не качаются.
Низкоуровневая работа с сайтом (запросы, разбор страницы) лежит в wml.py, здесь только описание листа."""
from lists_core import PageSource, register
import wml as site


class WML(PageSource):
    name = "wml"      # other_lists.wml, --src wml, --no-wml
    prefix = "wml"    # игрок без глобала получает ключ wml<id>
    tag = "WML"       # метка [WML] в рейтинге
    short = "wml"     # gdr_gl_wml.txt
    rev = 4           # ревизия: 4 = данные идут со страниц уровней (игровой ID уровня в записи, топ игроков не нужен)

    def display_nick(self, nick):
        return site.strip_clan(nick) or nick  # '[67] Kristage' -> 'Kristage'

    def fetch(self, store, workers=3, log=print):
        """Для каждого из топ-300 уровней: все рекорды (в том числе прогресс) и «first beaten by» (100%, без видео, если его нет
        среди рекордов). В игроках только те, у кого есть хотя бы одна запись на этих уровнях. Запись хранит игровой ID уровня
        ("i") и место уровня на WML ("pl"), так что одинаковые названия не путаются."""
        levels = site.fetch_levels(store, workers=workers, pause=self.pause, log=log)
        if not levels:
            return None
        players, records = {}, []

        def add_player(pid, nick, cc):
            pl = players.setdefault(pid, {"n": nick, "r": None, "c": cc})
            if not pl.get("c") and cc:
                pl["c"] = cc

        for place in sorted(levels):
            lv, seen = levels[place], set()
            for pid, nick, cc, pct, video in lv["recs"]:
                pid = str(pid)
                if pid in seen:
                    continue
                seen.add(pid)
                add_player(pid, nick, cc)
                records.append({"a": pid, "l": lv["name"], "p": pct, "v": video or None, "i": lv.get("gd"), "pl": place})
            first = lv.get("first")
            if first and str(first[0]) not in seen:
                pid = str(first[0])
                add_player(pid, first[1], None)
                records.append({"a": pid, "l": lv["name"], "p": 100, "v": None, "i": lv.get("gd"), "pl": place})
        log(f"  {self.name}: уровней {len(levels)}, игроков {len(players)}, записей {len(records)}")
        return {"players": players, "records": records}


register(WML())
