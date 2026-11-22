"""
Telegram approval bridge.

Runs every few minutes (via cron-job.org, like the other bots). Each run:

1) READS your button taps in Telegram (getUpdates) and applies the decision
   to Airtable — moving the record to the next status of our pipeline.
2) SENDS a Telegram card (with ✅ Да / ❌ Нет buttons) for anything that is
   waiting for your approval and hasn't been asked about yet.

Approval gates it covers:
- Content Inbox, Status = "Needs Review"      → Да: Approved      | Нет: Rejected
- Visual Jobs, Visual Status = "Brief Ready"  → Да: Prompts Approved (reel) /
  (reels & posts only; carousels                     Approved Visual (post)
   auto-process, so they are skipped)         | Нет: leave for manual edit
- Visual Jobs, Visual Status = "Needs Visual Review"
                                              → Да: Approved Visual | Нет: manual

State is kept in Airtable ("TG Notified Status" per record) + Telegram's own
update queue, so no extra database is needed.
"""

import hashlib
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from r2_storage import r2_is_configured, upload_file_to_r2


BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
CONTENT_TABLE = os.environ.get("AIRTABLE_TABLE_NAME", "Content Inbox")
VISUAL_TABLE = os.environ.get("AIRTABLE_VISUAL_TABLE_NAME", "Visual Jobs")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
NOTIFIED_FIELD = "TG Notified Status"


# ---------------------------------------------------------------- Telegram ---

def tg(method: str, **params):
    try:
        return requests.post(f"{TG_API}/{method}", json=params, timeout=30).json()
    except Exception as exc:
        print("Telegram call failed:", method, repr(exc))
        return {"ok": False}


def card_text_of(message: dict) -> str:
    """
    Текст карточки. У карточки с картинкой он лежит не в text, а в caption —
    без этого метка [v:rec...] не находится и ответ на обложку теряется.
    """
    return (message or {}).get("text") or (message or {}).get("caption") or ""


def tg_download(file_id: str) -> bytes:
    """Скачать файл, присланный в Телеграме. Пусто, если не получилось."""
    info = tg("getFile", file_id=file_id)
    file_path = ((info or {}).get("result") or {}).get("file_path")
    if not file_path:
        print("getFile не дал путь:", str(info)[:200])
        return b""
    try:
        response = requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=60
        )
        if response.status_code != 200:
            print("Файл не скачался:", response.status_code)
            return b""
        return response.content
    except Exception as exc:
        print("Файл не скачался:", repr(exc))
        return b""


# ---------------------------------------------------------------- Airtable ---

def at_url(table: str) -> str:
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(table, safe='')}"


def at_headers(write: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    if write:
        headers["Content-Type"] = "application/json"
    return headers


def at_request(method: str, url: str, **kwargs):
    """Airtable call with retries: the API occasionally stalls past 30s, and a
    single hiccup must not crash the whole bridge run (updates are re-read on
    the next run anyway). 3 attempts, 60s timeout, 5s pause between."""
    last_error = None
    for attempt in range(1, 4):
        try:
            return requests.request(method, url, timeout=60, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            print(f"Airtable {method} attempt {attempt}/3 failed: {exc!r}")
            time.sleep(5)
    print("Airtable unavailable after retries:", repr(last_error))
    return None


def at_list(table: str, formula: str) -> list:
    response = at_request(
        "GET", at_url(table), headers=at_headers(),
        params={"filterByFormula": formula, "pageSize": 50},
    )
    if response is None or response.status_code != 200:
        print("Airtable list failed:", table,
              getattr(response, "status_code", "no-response"))
        return []
    return response.json().get("records", [])


def at_get(table: str, record_id: str):
    """Returns the record dict, the string "missing" for a 4xx (deleted), or
    None when Airtable is unreachable (caller must NOT treat as missing)."""
    response = at_request("GET", f"{at_url(table)}/{record_id}", headers=at_headers())
    if response is None:
        return None
    if response.status_code != 200:
        return "missing"
    return response.json()


def at_update(table: str, record_id: str, fields: dict) -> bool:
    response = at_request(
        "PATCH", f"{at_url(table)}/{record_id}", headers=at_headers(write=True),
        json={"fields": fields, "typecast": True},
    )
    status = getattr(response, "status_code", "no-response")
    print("Airtable update:", table, record_id, fields, "->", status)
    return response is not None and response.status_code in (200, 201)


def sel(fields: dict, key: str) -> str:
    value = fields.get(key)
    if isinstance(value, dict):
        return value.get("name", "")
    return value if isinstance(value, str) else ""


def format_of(fields: dict) -> str:
    return (sel(fields, "Format") or sel(fields, "Chosen Format")).strip().lower()


# Пост существует в двух видах, и выбираются они при согласовании темы:
#   «Post»      — карточки с реальными фото по теме (так делается сейчас);
#   «Post Text» — только текст на цветном фоне (как делалось раньше).
# Для всей цепочки это один и тот же пост: одинаковый гейт, одинаковый бриф,
# одинаковая сборка. Разница только в том, ищет ли продакшн-бот иллюстрации.
POST_FORMATS = ("post", "post text")


def is_post_format(value: str) -> bool:
    return (value or "").strip().lower() in POST_FORMATS


# ----------------------------------------------------- обложка поста ---

COVER_FIELD = "Cover Photo URL"
COVER_PREVIEW_FIELD = "Cover Preview URL"
CANDIDATES_FIELD = "Photo Candidates"

# Сколько ждём обложку, прежде чем прислать обычную текстовую карточку.
# Нужно, чтобы пост не завис молча, если обложку собрать не удалось.
COVER_WAIT_MINUTES = int(os.environ.get("COVER_WAIT_MINUTES", "120"))


def record_age_minutes(record: dict) -> float:
    """Сколько минут записи. Если дату не разобрать — считаем старой."""
    created = str(record.get("createdTime") or "")
    try:
        when = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return float(COVER_WAIT_MINUTES * 100)
    return (datetime.now(timezone.utc) - when).total_seconds() / 60


def parse_photo_candidates(raw: str) -> list:
    """Разобрать «номер. ссылка | источник» в список {"url", "credit"}."""
    candidates = []
    for line in (raw or "").splitlines():
        match = re.match(r"^\s*\d+\.\s*(\S+)\s*(?:\|\s*(.*))?$", line)
        if match and match.group(1).startswith("http"):
            candidates.append(
                {"url": match.group(1), "credit": (match.group(2) or "").strip()}
            )
    return candidates


def set_cover_photo(record_id: str, url: str, source: str) -> str:
    """
    Поставить фото на обложку поста.

    Саму обложку рисует продакшн-бот, поэтому здесь только записываем выбор
    и чистим 'Cover Preview URL' — пустое поле для него означает «перерисуй».
    Как только новая обложка готова, мост присылает свежую карточку.
    """
    record = at_get(VISUAL_TABLE, record_id)
    if record is None:
        raise RuntimeError("Airtable unreachable — decision will be retried next run.")
    if record == "missing":
        return "Запись не найдена."

    fields = record.get("fields", {})
    status = sel(fields, "Visual Status")
    if status != "Brief Ready":
        return f"Обложку меняем до сборки поста, а сейчас статус: {status}."
    if not is_post_format(format_of(fields)):
        return "Замена обложки пока работает только для постов."

    at_update(VISUAL_TABLE, record_id, {
        COVER_FIELD: url,
        COVER_PREVIEW_FIELD: "",
    })
    return f"🖼 Взял {source} на обложку. Перерисую и пришлю новую карточку."


def upload_own_photo(record_id: str, data: bytes, suffix: str) -> str:
    """
    Выложить присланную владельцем картинку на хостинг и вернуть ссылку.

    Ссылка Телеграма не годится: она содержит токен бота и живёт недолго,
    а продакшн-бот забирает фото позже, уже на своём прогоне.
    """
    if not r2_is_configured():
        print("Хостинг не настроен — своё фото сохранить негде.")
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / f"cover{suffix or '.jpg'}"
        local.write_bytes(data)
        try:
            return upload_file_to_r2(
                str(local), f"telegram-covers/{record_id}_{int(time.time())}{suffix or '.jpg'}"
            )
        except Exception as exc:
            print("Своё фото не выложилось на хостинг:", repr(exc))
            return ""


# Отклонённая визуальная работа уходит в 'Failed'. Раньше отказ не менял
# статус вообще: бот отвечал «Отклонено», запись оставалась в 'Brief Ready'
# или 'Needs Visual Review' и жила в разделах Queued / Review вечно — так
# там с августа накопилось 26 записей.
REJECTED_VISUAL_STATUS = "Failed"


def reject_visual(record_id: str, fields: dict, stage: str) -> str:
    """
    Увести отклонённую визуальную работу из актуальных разделов.

    Карточка уходит в 'Failed', а тема в Content Inbox помечается 'Rejected',
    чтобы всё отклонённое лежало в одном списке. Тему, которая уже дошла до
    Buffer, не трогаем: там отказ относится к доработке, а не к публикации.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    note = sel(fields, "Render Notes").strip()
    reject_note = f"Отклонено в Телеграме на этапе «{stage}» {stamp}."
    at_update(VISUAL_TABLE, record_id, {
        "Visual Status": REJECTED_VISUAL_STATUS,
        "Render Notes": (note + "\n\n---\n\n" + reject_note) if note else reject_note,
    })

    answer = f"❌ Отклонено → {REJECTED_VISUAL_STATUS}. Из актуальных разделов убрал."

    source_id = sel(fields, "Source Content ID").strip()
    if source_id.startswith("rec"):
        topic = at_get(CONTENT_TABLE, source_id)
        if isinstance(topic, dict):
            topic_status = sel(topic.get("fields", {}), "Status")
            if topic_status not in ("Rejected", "Sent to Buffer"):
                at_update(CONTENT_TABLE, source_id, {"Status": "Rejected"})
                answer += " Тему тоже пометил отклонённой."

    return answer


# ------------------------------------------------------- apply a decision ---

def apply_decision(decision: str, table_key: str, record_id: str) -> str:
    table = CONTENT_TABLE if table_key == "content" else VISUAL_TABLE
    record = at_get(table, record_id)
    if record is None:
        # Airtable outage: crash the run WITHOUT confirming this update, so
        # the decision is retried on the next scheduled run instead of lost.
        raise RuntimeError("Airtable unreachable — decision will be retried next run.")
    if record == "missing":
        return "Запись не найдена."
    fields = record.get("fields", {})

    if table_key == "content":
        status = sel(fields, "Status")
        if status != "Needs Review":
            return f"Уже обработано (статус: {status})."
        format_map = {
            "post": "Post",
            "post_text": "Post Text",
            "reel": "Reel Idea",
            "carousel": "Carousel",
        }
        if decision in format_map:
            chosen = format_map[decision]
            at_update(table, record_id, {"Status": "Approved", "Format": chosen})
            return f"✅ Утверждено как «{chosen}»."
        if decision == "y":  # backward-compat with old Да/Нет cards
            at_update(table, record_id, {"Status": "Approved"})
            return "✅ Тема утверждена."
        at_update(table, record_id, {"Status": "Rejected"})
        return "❌ Тема отклонена."

    status = sel(fields, "Visual Status")
    fmt = format_of(fields)

    if status == "Brief Ready":
        # Цифра в ответе на карточку поста — «поставь на обложку вот этот
        # вариант». Нумерация — как в поле Photo Candidates и как в подписи
        # к карточке.
        if decision.startswith("nums:"):
            if not is_post_format(fmt):
                return "Номерами здесь выбирают обложку, а это не пост."
            wanted = int(decision.split(":", 1)[1].split(",")[0])
            candidates = parse_photo_candidates(sel(fields, CANDIDATES_FIELD))
            if not candidates:
                return "Пока не из чего выбирать: фото к этой теме не нашлись."
            if not 1 <= wanted <= len(candidates):
                return f"Есть варианты с 1 по {len(candidates)}."
            return set_cover_photo(
                record_id, candidates[wanted - 1]["url"], f"вариант {wanted}"
            )

        # «текст» на карточке поста — передумал, собери без фото.
        if decision == "y+text":
            if not is_post_format(fmt):
                return "Так отвечают только на карточку поста."
            at_update(table, record_id, {
                "Format": "Post Text",
                "Chosen Format": "Post Text",
                "Visual Status": "Approved Visual",
            })
            return "✅ Собираю без фото, только текстовые карточки."

        if decision in ("y", "y+reel05"):
            target = "Approved Visual" if is_post_format(fmt) else "Prompts Approved"
            fields_to_write = {"Visual Status": target}

            # Текстовый рилс собирается из готового текста поста, поэтому
            # здесь только отмечаем намерение: reel05_bot заберёт карточку
            # сам, когда пост дособран.
            if decision == "y+reel05":
                if not is_post_format(fmt):
                    return ("Текстовый рилс делается только из постов. "
                            "Ответь «да», чтобы просто утвердить.")
                fields_to_write[REEL05_FIELD] = True

            at_update(table, record_id, fields_to_write)

            if decision == "y+reel05":
                return f"✅ Утверждено → {target}. После поста соберу текстовый рилс."
            return f"✅ Утверждено → {target}."
        return reject_visual(record_id, fields, "готовые тексты")

    if status == "Needs Visual Review":
        if decision == "y":
            at_update(table, record_id, {"Visual Status": "Approved Visual"})
            return "✅ Визуал утверждён → Approved Visual."
        if decision.startswith("nums:"):
            slides = decision.split(":", 1)[1]
            # Only carousels have per-slide prompts to re-render from; the reel
            # pipeline works in keyframes and has its own gate.
            if "carousel" not in fmt:
                return "Переделка отдельных слайдов пока работает только для каруселей."
            at_update(table, record_id, {
                "Visual Status": "Redo Slides",
                "Redo Slides": slides,
            })
            return (
                f"🔁 Перерисую слайды {slides.replace(',', ', ')}. "
                f"Остальные остаются как есть — пришлю новую карточку."
            )
        return reject_visual(record_id, fields, "готовый визуал")

    return f"Уже обработано (статус: {status})."


# Card tag like [c:recXXXXXXXXXXXXXX] (content) / [v:rec...] (visual) —
# embedded in every card so a text REPLY can be matched back to the record.
TAG_RE = re.compile(r"\[(c|v):(rec[A-Za-z0-9]{14})\]")

CONTENT_WORDS = {
    "пост": "post", "post": "post",
    # Пост без иллюстраций — только текст на цветном фоне. Ответ приходит
    # уже со схлопнутыми пробелами вокруг «+», поэтому «пост + текст»
    # и «пост+текст» попадают в один ключ.
    "текст": "post_text", "пост+текст": "post_text",
    "пост текст": "post_text", "текстовый пост": "post_text",
    "пост без фото": "post_text", "без фото": "post_text",
    "рилс": "reel", "рил": "reel", "reel": "reel",
    "карусель": "carousel", "carousel": "carousel",
    "да": "y", "yes": "y", "ок": "y", "ok": "y", "+": "y",
    "нет": "n", "no": "n", "отмена": "n", "-": "n",
}
VISUAL_WORDS = {
    "да": "y", "yes": "y", "ок": "y", "ok": "y", "+": "y",
    "нет": "n", "no": "n", "отмена": "n", "-": "n",
    # Пост + текстовый рилс из того же текста (ветка Рил 05). Собрать рилс
    # можно только когда текст уже написан, поэтому решение принимается здесь,
    # на гейте готовых текстов, а не при согласовании темы.
    "да+рил": "y+reel05", "да+рил05": "y+reel05", "да+рилс": "y+reel05",
    "да+05": "y+reel05", "+рил": "y+reel05",
    # «текст» на карточке поста — собрать без фото, только текстовые карточки.
    "текст": "y+text", "без фото": "y+text", "да+текст": "y+text",
}

REEL05_FIELD = "Рил 05"


def parse_slide_numbers(text: str) -> list:
    """
    Slide numbers in a free-form reply, capped at 20 so a stray year or price
    pasted into the chat is not read as a re-render request. The production bot
    validates them again against the actual slide count.
    """
    numbers = []
    for token in re.findall(r"\d+", text or ""):
        number = int(token)
        if 1 <= number <= 20 and number not in numbers:
            numbers.append(number)
    return sorted(numbers)


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")


def looks_like_image_url(url: str) -> bool:
    """
    Похожа ли ссылка на саму картинку, а не на страницу.

    Нарочно строго: со страницы бот картинку не достанет, а молча взять
    на обложку HTML-страницу хуже, чем сказать, что ссылка не годится.
    """
    path = urlparse(url or "").path.lower()
    return path.endswith(IMAGE_SUFFIXES)


def handle_photo_reply(msg: dict) -> None:
    """
    Картинка в ответ на карточку поста — «вот эту на обложку».

    Файл скачивается из Телеграма и выкладывается на хостинг: ссылка
    Телеграма содержит токен бота и живёт недолго, а продакшн-бот забирает
    фото позже, на своём прогоне.
    """
    chat_id = msg.get("chat", {}).get("id")
    reply_to = msg.get("reply_to_message") or {}
    tag = TAG_RE.search(card_text_of(reply_to))

    def answer(text: str) -> None:
        tg("sendMessage", chat_id=chat_id,
           reply_to_message_id=msg.get("message_id"), text=text)

    if not tag:
        answer("Чтобы я понял, к какой карточке это фото — ответь им на саму "
               "карточку (свайп влево → «Ответить»).")
        return

    kind, record_id = tag.groups()
    if kind != "v":
        answer("Фото пригодится на карточке готовых текстов, а не при выборе темы.")
        return

    photos = msg.get("photo") or []
    document = msg.get("document") or {}
    if photos:
        # Телеграм присылает лестницу размеров, последний — самый большой.
        file_id, suffix = photos[-1].get("file_id"), ".jpg"
    elif str(document.get("mime_type", "")).startswith("image/"):
        file_id = document.get("file_id")
        suffix = Path(str(document.get("file_name") or "")).suffix or ".jpg"
    else:
        answer("Это не картинка. Пришли фото или файл-изображение.")
        return

    data = tg_download(file_id)
    if not data:
        answer("Не смог забрать файл из Телеграма. Пришли ещё раз.")
        return

    url = upload_own_photo(record_id, data, suffix)
    if not url:
        answer("Файл получил, но не смог выложить его на хостинг. "
               "Ссылку на картинку можно прислать текстом.")
        return

    result = set_cover_photo(record_id, url, "твоё фото")
    print("Cover by upload:", url, "->", result)
    answer(result)


def handle_text_reply(msg: dict) -> None:
    """A text message: if it is a REPLY to one of our cards, apply the word."""
    text = (msg.get("text") or "").strip().lower().rstrip(".!")
    if not text:
        return

    # «да + рил 05» и «да+рил05» — один и тот же ответ. Схлопываем пробелы,
    # иначе вариант с пробелами не найдётся в словаре и цифры из него уедут
    # в разбор номеров слайдов.
    text = re.sub(r"\s*\+\s*", "+", text)
    text = re.sub(r"(?<=[а-яё])\s+(?=\d)", "", text)

    reply_to = msg.get("reply_to_message") or {}
    tag = TAG_RE.search(card_text_of(reply_to))

    if not tag:
        # A bare command word without replying to a card — explain once.
        if text in CONTENT_WORDS or text in VISUAL_WORDS:
            tg("sendMessage", chat_id=msg.get("chat", {}).get("id"),
               text="Чтобы я понял, к какой карточке это относится — ответь этим словом на саму карточку (свайп влево → «Ответить»).")
        return

    kind, record_id = tag.groups()
    table_key = "content" if kind == "c" else "visual"
    words = CONTENT_WORDS if table_key == "content" else VISUAL_WORDS
    decision = words.get(text)

    chat_id = msg.get("chat", {}).get("id")

    # Ссылка на картинку в ответ на карточку поста — «вот эта на обложку».
    # Проверяем до слов: ссылка никогда не совпадёт с командой.
    if not decision and table_key == "visual":
        links = [
            url for url in extract_urls(msg.get("text") or "")
            if looks_like_image_url(url)
        ]
        if links:
            result = set_cover_photo(record_id, links[0], "картинку по ссылке")
            print("Cover by link:", links[0], "->", result)
            tg("sendMessage", chat_id=chat_id,
               reply_to_message_id=msg.get("message_id"), text=result)
            return

    # На визуальной карточке цифры значат одно из двух, смотря на каком гейте
    # стоит запись: на готовых текстах поста — какой вариант фото взять на
    # обложку, на готовом визуале карусели — какие слайды перерисовать.
    # apply_decision разбирается по статусу, здесь просто передаём числа.
    if not decision and table_key == "visual":
        numbers = parse_slide_numbers(text)
        if numbers:
            decision = "nums:" + ",".join(str(n) for n in numbers)

    if not decision:
        options = (
            "пост / текст / рилс / карусель / нет" if table_key == "content"
            else "да / нет / цифру (вариант фото или слайды на переделку)"
        )
        tg("sendMessage", chat_id=chat_id,
           reply_to_message_id=msg.get("message_id"),
           text=f"Не понял «{text}». Ответь: {options}")
        return

    result = apply_decision(decision, table_key, record_id)
    print("Text decision:", text, "->", result)
    tg("sendMessage", chat_id=chat_id,
       reply_to_message_id=msg.get("message_id"), text=result)


def handle_update(update: dict) -> None:
    message = update.get("message")
    if message:
        if message.get("photo") or message.get("document"):
            handle_photo_reply(message)
        else:
            handle_text_reply(message)
        return

    callback = update.get("callback_query")
    if not callback:
        return

    payload = callback.get("data") or ""
    print("Callback data:", payload)
    parts = payload.split("|")
    if len(parts) != 3:
        tg("answerCallbackQuery", callback_query_id=callback["id"], text="Непонятная кнопка")
        return

    decision, table_key, record_id = parts
    result = apply_decision(decision, table_key, record_id)
    print("Decision result:", result)

    tg("answerCallbackQuery", callback_query_id=callback["id"], text=result)
    message = callback.get("message", {})
    tg(
        "editMessageText",
        chat_id=message.get("chat", {}).get("id"),
        message_id=message.get("message_id"),
        text=(message.get("text", "") + f"\n\n— {result}"),
    )


def process_updates() -> int:
    me = (tg("getMe").get("result") or {})
    print("Polling bot:", me.get("id"), "@" + str(me.get("username")))

    # A webhook would silently divert updates away from getUpdates — remove it.
    webhook = (tg("getWebhookInfo").get("result") or {})
    print("Webhook url:", webhook.get("url") or "(none)",
          "| pending_update_count:", webhook.get("pending_update_count"))
    if webhook.get("url"):
        print("WARNING: webhook was set - deleting.")
        tg("deleteWebhook")

    # Drain loop. Two hard-won rules (proven live on 2026-07-06):
    # 1. allowed_updates MUST be [] (default = all common types). Passing an
    #    explicit list like ["message","callback_query"] made Telegram WITHHOLD
    #    queued callback_query updates from getUpdates while still counting
    #    them as pending — and the confirming call then destroyed them unread.
    #    The empty-list form was verified to deliver everything, repeatedly.
    # 2. Never ignore the result of a confirming call (offset=max+1): it can
    #    carry updates that arrived meanwhile, so each batch is processed until
    #    the queue is empty, and only then the final confirm is left standing.
    processed = 0
    offset = None

    while True:
        params = {"allowed_updates": []}
        if offset is not None:
            params["offset"] = offset
        data = tg("getUpdates", **params)
        if not data.get("ok", True):
            print("getUpdates NOT ok:", str(data)[:300])
            break

        updates = data.get("result", []) or []
        print("Batch:", [(u["update_id"], "callback" if u.get("callback_query") else "other") for u in updates])
        if not updates:
            break

        for update in updates:
            handle_update(update)
            processed += 1
            offset = update["update_id"] + 1

    return processed


# --------------------------------------------------- send approval cards ---

TG_CAPTION_LIMIT = 1024


def shorten_caption(text: str) -> str:
    """
    Подпись к картинке Телеграм ограничивает 1024 знаками. Подрезаем так,
    чтобы метка [v:rec...] осталась целой: без неё ответ не найдёт запись.
    """
    if len(text) <= TG_CAPTION_LIMIT:
        return text
    tag_at = text.rfind("[")
    tag = text[tag_at:] if tag_at >= 0 else ""
    keep = TG_CAPTION_LIMIT - len(tag) - 3
    return text[:max(0, keep)].rstrip() + "…\n\n" + tag


def send_card(text: str, keyboard_rows: list = None, photo_url: str = "") -> None:
    """
    Отправить карточку. С photo_url она уходит картинкой — так владелец
    видит обложку сразу в переписке. Если картинку отправить не удалось
    (ссылка ещё холодная, хостинг недоступен), уходит текстом со ссылкой:
    молча потерять карточку хуже, чем показать её без картинки.
    """
    if photo_url:
        params = {
            "chat_id": CHAT_ID,
            "photo": photo_url,
            "caption": shorten_caption(text),
        }
        if keyboard_rows:
            params["reply_markup"] = {"inline_keyboard": keyboard_rows}
        result = tg("sendPhoto", **params) or {}
        if result.get("ok"):
            return
        print("sendPhoto не прошёл, отправляю текстом:", str(result)[:200])
        text = f"{text}\n\nОбложка: {photo_url}"

    params = {"chat_id": CHAT_ID, "text": text}
    if keyboard_rows:
        params["reply_markup"] = {"inline_keyboard": keyboard_rows}
    tg("sendMessage", **params)


def extract_urls(text: str) -> list:
    return re.findall(r"https?://[^\s|]+", text or "")


def gate_fingerprint(fields: dict, status: str) -> str:
    """
    The "already asked" marker: gate status + a short hash of the editable
    content. Rule: a record sitting at a gate untouched is never re-sent, but
    ANY edit by the owner (rewriting a rejected topic and re-queueing it)
    changes the hash, so the bridge asks again.

    NOTIFIED_FIELD itself is deliberately not part of the hash — otherwise
    writing the marker would change it and the card would re-send forever.
    """
    payload = "|".join([
        status,
        sel(fields, "Title"),
        sel(fields, "HOOK"),
        sel(fields, "Final Caption"),
        sel(fields, "Job Title"),
        sel(fields, "Final Reel Caption"),
        sel(fields, "Slide Copy"),
        # Re-rendered slides change nothing in the texts above, so without this
        # a job coming back from "Redo Slides" could match its own pre-redo
        # fingerprint and never get a fresh card. clear_stale_markers() usually
        # wipes the marker while the job is away from the gate, but that is a
        # race against the production bot — this makes it deterministic.
        sel(fields, "Output Links"),
        format_of(fields),
    ])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]
    return f"{status}|{digest}"


def fetch_gate_records() -> tuple:
    """
    One query per table, covering BOTH things the bridge reads Airtable for:
    records sitting at an approval gate (may need a card) and records that
    have left a gate but still carry a marker (needs clearing).

    Doing those as two separate passes meant four queries every run on a bot
    that runs every ten minutes — around 570 requests a day, almost all of
    them finding nothing. That was the single biggest consumer of the base's
    monthly request budget, and it is what exhausted it on 2026-08-10.
    """
    content = at_list(
        CONTENT_TABLE,
        f"OR({{Status}}='Needs Review', {{{NOTIFIED_FIELD}}}!='')",
    )
    visual = at_list(
        VISUAL_TABLE,
        f"OR({{Visual Status}}='Brief Ready', {{Visual Status}}='Needs Visual Review',"
        f" {{{NOTIFIED_FIELD}}}!='')",
    )
    return content, visual


def clear_stale_markers(content_records: list, visual_records: list) -> int:
    """
    Wipe the "already asked" marker from records that have moved past a gate.

    Without this, a record that LEAVES a gate and later RETURNS to the same
    status (owner re-opens a rejected topic, or reuses the row for a new one)
    keeps its old marker, it matches the status again, and the card is never
    re-sent — the record hangs silently forever.

    Filtering happens here rather than in the query, so this shares the single
    fetch from fetch_gate_records() instead of asking Airtable again.
    """
    cleared = 0
    gates = {
        CONTENT_TABLE: ("Status", {"Needs Review"}),
        VISUAL_TABLE: ("Visual Status", {"Brief Ready", "Needs Visual Review"}),
    }

    for table, records in ((CONTENT_TABLE, content_records), (VISUAL_TABLE, visual_records)):
        status_field, gate_statuses = gates[table]
        for record in records:
            fields = record.get("fields", {})
            if not sel(fields, NOTIFIED_FIELD):
                continue
            if sel(fields, status_field) in gate_statuses:
                continue
            at_update(table, record["id"], {NOTIFIED_FIELD: ""})
            cleared += 1
    return cleared


def notify_pending(content_records: list, visual_records: list) -> int:
    sent = 0

    # Content Inbox — new topics awaiting review.
    for record in content_records:
        fields = record.get("fields", {})
        if sel(fields, "Status") != "Needs Review":
            continue
        fingerprint = gate_fingerprint(fields, "Needs Review")
        if sel(fields, NOTIFIED_FIELD) == fingerprint:
            continue
        title = sel(fields, "Title") or "Без названия"
        hook = sel(fields, "HOOK")
        caption = (sel(fields, "Final Caption") or "")[:500]
        rid = record["id"]
        # Потенциал хука по данным разведки конкурентов (если есть).
        score = fields.get("Hook Score")
        evidence = sel(fields, "Hook Evidence")
        score_block = ""
        if isinstance(score, (int, float)) and score > 0:
            score_block = f"🎯 Потенциал хука: {int(score)}/10\n{evidence}\n\n"
        text = (
            f"🆕 Новая тема на согласование\n\n{title}\n\n{score_block}{hook}\n\n{caption}\n\n"
            f"💬 Ответь на ЭТО сообщение (свайп → «Ответить»):\n"
            f"пост — карточки с фото по теме\n"
            f"текст — карточки только из текста, без фото\n"
            f"рилс / карусель / нет\n\n"
            f"[c:{rid}]"
        )
        send_card(text)
        at_update(CONTENT_TABLE, record["id"], {NOTIFIED_FIELD: fingerprint})
        sent += 1

    # Visual Jobs — brief review (reels/posts) + generated-visual review.
    for record in visual_records:
        fields = record.get("fields", {})
        status = sel(fields, "Visual Status")
        if status not in ("Brief Ready", "Needs Visual Review"):
            continue
        fmt = format_of(fields)

        # Carousel "Brief Ready" is auto-processed — not a human gate.
        if status == "Brief Ready" and not ("reel" in fmt or is_post_format(fmt)):
            continue
        fingerprint = gate_fingerprint(fields, status)
        if sel(fields, NOTIFIED_FIELD) == fingerprint:
            continue

        job_title = sel(fields, "Job Title") or "Без названия"
        rid = record["id"]
        cover_url = ""
        footer = (
            f"💬 Ответь на ЭТО сообщение (свайп → «Ответить»): да / нет\n\n"
            f"[v:{rid}]"
        )
        if status == "Brief Ready":
            preview = (sel(fields, "Final Reel Caption") or sel(fields, "Slide Copy") or "")[:600]
            # Текстовый рилс собирается из текста поста, поэтому предлагается
            # только постам и только здесь — раньше текста ещё нет.
            if is_post_format(fmt):
                cover_url = sel(fields, COVER_PREVIEW_FIELD).strip()
                candidates = parse_photo_candidates(sel(fields, CANDIDATES_FIELD))

                if (
                    fmt == "post"
                    and not cover_url
                    and record_age_minutes(record) < COVER_WAIT_MINUTES
                ):
                    # Обложка ещё собирается. Ждём её, чтобы прислать ОДНУ
                    # карточку с картинкой, а не сначала текстовую, а потом
                    # такую же с обложкой. Через COVER_WAIT_MINUTES карточка
                    # уйдёт и без обложки — чтобы пост не завис молча.
                    print("Жду обложку для", rid)
                    continue

                choices = ""
                if cover_url and len(candidates) > 1:
                    choices = (
                        f"цифра 2–{len(candidates)} — другое найденное фото\n"
                    )
                footer = (
                    f"💬 Ответь на ЭТО сообщение (свайп → «Ответить»):\n"
                    f"да — собрать и отправить в Buffer\n"
                    f"да+рил — то же, и следом текстовый рилс\n"
                    + choices +
                    f"своё фото или ссылку на картинку — поставлю на обложку\n"
                    f"текст — собрать без фото\n"
                    f"нет — отклонить\n\n"
                    f"[v:{rid}]"
                )
            head = (
                "🖼 Обложка и тексты готовы"
                if cover_url
                else f"📝 Тексты/промпты готовы ({fmt})"
            )
            text = (
                f"{head}\n\n{job_title}\n\n{preview}\n\n"
                f"Утвердить и запустить генерацию визуала?\n\n{footer}"
            )
        else:
            urls = extract_urls(sel(fields, "Output Links"))
            links = "\n".join(urls[:8]) if urls else "(ссылки — в карточке Airtable)"
            if "carousel" in fmt:
                footer = (
                    f"💬 Ответь на ЭТО сообщение (свайп → «Ответить»):\n"
                    f"да — утвердить\n"
                    f"номера слайдов (например: 2 5) — перерисовать только их\n"
                    f"нет — править руками в Airtable\n\n"
                    f"[v:{rid}]"
                )
            text = f"🖼 Визуал готов ({fmt})\n\n{job_title}\n\nПосмотри и утверди:\n{links}\n\n{footer}"

        send_card(text, photo_url=cover_url)
        at_update(VISUAL_TABLE, record["id"], {NOTIFIED_FIELD: fingerprint})
        sent += 1

    return sent


def main() -> None:
    print("Telegram bridge started.")

    # Read the two tables ONCE and reuse the result for both passes.
    #
    # The fetch now happens before decisions are applied, so notify_pending()
    # works from a snapshot taken a moment earlier. That is safe: a record
    # whose decision was just applied still carries the marker matching its
    # card, so it is skipped as "already asked" rather than asked twice. And
    # no bridge decision ever moves a record INTO a gate, so nothing that
    # deserves a card this run can be missing from the snapshot.
    content_records, visual_records = fetch_gate_records()
    print("Gate records read:", len(content_records), "content,", len(visual_records), "visual")

    print("Stale markers cleared:", clear_stale_markers(content_records, visual_records))
    processed = process_updates()
    print("Processed Telegram updates:", processed)
    sent = notify_pending(content_records, visual_records)
    print("Approval cards sent:", sent)
    print("Telegram bridge done.")


if __name__ == "__main__":
    main()
