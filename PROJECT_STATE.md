# SV Fashion Media OS — Current Working Pipeline

## Main bots

1. `sv_airtable_bot.py`
   - Collects fashion news from RSS_FEEDS and SOURCE_PAGES.
   - Uses Claude to select one editorial topic.
   - Creates a Content Inbox card through Airtable webhook.
   - Output status: `Needs Review`.

2. `transfer_to_visual_bot.py`
   - Reads Content Inbox records with `Status = Approved`.
   - Creates a new record in Visual Jobs.
   - Output status: `Visual Status = Queued`.

3. `visual_brief_bot.py`
   - Reads Visual Jobs records with `Visual Status = Queued`.
   - Matches the source post from Content Inbox by title.
   - Generates Visual Brief, Reel Script, Shot List, On-screen Text, Krea Prompt Pack.
   - Output status: `Visual Status = Brief Ready`.

4. `visual_production_bot.py`
   - Reads Visual Jobs Queue view.
   - Processes:
     - `Brief Ready` → keyframes → `Needs Visual Review`
     - `Approved Visual` → motion / assembly / text preview → `Needs Text Review`
     - `Approved Text` → final text / sound / cover / caption → `Ready for Buffer`

5. `reel05_bot.py` — ветка «Рил 05», эксперимент.
   - Two entry points: `Visual Status = Reel 05` set by hand (for older posts),
     or the `Рил 05` checkbox set by the Telegram bridge when the owner answers
     "да+рил" on a Post's brief card — the reel then runs once the post is done.
   - Takes the finished post text, splits it into screens, renders a vertical
     typewriter reel with Remotion (`reel05/`), uploads it to R2 and creates a
     Buffer draft.
   - Output status: `Sent for Buffer`, or `Ready for Buffer` if Buffer refused.
   - Deliberately separate from the rest: own status, own workflow, own script,
     so it cannot break posts, carousels or reels.

## Перестройка 2026-10-18 (общие модули)

- `editorial_rules.py` — правила понятности для русскоязычной аудитории и
  «редактор-адаптатор» (второй проход после автора: поясняет бренды, имена,
  аббревиатуры, англицизмы; без новых фактов и эмодзи). Используется в
  `sv_airtable_bot.py` (текст для Content Inbox) и `visual_brief_bot.py`
  (подпись для не-Post форматов; для Post подпись берётся дословно из
  одобренного текста).
- Хэштеги: `normalize_hashtags()` вызывается в `buffer_publish.create_instagram_draft`
  — для КАЖДОГО черновика не больше 5, `#sv_fashion_media` первым.
- `photo_editor.py` — реальные фото: картинки статьи-источника, Google
  Картинки через Serper (`SERPER_API_KEY`, без ключа пропускается),
  Wikimedia Commons; отбор Claude по картинке; обрезка 4:5 с фокусом;
  подпись «Фото: источник». Выключить: `PHOTO_EDITOR_ENABLED=0`.
- `post_layout.py` — вёрстка Post v2: карточки по целым предложениям,
  ровное заполнение, 5–7 карточек, фото на обложке и внутри цепочки,
  палитры. Цвет фона — поле Visual Jobs `Post Color` (пусто / «Авто» —
  чередование).
- Карусели: сначала реальные фото, Krea генерирует только недостающие слайды.
- Проверка без записи в Airtable/Buffer: workflow `Rebuild Smoke Test`
  (`tools/rebuild_smoke_test.py`, вход — ID записей Visual Jobs).

## Manual review points

1. Content Inbox:
   - `Needs Review` → manual approval → `Approved`

2. Visual Jobs:
   - `Needs Visual Review` → choose frames / fill Selected Frame Order → `Approved Visual`
   - `Needs Visual Review`, carousels only → reply to the Telegram card with
     slide numbers ("2 5") instead of "да" → `Redo Slides` → only those slides
     are rendered again, the approved ones are kept → `Needs Visual Review`

3. Visual Jobs:
   - `Needs Text Review` → edit Overlay Script / title / caption → `Approved Text`

## Important Airtable views

### Content Inbox
Approved records are picked up by `transfer_to_visual_bot.py`.

### Visual Jobs / Queue
Note: `visual_production_bot.py` selects records by `filterByFormula`, not by
this view, so a new actionable status (e.g. `Redo Slides`) needs no view edit —
only an entry in that formula.

Must include:
- `Brief Ready`
- `Approved Visual`
- `Approved Text`

Do not include:
- `Needs Visual Review`
- `Needs Text Review`
- `Ready for Buffer`

## Stable principle

Do not create new collector bots unless replacing `sv_airtable_bot.py`.
Current news collector is `sv_airtable_bot.py`.
