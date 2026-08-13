# -*- coding: utf-8 -*-
"""
Оновлює catalog.json з сайтів обох виробників: ціни, зникнення й появу позицій.
Запускається за розкладом через GitHub Actions (.github/workflows/update-prices.yml).

Три різні завдання з різною надійністю:

1. ЦІНИ — оновлюються завжди для позицій, які впевнено зіставились.
2. ЗНИКЛІ ПОЗИЦІЇ — позначаються прапорцем "discontinued": true, а не видаляються.
   Так вони ховаються з каталогу, але лишаються валідними, якщо вже осіли в
   чиїйсь морозилці чи меню на тиждень. Сам Удома: 404 на сторінці товару —
   сигнал однозначний, позначаємо одразу. Галя: позиції немає серед розпізнаних
   назв — сигнал слабший (могли просто не впізнати текст), тому позначаємо
   тільки після двох поспіль невдалих спроб.
3. НОВІ ПОЗИЦІЇ — лише для Галі (там впізнавання назв уже перевірене й надійне).
   Для Сам Удома надійного способу знайти нові товари без ризику наплутати
   немає — цього кроку тут свідомо нема. Кандидатів у нові позиції скрипт НЕ
   додає сам (не вміє підібрати категорію/фото/КБЖУ) — тільки пише список у
   new_items_report.md, щоб було видно при перевірці репозиторію.

Загальні запобіжники:
- Ніколи не чіпає назву/склад/фото/КБЖУ/категорію — тільки pricePerKg, packPrice,
  priceCheckedAt, discontinued, missCount.
- Якщо ціна відрізняється від старої більш ніж удвічі в той чи інший бік —
  вважає це помилкою парсингу, а не реальною зміною, і пропускає.
- Якщо для Галі впізналось підозріло мало товарів — структура сторінки, схоже,
  змінилась; пропускає оновлення Галі цього разу, щоб не зіпсувати дані наосліп.
- Ніщо не падає з ненульовим кодом виходу через окрему невдалу позицію.
"""
import datetime
import json
import re
import sys
import time
import urllib.request
import urllib.error

CATALOG_PATH = "catalog.json"
REPORT_PATH = "new_items_report.md"
USER_AGENT = "Mozilla/5.0 (compatible; MorozylkaPriceBot/1.0; +https://github.com)"
TIMEOUT = 20
MIN_RATIO, MAX_RATIO = 0.4, 2.5      # безпечні межі зміни ціни за один прогін
MIN_GALYA_MATCHES = 40               # нижче цього — вважаємо парсинг зламаним
GALYA_URL = "https://galyabaluvanakyiv.com.ua/"


def today():
    return datetime.date.today().isoformat()


def fetch(url):
    """Повертає (status, text). status=None означає мережеву помилку (не HTTP-код)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, str(e)


def sane_change(old, new):
    if old <= 0 or new <= 0:
        return False
    ratio = new / old
    return MIN_RATIO <= ratio <= MAX_RATIO


# ───────────────────────── Сам Удома ─────────────────────────
# У кожної позиції своя сторінка з JSON-LD-блоком schema.org/Offer,
# де ціна вказана прямим текстом — найнадійніше джерело з двох.
OFFER_PRICE_RE = re.compile(r'"@type"\s*:\s*"Offer".*?"price"\s*:\s*"([\d.]+)"', re.S)


def update_samudoma(items, log):
    targets = [it for it in items if it["producer"] == "samudoma" and it.get("url")]
    updated = 0
    for it in targets:
        status, body = fetch(it["url"])

        if status == 404:
            if not it.get("discontinued"):
                it["discontinued"] = True
                log.append(f"СУ зникло з продажу (404): {it['name']}")
            continue

        if status is None or body is None:
            log.append(f"СУ помилка завантаження {it['id']}: {body or 'статус '+str(status)}")
            continue

        m = OFFER_PRICE_RE.search(body)
        if not m:
            log.append(f"СУ не знайшов ціну на сторінці: {it['id']}")
            continue

        new_price = float(m.group(1))
        old_price = it["pricePerKg"]
        if not sane_change(old_price, new_price):
            log.append(f"СУ підозріла зміна {it['id']}: {old_price} -> {new_price}, пропускаю")
            continue

        if it.get("discontinued"):
            it["discontinued"] = False
            log.append(f"СУ знову в продажу: {it['name']}")

        if abs(new_price - old_price) > 0.01:
            log.append(f"СУ оновлено {it['name']}: {old_price} -> {new_price} ₴/кг")
            updated += 1

        it["pricePerKg"] = new_price
        it["packPrice"] = round(new_price * it["packWeight"] / 1000, 2)
        it["priceCheckedAt"] = today()
        time.sleep(0.25)  # не бомбардуємо сайт запитами поспіль

    log.append(f"СУ: перевірено {len(targets)}, змінено цін {updated}")
    log.append(
        "СУ: пошук нових позицій не робимо — немає надійного способу відрізнити "
        "новий товар від помилки парсингу без списку сторінок сайту."
    )


# ───────────────────────── Галя Балувана ─────────────────────────
# Увесь каталог на одній сторінці (Tilda), без окремого блоку даних на товар.
# Працюємо по тексту сторінки: "Назва товару" -> число -> "грн" йдуть підряд.
TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
STRIP_TAGS_RE = re.compile(r"<[^>]+>")
NAME_LINE_RE = re.compile(r"^[А-ЯҐЄІЇа-яґєії][^\n]{2,78}$")
NUM_LINE_RE = re.compile(r"^\d{2,5}(?:[.,]\d{1,2})?$")


def html_to_lines(html):
    html = TAG_RE.sub(" ", html)
    text = STRIP_TAGS_RE.sub("\n", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return [ln for ln in lines if ln]


def build_galya_price_map(html):
    lines = html_to_lines(html)
    price_map = {}
    for i in range(len(lines) - 2):
        name = lines[i]
        if name.startswith("NEW "):
            name = name[4:].strip()
        if not NAME_LINE_RE.match(name):
            continue
        if not NUM_LINE_RE.match(lines[i + 1]):
            continue
        if not lines[i + 2].lower().startswith("грн"):
            continue
        try:
            price = float(lines[i + 1].replace(",", "."))
        except ValueError:
            continue
        price_map.setdefault(name, price)  # перше входження — найнадійніше
    return price_map


def update_galya(items, log):
    targets = [it for it in items if it["producer"] == "galya"]
    new_candidates = []
    if not targets:
        return new_candidates

    status, html = fetch(GALYA_URL)
    if html is None:
        log.append(f"Галя: помилка завантаження сторінки: {html or status}")
        return new_candidates

    price_map = build_galya_price_map(html)
    if len(price_map) < MIN_GALYA_MATCHES:
        log.append(
            f"Галя: розпізнав лише {len(price_map)} товарів (очікував {MIN_GALYA_MATCHES}+) — "
            "схоже, сайт змінив розмітку. Пропускаю оновлення Галі цього разу, "
            "щоб не зіпсувати дані наосліп."
        )
        return new_candidates

    existing_names = {it["name"] for it in targets}
    updated, missing, flagged, back = 0, 0, 0, 0

    for it in targets:
        new_price = price_map.get(it["name"])
        if new_price is None:
            missing += 1
            it["missCount"] = it.get("missCount", 0) + 1
            if it["missCount"] >= 2 and not it.get("discontinued"):
                it["discontinued"] = True
                flagged += 1
                log.append(f"Галя зникло з продажу (не знайдено 2 рази поспіль): {it['name']}")
            else:
                log.append(f"Галя не знайшла на сторінці ({it['missCount']}-й раз): {it['name']}")
            continue

        it["missCount"] = 0
        old_price = it["pricePerKg"]
        if not sane_change(old_price, new_price):
            log.append(f"Галя підозріла зміна {it['id']}: {old_price} -> {new_price}, пропускаю")
            continue

        if it.get("discontinued"):
            it["discontinued"] = False
            back += 1
            log.append(f"Галя знову в продажу: {it['name']}")

        if abs(new_price - old_price) > 0.01:
            log.append(f"Галя оновлено {it['name']}: {old_price} -> {new_price} ₴/кг")
            updated += 1

        it["pricePerKg"] = new_price
        it["packPrice"] = round(new_price * it["packWeight"] / 1000, 2)
        it["priceCheckedAt"] = today()

    # усе, що є на сторінці, але не збігається з жодною наявною назвою — кандидат у нові
    new_candidates = sorted(name for name in price_map if name not in existing_names)

    log.append(
        f"Галя: перевірено {len(targets)}, змінено цін {updated}, "
        f"не знайдено {missing}, щойно позначено зниклими {flagged}, "
        f"повернулось у продаж {back}, можливих нових позицій {len(new_candidates)}"
    )
    return new_candidates


def write_report(new_candidates, log):
    lines = [f"# Нові позиції — перевірка {today()}", ""]
    if new_candidates:
        lines.append(
            "Ці назви є на сторінці Галі, але не збігаються з жодною позицією в каталозі. "
            "Це може бути справді новий товар або трохи інша назва вже наявного — "
            "перевір і додай вручну через build_catalog.py, якщо це щось нове."
        )
        lines.append("")
        for name in new_candidates:
            lines.append(f"- {name}")
    else:
        lines.append("Нічого нового не знайдено цього разу.")
    lines.append("")
    lines.append("<details><summary>Повний лог перевірки</summary>")
    lines.append("")
    lines.append("```")
    lines.extend(log)
    lines.append("```")
    lines.append("</details>")
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)

    log = []
    update_samudoma(catalog["items"], log)
    new_candidates = update_galya(catalog["items"], log)
    catalog["updatedAt"] = today()

    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
        f.write("\n")

    write_report(new_candidates, log)

    print("\n".join(log) if log else "Без змін.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
