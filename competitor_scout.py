"""
Разведчик конкурентов SV Fashion Media.

1. Берёт список аккаунтов из Airtable, таблица «Конкуренты» (галочка «В анализе»).
2. Собирает посты за последние SCOUT_DAYS дней:
   - Telegram — бесплатно, из публичной ленты t.me/s/<канал> (просмотры видны всем);
   - Instagram — через Apify (лайки, комментарии, просмотры видео).
   + Поиск новых русскоязычных Instagram-аккаунтов по хэштегам (SCOUT_HASHTAGS).
3. Ищет «выбросы»: во сколько раз пост обогнал обычный уровень СВОЕГО аккаунта.
4. Claude разбирает выбросы: тема, триггер, формула хука, формат, сценарий —
   и пишет отчёт с темами для SV Fashion и доказательствами.

Ничего не публикует и не меняет в Airtable. Результат — outputs/scout/:
REPORT.md, posts.csv, outliers.json, discovered_accounts.csv
"""

from __future__ import annotations

import csv
import json
import os
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6"

COMPETITORS_TABLE = os.environ.get("SCOUT_TABLE", "Конкуренты")
DAYS = int(os.environ.get("SCOUT_DAYS", "30"))
MIN_AGE_HOURS = 48            # свежие посты ещё набирают просмотры — не сравниваем
IG_POSTS_PER_ACCOUNT = int(os.environ.get("SCOUT_IG_LIMIT", "40"))
HASHTAGS = [h.strip() for h in os.environ.get(
    "SCOUT_HASHTAGS", "модныйбизнес,историямоды,дизайнеродежды,конструированиеодежды"
).split(",") if h.strip()]
OUTLIER_MIN = 2.5             # во сколько раз выше нормы аккаунта считается выбросом
TOP_FOR_ANALYSIS = 45

OUT = Path("outputs/scout")
OUT.mkdir(parents=True, exist_ok=True)
NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(days=DAYS)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def log(*args):
    print("[scout]", *args, flush=True)


# ---------------------------------------------------------------------------
# Список конкурентов
# ---------------------------------------------------------------------------

def load_competitors():
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(COMPETITORS_TABLE)}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    rows, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for rec in data.get("records", []):
            f = rec.get("fields", {})
            if not f.get("В анализе") or not f.get("Аккаунт"):
                continue
            rows.append({
                "name": f.get("Название", ""),
                "platform": f.get("Платформа", ""),
                "handle": str(f.get("Аккаунт", "")).lstrip("@").strip(),
                "group": f.get("Группа", ""),
            })
        offset = data.get("offset")
        if not offset:
            break
    log(f"competitors: {len(rows)} active")
    return rows


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def parse_count(text: str) -> int:
    text = (text or "").strip().replace(",", ".").upper()
    m = re.match(r"([\d.]+)\s*([KM]?)", text)
    if not m:
        return 0
    value = float(m.group(1))
    return int(value * {"K": 1_000, "M": 1_000_000}.get(m.group(2), 1))


def fetch_telegram(handle: str, max_pages: int = 30):
    posts, before = [], None
    for _ in range(max_pages):
        url = f"https://t.me/s/{handle}" + (f"?before={before}" if before else "")
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        if resp.status_code != 200:
            log(f"tg {handle}: HTTP {resp.status_code}")
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        messages = soup.select("div.tgme_widget_message[data-post]")
        if not messages:
            break
        ids, oldest, newest, dated = [], None, None, 0
        for msg in messages:
            post_id = int(msg["data-post"].split("/")[-1])
            ids.append(post_id)
            time_tag = msg.select_one("a.tgme_widget_message_date time")
            if not time_tag or not time_tag.get("datetime"):
                continue
            date = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
            oldest = date if oldest is None or date < oldest else oldest
            newest = date if newest is None or date > newest else newest
            dated += 1
            views_tag = msg.select_one("span.tgme_widget_message_views")
            text_tag = msg.select_one("div.tgme_widget_message_text")
            text = text_tag.get_text("\n", strip=True) if text_tag else ""
            has_photo = bool(msg.select_one(".tgme_widget_message_photo_wrap"))
            has_video = bool(msg.select_one(".tgme_widget_message_video_player, video"))
            forwarded = bool(msg.select_one(".tgme_widget_message_forwarded_from"))
            posts.append({
                "platform": "Telegram",
                "account": handle,
                "url": f"https://t.me/{handle}/{post_id}",
                "date": date.isoformat(),
                "text": text,
                "format": "видео" if has_video else ("фото" if has_photo else "текст"),
                "views": parse_count(views_tag.get_text() if views_tag else ""),
                "likes": 0,
                "comments": 0,
                "forwarded": forwarded,
            })
        log(f"tg {handle} page: {len(messages)} msgs, {dated} dated, "
            f"{oldest:%Y-%m-%d} … {newest:%Y-%m-%d}" if newest else f"tg {handle} page: {len(messages)} msgs, no dates")
        # Останавливаемся, только когда ВСЯ страница старше окна: отдельные
        # посты со старой датой (репосты, закрепы) не должны обрывать сбор.
        if newest and newest < SINCE:
            break
        if before is not None and min(ids) >= before:
            break
        before = min(ids)
        time.sleep(0.6)
    # дубликаты (альбомы) и вне окна
    seen, unique = set(), []
    for p in posts:
        if p["url"] in seen:
            continue
        seen.add(p["url"])
        if datetime.fromisoformat(p["date"]) >= SINCE:
            unique.append(p)
    log(f"tg {handle}: {len(unique)} posts")
    return unique


# ---------------------------------------------------------------------------
# Instagram через Apify
# ---------------------------------------------------------------------------

def apify_check():
    if not APIFY_TOKEN:
        log("APIFY_TOKEN не задан — Instagram пропускаем")
        return False
    resp = requests.get("https://api.apify.com/v2/users/me", params={"token": APIFY_TOKEN}, timeout=30)
    if resp.status_code != 200:
        log(f"Apify: токен не принят (HTTP {resp.status_code})")
        return False
    data = resp.json().get("data", {})
    plan = (data.get("plan") or {}).get("id", "?")
    log(f"Apify OK: пользователь {data.get('username')}, план {plan}")
    return True


def apify_run(actor: str, payload: dict, max_wait: int = 900):
    run = requests.post(
        f"https://api.apify.com/v2/acts/{actor}/runs",
        params={"token": APIFY_TOKEN},
        json=payload,
        timeout=60,
    )
    run.raise_for_status()
    run_id = run.json()["data"]["id"]
    dataset = run.json()["data"]["defaultDatasetId"]
    log(f"apify run {run_id} started")
    waited = 0
    while waited < max_wait:
        time.sleep(15)
        waited += 15
        status = requests.get(
            f"https://api.apify.com/v2/actor-runs/{run_id}", params={"token": APIFY_TOKEN}, timeout=30
        ).json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            log(f"apify run {run_id}: {status}")
            break
    items = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset}/items",
        params={"token": APIFY_TOKEN, "clean": "true", "format": "json"},
        timeout=120,
    ).json()
    return items if isinstance(items, list) else []


def ig_item_to_post(item: dict):
    ts = item.get("timestamp")
    if not ts:
        return None
    date = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    kind = item.get("type") or ""
    product = item.get("productType") or ""
    fmt = "рилс" if product == "clips" or kind == "Video" else ("карусель" if kind == "Sidecar" else "фото")
    return {
        "platform": "Instagram",
        "account": item.get("ownerUsername", ""),
        "url": item.get("url", ""),
        "date": date.isoformat(),
        "text": item.get("caption") or "",
        "format": fmt,
        "views": int(item.get("videoPlayCount") or item.get("videoViewCount") or 0),
        "likes": max(0, int(item.get("likesCount") or 0)),
        "comments": int(item.get("commentsCount") or 0),
        "forwarded": False,
        "owner_full_name": item.get("ownerFullName", ""),
    }


def fetch_instagram(handles):
    if not handles:
        return []
    items = apify_run(
        "apify~instagram-scraper",
        {
            "directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
            "resultsType": "posts",
            "resultsLimit": IG_POSTS_PER_ACCOUNT,
            "onlyPostsNewerThan": SINCE.strftime("%Y-%m-%d"),
            "addParentData": False,
        },
    )
    posts = [p for p in (ig_item_to_post(i) for i in items) if p]
    posts = [p for p in posts if datetime.fromisoformat(p["date"]) >= SINCE]
    for h in handles:
        log(f"ig {h}: {sum(1 for p in posts if p['account'].lower() == h.lower())} posts")
    return posts


def discover_instagram():
    """Новые русскоязычные аккаунты по хэштегам: кто набирает вовлечённость."""
    if not HASHTAGS:
        return []
    items = apify_run(
        "apify~instagram-scraper",
        {
            "directUrls": [f"https://www.instagram.com/explore/tags/{quote(h)}/" for h in HASHTAGS],
            "resultsType": "posts",
            "resultsLimit": 40,
            "addParentData": False,
        },
    )
    posts = [p for p in (ig_item_to_post(i) for i in items) if p]
    by_owner = {}
    for p in posts:
        if not p["account"]:
            continue
        o = by_owner.setdefault(p["account"], {"account": p["account"], "name": p.get("owner_full_name", ""),
                                               "posts": 0, "engagement": 0, "sample": ""})
        o["posts"] += 1
        o["engagement"] += p["likes"] + 3 * p["comments"]
        if not o["sample"]:
            o["sample"] = p["text"][:160].replace("\n", " ")
    ranked = sorted(by_owner.values(), key=lambda o: -o["engagement"])
    log(f"discovered accounts: {len(ranked)}")
    return ranked[:40]


# ---------------------------------------------------------------------------
# Выбросы
# ---------------------------------------------------------------------------

def metric(p):
    if p["platform"] == "Telegram":
        return p["views"]
    return p["likes"] + 3 * p["comments"]


def score_outliers(posts):
    eligible = [
        p for p in posts
        if not p.get("forwarded")
        and (p["text"] or p["format"] != "текст")
        and NOW - datetime.fromisoformat(p["date"]) >= timedelta(hours=MIN_AGE_HOURS)
        and metric(p) > 0
    ]
    by_acc = {}
    for p in eligible:
        by_acc.setdefault((p["platform"], p["account"].lower()), []).append(p)
    baselines = {}
    for key, items in by_acc.items():
        if len(items) < 5:
            continue
        med = statistics.median(metric(p) for p in items)
        baselines[key] = med
        for p in items:
            p["baseline"] = med
            p["score"] = round(metric(p) / med, 2) if med else 0
            p["suspect"] = p["score"] >= 15   # такой разрыв часто значит рекламу или посев
    scored = [p for p in eligible if "score" in p]
    return scored, baselines


# ---------------------------------------------------------------------------
# Разбор Claude
# ---------------------------------------------------------------------------

OUR_CONTEXT = """
Наш аккаунт @sv_fashionacademy (21 тыс. подписчиков, русскоязычный, автор — практик высокой моды с
20-летним опытом, бывший дом Валентина Юдашкина). Лучшие посты за лето–осень 2026 (охват):
- «Сорок лет делать Dior — и ни разу не быть названным» (индийское ателье-субподрядчик) — 42 078, +227 подписчиков
- последнее шоу Глена Мартенса для Diesel — 3 032
- Жан-Поль Готье и лицо подруги Катрин Денёв — 2 563
- Виктория Бэкхэм: 20 лет спустя — 1 440
- «За 47 лет ни одна линия Armani не получала внешнего директора» — 1 018
Обычный пост — около 400. Рилсы-нарезки показов — около 300 и смотрят 6 секунд.
Аудитория: дизайнеры, владельцы небольших ателье и брендов, студенты, продвинутые любители моды.
Главные триггеры аудитории: невидимость мастера, обесценивание ремесла, возраст, вкус против денег,
бренды-самозванцы, падение качества.
"""


def analyse(outliers, flops, baselines):
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def pack(items):
        lines = []
        for p in items:
            flag = " (подозрительно высокий — возможно, пост продвигали рекламой)" if p.get("suspect") else ""
            lines.append(
                f"- [{p['platform']} @{p['account']}] ×{p['score']}{flag} к норме аккаунта "
                f"({metric(p)} при норме {int(p['baseline'])}); формат: {p['format']}; {p['url']}\n"
                f"  Текст: {p['text'][:700].replace(chr(10), ' / ')}"
            )
        return "\n".join(lines)

    prompt = f"""
Ты — аналитик контента русскоязычного fashion-медиа. Ниже посты конкурентов за последние {DAYS} дней.
«×N» — во сколько раз пост обогнал обычный уровень СВОЕГО аккаунта (для Telegram считаем просмотры,
для Instagram — лайки + 3×комментарии). Это и есть сигнал того, что тема или подача зацепила.

КОНТЕКСТ НАШЕГО АККАУНТА:
{OUR_CONTEXT}

ВЫБРОСЫ (лучшие):
{pack(outliers)}

ПРОВАЛЫ (для контраста, худшие относительно нормы):
{pack(flops)}

Напиши отчёт для владельца аккаунта. Он не аналитик, быстро устаёт — коротко, конкретно, без воды,
по-русски. Каждый вывод подкрепляй ссылками на посты из списка. Не выдумывай данных, которых нет.

ЖЁСТКИЕ ПРАВИЛА:
- Не строй вывод на одном посте с подозрительно высоким значением — такие посты часто продвигали
  рекламой. Вывод — только то, что повторяется у нескольких постов или аккаунтов.
- Хуки и темы для SV Fashion пиши голосом медиа: без первого лица («я», «мне», «я держала»),
  без регалий автора («я 20 лет работала…») и без ВЫДУМАННЫХ историй и фактов (заказчицы,
  спасённые выкройки, «Юдашкин делал три дня»). Только то, что можно проверить по новостям
  и открытым фактам. Опыт практика проявляется в угле и точности взгляда, а не в рассказах о себе.
- Темы для SV Fashion — по реальным текущим поводам из постов выше или по повторяющимся типам
  тем, а не придуманные события.

Структура (markdown):
## Главное
5 выводов: что системно цепляет аудиторию в этом поле.
## Формулы хуков
8–10 формул первой строки/заголовка, которые повторяются у выбросов. Для каждой: формула, пример из
поста (цитата до 12 слов), ссылка, и как её применить в нашем голосе практика.
## Темы, которые заходят
Типы тем/поводов, которые чаще дают выбросы, с примерами.
## Сценарии
3–5 повторяющихся структур поста (как выстроен текст от первой строки до финала).
## Что не заходит
3–5 пунктов по провалам.
## 10 тем для SV Fashion
Конкретные темы на ближайшие недели с углом практика haute couture. Для каждой: тема, хук-первая
строка, формат (карусель/текст/рилс), на какой выброс опирается (ссылка), какой триггер аудитории.
## Чего в поле нет
Где свободная ниша для нашего голоса (по тому, что видно в данных).
"""
    message = client.messages.create(
        model=MODEL,
        max_tokens=12000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------

def main():
    competitors = load_competitors()
    posts = []

    for c in competitors:
        if c["platform"] == "Telegram":
            try:
                posts += fetch_telegram(c["handle"])
            except Exception as exc:
                log(f"tg {c['handle']} failed: {exc!r}")

    discovered = []
    if apify_check():
        ig_handles = [c["handle"] for c in competitors if c["platform"] == "Instagram"]
        try:
            posts += fetch_instagram(ig_handles)
        except Exception as exc:
            log(f"instagram failed: {exc!r}")
        try:
            discovered = discover_instagram()
        except Exception as exc:
            log(f"discovery failed: {exc!r}")

    scored, baselines = score_outliers(posts)
    scored.sort(key=lambda p: -p["score"])
    outliers = [p for p in scored if p["score"] >= OUTLIER_MIN][:TOP_FOR_ANALYSIS]
    if len(outliers) < 15:
        outliers = scored[:25]
    flops = sorted(scored, key=lambda p: p["score"])[:12]
    log(f"posts: {len(posts)}, scored: {len(scored)}, outliers ≥×{OUTLIER_MIN}: "
        f"{sum(1 for p in scored if p['score'] >= OUTLIER_MIN)}")

    with open(OUT / "posts.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["platform", "account", "date", "format", "views", "likes",
                                                "comments", "baseline", "score", "url", "text"])
        writer.writeheader()
        for p in scored:
            writer.writerow({k: (p.get(k, "") if k != "text" else p["text"][:500]) for k in writer.fieldnames})
    (OUT / "outliers.json").write_text(json.dumps(outliers, ensure_ascii=False, indent=1), encoding="utf-8")
    if discovered:
        with open(OUT / "discovered_accounts.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["account", "name", "posts", "engagement", "sample"])
            writer.writeheader()
            writer.writerows(discovered)

    summary = [f"# Разведка конкурентов — {NOW:%d.%m.%Y}", "",
               f"Окно: {DAYS} дней. Постов собрано: {len(posts)}, в сравнении: {len(scored)}, "
               f"выбросов (×{OUTLIER_MIN} и выше): {sum(1 for p in scored if p['score'] >= OUTLIER_MIN)}.", "",
               "Норма аккаунтов (медиана просмотров / вовлечённости):"]
    for (platform, acc), med in sorted(baselines.items()):
        n = sum(1 for p in scored if p["account"].lower() == acc)
        summary.append(f"- {platform} @{acc}: {int(med)} (постов: {n})")
    summary.append("")

    report = analyse(outliers, flops, baselines) if outliers else "Недостаточно данных для разбора."
    top_table = ["", "## Топ-20 выбросов", "", "| × | Аккаунт | Формат | Первая строка | Ссылка |", "|---|---|---|---|---|"]
    for p in scored[:20]:
        first = (p["text"].split("\n")[0] if p["text"] else "—")[:80].replace("|", "/")
        top_table.append(f"| {p['score']} | @{p['account']} | {p['format']} | {first} | {p['url']} |")
    if discovered:
        top_table += ["", "## Новые Instagram-аккаунты по хэштегам", "",
                      "| Аккаунт | Имя | Постов | Вовлечённость |", "|---|---|---|---|"]
        for o in discovered[:15]:
            top_table.append(f"| @{o['account']} | {o['name']} | {o['posts']} | {o['engagement']} |")

    (OUT / "REPORT.md").write_text("\n".join(summary) + "\n" + report + "\n" + "\n".join(top_table),
                                   encoding="utf-8")
    log("report written")


if __name__ == "__main__":
    main()
