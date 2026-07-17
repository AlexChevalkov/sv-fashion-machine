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
import time
from urllib.parse import quote

import requests


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
        format_map = {"post": "Post", "reel": "Reel Idea", "carousel": "Carousel"}
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
        if decision == "y":
            target = "Approved Visual" if fmt == "post" else "Prompts Approved"
            at_update(table, record_id, {"Visual Status": target})
            return f"✅ Утверждено → {target}."
        return "❌ Отклонено. Поправь тексты/промпты в Airtable."

    if status == "Needs Visual Review":
        if decision == "y":
            at_update(table, record_id, {"Visual Status": "Approved Visual"})
            return "✅ Визуал утверждён → Approved Visual."
        return "❌ Отклонено. Поправь визуал в Airtable."

    return f"Уже обработано (статус: {status})."


# Card tag like [c:recXXXXXXXXXXXXXX] (content) / [v:rec...] (visual) —
# embedded in every card so a text REPLY can be matched back to the record.
TAG_RE = re.compile(r"\[(c|v):(rec[A-Za-z0-9]{14})\]")

CONTENT_WORDS = {
    "пост": "post", "post": "post",
    "рилс": "reel", "рил": "reel", "reel": "reel",
    "карусель": "carousel", "carousel": "carousel",
    "да": "y", "yes": "y", "ок": "y", "ok": "y", "+": "y",
    "нет": "n", "no": "n", "отмена": "n", "-": "n",
}
VISUAL_WORDS = {
    "да": "y", "yes": "y", "ок": "y", "ok": "y", "+": "y",
    "нет": "n", "no": "n", "отмена": "n", "-": "n",
}


def handle_text_reply(msg: dict) -> None:
    """A text message: if it is a REPLY to one of our cards, apply the word."""
    text = (msg.get("text") or "").strip().lower().rstrip(".!")
    if not text:
        return

    reply_to = msg.get("reply_to_message") or {}
    tag = TAG_RE.search(reply_to.get("text") or "")

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
    if not decision:
        options = "пост / рилс / карусель / нет" if table_key == "content" else "да / нет"
        tg("sendMessage", chat_id=chat_id,
           reply_to_message_id=msg.get("message_id"),
           text=f"Не понял «{text}». Ответь одним словом: {options}")
        return

    result = apply_decision(decision, table_key, record_id)
    print("Text decision:", text, "->", result)
    tg("sendMessage", chat_id=chat_id,
       reply_to_message_id=msg.get("message_id"), text=result)


def handle_update(update: dict) -> None:
    message = update.get("message")
    if message:
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

def send_card(text: str, keyboard_rows: list = None) -> None:
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
        format_of(fields),
    ])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]
    return f"{status}|{digest}"


def clear_stale_markers() -> int:
    """
    Wipe the "already asked" marker from records that have moved past a gate.

    Without this, a record that LEAVES a gate and later RETURNS to the same
    status (owner re-opens a rejected topic, or reuses the row for a new one)
    keeps its old marker, it matches the status again, and the card is never
    re-sent — the record hangs silently forever.
    """
    cleared = 0
    targets = (
        (CONTENT_TABLE, f"AND({{{NOTIFIED_FIELD}}} != '', {{Status}} != 'Needs Review')"),
        (VISUAL_TABLE, f"AND({{{NOTIFIED_FIELD}}} != '', {{Visual Status}} != 'Brief Ready',"
                       f" {{Visual Status}} != 'Needs Visual Review')"),
    )
    for table, formula in targets:
        for record in at_list(table, formula):
            at_update(table, record["id"], {NOTIFIED_FIELD: ""})
            cleared += 1
    return cleared


def notify_pending() -> int:
    sent = 0

    # Content Inbox — new topics awaiting review.
    for record in at_list(CONTENT_TABLE, "{Status}='Needs Review'"):
        fields = record["fields"]
        fingerprint = gate_fingerprint(fields, "Needs Review")
        if sel(fields, NOTIFIED_FIELD) == fingerprint:
            continue
        title = sel(fields, "Title") or "Без названия"
        hook = sel(fields, "HOOK")
        caption = (sel(fields, "Final Caption") or "")[:500]
        rid = record["id"]
        text = (
            f"🆕 Новая тема на согласование\n\n{title}\n\n{hook}\n\n{caption}\n\n"
            f"💬 Ответь на ЭТО сообщение одним словом (свайп → «Ответить»):\n"
            f"пост / рилс / карусель / нет\n\n"
            f"[c:{rid}]"
        )
        send_card(text)
        at_update(CONTENT_TABLE, record["id"], {NOTIFIED_FIELD: fingerprint})
        sent += 1

    # Visual Jobs — brief review (reels/posts) + generated-visual review.
    formula = "OR({Visual Status}='Brief Ready',{Visual Status}='Needs Visual Review')"
    for record in at_list(VISUAL_TABLE, formula):
        fields = record["fields"]
        status = sel(fields, "Visual Status")
        fmt = format_of(fields)

        # Carousel "Brief Ready" is auto-processed — not a human gate.
        if status == "Brief Ready" and not ("reel" in fmt or fmt == "post"):
            continue
        fingerprint = gate_fingerprint(fields, status)
        if sel(fields, NOTIFIED_FIELD) == fingerprint:
            continue

        job_title = sel(fields, "Job Title") or "Без названия"
        rid = record["id"]
        footer = (
            f"💬 Ответь на ЭТО сообщение (свайп → «Ответить»): да / нет\n\n"
            f"[v:{rid}]"
        )
        if status == "Brief Ready":
            preview = (sel(fields, "Final Reel Caption") or sel(fields, "Slide Copy") or "")[:600]
            text = (
                f"📝 Тексты/промпты готовы ({fmt})\n\n{job_title}\n\n{preview}\n\n"
                f"Утвердить и запустить генерацию визуала?\n\n{footer}"
            )
        else:
            urls = extract_urls(sel(fields, "Output Links"))
            links = "\n".join(urls[:8]) if urls else "(ссылки — в карточке Airtable)"
            text = f"🖼 Визуал готов ({fmt})\n\n{job_title}\n\nПосмотри и утверди:\n{links}\n\n{footer}"

        send_card(text)
        at_update(VISUAL_TABLE, record["id"], {NOTIFIED_FIELD: fingerprint})
        sent += 1

    return sent


def main() -> None:
    print("Telegram bridge started.")
    print("Stale markers cleared:", clear_stale_markers())
    processed = process_updates()
    print("Processed Telegram updates:", processed)
    sent = notify_pending()
    print("Approval cards sent:", sent)
    print("Telegram bridge done.")


if __name__ == "__main__":
    main()
