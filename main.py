import os
import json
import feedparser
import anthropic
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# ─── CONFIG ───────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BUFFER_ACCESS_TOKEN = os.environ["BUFFER_ACCESS_TOKEN"]

RSS_FEEDS = [
    "https://www.vogue.com/feed/rss",
    "https://www.businessoffashion.com/feed",
    "https://www.elle.com/rss/all.xml/",
    "https://www.dazeddigital.com/rss",
    "https://www.buro247.me/rss.xml",
]

PUBLISH_DAYS = [6, 2, 3, 4]  # вс=6, ср=2, чт=3, пт=4

MASTER_PROMPT = """Ты помогаешь вести Instagram-блог @sv_fashionacademy — профессиональный блог о моде и стиле для русскоязычной аудитории.

Автор блога — Alex Chevalkov (ex-Valentin Yudashkin), профессионал высокой моды с 20-летним опытом, основатель Академии моды «Saint Valentine», автор учебного пособия для дизайнеров.

СТИЛЬ ТЕКСТОВ:
- Ироничный insider-тон — пишешь как человек из индустрии, а не наблюдатель снаружи
- Короткие афористичные фразы, лёгкая провокация без агрессии
- Профессиональный юмор над коллегами — допустим и приветствуется
- Чередуй экспертный анализ и живую эмоциональную реакцию
- Можно использовать многоточия, КАПСЛОК для акцентов, эмодзи — умеренно

ДЛИНА: 80-150 слов. Коротко, ёмко, с характером. Без воды.

ХЕШТЕГИ: добавь 5-7 релевантных хештегов в конце на русском и английском."""


def extract_image_from_entry(entry):
    """Извлекает URL изображения из RSS записи"""
    # 1. Медиа контент (media:content)
    if hasattr(entry, 'media_content') and entry.media_content:
        for media in entry.media_content:
            if media.get('type', '').startswith('image'):
                return media.get('url')

    # 2. Enclosures
    if hasattr(entry, 'enclosures') and entry.enclosures:
        for enc in entry.enclosures:
            if 'image' in enc.get('type', ''):
                return enc.get('url') or enc.get('href')

    # 3. Media thumbnail
    if hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
        return entry.media_thumbnail[0].get('url')

    # 4. Парсим HTML в summary
    summary = entry.get('summary', '') or entry.get('content', [{}])[0].get('value', '')
    if summary:
        soup = BeautifulSoup(summary, 'html.parser')
        img = soup.find('img')
        if img and img.get('src'):
            src = img['src']
            if src.startswith('http') and any(ext in src.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                return src

    # 5. Парсим HTML в content
    if hasattr(entry, 'content') and entry.content:
        for c in entry.content:
            soup = BeautifulSoup(c.get('value', ''), 'html.parser')
            img = soup.find('img')
            if img and img.get('src'):
                src = img['src']
                if src.startswith('http'):
                    return src

    return None


def fetch_news():
    """Собирает новости из RSS источников с фото"""
    articles = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                image_url = extract_image_from_entry(entry)
                articles.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "")[:500],
                    "link": entry.get("link", ""),
                    "source": feed.feed.get("title", url),
                    "image_url": image_url
                })
        except Exception as e:
            print(f"Ошибка RSS {url}: {e}")

    # Приоритет статьям с фото
    with_photo = [a for a in articles if a.get('image_url')]
    without_photo = [a for a in articles if not a.get('image_url')]
    return (with_photo + without_photo)[:10]


def generate_post(articles):
    """Генерирует пост через Claude API"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    news_text = "\n\n".join([
        f"ИСТОЧНИК: {a['source']}\nЗАГОЛОВОК: {a['title']}\nКРАТКО: {a['summary']}\nФОТО: {'есть' if a.get('image_url') else 'нет'}"
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
    """Генерирует текст для сторис"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        system=MASTER_PROMPT,
        messages=[{
            "role": "user",
            "content": f"На основе этого поста напиши короткий тизер для Instagram Stories (максимум 2-3 предложения, интригующий):\n\n{post_text}"
        }]
    )
    return message.content[0].text


def get_buffer_channel_id():
    """Получает ID Instagram канала в Buffer"""
    url = "https://api.buffer.com"
    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    # Шаг 1: получаем организации через account
    org_query = """
    query GetOrganizations {
        account {
            organizations {
                id
                name
            }
        }
    }
    """
    try:
        response = requests.post(url, json={"query": org_query}, headers=headers)
        data = response.json()
        print(f"Buffer account response: {json.dumps(data, indent=2)[:300]}")

        orgs = data.get("data", {}).get("account", {}).get("organizations", [])
        if not orgs:
            print("Организации не найдены")
            return None

        org_id = orgs[0]["id"]
        print(f"Организация: {orgs[0]['name']} (ID: {org_id})")

        # Шаг 2: получаем каналы для организации (inline, без переменных)
        channels_query = """
        query GetChannels {
            channels(input: { organizationId: "%s" }) {
                id
                name
                service
            }
        }
        """ % org_id
        response2 = requests.post(
            url,
            json={"query": channels_query},
            headers=headers
        )
        data2 = response2.json()
        print(f"Buffer channels response: {json.dumps(data2, indent=2)[:300]}")

        channels = data2.get("data", {}).get("channels", [])
        for channel in channels:
            if channel.get("service") == "instagram":
                print(f"Найден Instagram канал: {channel['id']}")
                return channel["id"]

        print("Instagram канал не найден среди каналов")
    except Exception as e:
        print(f"Ошибка получения канала Buffer: {e}")
    return None


def create_buffer_draft(post_text, channel_id, image_url=None):
    """Создаёт черновик в Buffer"""
    url = "https://api.buffer.com"
    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    # Экранируем текст для GraphQL
    safe_text = post_text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

    # Добавляем медиа если есть фото
    media_part = ""
    if image_url:
        safe_url = image_url.replace('"', '\\"')
        media_part = f'''assets: [{{ url: "{safe_url}", mediaType: image }}],'''

    mutation = f'''
    mutation CreateDraftPost {{
        createPost(input: {{
            text: "{safe_text}",
            channelId: "{channel_id}",
            schedulingType: automatic,
            mode: addToQueue,
            saveToDraft: true,
            {media_part}
        }}) {{
            ... on PostActionSuccess {{
                post {{
                    id
                    text
                }}
            }}
            ... on MutationError {{
                message
            }}
        }}
    }}
    '''

    try:
        response = requests.post(
            url,
            json={"query": mutation},
            headers=headers
        )
        result = response.json()
        print(f"Buffer draft response: {json.dumps(result, indent=2)[:500]}")
        return result
    except Exception as e:
        print(f"Ошибка создания черновика: {e}")
        return None


def should_run_today():
    if os.environ.get("MANUAL_RUN") == "true":
        return True
    today = datetime.now().weekday()
    return today in PUBLISH_DAYS


def save_results(post, story, image_url=None):
    today = datetime.now().strftime("%Y-%m-%d")
    results = {
        "date": today,
        "post": post,
        "story": story,
        "image_url": image_url,
        "generated_at": datetime.now().isoformat()
    }
    with open(f"generated_{today}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print(f"ПОСТ ДЛЯ INSTAGRAM ({today}):")
    print(f"{'='*50}")
    print(post)
    if image_url:
        print(f"\nФОТО: {image_url}")
    print(f"\n{'='*50}")
    print("ТЕКСТ ДЛЯ СТОРИС:")
    print(f"{'='*50}")
    print(story)
    print(f"{'='*50}\n")


def main():
    print(f"Запуск бота: {datetime.now().isoformat()}")

    if not should_run_today():
        print("Сегодня не день публикации. Выход.")
        return

    print("Собираю новости...")
    articles = fetch_news()

    # Берём лучшую статью с фото для визуала
    best_article = next((a for a in articles if a.get('image_url')), None)
    image_url = best_article.get('image_url') if best_article else None

    if image_url:
        print(f"Найдено фото: {image_url}")
    else:
        print("Фото не найдено в RSS, пост будет без фото")

    print("Генерирую пост...")
    post = generate_post(articles)

    print("Генерирую текст для сторис...")
    story = generate_story(post)

    save_results(post, story, image_url)

    print("Подключаюсь к Buffer...")
    channel_id = get_buffer_channel_id()

    if channel_id:
        print("Создаю черновик в Buffer...")
        result = create_buffer_draft(post, channel_id, image_url)
        if result and "errors" not in result:
            print("✅ Черновик создан в Buffer!")
            if image_url:
                print("✅ Фото прикреплено!")
        else:
            print("⚠️ Проблема с Buffer — пост сохранён локально")
    else:
        print("⚠️ Instagram канал не найден в Buffer")
        print("Пост сохранён локально в JSON файле")

    print("Готово!")


if __name__ == "__main__":
    main()
