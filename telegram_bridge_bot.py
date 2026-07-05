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

import os
import re
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


def at_list(table: str, formula: str) -> list:
    response = requests.get(
        at_url(table), headers=at_headers(),
        params={"filterByFormula": formula, "pageSize": 50}, timeout=30,
    )
    if response.status_code != 200:
        print("Airtable list failed:", table, response.status_code, response.text[:300])
        return []
    return response.json().get("records", [])


def at_get(table: str, record_id: str):
    response = requests.get(f"{at_url(table)}/{record_id}", headers=at_headers(), timeout=30)
    return response.json() if response.status_code == 200 else None


def at_update(table: str, record_id: str, fields: dict) -> bool:
    response = requests.patch(
        f"{at_url(table)}/{record_id}", headers=at_headers(write=True),
        json={"fields": fields, "typecast": True}, timeout=30,
    )
    print("Airtable update:", table, record_id, fields, "->", response.status_code)
    return response.status_code in (200, 201)


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
    if not record:
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

    # Explicitly request callback_query: Telegram REMEMBERS the last
    # allowed_updates, and the old (text-based) bridge may have restricted it
    # to ["message"], which silently drops button taps.
    data = tg("getUpdates", allowed_updates=["message", "callback_query"])
    if not data.get("ok", True):
        print("getUpdates NOT ok:", str(data)[:300])
    updates = data.get("result", []) or []
    print("Raw updates:", [(u["update_id"], "callback" if u.get("callback_query") else "other") for u in updates])
    max_update_id = None

    for update in updates:
        max_update_id = update["update_id"]
        callback = update.get("callback_query")
        if not callback:
            continue

        payload = callback.get("data") or ""
        print("Callback data:", payload)
        parts = payload.split("|")
        if len(parts) != 3:
            tg("answerCallbackQuery", callback_query_id=callback["id"], text="Непонятная кнопка")
            continue

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

    # Confirm processed updates so the next run does not see them again.
    if max_update_id is not None:
        tg("getUpdates", offset=max_update_id + 1, allowed_updates=["message", "callback_query"])

    return len(updates)


# --------------------------------------------------- send approval cards ---

def send_card(text: str, keyboard_rows: list) -> None:
    tg("sendMessage", chat_id=CHAT_ID, text=text,
       reply_markup={"inline_keyboard": keyboard_rows})


def extract_urls(text: str) -> list:
    return re.findall(r"https?://[^\s|]+", text or "")


def notify_pending() -> int:
    sent = 0

    # Content Inbox — new topics awaiting review.
    for record in at_list(CONTENT_TABLE, "{Status}='Needs Review'"):
        fields = record["fields"]
        if sel(fields, NOTIFIED_FIELD) == "Needs Review":
            continue
        title = sel(fields, "Title") or "Без названия"
        hook = sel(fields, "HOOK")
        caption = (sel(fields, "Final Caption") or "")[:500]
        text = (
            f"🆕 Новая тема на согласование\n\n{title}\n\n{hook}\n\n{caption}\n\n"
            f"В каком формате развить?"
        )
        rid = record["id"]
        keyboard = [
            [
                {"text": "📝 Post", "callback_data": f"post|content|{rid}"},
                {"text": "🎬 Reel Idea", "callback_data": f"reel|content|{rid}"},
                {"text": "🖼 Carousel", "callback_data": f"carousel|content|{rid}"},
            ],
            [{"text": "❌ Нет", "callback_data": f"n|content|{rid}"}],
        ]
        send_card(text, keyboard)
        at_update(CONTENT_TABLE, record["id"], {NOTIFIED_FIELD: "Needs Review"})
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
        if sel(fields, NOTIFIED_FIELD) == status:
            continue

        job_title = sel(fields, "Job Title") or "Без названия"
        if status == "Brief Ready":
            preview = (sel(fields, "Final Reel Caption") or sel(fields, "Slide Copy") or "")[:600]
            text = (
                f"📝 Тексты/промпты готовы ({fmt})\n\n{job_title}\n\n{preview}\n\n"
                f"Утвердить и запустить генерацию визуала?"
            )
        else:
            urls = extract_urls(sel(fields, "Output Links"))
            links = "\n".join(urls[:8]) if urls else "(ссылки — в карточке Airtable)"
            text = f"🖼 Визуал готов ({fmt})\n\n{job_title}\n\nПосмотри и утверди:\n{links}"

        rid = record["id"]
        keyboard = [[
            {"text": "✅ Да", "callback_data": f"y|visual|{rid}"},
            {"text": "❌ Нет", "callback_data": f"n|visual|{rid}"},
        ]]
        send_card(text, keyboard)
        at_update(VISUAL_TABLE, record["id"], {NOTIFIED_FIELD: status})
        sent += 1

    return sent


def main() -> None:
    print("Telegram bridge started.")
    processed = process_updates()
    print("Processed Telegram updates:", processed)
    sent = notify_pending()
    print("Approval cards sent:", sent)
    print("Telegram bridge done.")


if __name__ == "__main__":
    main()
