import os
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

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
    "https://www.vogue.com/feed/rss",
    "https://www.businessoffashion.com/feed",
    "https://www.wwd.com/feed/",
    "https://www.thefashionlaw.com/feed/",
    "https://www.dazeddigital.com/rss",
    "https://www.harpersbazaar.com/rss/all.xml/",
    "https://hypebeast.com/feed",
    "https://www.highsnobiety.com/feed/",
    "https://www.showstudio.com/rss.xml",
    "https://www.buro247.me/rss.xml",
    "https://theblueprint.ru/rss",
    "https://fashionjackson.com,
    "https://www.jessicawang.com,
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

CONTEXT_DOMAINS = [
    "thefashionlaw.com",
    "showstudio.com",
    "metmuseum.org",
    "vam.ac.uk",
    "palaisgalliera.paris.fr",
    "kering.com",
    "lvmh.com",
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

    if any(d in domain for d in MAINSTREAM_DOMAINS):
        return "mainstream_source"

    return "other_source"


def fetch_existing_content() -> tuple[set[str], set[str], list[str]]:
    existing_urls = set()
    existing_titles = set()
    recent_titles = []

    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID or not AIRTABLE_TABLE_NAME:
        print("Airtable read secrets are missing. Duplicate filter disabled.")
        return existing_urls, existing_titles, recent_titles

    table_encoded = quote(AIRTABLE_TABLE_NAME, safe="")
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"

    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    }

    offset = None
    pages = 0

    while pages < 3:
        params = [
            ("pageSize", "100"),
            ("fields[]", "Title"),
            ("fields[]", "Source URL"),
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

    print(f"Existing Airtable titles: {len(existing_titles)}")
    print(f"Existing Airtable URLs: {len(existing_urls)}")

    return existing_urls, existing_titles, recent_titles[:50]


def fetch_articles() -> list[dict]:
    articles = []

    for feed_url in RSS_FEEDS:
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

    seen = set()
    unique_articles = []

    for article in articles:
        key = normalize_url(article["link"])
        if key in seen:
            continue
        seen.add(key)
        unique_articles.append(article)

    return unique_articles[:60]


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
    unused = []

    for item in EVERGREEN_IDEAS:
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

    for index, article in enumerate(articles[:25], start=1):
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

    recent_titles_text = "\n".join([f"- {title}" for title in recent_titles[:30]]) or "Пока нет."

    system_prompt = """
Ты — главный редактор экспертного fashion-медиа @sv_fashionacademy.

Наша позиция:
Не только тренды. Главное - контекст моды и его изменения, влияющие на моду.
Мы не только сообщаем, что произошло. Мы объясняем, почему это важно и что это говорит о моде с точки зрения профессионалов моды.

Задача:
Выбрать инфоповод не по громкости, а по потенциалу авторского экспертного комментария Александра Чевалкова.

Не отдавай автоматический приоритет Vogue, WWD, BoF и другим самым известным источникам.
Большую громкую новость можно выбрать только если есть неожиданный экспертный угол.

Предпочитай темы, где можно показать:
- коды и днк бренда;
- смену визуального языка;
- механику создания и продвижения luxury сектора в одежде и аксессуарах;
- стратегию модной индустрии;
- вкус / невкус;
- архив как инструмент формирования будущих концепций;
- культурный контекст событий в моде ми стиле;
- бизнес-логику в моде;
- визуальную власть образа, законы его создания, законы восприятия потребителем.

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
Не только тренды. Контекст моды.
Архивы, показы, бренды, вкус, индустрия.

Авторский тон:
сухо, умно, точно, без глянцевой восторженности, с юмором и легким троллингом.
Не продавать курс.
Не писать как SMM.
Не использовать слова: must-have, икона стиля, роскошь во всей красе, вдохновляемся.
Давать не пересказ новости, а контекст: почему это важно, что это говорит о бренде, вкусе, индустрии или визуальной культуре.
Предлагать варианты использования выводов в дизайнерской практике.
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

    print("\n=== Generated card ===")
    print(json.dumps(card, ensure_ascii=False, indent=2))

    send_to_airtable_webhook(card)

    print("Done. Card sent to Airtable Alex Review.")


if __name__ == "__main__":
    main()
