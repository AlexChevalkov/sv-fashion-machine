import os
import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse, urljoin

import time

import feedparser
import requests
import anthropic
from bs4 import BeautifulSoup


ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AIRTABLE_WEBHOOK_URL = os.environ["AIRTABLE_WEBHOOK_URL"]

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME")

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


RSS_FEEDS = [
    # Global mainstream
    "https://www.vogue.com/feed/rss",
    "https://www.businessoffashion.com/feed",
    "https://www.wwd.com/feed/",
    "https://www.thefashionlaw.com/feed/",
    "https://www.dazeddigital.com/rss",
    "https://www.harpersbazaar.com/rss/all.xml/",
    "https://hypebeast.com/feed",
    "https://www.highsnobiety.com/feed/",
    "https://www.showstudio.com/rss.xml",
    "https://theblueprint.ru/rss",

    # Regional / business / culture
    "https://jingdaily.com/feed/",
    "https://www.voguearabia.com/feed/rss",
    "https://www.vogue.in/feed/rss",
    "https://www.fashionsnap.com/feed/",
]


SOURCE_PAGES = [
    # China / Asia luxury
    {"name": "Jing Daily", "url": "https://jingdaily.com/"},
    {"name": "Jing Daily Fashion", "url": "https://jingdaily.com/fashion"},
    {"name": "Jing Daily Retail", "url": "https://jingdaily.com/retail"},
    {"name": "Dao Insights Luxury", "url": "https://daoinsights.com/tag/industries-luxury/"},

    # Japan
    {"name": "FASHIONSNAP Japan", "url": "https://www.fashionsnap.com/"},
    {"name": "Fashion Press Japan", "url": "https://www.fashion-press.net/"},
    {"name": "The Fashion Post Japan", "url": "https://fashionpost.jp/"},

    # Middle East / UAE
    {"name": "Vogue Arabia", "url": "https://www.voguearabia.com/fashion/"},
    {"name": "Arabian Business Fashion", "url": "https://www.arabianbusiness.com/t-magazine/fashion"},
    {"name": "FashionNetwork UAE", "url": "https://ae.fashionnetwork.com/"},

    # India
    {"name": "Vogue India Fashion", "url": "https://www.vogue.in/fashion"},
    {"name": "Elle India Fashion", "url": "https://elle.in/fashion"},
    {"name": "FashionNetwork India", "url": "https://in.fashionnetwork.com/"},

    # USA / Americas
    {"name": "FashionNetwork USA", "url": "https://us.fashionnetwork.com/"},
    {"name": "Fashionista", "url": "https://fashionista.com/"},
    {"name": "CFDA News", "url": "https://cfda.com/news"},

    # Latin America
    {"name": "FashionNetwork Brazil", "url": "https://br.fashionnetwork.com/"},
    {"name": "FashionNetwork Mexico", "url": "https://mx.fashionnetwork.com/"},
    {"name": "FashionNetwork Latin America", "url": "https://pe.fashionnetwork.com/"},

    # Global context
    {"name": "FashionNetwork Worldwide", "url": "https://ww.fashionnetwork.com/"},
    {"name": "Vogue Business", "url": "https://www.voguebusiness.com/"},
    {"name": "The Fashion Law", "url": "https://www.thefashionlaw.com/"},
    {"name": "SHOWstudio", "url": "https://www.showstudio.com/"},
]


MAINSTREAM_DOMAINS = [
    "vogue.com",
    "businessoffashion.com",
    "wwd.com",
    "harpersbazaar.com",
    "elle.com",
    "dazeddigital.com",
    "hypebeast.com",
    "highsnobiety.com",
]


REGIONAL_DOMAINS = [
    "jingdaily.com",
    "daoinsights.com",
    "fashionsnap.com",
    "fashion-press.net",
    "fashionpost.jp",
    "voguearabia.com",
    "arabianbusiness.com",
    "ae.fashionnetwork.com",
    "in.fashionnetwork.com",
    "vogue.in",
    "elle.in",
    "us.fashionnetwork.com",
    "br.fashionnetwork.com",
    "mx.fashionnetwork.com",
    "pe.fashionnetwork.com",
    "fashionista.com",
    "cfda.com",
]


CONTEXT_DOMAINS = [
    "thefashionlaw.com",
    "showstudio.com",
    "metmuseum.org",
    "vam.ac.uk",
    "palaisgalliera.paris.fr",
    "kering.com",
    "lvmh.com",
    "voguebusiness.com",
]


EVERGREEN_IDEAS = [
    {
        "title": "Chanel узнаётся без логотипа",
        "angle": "Сильный бренд строится не на логотипе, а на системе повторяемых кодов: цвет, силуэт, материал, жест, архив.",
    },
    {
        "title": "Почему luxury продаёт дистанцию",
        "angle": "Luxury работает не через доступность, а через ощущение дистанции, исключительности и контроля желания.",
    },
    {
        "title": "Красивая вещь ещё не является дизайном",
        "angle": "Дизайн начинается не с красоты, а с идеи, функции, силуэта, контекста и точности решения.",
    },
    {
        "title": "Почему Pinterest не создаёт коллекцию",
        "angle": "Мудборд может собрать настроение, но не заменяет авторскую тему, конфликт и структуру коллекции.",
    },
    {
        "title": "Архив — не прошлое, а инструмент",
        "angle": "Бренды используют архив не как ностальгию, а как способ удержать идентичность в хаосе рынка.",
    },
    {
        "title": "Вкус — это не красиво",
        "angle": "Вкус — это точность выбора: что убрать, где остановиться, какую паузу оставить.",
    },
    {
        "title": "Почему логотип — слабый костыль бренда",
        "angle": "Когда бренд держится только на логотипе, значит его визуальный язык недостаточно силён.",
    },
    {
        "title": "Что дизайнер видит за первые 10 секунд показа",
        "angle": "Профессионал сразу считывает силуэт, ритм, цвет, пропорции, кастинг и общую интонацию коллекции.",
    },
]


def clean_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(" ", strip=True)


def normalize_title(title: str) -> str:
    title = title.lower().strip()
    title = re.sub(r"[^\w\sа-яА-ЯёЁ]", " ", title)
    title = re.sub(r"\s+", " ", title)
    return title


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def get_domain(url: str) -> str:
    if not url:
        return ""
    return urlparse(url).netloc.lower().replace("www.", "")


def source_role(url: str) -> str:
    domain = get_domain(url)

    if any(d in domain for d in CONTEXT_DOMAINS):
        return "context_source"

    if any(d in domain for d in REGIONAL_DOMAINS):
        return "regional_source"

    if any(d in domain for d in MAINSTREAM_DOMAINS):
        return "mainstream_source"

    return "other_source"


HISTORY_DAYS = 120
MAX_HISTORY_PAGES = 20


def fetch_existing_content() -> tuple[set[str], set[str], list[str]]:
    existing_urls = set()
    existing_titles = set()
    recent_titles = []

    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID or not AIRTABLE_TABLE_NAME:
        print("Airtable read secrets are missing. Duplicate filter disabled.")
        return existing_urls, existing_titles, recent_titles

    table_encoded = quote(AIRTABLE_TABLE_NAME, safe="")
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    # Filter to last HISTORY_DAYS days so the query stays fast as the base grows.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    date_filter = f"IS_AFTER(CREATED_TIME(), '{cutoff}')"

    offset = None
    pages = 0

    while pages < MAX_HISTORY_PAGES:
        params = [
            ("pageSize", "100"),
            ("fields[]", "Title"),
            ("fields[]", "Source URL"),
            ("filterByFormula", date_filter),
            ("sort[0][field]", "Created"),
            ("sort[0][direction]", "desc"),
        ]

        if offset:
            params.append(("offset", offset))

        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code != 200:
            print("Could not read Airtable history.")
            print("Status:", response.status_code)
            print("Response:", response.text[:1000])
            return existing_urls, existing_titles, recent_titles

        data = response.json()

        for record in data.get("records", []):
            fields = record.get("fields", {})
            title = fields.get("Title", "")
            source_url = fields.get("Source URL", "")

            if title:
                existing_titles.add(normalize_title(title))
                recent_titles.append(title)

            if source_url:
                existing_urls.add(normalize_url(source_url))

        offset = data.get("offset")
        pages += 1

        if not offset:
            break

    print(f"Existing Airtable titles (last {HISTORY_DAYS} days): {len(existing_titles)}")
    print(f"Existing Airtable URLs (last {HISTORY_DAYS} days): {len(existing_urls)}")

    return existing_urls, existing_titles, recent_titles[:50]


def is_probably_article_link(url: str, title: str) -> bool:
    if not url or not title:
        return False

    title = title.strip()
    lower_url = url.lower()

    if len(title) < 18 or len(title) > 180:
        return False

    bad_fragments = [
        "privacy",
        "cookie",
        "terms",
        "contact",
        "about",
        "careers",
        "advertise",
        "newsletter",
        "subscribe",
        "login",
        "account",
        "signin",
        "sign-in",
        "sitemap",
        "rss",
        "facebook.com",
        "instagram.com",
        "youtube.com",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "linkedin.com",
        "pinterest.com",
        "apps.apple.com",
        "play.google.com",
    ]

    if any(fragment in lower_url for fragment in bad_fragments):
        return False

    return True


SOURCES_TABLE_NAME = os.environ.get("AIRTABLE_SOURCES_TABLE_NAME", "Sources")
EVERGREEN_TABLE_NAME = os.environ.get("AIRTABLE_EVERGREEN_TABLE_NAME", "Evergreen Ideas")


def fetch_sources_from_airtable():
    """
    Read active sources from the Airtable 'Sources' table.
    Returns (rss_feeds, source_pages) or None if it can't be read, so callers
    fall back to the built-in lists.
    """
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID):
        return None

    table_encoded = quote(SOURCES_TABLE_NAME, safe="")
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    rss_feeds = []
    source_pages = []
    offset = None

    try:
        while True:
            params = [("pageSize", "100")]
            if offset:
                params.append(("offset", offset))

            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code != 200:
                print("Sources table read failed:", response.status_code, response.text[:300])
                return None

            data = response.json()
            for record in data.get("records", []):
                fields = record.get("fields", {})
                if not fields.get("Active1"):
                    continue

                source_url = (fields.get("Source URL") or "").strip()
                source_name = (fields.get("Source Name") or source_url).strip()
                fetch_type = (fields.get("Fetch Type") or "").strip().lower()

                if not source_url:
                    continue

                if fetch_type == "rss":
                    rss_feeds.append(source_url)
                elif fetch_type in ("page", "press page"):
                    source_pages.append({"name": source_name, "url": source_url})
                # youtube / instagram / manual are skipped for now

            offset = data.get("offset")
            if not offset:
                break

        print(f"Sources from Airtable: {len(rss_feeds)} RSS, {len(source_pages)} pages")
        return rss_feeds, source_pages

    except Exception as error:
        print("Sources table read error:", error)
        return None


def fetch_evergreen_from_airtable():
    """
    Read evergreen ideas whose Status is not 'Used' or 'Archive' from the
    Airtable 'Evergreen Ideas' table. Returns a list of
    {title, angle, record_id}, or None if it can't be read.
    """
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID):
        return None

    table_encoded = quote(EVERGREEN_TABLE_NAME, safe="")
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    items = []
    offset = None

    try:
        while True:
            params = [("pageSize", "100")]
            if offset:
                params.append(("offset", offset))

            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code != 200:
                print("Evergreen table read failed:", response.status_code, response.text[:300])
                return None

            data = response.json()
            for record in data.get("records", []):
                fields = record.get("fields", {})
                status = (fields.get("Status") or "").strip().lower()
                if status in ("used", "archive"):
                    continue

                title = (fields.get("Idea Title") or "").strip()
                angle = (fields.get("Draft Angle") or "").strip()
                if not title:
                    continue

                items.append({"title": title, "angle": angle, "record_id": record["id"]})

            offset = data.get("offset")
            if not offset:
                break

        print(f"Evergreen from Airtable: {len(items)} unused ideas")
        return items

    except Exception as error:
        print("Evergreen table read error:", error)
        return None


def mark_evergreen_used(record_id: str) -> None:
    """Set an evergreen idea's Status to 'Used' so it rotates out."""
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID and record_id):
        return

    table_encoded = quote(EVERGREEN_TABLE_NAME, safe="")
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}/{record_id}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.patch(
            url,
            headers=headers,
            json={"fields": {"Status": "Used"}, "typecast": True},
            timeout=30,
        )
        print("Mark evergreen Used:", response.status_code, response.text[:200])
    except Exception as error:
        print("Mark evergreen error:", error)


def fetch_page_articles(source_pages: list[dict]) -> list[dict]:
    page_articles = []
    headers = {"User-Agent": "Mozilla/5.0 SVFashionBot/2.0"}

    for source in source_pages:
        source_name = source["name"]
        page_url = source["url"]

        try:
            response = requests.get(page_url, headers=headers, timeout=20)

            if response.status_code != 200:
                print(f"Page source skipped: {source_name} | status {response.status_code}")
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            count = 0

            for link_tag in soup.find_all("a"):
                title = clean_html(link_tag.get_text(" ", strip=True))
                href = link_tag.get("href", "")

                if not href:
                    continue

                full_url = urljoin(page_url, href)

                if not is_probably_article_link(full_url, title):
                    continue

                page_articles.append(
                    {
                        "source": source_name,
                        "title": title,
                        "summary": f"Материал найден на региональном или контекстном источнике: {source_name}. Тема требует редакционной оценки, а не пересказа.",
                        "link": full_url,
                        "role": source_role(full_url),
                    }
                )

                count += 1

                if count >= 6:
                    break

        except Exception as error:
            print(f"Page source error for {source_name}: {error}")

        time.sleep(0.5)

    return page_articles


def fetch_articles() -> list[dict]:
    articles = []

    sources = fetch_sources_from_airtable()
    if sources is not None and (sources[0] or sources[1]):
        rss_feeds, source_pages = sources
        print("Using sources from the Airtable Sources table.")
    else:
        rss_feeds, source_pages = RSS_FEEDS, SOURCE_PAGES
        print("Airtable Sources unavailable — using built-in fallback sources.")

    for feed_url in rss_feeds:
        try:
            feed = feedparser.parse(feed_url)
            source_name = feed.feed.get("title", feed_url)

            for entry in feed.entries[:7]:
                title = clean_html(entry.get("title", "")).strip()
                summary = clean_html(entry.get("summary", "")).strip()
                link = entry.get("link", "").strip()

                if not title or not link:
                    continue

                articles.append(
                    {
                        "source": source_name,
                        "title": title,
                        "summary": summary[:900],
                        "link": link,
                        "role": source_role(link),
                    }
                )

        except Exception as error:
            print(f"RSS error for {feed_url}: {error}")

    articles.extend(fetch_page_articles(source_pages))

    seen = set()
    unique_articles = []

    for article in articles:
        key = normalize_url(article["link"])

        if key in seen:
            continue

        seen.add(key)
        unique_articles.append(article)

    print(f"RSS + page candidates collected: {len(unique_articles)}")

    return unique_articles[:90]


def filter_new_articles(
    articles: list[dict],
    existing_urls: set[str],
    existing_titles: set[str],
) -> list[dict]:
    new_articles = []

    for article in articles:
        url_key = normalize_url(article.get("link", ""))
        title_key = normalize_title(article.get("title", ""))

        if url_key and url_key in existing_urls:
            continue

        if title_key and title_key in existing_titles:
            continue

        new_articles.append(article)

    return new_articles


def filter_unused_evergreen(existing_titles: set[str]) -> list[dict]:
    table_ideas = fetch_evergreen_from_airtable()

    if table_ideas:
        source_ideas = table_ideas
    else:
        # Fallback to the built-in list if the Airtable table can't be read.
        source_ideas = [
            {"title": item["title"], "angle": item["angle"], "record_id": ""}
            for item in EVERGREEN_IDEAS
        ]

    unused = []
    for item in source_ideas:
        if normalize_title(item["title"]) not in existing_titles:
            unused.append(item)

    return unused


def extract_json(text: str) -> dict:
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)

    if not match:
        raise ValueError(f"No JSON object found in Claude response:\n{text}")

    return json.loads(match.group(0))


def build_candidates_text(articles: list[dict]) -> str:
    if not articles:
        return "Нет свежих новых инфоповодов после удаления дублей."

    blocks = []

    for index, article in enumerate(articles[:30], start=1):
        blocks.append(
            f"{index}. ROLE: {article['role']}\n"
            f"ИСТОЧНИК: {article['source']}\n"
            f"ЗАГОЛОВОК: {article['title']}\n"
            f"КРАТКО: {article['summary']}\n"
            f"ССЫЛКА: {article['link']}"
        )

    return "\n\n".join(blocks)


def select_editorial_topic(
    articles: list[dict],
    evergreen_ideas: list[dict],
    recent_titles: list[str],
) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    candidates_text = build_candidates_text(articles)

    evergreen_text = "\n".join(
        [f"- {item['title']}: {item['angle']}" for item in evergreen_ideas[:10]]
    ) or "Нет unused evergreen-тем."

    recent_titles_text = "\n".join(
        [f"- {title}" for title in recent_titles[:30]]
    ) or "Пока нет."

    system_prompt = """
Ты — главный редактор экспертного fashion-медиа @sv_fashionacademy.

Наша позиция:
Не только тренды. Контекст моды в развитии.
Мы не только сообщаем, что произошло. Мы объясняем, почему это важно и что это говорит о моде с точки зрения профессионалов моды.

Задача:
Выбрать инфоповод не по громкости, а по потенциалу авторского экспертного комментария Александра Чевалкова Alexander Cheval

Не отдавай автоматический приоритет Vogue, WWD, BoF и другим самым известным источникам.
Большую громкую новость можно выбрать только если есть неожиданный экспертный угол.

Особый бонус получают:
- regional_source: Китай, Япония, Индия, Ближний Восток, Латинская Америка, локальные fashion-рынки;
- context_source: law, business, museums, archives, fashion culture, luxury strategy.

Если mainstream_source и regional_source равны по силе, выбирай regional_source.
Цель — находить темы, которые подписчик вряд ли увидит везде, но которые помогают понять моду глубже.

Предпочитай темы, где можно показать:
- коды и днк бренда;
- смену и развитие визуального языка;
- механику бинеса luxury сектора одежды и аксессуаров;
- стратегию индустрии моды в развитии;
- хороший вкус / плохой вкус;
- архив как инструмент для будущего развития в дизайне моды;
- культурный контекст моды и стиля;
- бизнес-логику в моде;
- визуальную власть образа, законы восприятия потребителем.

Пиши по-русски.
"""

    user_prompt = f"""
Свежие инфоповоды:

{candidates_text}

Недавно уже использованные темы, их НЕ нужно повторять семантически:

{recent_titles_text}

Evergreen-темы на случай, если свежие инфоповоды слишком банальны:

{evergreen_text}

Оцени инфоповоды по системе:

expert_angle_score: 0-30
Есть ли экспертный угол для автора?

originality_score: 0-25
Насколько тема неочевидна и не выглядит как пересказ того, что уже везде?

audience_value_score: 0-20
Поможет ли это подписчику лучше понимать бренды, вкус, коллекции, индустрию?

depth_score: 0-15
Есть ли культурная, архивная, бизнесовая или визуальная глубина?

overexposure_penalty: 0-25
Штраф за слишком очевидную, массовую, уже заезженную новость.

total_score = expert_angle_score + originality_score + audience_value_score + depth_score - overexposure_penalty

Верни СТРОГО валидный JSON без markdown:

{{
  "selected_type": "news или evergreen",
  "selected_index": 1,
  "selected_title": "название выбранной темы",
  "selected_source_url": "ссылка, если news; пустая строка, если evergreen",
  "selected_reason": "почему это выбрано",
  "editorial_angle": "какой уникальный авторский угол использовать",
  "why_follow_this_account": "почему подписчику нужен именно этот аккаунт для этой темы",
  "overexposure_risk": "low/medium/high",
  "scores": {{
    "expert_angle_score": 0,
    "originality_score": 0,
    "audience_value_score": 0,
    "depth_score": 0,
    "overexposure_penalty": 0,
    "total_score": 0
  }},
  "top_rejected": [
    {{
      "title": "отклонённая громкая тема",
      "reason": "почему не выбрана"
    }}
  ]
}}

Правила:
- Если свежая громкая новость слишком очевидна — не выбирай её.
- Если громкая новость всё же выбрана, обязательно дай угол, который отличается от обычного пересказа.
- Если все новости слабые — выбери evergreen.
- Не повторяй темы из списка уже использованных.
"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=1600,
        temperature=0.2,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text

    print("\n=== Editorial Scoring raw response ===")
    print(response_text)

    selection = extract_json(response_text)

    print("\n=== Selected editorial topic ===")
    print(json.dumps(selection, ensure_ascii=False, indent=2))

    return selection


def build_selected_context(
    selection: dict,
    articles: list[dict],
    evergreen_ideas: list[dict],
) -> dict:
    selected_type = selection.get("selected_type", "news")
    selected_index = int(selection.get("selected_index", 1))

    if selected_type == "evergreen":
        index = max(1, selected_index) - 1

        if index >= len(evergreen_ideas):
            index = 0

        item = evergreen_ideas[index] if evergreen_ideas else {
            "title": selection.get("selected_title", "Мода как контекст"),
            "angle": selection.get("editorial_angle", ""),
        }

        return {
            "type": "evergreen",
            "title": item["title"],
            "summary": item["angle"],
            "source": "Evergreen Ideas",
            "link": "",
            "evergreen_record_id": item.get("record_id", ""),
            "editorial_angle": selection.get("editorial_angle", ""),
            "selected_reason": selection.get("selected_reason", ""),
            "why_follow": selection.get("why_follow_this_account", ""),
        }

    index = max(1, selected_index) - 1

    if index >= len(articles):
        index = 0

    article = articles[index] if articles else {
        "title": selection.get("selected_title", "Мода как контекст"),
        "summary": selection.get("editorial_angle", ""),
        "source": "Fallback",
        "link": "",
    }

    return {
        "type": "news",
        "title": article["title"],
        "summary": article["summary"],
        "source": article["source"],
        "link": article["link"],
        "editorial_angle": selection.get("editorial_angle", ""),
        "selected_reason": selection.get("selected_reason", ""),
        "why_follow": selection.get("why_follow_this_account", ""),
    }


def generate_card(selected: dict) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt = """
Ты — редактор экспертного fashion-медиа @sv_fashionacademy.

Позиционирование:
Не только тренды. Контекст моды в развитии.
Архивы, показы, бренды, вкус, индустрия.

Авторский тон:
сухо, умно, точно, без глянцевой восторженности, с юмором и легким троллингом.
Не продавать курс.
Не писать как SMM.
Не использовать слова: must-have, икона стиля, роскошь во всей красе, вдохновляемся.
Давать не пересказ новости, а контекст: почему это важно, что это говорит о бренде, вкусе, индустрии или визуальной культуре.
Предлагать варианты использования выводов для профессионалов индустрии.
Пиши по-русски.
"""

    user_prompt = f"""
Выбранный инфоповод:

Тип: {selected['type']}
Источник: {selected['source']}
Заголовок: {selected['title']}
Кратко: {selected['summary']}
Ссылка: {selected['link']}

Почему выбран:
{selected['selected_reason']}

Авторский угол:
{selected['editorial_angle']}

Почему подписчику нужен именно этот аккаунт:
{selected['why_follow']}

Сделай карточку для Airtable.

Верни СТРОГО валидный JSON без пояснений и без markdown.

Схема JSON:
{{
  "title": "короткое название темы",
  "status": "Needs Review",
  "format": "Single Post",
  "rubric": "Fashion Context",
  "hook": "первая сильная фраза до 12 слов",
  "visual_headline": "короткий заголовок для картинки до 5 слов",
  "final_caption": "готовый Instagram caption 700-1200 знаков, короткие абзацы",
  "raw_text": "источник + почему выбран + авторский угол",
  "source_url": "ссылка на источник или пустая строка"
}}

Важно:
- caption должен быть не пересказом новости, а редакционной колонкой;
- первая строка должна быть сильной;
- в тексте должна чувствоваться причина следить именно за этим аккаунтом;
- status всегда "Needs Review";
- format всегда "Single Post";
- rubric пока всегда "Fashion Context".
"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=1400,
        temperature=0.35,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text

    print("\n=== Claude card raw response ===")
    print(response_text)

    card = extract_json(response_text)

    required = [
        "title",
        "status",
        "format",
        "rubric",
        "hook",
        "visual_headline",
        "final_caption",
        "raw_text",
        "source_url",
    ]

    for field in required:
        if field not in card:
            raise ValueError(f"Missing field from Claude response: {field}")

    card["status"] = "Needs Review"
    card["format"] = "Single Post"
    card["rubric"] = "Fashion Context"

    if not card.get("source_url"):
        card["source_url"] = selected.get("link", "")

    return card


def send_to_airtable_webhook(card: dict) -> None:
    response = requests.post(AIRTABLE_WEBHOOK_URL, json=card, timeout=30)

    print("Webhook status:", response.status_code)
    print("Webhook response:", response.text[:1000])

    if response.status_code not in [200, 201, 202]:
        raise RuntimeError("Airtable webhook request failed")


def main() -> None:
    print("SV Airtable Bot started:", datetime.now(timezone.utc).isoformat())
    webhook_info = urlparse(AIRTABLE_WEBHOOK_URL)
    print("Webhook host:", webhook_info.netloc)
    print("Airtable base id:", AIRTABLE_BASE_ID[:8] + "..." if AIRTABLE_BASE_ID else "missing")
    print("Airtable table name:", AIRTABLE_TABLE_NAME or "missing")

    existing_urls, existing_titles, recent_titles = fetch_existing_content()

    all_articles = fetch_articles()
    print(f"Fetched articles before duplicate filter: {len(all_articles)}")

    new_articles = filter_new_articles(all_articles, existing_urls, existing_titles)
    print(f"Fresh articles after duplicate filter: {len(new_articles)}")

    unused_evergreen = filter_unused_evergreen(existing_titles)
    print(f"Unused evergreen ideas: {len(unused_evergreen)}")

    selection = select_editorial_topic(new_articles, unused_evergreen, recent_titles)
    selected_context = build_selected_context(selection, new_articles, unused_evergreen)

    print("\n=== Final selected context ===")
    print(json.dumps(selected_context, ensure_ascii=False, indent=2))

    card = generate_card(selected_context)

    # Tag the card so Content Inbox can show it came from the bot.
    card["source"] = "Bot"

    print("\n=== Generated card ===")
    print(json.dumps(card, ensure_ascii=False, indent=2))

    send_to_airtable_webhook(card)

    # If an evergreen idea was used, mark it 'Used' so it rotates out next time.
    if selected_context.get("type") == "evergreen":
        mark_evergreen_used(selected_context.get("evergreen_record_id", ""))

    print("Done. Card sent to Airtable Alex Review.")


if __name__ == "__main__":
    main()
