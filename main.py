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
    # Международные
    "https://www.vogue.com/feed/rss",
    "https://www.businessoffashion.com/feed",
    "https://www.elle.com/rss/all.xml/",
    "https://www.dazeddigital.com/rss",
    "https://www.harpersbazaar.com/rss/all.xml/",
    "https://www.wwd.com/feed/",
    "https://hypebeast.com/feed",
    "https://www.highsnobiety.com/feed/",
    # Русскоязычные
    "https://www.buro247.me/rss.xml",
    "https://theblueprint.ru/rss",
    "https://www.tatler.ru/rss",
]

PUBLISH_DAYS = list(range(7))  # все дни

# Публикуем каждый день кроме субботы (5)
PUBLISH_DAYS = [0, 1, 2, 3, 4, 6]  # все дни кроме субботы

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
        media_part = f'''assets: [{{ image: {{ url: "{safe_url}" }} }}],'''

    # Экранируем только текст поста для inline GraphQL
    import json as json_module
    safe_text = json_module.dumps(post_text)[1:-1]  # убираем внешние кавычки

    if image_url:
        safe_image = json_module.dumps(image_url)[1:-1]
        assets_part = f'assets: [{{ image: {{ url: "{safe_image}" }} }}],'
    else:
        assets_part = ""

    mutation = f"""
    mutation {{
        createPost(input: {{
            text: "{safe_text}",
            channelId: "{channel_id}",
            schedulingType: automatic,
            mode: addToQueue,
            saveToDraft: true,
            metadata: {{
                instagram: {{
                    type: post,
                    shouldShareToFeed: true
                }}
            }},
            {assets_part}
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
    """

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


def post_to_telegram(post_text, image_url=None):
    """Публикует пост в Telegram канал"""
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN не найден, пропускаю")
        return None

    try:
        if image_url:
            # Пост с фото
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            payload = {
                "chat_id": TELEGRAM_CHANNEL,
                "photo": image_url,
                "caption": post_text,
                "parse_mode": "HTML"
            }
        else:
            # Текстовый пост
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHANNEL,
                "text": post_text,
                "parse_mode": "HTML"
            }

        response = requests.post(url, json=payload, timeout=30)
        result = response.json()

        if result.get("ok"):
            print("✅ Пост опубликован в Telegram!")
        else:
            print(f"⚠️ Ошибка Telegram: {result.get('description')}")
            # Если ошибка parse_mode — пробуем без него
            if "parse" in str(result.get('description', '')).lower():
                payload.pop("parse_mode")
                response2 = requests.post(url, json=payload, timeout=30)
                result2 = response2.json()
                if result2.get("ok"):
                    print("✅ Пост опубликован в Telegram (без форматирования)!")
                    return result2

        return result
    except Exception as e:
        print(f"Ошибка публикации в Telegram: {e}")
        return None


def should_run_today():
    if os.environ.get("MANUAL_RUN") == "true":
        return True
    today = datetime.now().weekday()
    return today in PUBLISH_DAYS


def create_story_reminder(story_text, channel_id):
    """Создаёт напоминание для сторис в Buffer на 22:00 МСК"""
    url = "https://api.buffer.com"
    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    import json as json_module
    safe_text = json_module.dumps(story_text)[1:-1]

    # 22:00 МСК = 19:00 UTC
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    reminder_time = now.replace(hour=19, minute=0, second=0, microsecond=0)
    # Если уже позже 19:00 UTC — ставим на следующий день
    if now.hour >= 19:
        from datetime import timedelta
        reminder_time += timedelta(days=1)

    due_at = reminder_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    mutation = f"""
    mutation {{
        createPost(input: {{
            text: "{safe_text}",
            channelId: "{channel_id}",
            schedulingType: notification,
            mode: customScheduled,
            dueAt: "{due_at}",
            saveToDraft: false,
            metadata: {{
                instagram: {{
                    type: story,
                    shouldShareToFeed: false
                }}
            }}
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
    """

    try:
        response = requests.post(url, json={"query": mutation}, headers=headers)
        result = response.json()
        print(f"Story reminder response: {json.dumps(result, indent=2)[:300]}")
        return result
    except Exception as e:
        print(f"Ошибка создания напоминания для сторис: {e}")
        return None


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
        # Проверяем размер изображения — Instagram максимум 5000px
        try:
            head = requests.head(image_url, timeout=5)
            img_response = requests.get(image_url, timeout=10, stream=True)
            img_response.raw.decode_content = True
            from PIL import Image
            import io
            img_data = img_response.content
            img = Image.open(io.BytesIO(img_data))
            w, h = img.size
            if w > 4000 or h > 4000:
                print(f"Фото слишком большое ({w}x{h}px), изменяю размер...")
                img.thumbnail((4000, 4000), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=85)
                buf.seek(0)
                # Загружаем на tmpfiles.org
                upload = requests.post(
                    "https://tmpfiles.org/api/v1/upload",
                    files={"file": ("image.jpg", buf, "image/jpeg")},
                    timeout=30
                )
                if upload.status_code == 200:
                    url_data = upload.json()
                    image_url = url_data.get("data", {}).get("url", "").replace("tmpfiles.org/", "tmpfiles.org/dl/")
                    print(f"Изображение изменено и загружено: {image_url}")
                else:
                    print("Не удалось загрузить изображение, пост без фото")
                    image_url = None
            else:
                print(f"Фото подходит ({w}x{h}px): {image_url}")
        except Exception as e:
            print(f"Ошибка обработки фото: {e}, пост без фото")
            image_url = None
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

    # Отправляем напоминание для сторис на 22:00 МСК
    if channel_id:
        print("Создаю напоминание для сторис на 22:00 МСК...")
        story_result = create_story_reminder(story, channel_id)
        if story_result and "errors" not in story_result:
            print("✅ Напоминание для сторис создано в Buffer!")
        else:
            print("⚠️ Не удалось создать напоминание для сторис")

    # Публикуем в Telegram
    print("Публикую в Telegram...")
    post_to_telegram(post, image_url)

    print("Готово!")


if __name__ == "__main__":
    main()
