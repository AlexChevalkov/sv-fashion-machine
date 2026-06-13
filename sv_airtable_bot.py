import os
import json
import re
from datetime import datetime, timezone

import feedparser
import requests
import anthropic
from bs4 import BeautifulSoup


ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AIRTABLE_WEBHOOK_URL = os.environ["AIRTABLE_WEBHOOK_URL"]

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
]


def clean_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(" ", strip=True)


def fetch_articles() -> list[dict]:
    articles = []

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            source_name = feed.feed.get("title", feed_url)

            for entry in feed.entries[:5]:
                title = clean_html(entry.get("title", "")).strip()
                summary = clean_html(entry.get("summary", "")).strip()
                link = entry.get("link", "").strip()

                if not title or not link:
                    continue

                articles.append(
                    {
                        "source": source_name,
                        "title": title,
                        "summary": summary[:700],
                        "link": link,
                    }
                )

        except Exception as error:
            print(f"RSS error for {feed_url}: {error}")

    # убираем повторы по ссылке
    seen = set()
    unique_articles = []
    for article in articles:
        if article["link"] in seen:
            continue
        seen.add(article["link"])
        unique_articles.append(article)

    return unique_articles[:35]


def build_news_text(articles: list[dict]) -> str:
    if not articles:
        return "Свежих новостей не найдено."

    blocks = []
    for index, article in enumerate(articles[:20], start=1):
        blocks.append(
            f"{index}. ИСТОЧНИК: {article['source']}\n"
            f"ЗАГОЛОВОК: {article['title']}\n"
            f"КРАТКО: {article['summary']}\n"
            f"ССЫЛКА: {article['link']}"
        )

    return "\n\n".join(blocks)


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


def generate_card(articles: list[dict]) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    news_text = build_news_text(articles)
    evergreen_text = "\n".join(
        [f"- {item['title']}: {item['angle']}" for item in EVERGREEN_IDEAS]
    )

    system_prompt = """
Ты — редактор экспертного fashion-медиа @sv_fashionacademy.

Позиционирование:
Не тренды. Контекст моды.
Архивы, показы, бренды, вкус, индустрия.

Авторский тон:
сухо, умно, точно, без глянцевой восторженности.
Не продавать курс.
Не писать как SMM.
Не использовать слова: must-have, икона стиля, роскошь во всей красе, вдохновляемся.
Давать не пересказ новости, а контекст: почему это важно, что это говорит о бренде, вкусе, индустрии или визуальной культуре.

Пиши по-русски.
"""

    user_prompt = f"""
Вот свежие fashion-материалы:

{news_text}

Если среди них есть сильный инфоповод — выбери один.
Если все новости слабые — возьми одну evergreen-тему из списка:

{evergreen_text}

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
  "raw_text": "краткое описание исходного повода",
  "source_url": "ссылка на источник или пустая строка"
}}

Важно:
- status всегда "Needs Review"
- format пока всегда "Single Post"
- rubric пока всегда "Fashion Context"
- caption должен звучать как редакционная колонка, а не как новостной пересказ
"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text
    print("Claude raw response:")
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

    # Страховка, чтобы Airtable Automation не споткнулся
    card["status"] = "Needs Review"
    card["format"] = "Single Post"
    card["rubric"] = "Fashion Context"

    return card


def send_to_airtable_webhook(card: dict) -> None:
    response = requests.post(AIRTABLE_WEBHOOK_URL, json=card, timeout=30)

    print("Webhook status:", response.status_code)
    print("Webhook response:", response.text[:1000])

    if response.status_code not in [200, 201, 202]:
        raise RuntimeError("Airtable webhook request failed")


def main() -> None:
    print("SV Airtable Bot started:", datetime.now(timezone.utc).isoformat())

    articles = fetch_articles()
    print(f"Fetched articles: {len(articles)}")

    card = generate_card(articles)

    print("Generated card:")
    print(json.dumps(card, ensure_ascii=False, indent=2))

    send_to_airtable_webhook(card)

    print("Done. Card sent to Airtable Alex Review.")


if __name__ == "__main__":
    main()
