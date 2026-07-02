import os
import json
import re
from urllib.parse import quote
from datetime import datetime, timezone

import requests
import anthropic


ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

CONTENT_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Content Inbox")
VISUAL_TABLE_NAME = os.environ.get("AIRTABLE_VISUAL_TABLE_NAME", "Visual Jobs")

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def airtable_table_url(table_name: str) -> str:
    table_encoded = quote(table_name, safe="")
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"


def airtable_headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def normalize_title(title: str) -> str:
    title = (title or "").lower().strip()
    title = re.sub(r"[^\w\sа-яА-ЯёЁ]", " ", title)
    title = re.sub(r"\s+", " ", title)
    return title


def extract_json(text: str) -> dict:
    text = text.strip()

    # Убираем markdown-обёртку ```json ... ```
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError(
            "No complete JSON object found. Claude response was probably truncated.\n"
            f"Response preview:\n{text[:2000]}"
        )

    json_text = text[start:end + 1]
    return json.loads(json_text)


def get_table_field_names(table_name: str) -> set[str]:
    """
    Reads Airtable schema and returns existing field names.
    If schema read fails, returns empty set and we update directly.
    """
    url = f"https://api.airtable.com/v0/meta/bases/{AIRTABLE_BASE_ID}/tables"

    try:
        response = requests.get(url, headers=airtable_headers(), timeout=30)

        if response.status_code != 200:
            print("Schema read skipped.")
            print("Schema status:", response.status_code)
            print("Schema response:", response.text[:500])
            return set()

        data = response.json()

        for table in data.get("tables", []):
            if table.get("name") == table_name:
                return {field.get("name") for field in table.get("fields", [])}

        print(f"Table not found in schema: {table_name}")
        return set()

    except Exception as error:
        print("Schema read error:", error)
        return set()


def fetch_queued_visual_job() -> dict | None:
    url = airtable_table_url(VISUAL_TABLE_NAME)

    params = {
        "pageSize": 1,
        "filterByFormula": "{Visual Status} = 'Queued'",
    }

    response = requests.get(
        url,
        headers=airtable_headers(),
        params=params,
        timeout=30,
    )

    print("Read Visual Jobs status:", response.status_code)
    print("Read Visual Jobs preview:", response.text[:700])

    if response.status_code != 200:
        raise RuntimeError("Could not read Visual Jobs")

    records = response.json().get("records", [])

    if not records:
        print("No Queued Visual Jobs found.")
        return None

    return records[0]


def find_matching_post(source_post_title: str) -> dict:
    if not source_post_title:
        return {}

    url = airtable_table_url(CONTENT_TABLE_NAME)

    # Escape single quotes for Airtable formula (title is user content, not record ID).
    title_safe = source_post_title.replace("\\", "\\\\").replace("'", "\\'")
    formula = f"{{Title}} = '{title_safe}'"

    params = [
        ("pageSize", "1"),
        ("filterByFormula", formula),
        ("fields[]", "Title"),
        ("fields[]", "HOOK"),
        ("fields[]", "Visual Headline"),
        ("fields[]", "Final Caption"),
        ("fields[]", "Raw Text"),
        ("fields[]", "Rubric"),
        ("fields[]", "Source URL"),
    ]

    response = requests.get(
        url,
        headers=airtable_headers(),
        params=params,
        timeout=30,
    )

    print("Find matching post status:", response.status_code)

    if response.status_code != 200:
        print("Content Inbox lookup failed, fallback to Visual Job only.")
        print(response.text[:700])
        return {}

    records = response.json().get("records", [])

    if records:
        fields = records[0].get("fields", {})
        print("Matched Content Inbox post:", fields.get("Title", ""))
        return fields

    print("No exact Content Inbox match. Using Visual Job data only.")
    return {}


def generate_visual_brief(job_fields: dict, post_fields: dict) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    source_post_title = job_fields.get("Source Post Title", "")
    job_title = job_fields.get("Job Title", "")
    chosen_format = (
        job_fields.get("Chosen Format")
        or job_fields.get("Format")
        or job_fields.get("Recommended Format")
        or "Reel + Carousel"
    )

    is_post = str(chosen_format).strip().lower() == "post"

    post_block = ""
    if is_post:
        post_block = (
            "\n\nВАЖНО — ЭТО ФОРМАТ POST (текстовый пост слайдами):\n"
            "- Это НЕ reel и НЕ image-карусель. Пост = чистый ТЕКСТ, разложенный по слайдам с цветным фоном.\n"
            "- Final Reel Caption = ПОЛНЫЙ готовый текст поста для Instagram (он же пойдёт в подпись).\n"
            "- Slide Copy = ТОТ ЖЕ текст поста, осмысленно разбитый на 3–7 слайдов. Число слайдов реши сам(а) по длине текста: короткий текст — меньше слайдов, длинный — больше. Каждый слайд — законченный смысловой блок (1–4 коротких предложения). РАЗДЕЛЯЙ слайды ПУСТОЙ СТРОКОЙ между блоками.\n"
            "- Slide Count = число слайдов, на которое ты разбил(а) текст (3–7).\n"
            "- Поля для reel и image-карусели (Reel Keyframe Prompts, Reel Motion Prompts, On-screen Text, Generated Carousel Prompts, Krea Prompt Pack, Shot List) для Post НЕ нужны — верни их пустой строкой \"\".\n"
        )
    visual_mode = job_fields.get("Visual Mode", "Hybrid")

    try:
        keyframe_count = int(float(str(job_fields.get("Keyframe Count") or 3)))
    except (ValueError, TypeError):
        keyframe_count = 3
    keyframe_count = max(2, min(8, keyframe_count))

    post_title = post_fields.get("Title", source_post_title)
    hook = post_fields.get("HOOK", "")
    visual_headline = post_fields.get("Visual Headline", "")
    final_caption = post_fields.get("Final Caption", "")
    raw_text = post_fields.get("Raw Text", "")
    rubric = post_fields.get("Rubric", "")
    source_url = post_fields.get("Source URL", "")

    system_prompt = """
Ты — visual strategist и арт-директор fashion media.

Проект:
SV Fashion Media / @sv_fashionacademy

Позиция:
Не только тренды. Контекст моды и стиля.
Показы, бренды, знаковые события, вкус, модная индустрия, архивы.
Авторский взгляд Alexandre Chevalkov.

Задача: 
Из утверждённого поста сделать визуальное ТЗ для роста аудитории:
- в первую очередь Reels;
- во вторую очередь Carousel;
- визуальный стиль — Hybrid: fashion-media intelligence + art/image-driven visual appeal.

Нужно мыслить как креативный директор, редактор, стилист и growth strategist.

Не делай банальный fashion moodboard.
Не делай случайную красивость.
Не делай только глянец имиджи.
Визуал должен выглядеть как fashion media, но достаточно цепко и ярко для роста охвата.

Пиши по-русски.
"""

    user_prompt = f"""
Данные Visual Job:

Job Title: {job_title}
Source Post Title: {source_post_title}
Chosen Format: {chosen_format}
Visual Mode: {visual_mode}

Данные исходного поста из Content Inbox:

Title: {post_title}
Rubric: {rubric}
HOOK: {hook}
Visual Headline: {visual_headline}
Final Caption:
{final_caption}

Raw Text:
{raw_text}

Source URL:
{source_url}

Сделай Visual Brief для Reels и/или Carousel.
{post_block}
Важно:
- Если Chosen Format = "Reel + Carousel", сделай оба пакета.
- Reels должны быть рассчитаны на рост охвата: сильные первые 2 секунды, ясный визуальный конфликт, 20-40 секунд.
- Carousel должна давать сохранения: структура, выводы, понятные слайды.
- Krea Prompt Pack должен быть пригоден для работы в Krea: отдельно image prompts, video prompts, cover prompts, style rules.
- Krea Prompt Pack должен быть конкретным, но компактным: максимум 3000 знаков.
- Reel Keyframe Prompts — обязательное поле, если Chosen Format содержит Reel.
- Reel Keyframe Prompts должен содержать РОВНО {keyframe_count} prompt-блоков.
- Каждый keyframe prompt = одна неподвижная вертикальная 9:16 fashion editorial картинка.
- Keyframe 1 = hook / opening frame.
- Последний keyframe (номер {keyframe_count}) = final / conclusion frame.
- Промежуточные keyframes = развитие визуального аргумента (conflict, деталь, контраст).
- Keyframes должны идти логичной последовательностью от hook к final.
- ВСЕ keyframes (включая финальный) должны быть ФОТОГРАФИЧЕСКИМИ editorial-сценами, пригодными для оживления в видео. НЕ делай кадр чисто типографической карточкой (текст на пустом/чёрном фоне) — image-to-video такие не оживляет. Закрывающую мысль бот добавит сам как overlay-текст, поэтому внутри картинки текст не нужен.
- В Reel Keyframe Prompts запрещены слова и логика: montage, fast cut, sequence, storyboard, split-screen, slow pan, zoom-in, transition, motion, video.
- В Reel Keyframe Prompts и Krea Prompt Pack избегай формулировок, которые рубит AI-модерация: обнажённость, эротика, явное/голое тело, nude, naked, bare torso, underwear, провокация. Даже если тема архива связана с телом — переводи идею в одежду, ткань, силуэт, фактуру, архивную деталь, абстракцию. Кадры должны быть editorial и безопасными для генерации.
- Если нужна идея split-screen, перепиши её как один still: comparative flat lay, two objects side by side, controlled editorial composition.
- Если нужна идея montage, перепиши её как один still: arranged textile details in one controlled composition.
- Reel Motion Prompts — отдельное поле для motion/video-сцен. Там можно использовать pan, zoom, transition, rhythm edit.
- Reel Motion Prompts должен содержать РОВНО {keyframe_count} блоков — по одному motion-промпту на каждый keyframe.
- On-screen Text: РОВНО {keyframe_count} коротких фраз, КАЖДАЯ С НОВОЙ СТРОКИ (по одной на кадр). Только сам текст фразы — БЕЗ подписей «Сцена 1:», «Сцена 2:» и т.п., без кавычек «», без разделителя « / », без нумерации. Просто чистый текст, по строке на сцену.
- Selected Keyframe Scenes должен перечислять номера всех {keyframe_count} кадров по порядку через запятую (например 1,2,3).
- Нельзя использовать Krea Prompt Pack как источник готовых keyframes. Krea Prompt Pack — только общий human-readable summary.
- Slide Copy: ровно Slide Count коротких фраз, КАЖДАЯ С НОВОЙ СТРОКИ (по одной на слайд, в порядке слайдов). Только текст фразы — БЕЗ подписей «Слайд 1:», «Слайд 2:» и т.п., без кавычек «», без разделителя « / », без нумерации. Первая строка = текст обложки (слайд 1).
- Generated Carousel Prompts должен содержать РОВНО столько отдельных промптов, сколько указано в Slide Count.
- Каждый prompt = только ОДНА картинка для ОДНОГО слайда.
- Нельзя писать в одном prompt: Cover image + Slide 2 + Slide 3 + Slide 4.
- Нельзя включать Reel scenes в Generated Carousel Prompts.
- Нельзя включать весь Krea Prompt Pack целиком внутрь каждого prompt.
- Нельзя включать overlay text / caption / русские тексты внутрь image prompt.
- Каждый prompt должен описывать только визуальную сцену для конкретного слайда.
- Разделитель между prompt-блоками строго такой:

---

- Внутри каждого prompt можно повторять короткие style rules и negative prompts.
- Render Notes максимум 1000 знаков.
- Не пиши длинные референс-листы и длинные объяснения.
- Не полагайся на точное написание текста внутри AI-изображений. Текст лучше как overlay.
- Для Krea учитывай:
  - Nano Banana / Krea Image — для image editing, fashion frames, carousel cover, image-led slides.
  - Kling или Runway — для коротких video scenes / motion shots.
  - Для Reel + Carousel лучше ставить Krea Model Recommendation = "Manual Choice".

Верни СТРОГО валидный JSON без markdown и без пояснений.

Схема JSON:

{{
  "Visual Hook": "короткая визуальная идея до 12 слов",
  "Visual Concept": "общее арт-директорское описание визуального решения",
  "Visual Mode": "Hybrid",
  "Reel Hook": "первая фраза/кадр для первых 2 секунд",
  "Reel Duration": "30 sec",
  "Reel Script": "voiceover script для рилса",
  "Shot List": "покадровый план: сцена 1, сцена 2, сцена 3...",
  "On-screen Text": "ровно {keyframe_count} коротких фраз, каждая с новой строки, по одной на сцену/кадр. Только текст фразы, без подписей 'Сцена N', без кавычек, без нумерации",
  "Carousel Cover": "текст обложки карусели",
  "Slide Count": 7,
  "Slide Structure": "структура слайдов 1-7",
  "Slide Copy": "по одной короткой фразе на слайд, каждая с новой строки, ровно Slide Count строк. Первая строка = текст обложки. Без подписей 'Слайд N', без кавычек, без ' / ', без нумерации — только текст",
  "Krea Prompt Pack": "детальный prompt pack: cover image, carousel images, reel scenes, style rules, negative prompts",
  "Final Reel Caption": "готовый финальный текст-подпись (caption) для поста в Instagram: цепляющий, живой, с уместными эмодзи и релевантными хэштегами в конце. Именно этот текст уйдёт в пост — его пользователь проверяет на этом же этапе",
  "Reel Keyframe Prompts": "ровно {keyframe_count} отдельных still image prompts для keyframes рилса (от hook через развитие к final). Каждый prompt описывает одну неподвижную 9:16 картинку, без motion language, без montage, без split-screen, без storyboard. Разделитель между prompt-блоками строго: пустая строка + --- + пустая строка",
  "Reel Motion Prompts": "отдельные video prompts для motion-сцен рилса. Здесь можно использовать slow zoom, pan, transition, rhythm edit. Разделитель между сценами строго: пустая строка + --- + пустая строка",
  "Selected Keyframe Scenes": "номера всех {keyframe_count} кадров по порядку через запятую, например: 1,2,3",
  "Generated Carousel Prompts": "готовая строка из отдельных image prompts для каждого слайда, разделённых строго через пустая строка + --- + пустая строка",
  "Krea Model Recommendation": "Manual Choice",
  "Render Notes": "практические заметки: что делать в Krea, какие модели выбрать, что потом наложить вручную"
}}

Допустимые значения:
Visual Mode: "Editorial Fashion Media", "AI Fashion Image", "Analytical Slides", "Hybrid"
Reel Duration: "15 sec", "30 sec", "45 sec", "60 sec"
Krea Model Recommendation: "Krea Image", "Nano Banana", "Kling", "Runway", "Veo / Sora later", "Manual Choice"
"""

    message = client.messages.create(
    model=MODEL,
    max_tokens=5000,
    temperature=0.25,
    system=system_prompt,
    messages=[{"role": "user", "content": user_prompt}],
)

    response_text = message.content[0].text

    print("\n=== Claude Visual Brief raw response ===")
    print(response_text)

    brief = extract_json(response_text)

    required_fields = [
        "Visual Hook",
        "Visual Concept",
        "Visual Mode",
        "Reel Hook",
        "Reel Duration",
        "Reel Script",
        "Shot List",
        "On-screen Text",
        "Carousel Cover",
        "Slide Count",
        "Slide Structure",
        "Slide Copy",
        "Krea Prompt Pack",
        "Reel Keyframe Prompts",
        "Reel Motion Prompts",
        "Selected Keyframe Scenes",
        "Generated Carousel Prompts",
        "Krea Model Recommendation",
        "Render Notes",
    ]

    for field in required_fields:
        if field not in brief:
            if is_post:
                # Post uses only the text fields; anything else may be absent.
                brief[field] = ""
                continue
            raise ValueError(f"Missing field from Claude response: {field}")

    brief["Visual Mode"] = brief.get("Visual Mode") or "Hybrid"
    brief["Krea Model Recommendation"] = brief.get("Krea Model Recommendation") or "Manual Choice"

    if chosen_format == "Reel + Carousel":
        brief["Krea Model Recommendation"] = "Manual Choice"

    return brief


def update_visual_job(record_id: str, brief: dict) -> None:
    url = f"{airtable_table_url(VISUAL_TABLE_NAME)}/{record_id}"

    update_fields = {
        "Visual Status": "Brief Ready",
        "Visual Hook": brief.get("Visual Hook", ""),
        "Visual Concept": brief.get("Visual Concept", ""),
        "Visual Mode": brief.get("Visual Mode", "Hybrid"),
        "Reel Hook": brief.get("Reel Hook", ""),
        "Reel Duration": brief.get("Reel Duration", "30 sec"),
        "Reel Script": brief.get("Reel Script", ""),
        "Shot List": brief.get("Shot List", ""),
        "On-screen Text": brief.get("On-screen Text", ""),
        "Carousel Cover": brief.get("Carousel Cover", ""),
        "Slide Count": brief.get("Slide Count", 7),
        "Slide Structure": brief.get("Slide Structure", ""),
        "Slide Copy": brief.get("Slide Copy", ""),
        "Krea Prompt Pack": brief.get("Krea Prompt Pack", ""),
        "Final Reel Caption": brief.get("Final Reel Caption", ""),
        "Reel Keyframe Prompts": brief.get("Reel Keyframe Prompts", ""),
        "Reel Motion Prompts": brief.get("Reel Motion Prompts", ""),
        "Selected Keyframe Scenes": brief.get("Selected Keyframe Scenes", "1,3,6"),
        "Generated Carousel Prompts": brief.get("Generated Carousel Prompts", ""),
        "Krea Model Recommendation": brief.get("Krea Model Recommendation", "Manual Choice"),
        "Render Notes": brief.get("Render Notes", ""),
    }

    existing_fields = get_table_field_names(VISUAL_TABLE_NAME)

    if existing_fields:
        update_fields = {
            key: value for key, value in update_fields.items()
            if key in existing_fields
        }

    payload = {
        "fields": update_fields,
        "typecast": True,
    }

    response = requests.patch(
        url,
        headers=airtable_headers(),
        json=payload,
        timeout=30,
    )

    print("Update Visual Job status:", response.status_code)
    print("Update Visual Job preview:", response.text[:1000])

    if response.status_code not in [200, 201, 202]:
        raise RuntimeError("Could not update Visual Job")


def main() -> None:
    print("Visual Brief Bot started:", datetime.now(timezone.utc).isoformat())

    job = fetch_queued_visual_job()

    if not job:
        return

    record_id = job["id"]
    job_fields = job.get("fields", {})

    print("\n=== Visual Job ===")
    print(json.dumps(job_fields, ensure_ascii=False, indent=2))

    source_post_title = job_fields.get("Source Post Title", "")
    post_fields = find_matching_post(source_post_title)

    print("\n=== Matched Post Fields ===")
    print(json.dumps(post_fields, ensure_ascii=False, indent=2)[:2500])

    brief = generate_visual_brief(job_fields, post_fields)

    print("\n=== Generated Visual Brief ===")
    print(json.dumps(brief, ensure_ascii=False, indent=2))

    update_visual_job(record_id, brief)

    print("Done. Visual Job moved to Brief Ready.")


if __name__ == "__main__":
    main()
