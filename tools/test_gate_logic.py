"""
Проверки гейтов Телеграма без обращений к Airtable, Телеграму и хостингу.

Запуск из корня репозитория:  python tools/test_gate_logic.py

Покрыто: выбор формата темы (пост с фото / только текст), отказ по
визуальной работе, выбор обложки поста (цифра, своё фото, ссылка) и порядок
фото при сборке карусели.
"""
import datetime as _dt
import os
import sys
import types
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)

for key, value in {
    "TELEGRAM_BOT_TOKEN": "test",
    "TELEGRAM_CHAT_ID": "1",
    "AIRTABLE_API_KEY": "test",
    "AIRTABLE_BASE_ID": "app00000000000000",
    "ANTHROPIC_API_KEY": "test",
    "KREA_API_KEY": "test",
    "R2_ACCESS_KEY_ID": "test",
    "R2_SECRET_ACCESS_KEY": "test",
    "R2_ENDPOINT": "https://r2.example.com",
    "R2_PUBLIC_BASE": "https://pub-test.r2.dev",
}.items():
    os.environ.setdefault(key, value)

# anthropic нужен ботам только для запросов к Claude, которых здесь нет.
if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")
    _stub.Anthropic = lambda **kwargs: None
    sys.modules["anthropic"] = _stub

import telegram_bridge_bot as b
import visual_production_bot as p

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: получено {got!r}, ожидалось {want!r}")


updates = []
store = {}
sent_cards = []


def fake_update(table, record_id, fields):
    updates.append((table, record_id, dict(fields)))
    return True


def fake_get(table, record_id):
    return store.get((table, record_id), "missing")


def fake_send_card(text, keyboard_rows=None, photo_url=""):
    sent_cards.append({"text": text, "photo": photo_url})


b.at_update = fake_update
b.at_get = fake_get
b.send_card = fake_send_card
b.tg = lambda method, **params: {"ok": True}


# --- 1. Выбор формата при согласовании темы -------------------------------
check("слово 'пост'", b.CONTENT_WORDS.get("пост"), "post")
check("слово 'текст'", b.CONTENT_WORDS.get("текст"), "post_text")
check("слово 'пост+текст'", b.CONTENT_WORDS.get("пост+текст"), "post_text")
check("слово 'без фото'", b.CONTENT_WORDS.get("без фото"), "post_text")

store[(b.CONTENT_TABLE, "rec00000000000001")] = {
    "fields": {"Status": "Needs Review", "Title": "Тема"}
}
updates.clear()
answer = b.apply_decision("post_text", "content", "rec00000000000001")
check("тема → Post Text", updates[-1][2], {"Status": "Approved", "Format": "Post Text"})
check("ответ про Post Text", answer, "✅ Утверждено как «Post Text».")

store[(b.CONTENT_TABLE, "rec00000000000002")] = {
    "fields": {"Status": "Needs Review", "Title": "Тема"}
}
updates.clear()
b.apply_decision("post", "content", "rec00000000000002")
check("тема → Post", updates[-1][2], {"Status": "Approved", "Format": "Post"})

# --- 2. Текстовый пост ведёт себя как пост на гейте брифа -----------------
check("is_post_format('post')", b.is_post_format("post"), True)
check("is_post_format('post text')", b.is_post_format("Post Text"), True)
check("is_post_format('carousel')", b.is_post_format("carousel"), False)

store[(b.VISUAL_TABLE, "rec00000000000010")] = {
    "fields": {"Visual Status": "Brief Ready", "Format": "Post Text"}
}
updates.clear()
b.apply_decision("y", "visual", "rec00000000000010")
check(
    "Post Text «да» → Approved Visual",
    updates[-1][2],
    {"Visual Status": "Approved Visual"},
)

store[(b.VISUAL_TABLE, "rec00000000000011")] = {
    "fields": {"Visual Status": "Brief Ready", "Format": "Post Text"}
}
updates.clear()
b.apply_decision("y+reel05", "visual", "rec00000000000011")
check(
    "Post Text «да+рил» → рилс заказан",
    updates[-1][2],
    {"Visual Status": "Approved Visual", "Рил 05": True},
)

store[(b.VISUAL_TABLE, "rec00000000000012")] = {
    "fields": {"Visual Status": "Brief Ready", "Format": "Reel Idea"}
}
updates.clear()
b.apply_decision("y", "visual", "rec00000000000012")
check(
    "рилс «да» → Prompts Approved",
    updates[-1][2],
    {"Visual Status": "Prompts Approved"},
)

# --- 3. Отказ уводит работу в Failed --------------------------------------
store[(b.VISUAL_TABLE, "rec00000000000020")] = {
    "fields": {
        "Visual Status": "Brief Ready",
        "Format": "Post",
        "Render Notes": "старая заметка",
        "Source Content ID": "rec00000000000021",
    }
}
store[(b.CONTENT_TABLE, "rec00000000000021")] = {"fields": {"Status": "Approved"}}
updates.clear()
answer = b.apply_decision("n", "visual", "rec00000000000020")
check("отказ на брифе → Failed", updates[0][2]["Visual Status"], "Failed")
check("старая заметка сохранена", "старая заметка" in updates[0][2]["Render Notes"], True)
check("причина записана", "Отклонено в Телеграме" in updates[0][2]["Render Notes"], True)
check("тема → Rejected", updates[1], (b.CONTENT_TABLE, "rec00000000000021", {"Status": "Rejected"}))
check("ответ упоминает тему", "Тему тоже пометил отклонённой" in answer, True)

# Тему, которая уже ушла в Buffer, не трогаем.
store[(b.VISUAL_TABLE, "rec00000000000030")] = {
    "fields": {
        "Visual Status": "Needs Visual Review",
        "Format": "Post",
        "Source Content ID": "rec00000000000031",
    }
}
store[(b.CONTENT_TABLE, "rec00000000000031")] = {"fields": {"Status": "Sent to Buffer"}}
updates.clear()
answer = b.apply_decision("n", "visual", "rec00000000000030")
check("отказ на визуале → Failed", updates[0][2]["Visual Status"], "Failed")
check("опубликованную тему не трогаем", len(updates), 1)
check("ответ без упоминания темы", "Тему тоже" in answer, False)

# Отказ без привязки к теме не падает.
store[(b.VISUAL_TABLE, "rec00000000000040")] = {
    "fields": {"Visual Status": "Needs Visual Review", "Format": "Carousel"}
}
updates.clear()
b.apply_decision("n", "visual", "rec00000000000040")
check("отказ без Source Content ID", len(updates), 1)

# Утверждение и переделка слайдов не сломались.
store[(b.VISUAL_TABLE, "rec00000000000050")] = {
    "fields": {"Visual Status": "Needs Visual Review", "Format": "Carousel"}
}
updates.clear()
b.apply_decision("nums:2,5", "visual", "rec00000000000050")
check(
    "переделка слайдов",
    updates[-1][2],
    {"Visual Status": "Redo Slides", "Redo Slides": "2,5"},
)


# --- Метка карточки читается и из подписи к картинке --------------------
check("метка из text", b.card_text_of({"text": "a [v:rec00000000000001]"}),
      "a [v:rec00000000000001]")
check("метка из caption", b.card_text_of({"caption": "b [v:rec00000000000002]"}),
      "b [v:rec00000000000002]")
check("метка находится в подписи",
      bool(b.TAG_RE.search(b.card_text_of({"caption": "x [v:rec00000000000002]"}))),
      True)

# --- Список вариантов фото: продакшн-бот пишет, мост читает -------------
photos = [
    {"url": "https://pub-test.r2.dev/a.jpg", "credit": "Vogue"},
    {"url": "https://pub-test.r2.dev/b.jpg", "credit": ""},
    {"url": "https://pub-test.r2.dev/c.jpg", "credit": "BoF"},
]
raw = p.format_photo_candidates(photos)
check("формат списка", raw.splitlines()[0], "1. https://pub-test.r2.dev/a.jpg | Vogue")
check("вариант без подписи", raw.splitlines()[1], "2. https://pub-test.r2.dev/b.jpg")
check("продакшн-бот читает свой список", p.parse_photo_candidates(raw), photos)
check("мост читает тот же список", b.parse_photo_candidates(raw), photos)
check("мусор не ломает разбор",
      b.parse_photo_candidates("не ссылка\n1. ftp://x\n2. https://ok.jpg | A"),
      [{"url": "https://ok.jpg", "credit": "A"}])

# --- Цифра на карточке поста меняет обложку ----------------------------
def post_brief(record_id, fmt="Post", candidates=raw, cover=""):
    store[(b.VISUAL_TABLE, record_id)] = {
        "fields": {
            "Visual Status": "Brief Ready",
            "Format": fmt,
            "Photo Candidates": candidates,
            "Cover Photo URL": cover,
        }
    }


post_brief("rec00000000000010")
updates.clear()
answer = b.apply_decision("nums:3", "visual", "rec00000000000010")
check("цифра 3 → третий вариант на обложку", updates[-1][2], {
    "Cover Photo URL": "https://pub-test.r2.dev/c.jpg",
    "Cover Preview URL": "",
})
check("ответ про вариант", "вариант 3" in answer, True)

post_brief("rec00000000000011")
updates.clear()
answer = b.apply_decision("nums:9", "visual", "rec00000000000011")
check("цифра вне списка — без записи", len(updates), 0)
check("ответ про диапазон", "с 1 по 3" in answer, True)

post_brief("rec00000000000012", candidates="")
updates.clear()
answer = b.apply_decision("nums:2", "visual", "rec00000000000012")
check("нет вариантов — без записи", len(updates), 0)
check("ответ про отсутствие фото", "не нашлись" in answer, True)

post_brief("rec00000000000013", fmt="Reel Idea")
updates.clear()
answer = b.apply_decision("nums:2", "visual", "rec00000000000013")
check("цифры на рилсе — без записи", len(updates), 0)
check("ответ: это не пост", "не пост" in answer, True)

# Переделка слайдов карусели работает как раньше.
store[(b.VISUAL_TABLE, "rec00000000000014")] = {
    "fields": {"Visual Status": "Needs Visual Review", "Format": "Carousel"}
}
updates.clear()
b.apply_decision("nums:2,5", "visual", "rec00000000000014")
check("карусель: цифры → переделать слайды", updates[-1][2],
      {"Visual Status": "Redo Slides", "Redo Slides": "2,5"})

# --- «текст» на карточке поста -----------------------------------------
post_brief("rec00000000000020")
updates.clear()
answer = b.apply_decision("y+text", "visual", "rec00000000000020")
check("«текст» → собрать без фото", updates[-1][2], {
    "Format": "Post Text",
    "Chosen Format": "Post Text",
    "Visual Status": "Approved Visual",
})
check("слово «текст» распознаётся", b.VISUAL_WORDS.get("текст"), "y+text")

post_brief("rec00000000000021", fmt="Reel Idea")
updates.clear()
b.apply_decision("y+text", "visual", "rec00000000000021")
check("«текст» на рилсе — без записи", len(updates), 0)

# --- Обложку меняем только до сборки -----------------------------------
store[(b.VISUAL_TABLE, "rec00000000000030")] = {
    "fields": {"Visual Status": "Sent for Buffer", "Format": "Post"}
}
updates.clear()
answer = b.set_cover_photo("rec00000000000030", "https://x/y.jpg", "твоё фото")
check("после сборки обложку не меняем", len(updates), 0)
check("ответ про статус", "Sent for Buffer" in answer, True)

post_brief("rec00000000000031")
updates.clear()
answer = b.set_cover_photo("rec00000000000031", "https://x/y.jpg", "твоё фото")
check("своё фото на обложку", updates[-1][2], {
    "Cover Photo URL": "https://x/y.jpg",
    "Cover Preview URL": "",
})

# --- Ссылка на картинку ------------------------------------------------
check("jpg — картинка", b.looks_like_image_url("https://a.com/x/y.JPG?v=2"), True)
check("webp — картинка", b.looks_like_image_url("https://a.com/y.webp"), True)
check("страница — не картинка", b.looks_like_image_url("https://a.com/news/item"), False)

# --- Подпись к картинке не теряет метку --------------------------------
long_text = "ю" * 2000 + "\n\n[v:rec00000000000040]"
short = b.shorten_caption(long_text)
check("подпись укладывается в лимит", len(short) <= b.TG_CAPTION_LIMIT, True)
check("метка сохранилась", "[v:rec00000000000040]" in short, True)
check("короткую подпись не трогаем", b.shorten_caption("мало [v:rec1]"), "мало [v:rec1]")

# --- Карточка поста: ждём обложку, потом шлём картинкой -----------------
now = _dt.datetime.now(_dt.timezone.utc)


def visual_record(record_id, created, fields):
    return {"id": record_id, "createdTime": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "fields": fields}


fresh_no_cover = visual_record(
    "rec00000000000050", now - _dt.timedelta(minutes=5),
    {"Visual Status": "Brief Ready", "Format": "Post", "Job Title": "Свежий",
     "Slide Copy": "текст"},
)
old_no_cover = visual_record(
    "rec00000000000051", now - _dt.timedelta(days=3),
    {"Visual Status": "Brief Ready", "Format": "Post", "Job Title": "Старый",
     "Slide Copy": "текст"},
)
with_cover = visual_record(
    "rec00000000000052", now - _dt.timedelta(minutes=40),
    {"Visual Status": "Brief Ready", "Format": "Post", "Job Title": "С обложкой",
     "Slide Copy": "текст", "Cover Preview URL": "https://pub-test.r2.dev/cover.png",
     "Photo Candidates": raw},
)
text_post = visual_record(
    "rec00000000000053", now - _dt.timedelta(minutes=5),
    {"Visual Status": "Brief Ready", "Format": "Post Text", "Job Title": "Текстовый",
     "Slide Copy": "текст"},
)

sent_cards.clear()
updates.clear()
count = b.notify_pending([], [fresh_no_cover, old_no_cover, with_cover, text_post])
titles = [c["text"].split("\n\n")[1] for c in sent_cards]
check("свежий пост без обложки — ждём", "Свежий" in titles, False)
check("старый пост без обложки — карточка уходит", "Старый" in titles, True)
check("пост с обложкой — карточка уходит", "С обложкой" in titles, True)
check("текстовый пост не ждёт обложку", "Текстовый" in titles, True)
check("карточек отправлено", count, 3)

by_title = {c["text"].split("\n\n")[1]: c for c in sent_cards}
check("обложка ушла картинкой", by_title["С обложкой"]["photo"],
      "https://pub-test.r2.dev/cover.png")
check("без обложки — без картинки", by_title["Старый"]["photo"], "")
check("предложен выбор варианта", "цифра 2–3" in by_title["С обложкой"]["text"], True)
check("предложено своё фото", "своё фото" in by_title["С обложкой"]["text"], True)
check("без вариантов выбор не предлагаем",
      "цифра" in by_title["Старый"]["text"], False)

# --- Продакшн-бот: порядок фото для сборки ------------------------------
prepared = []


def fake_prepare(url, out_path, credit="", page="", with_credit=False, focus=None):
    prepared.append(url)
    return f"/tmp/{len(prepared)}.jpg"


p.prepare_photo_from_url = fake_prepare

prepared.clear()
result = p.fetch_post_photos(
    {"Photo Candidates": raw, "Cover Photo URL": "https://pub-test.r2.dev/c.jpg"},
    p.Path("/tmp/claude-0/x"),
)
check("выбранная обложка идёт первой", prepared[0], "https://pub-test.r2.dev/c.jpg")
check("остальные — в исходном порядке", prepared[1:],
      ["https://pub-test.r2.dev/a.jpg", "https://pub-test.r2.dev/b.jpg"])
check("обложка не дублируется", len(prepared), 3)
check("подписи сохранены", [x["credit"] for x in result], ["BoF", "Vogue", ""])

prepared.clear()
p.fetch_post_photos({"Photo Candidates": raw}, p.Path("/tmp/claude-0/x"))
check("без выбора обложки — порядок как нашли", prepared[0],
      "https://pub-test.r2.dev/a.jpg")

# Обложку не удалось забрать — лучше без фото, чем с чужой обложкой.
p.prepare_photo_from_url = lambda *a, **kw: None
check("обложка не отдалась → без фото",
      p.fetch_post_photos({"Photo Candidates": raw}, p.Path("/tmp/claude-0/x")), [])

# Фото не выбраны вообще — тоже пусто, сборка поищет сама.
check("пустой список", p.fetch_post_photos({}, p.Path("/tmp/claude-0/x")), [])

# --- Маршрутизация форматов --------------------------------------------
check("Post — пост", "post" in p.POST_FORMATS, True)
check("Post Text — пост", "post text" in p.POST_FORMATS, True)
check("текстовый пост без фото", p.POST_FORMAT_TEXT_ONLY, "post text")

if fails:
    print("ОШИБКИ:")
    for failure in fails:
        print(" -", failure)
    sys.exit(1)
print("Все проверки прошли.")
