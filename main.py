import os
import json
import feedparser
import anthropic
import requests
from datetime import datetime

# ─── CONFIG ───────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BUFFER_ACCESS_TOKEN = os.environ["BUFFER_ACCESS_TOKEN"]

# RSS источники
RSS_FEEDS = [
    "https://www.vogue.com/feed/rss",
    "https://www.businessoffashion.com/feed",
    "https://www.elle.com/rss/all.xml/",
    "https://www.dazeddigital.com/rss",
    "https://www.buro247.me/rss.xml",
]

# Расписание: воскресенье=0, понедельник=1 ... суббота=6
PUBLISH_DAYS = [6, 2, 3, 4]  # вс=6, ср=2, чт=3, пт=4  # вс, ср, чт, пт

# Время публикации (МСК = UTC+3)
PUBLISH_TIME_UTC = "06:30"  # 9:30 МСК

# Мастер-промпт голоса Alex Chevalkov
MASTER_PROMPT = """Ты помогаешь вести Instagram-блог @sv_fashionacademy — профессиональный блог о моде и стиле для русскоязычной аудитории. 

Автор блога — Alex Chevalkov (Alex Chevalkov, ex-Valentin Yudashkin), профессионал высокой моды с 20-летним опытом, основатель Академии моды «Saint Valentine», автор учебного пособия для дизайнеров.

СТИЛЬ ТЕКСТОВ:
- Ироничный insider-тон — пишешь как человек из индустрии, а не наблюдатель снаружи
- Короткие афористичные фразы, лёгкая провокация без агрессии
- Профессиональный юмор над коллегами — допустим и приветствуется
- Чередуй экспертный анализ и живую эмоциональную реакцию
- Можно использовать многоточия, КАПСЛОК для акцентов, эмодзи — умеренно

ФОРМАТЫ:
1. Новость + острое мнение эксперта
2. Инсайт про индустрию (тренды, механики моды)  
3. Провокационный вопрос аудитории
4. Эмоциональная реакция на событие

АУДИТОРИЯ: дизайнеры, студенты-дизайнеры, профессионалы индустрии, продвинутые любители моды. Русскоязычные.

ДЛИНА: 80-150 слов. Коротко, ёмко, с характером. Без воды.

ХЕШТЕГИ: добавь 5-7 релевантных хештегов в конце на русском и английском.
Примеры: #мода #стиль #fashionblog #дизайн #неделямоды #fashiondesign #hautecouture"""


def fetch_news():
    """Собирает новости из RSS источников"""
    articles = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                articles.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "")[:500],
                    "link": entry.get("link", ""),
                    "source": feed.feed.get("title", url)
                })
        except Exception as e:
            print(f"Ошибка RSS {url}: {e}")
    return articles[:10]


def generate_post(articles):
    """Генерирует пост через Claude API"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    news_text = "\n\n".join([
        f"ИСТОЧНИК: {a['source']}\nЗАГОЛОВОК: {a['title']}\nКРАТКО: {a['summary']}"
        for a in articles[:5]
    ])

    prompt = f"""Вот свежие новости из мира моды:

{news_text}

Выбери ОДНУ самую интересную или провокационную новость и напиши Instagram-пост в стиле блога.
Если новости скучные — напиши профессиональный инсайт о моде без привязки к конкретной новости.

Верни ТОЛЬКО текст поста, без пояснений."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=MASTER_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )

    return message.content[0].text


def generate_story(post_text):
    """Генерирует текст для сторис на основе поста"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        system=MASTER_PROMPT,
        messages=[{
            "role": "user",
            "content": f"На основе этого поста напиши короткий тизер для Instagram Stories (максимум 2-3 предложения, интригующий, чтобы хотелось читать дальше):\n\n{post_text}"
        }]
    )

    return message.content[0].text


def get_buffer_profile_id():
    """Получает ID профиля Instagram в Buffer"""
    url = "https://api.buffer.com/graphql"
    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    query = """
    query {
        organizations {
            channels {
                id
                name
                service
            }
        }
    }
    """
    response = requests.post(url, json={"query": query}, headers=headers)
    data = response.json()

    try:
        for org in data["data"]["organizations"]:
            for channel in org["channels"]:
                if channel["service"] == "instagram":
                    return channel["id"]
    except Exception as e:
        print(f"Ошибка получения профиля Buffer: {e}")
        print(f"Ответ: {data}")
    return None


def schedule_post_buffer(post_text, channel_id):
    """Отправляет пост в Buffer как черновик"""
    url = "https://api.buffer.com/graphql"
    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    mutation = """
    mutation CreateDraft($input: CreateDraftPostInput!) {
        createDraftPost(input: $input) {
            ... on Post {
                id
                text
                status
            }
        }
    }
    """

    variables = {
        "input": {
            "channelId": channel_id,
            "content": {
                "text": post_text
            }
        }
    }

    response = requests.post(
        url,
        json={"query": mutation, "variables": variables},
        headers=headers
    )

    return response.json()


def save_results(post, story):
    """Сохраняет результаты в файл для логов"""
    today = datetime.now().strftime("%Y-%m-%d")
    results = {
        "date": today,
        "post": post,
        "story": story,
        "generated_at": datetime.now().isoformat()
    }

    with open(f"generated_{today}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print(f"ПОСТ ДЛЯ INSTAGRAM ({today}):")
    print(f"{'='*50}")
    print(post)
    print(f"\n{'='*50}")
    print("ТЕКСТ ДЛЯ СТОРИС:")
    print(f"{'='*50}")
    print(story)
    print(f"{'='*50}\n")


def should_run_today():
    """Проверяет нужно ли публиковать сегодня"""
    # Если запущен вручную (MANUAL_RUN=true) — всегда публикуем
    if os.environ.get("MANUAL_RUN") == "true":
        return True
    today = datetime.now().weekday()
    # weekday(): пн=0, вт=1, ср=2, чт=3, пт=4, сб=5, вс=6
    return today in PUBLISH_DAYS


def main():
    print(f"Запуск бота: {datetime.now().isoformat()}")

    if not should_run_today():
        print("Сегодня не день публикации. Выход.")
        return

    print("Собираю новости...")
    articles = fetch_news()

    if not articles:
        print("Новостей не найдено, генерирую инсайт...")

    print("Генерирую пост...")
    post = generate_post(articles)

    print("Генерирую текст для сторис...")
    story = generate_story(post)

    print("Сохраняю результаты...")
    save_results(post, story)

    print("Отправляю в Buffer...")
    channel_id = get_buffer_profile_id()

    if channel_id:
        result = schedule_post_buffer(post, channel_id)
        print(f"Buffer ответ: {result}")
        print("✅ Пост добавлен в Buffer как черновик!")
    else:
        print("⚠️ Не удалось найти Instagram канал в Buffer")
        print("Пост сохранён локально в JSON файле")

    print("Готово!")


if __name__ == "__main__":
    main()
