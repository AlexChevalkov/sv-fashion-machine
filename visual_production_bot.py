import os
import re
import json
import math
import time
import subprocess
import textwrap
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import requests
import anthropic
from PIL import Image, ImageDraw, ImageFont


# =========================================================
# ENV
# =========================================================

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
KREA_API_KEY = os.environ["KREA_API_KEY"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

ANTHROPIC_MODEL = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6").strip()

AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Visual Jobs")
AIRTABLE_VIEW_NAME = os.environ.get("AIRTABLE_VIEW_NAME", "Queued Visual Jobs")

BRAND_NAME = os.environ.get("BRAND_NAME", "SV FASHION MEDIA")
INSTAGRAM_HANDLE = os.environ.get("INSTAGRAM_HANDLE", "@sv_fashionacademy")
GITHUB_RUN_URL = os.environ.get("GITHUB_RUN_URL", "")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Krea endpoints
# Если у тебя в текущем тестовом коде другие endpoint'ы — просто замени значения в secrets/env.
KREA_API_BASE = "https://api.krea.ai"
KREA_ASPECT_RATIO = "4:5"
# Typography / rendering
CANVAS_W = 1080
CANVAS_H = 1350
MAX_SLIDES = 7
MIN_SLIDES = 5

# Fonts
FONT_REGULAR = os.environ.get(
    "FONT_REGULAR",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf"
)
FONT_BOLD = os.environ.get(
    "FONT_BOLD",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"
)

# Statuses
STATUS_QUEUED = "Queued"
STATUS_RENDERING = "In Production"
STATUS_NEEDS_REVIEW = "Needs Visual Review"
STATUS_APPROVED = "Approved Visual"
STATUS_READY_FOR_BUFFER = "Ready for Buffer"
STATUS_ERROR = "Failed"


# =========================================================
# HELPERS
# =========================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_get(fields: Dict[str, Any], key: str, default: str = "") -> str:
    value = fields.get(key, default)
    if value is None:
        return default
    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x is not None)
    return str(value)


def clamp_slide_count(value: Any) -> int:
    try:
        n = int(value)
    except Exception:
        n = 6
    return max(MIN_SLIDES, min(MAX_SLIDES, n))


def extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model response:\n{text}")

    return json.loads(match.group(0))


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def shorten(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


# =========================================================
# AIRTABLE
# =========================================================

def airtable_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def airtable_base_url() -> str:
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(AIRTABLE_TABLE_NAME, safe='')}"


def get_queued_visual_jobs(limit: int = 1) -> List[Dict[str, Any]]:
    """
    Берём jobs со статусом Queued.
    Лучше иметь отдельный view Queued Visual Jobs,
    но если view нет — можно читать всю таблицу и фильтровать формулой.
    """
    url = airtable_base_url()
    params = {
        "maxRecords": limit,
        "view": AIRTABLE_VIEW_NAME,
    }

    response = requests.get(url, headers=airtable_headers(), params=params, timeout=30)
    print("Read Visual Jobs status:", response.status_code)
    print("Read Visual Jobs preview:", shorten(response.text, 1200))

    response.raise_for_status()
    data = response.json()
    records = data.get("records", [])

    if not records:
        # fallback: formula query
        params = {
            "maxRecords": limit,
            "filterByFormula": (
                "OR("
                "{Visual Status}='Queued',"
                "AND("
                "OR({Format}='Reel',{Chosen Format}='Reel'),"
                "{Visual Status}='Approved Visual'"
                ")"
                ")"
            ),
        }
        response = requests.get(url, headers=airtable_headers(), params=params, timeout=30)
        print("Fallback read status:", response.status_code)
        print("Fallback read preview:", shorten(response.text, 1200))
        response.raise_for_status()
        data = response.json()
        records = data.get("records", [])

    return records


def update_airtable_record(record_id: str, fields: Dict[str, Any]) -> None:
    url = f"{airtable_base_url()}/{record_id}"
    payload = {
        "fields": fields,
        "typecast": True,
    }
    response = requests.patch(url, headers=airtable_headers(), json=payload, timeout=30)
    print("Update Visual Job status:", response.status_code)
    print("Update Visual Job preview:", shorten(response.text, 1200))
    response.raise_for_status()


def build_output_links_text(raw_items: List[Dict[str, str]], assembled_paths: List[str]) -> str:
    lines = []
    lines.append(f"Generated at: {now_iso()}")
    lines.append("")

    lines.append("Krea raw images:")
    for item in raw_items:
        slide_num = item["slide"]
        url = item["url"]
        job_id = item["job_id"]
        lines.append(f"Slide {slide_num}: {url} | job_id: {job_id}")

    lines.append("")
    lines.append("Assembled local files:")
    for path in assembled_paths:
        lines.append(path)

    return "\n".join(lines)


# =========================================================
# SOURCE CONTEXT
# =========================================================

def build_source_context(fields: Dict[str, Any]) -> str:
    """
    Собираем максимум полезного контекста из Visual Jobs.
    Если каких-то полей нет — ничего страшного.
    """

    source_post_title = safe_get(fields, "Source Post Title")
    source_post_text = safe_get(fields, "Source Post Text")
    source_final_caption = safe_get(fields, "Source Final Caption")
    source_raw_text = safe_get(fields, "Source Raw Text")
    source_hook = safe_get(fields, "Source Hook")
    source_url = safe_get(fields, "Source URL")
    chosen_format = safe_get(fields, "Chosen Format", safe_get(fields, "Format", "Carousel"))
    visual_mode = safe_get(fields, "Visual Mode", "Hybrid")
    job_title = safe_get(fields, "Job Title", "Visual Production Job")

    blocks = [
        f"Job Title: {job_title}",
        f"Chosen Format: {chosen_format}",
        f"Visual Mode: {visual_mode}",
        f"Source Post Title: {source_post_title}",
        f"Source Hook: {source_hook}",
        f"Source Post Text: {source_post_text}",
        f"Source Final Caption: {source_final_caption}",
        f"Source Raw Text: {source_raw_text}",
        f"Source URL: {source_url}",
    ]

    return "\n".join(block for block in blocks if block.strip())


# =========================================================
# CLAUDE / BRIEF GENERATION
# =========================================================

def generate_visual_brief(record: Dict[str, Any]) -> Dict[str, Any]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    fields = record["fields"]
    source_context = build_source_context(fields)

    system_prompt = f"""
Ты — Visual Editor и Creative Director для {BRAND_NAME}.

Контекст бренда:
- Умное fashion-медиа
- Не глянец ради глянца, а editorial intelligence
- Минимализм, дистанция, ощущение premium
- Визуал должен ощущаться как fashion editorial, not stock

Твоя задача:
1. Создать visual brief для одного поста
2. Сразу спроектировать карусель 5–7 слайдов
3. Дать Krea-ready prompts для каждого слайда

ЖЁСТКИЕ ПРАВИЛА:
- Для карусели всегда выбирай от 5 до 7 слайдов
- Делай не механические 7, а столько, сколько действительно нужно по смыслу
- Слайд 1 — cover
- Последний слайд — чёткий editorial takeaway / финальная мысль
- Каждый слайд должен содержать КОРОТКИЙ текст
- Текст для слайда должен быть типографически удобным:
  - максимум 2 короткие строки
  - максимум примерно 12 слов в строке
  - не писать длинные абзацы
- Первый слайд: короткий cover headline, 2–5 слов
- Body slides: короткие ясные мысли, не caption-length

Правила визуального языка для Krea:
- The object must look like a deliberate fashion editorial symbol, not a random product still life.
- Composition should feel like a magazine cover background, with clear negative space reserved for typography.
- cold editorial light
- matte surfaces
- stone / linen / paper / plaster textures
- quiet premium mood
- no people unless conceptually necessary
- no stock photo feel
- no glossy catalogue look
- no visual clutter
- no text inside the generated image

Если тема слабая для карусели, всё равно выстрой её так, чтобы структура была сильной:
hook → contrast → explanation → implication → final line
"""

    user_prompt = f"""
Вот контекст исходного поста:

{source_context}

Верни СТРОГО валидный JSON.
Без markdown. Без пояснений.

Схема:
{{
  "job_title": "короткое название visual job",
  "chosen_format": "Carousel" или "Reel + Carousel",
  "visual_mode": "Hybrid" или другое",
  "visual_hook": "короткий hook для visual brief",
  "visual_concept": "5-10 предложений про общую визуальную систему",
  "reel_hook": "короткий reel hook",
  "reel_duration": "30 sec",
  "reel_script": "короткий reel script",
  "shot_list": "список сцен для reel",
  "on_screen_text": "какие фразы поверх видео",
  "carousel_cover": "заголовок обложки, 2-5 слов",
  "slide_count": 5,
  "slide_texts": [
    "Слайд 1 текст",
    "Слайд 2 текст",
    "Слайд 3 текст"
  ],
  "krea_model_recommendation": "Krea Image / Nano Banana / Manual Choice",
  "render_notes": "технические примечания по рендеру и монтажу",
  "krea_prompt_pack": "общие style rules и negative prompts",
  "krea_prompts": [
    "prompt for slide 1",
    "prompt for slide 2",
    "prompt for slide 3"
  ]
}}

Требования:
- slide_count от 5 до 7
- slide_texts длиной ровно slide_count
- slide_texts[0] должен смыслово совпадать с carousel_cover
- krea_prompts длиной ровно slide_count
- slide_texts должны быть короткими, пригодными для хорошей типографики
- не делай длинных абзацев в slide_texts
"""

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=3000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text
    print("Claude brief raw response:")
    print(response_text)

    brief = extract_json(response_text)

    required = [
        "job_title",
        "chosen_format",
        "visual_mode",
        "visual_hook",
        "visual_concept",
        "reel_hook",
        "reel_duration",
        "reel_script",
        "shot_list",
        "on_screen_text",
        "carousel_cover",
        "slide_count",
        "slide_texts",
        "krea_model_recommendation",
        "render_notes",
        "krea_prompt_pack",
        "krea_prompts",
    ]
    for key in required:
        if key not in brief:
            raise ValueError(f"Claude response missing key: {key}")

    brief["slide_count"] = clamp_slide_count(brief["slide_count"])

    if not isinstance(brief["slide_texts"], list):
        raise ValueError("slide_texts must be a list")
    if not isinstance(brief["krea_prompts"], list):
        raise ValueError("krea_prompts must be a list")

    # Нормализуем длины
    slide_texts = [str(x).strip() for x in brief["slide_texts"] if str(x).strip()]
    krea_prompts = [str(x).strip() for x in brief["krea_prompts"] if str(x).strip()]

    # Если Claude вернул меньше — дополним
    while len(slide_texts) < brief["slide_count"]:
        slide_texts.append("EDITORIAL THOUGHT")

    while len(krea_prompts) < brief["slide_count"]:
        idx = len(krea_prompts) + 1
        fallback_prompt = (
            f"{brief['krea_prompt_pack']} "
            f"Slide {idx}. Editorial luxury still image with clear negative space, "
            f"aligned with the theme: {slide_texts[idx - 1]}"
        )
        krea_prompts.append(fallback_prompt)

    brief["slide_texts"] = slide_texts[:brief["slide_count"]]
    brief["krea_prompts"] = krea_prompts[:brief["slide_count"]]

    return brief


def format_slide_copy_for_airtable(slide_texts: List[str]) -> str:
    lines = []
    for idx, text in enumerate(slide_texts, start=1):
        lines.append(f"Slide {idx}: {text}")
    return "\n".join(lines)


def brief_to_airtable_fields(brief: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "Job Title": brief["job_title"],
        "Chosen Format": brief["chosen_format"],
        "Format": brief["chosen_format"],
        "Visual Mode": brief["visual_mode"],
        "Visual Hook": brief["visual_hook"],
        "Visual Concept": brief["visual_concept"],
        "Reel Hook": brief["reel_hook"],
        "Reel Duration": brief["reel_duration"],
        "Reel Script": brief["reel_script"],
        "Shot List": brief["shot_list"],
        "On-screen Text": brief["on_screen_text"],
        "Carousel Cover": brief["carousel_cover"],
        "Slide Count": brief["slide_count"],
        "Slide Copy": format_slide_copy_for_airtable(brief["slide_texts"]),
        "Krea Prompt Pack": brief["krea_prompt_pack"],
        "Krea Model Recommendation": "Manual Choice",
        "Render Notes": brief["render_notes"],
        "Visual Status": STATUS_RENDERING,
    }


# =========================================================
# KREA
# =========================================================

def krea_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {KREA_API_KEY}",
        "Content-Type": "application/json",
    }


def create_krea_image_job(prompt: str, aspect_ratio: str = KREA_ASPECT_RATIO) -> str:
    url = f"{KREA_API_BASE}/generate/image/krea/krea-2/medium"

    payload = {
        "prompt": prompt[:4000],
        "aspect_ratio": aspect_ratio,
        "resolution": "1K",
        "creativity": "low",
    }

    response = requests.post(
        url,
        headers=krea_headers(),
        json=payload,
        timeout=60,
    )

    print("Create Krea job URL:", url)
    print("Create Krea job status:", response.status_code)
    print("Create Krea job preview:", shorten(response.text, 1200))

    response.raise_for_status()

    data = response.json()
    job_id = data.get("job_id")

    if not job_id:
        raise RuntimeError(f"Krea did not return job_id: {data}")

    return job_id


def poll_krea_job(job_id: str, max_wait_seconds: int = 360) -> str:
    url = f"{KREA_API_BASE}/jobs/{job_id}"
    started = time.time()

    while time.time() - started < max_wait_seconds:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {KREA_API_KEY}"},
            timeout=60,
        )

        print("Poll Krea URL:", url)
        print("Poll Krea status:", response.status_code)
        print("Poll Krea preview:", shorten(response.text, 1200))

        response.raise_for_status()

        data = response.json()
        status = data.get("status")

        if status == "completed":
            result = data.get("result") or {}
            urls = result.get("urls") or []

            if urls:
                return urls[0]

            raise RuntimeError(f"Krea job completed but no image URL found: {data}")

        if status in {"failed", "cancelled", "canceled"}:
            raise RuntimeError(f"Krea job failed: {data}")

        time.sleep(5)

    raise TimeoutError(f"Krea job timed out: {job_id}")

def download_image(url: str, destination: Path) -> None:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    destination.write_bytes(response.content)


# =========================================================
# TYPOGRAPHY
# =========================================================

def get_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size)


def sentence_split(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    text = text.replace(" / ", "\n")
    text = re.sub(r"\s+", " ", text).strip()

    # Разбиваем на предложения
    parts = re.split(r"(?<=[\.\!\?])\s+", text)
    cleaned = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        cleaned.extend([x.strip() for x in part.split("\n") if x.strip()])

    return cleaned if cleaned else [text]


def normalize_overlay_text(text: str, is_cover: bool = False) -> List[str]:
    """
    Правила:
    1) Каждое предложение — с новой строки
    2) Первая строка — uppercase
    3) Для cover допускается 2 строки максимум
    """
    parts = sentence_split(text)
    if not parts:
        return ["EDITORIAL"]

    lines: List[str] = []
    for idx, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        if idx == 0:
            part = part.upper()
        lines.append(part)

    if is_cover:
        # cover жёстче ограничиваем
        if len(lines) > 2:
            first = lines[0]
            rest = " ".join(lines[1:])
            lines = [first, rest]

    return lines


def wrap_text_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    words = text.split()
    if not words:
        return [""]

    lines = []
    current = words[0]

    for word in words[1:]:
        trial = f"{current} {word}"
        bbox = draw.textbbox((0, 0), trial, font=font)
        width = bbox[2] - bbox[0]
        if width <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines


def prepare_text_block(
    draw: ImageDraw.ImageDraw,
    lines: List[str],
    is_cover: bool,
    max_width: int,
) -> Tuple[List[Tuple[str, str]], int, int]:
    """
    Возвращает:
    [
      ("bold", "FIRST LINE"),
      ("regular", "second line"),
      ...
    ]
    + width + height
    """
    if is_cover:
        font_bold = get_font(FONT_BOLD, 58)
        font_regular = get_font(FONT_REGULAR, 58)
        line_spacing = 8
    else:
        font_bold = get_font(FONT_BOLD, 42)
        font_regular = get_font(FONT_REGULAR, 42)
        line_spacing = 6

    prepared: List[Tuple[str, str]] = []
    max_block_width = 0
    total_height = 0

    for idx, raw_line in enumerate(lines):
        role = "bold" if idx == 0 else "regular"
        font = font_bold if role == "bold" else font_regular

        wrapped = wrap_text_lines(draw, raw_line, font, max_width)
        for line in wrapped:
            prepared.append((role, line))
            bbox = draw.textbbox((0, 0), line, font=font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            max_block_width = max(max_block_width, w)
            total_height += h + line_spacing

    total_height = max(0, total_height - line_spacing)
    return prepared, max_block_width, total_height


def draw_brand_header(draw: ImageDraw.ImageDraw, slide_num: int, total_slides: int) -> None:
    header_font = get_font(FONT_REGULAR, 26)
    small_font = get_font(FONT_REGULAR, 22)

    # Brand
    draw.text((80, 70), BRAND_NAME, fill=(255, 255, 255, 235), font=header_font)

    # line under brand
    draw.line((80, 125, 180, 125), fill=(255, 255, 255, 220), width=2)

    # slide counter
    counter = f"{slide_num:02d}/{total_slides:02d}"
    bbox = draw.textbbox((0, 0), counter, font=small_font)
    w = bbox[2] - bbox[0]
    draw.text((CANVAS_W - 80 - w, 70), counter, fill=(255, 255, 255, 235), font=small_font)


def draw_handle(draw: ImageDraw.ImageDraw) -> None:
    handle_font = get_font(FONT_REGULAR, 22)
    draw.text((80, CANVAS_H - 95), INSTAGRAM_HANDLE, fill=(255, 255, 255, 235), font=handle_font)


def add_text_overlay(
    base: Image.Image,
    slide_text: str,
    slide_num: int,
    total_slides: int,
    is_cover: bool = False,
) -> Image.Image:
    img = base.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    draw_brand_header(draw, slide_num, total_slides)
    draw_handle(draw)

    lines = normalize_overlay_text(slide_text, is_cover=is_cover)

    text_area_width = int(CANVAS_W * 0.70) if is_cover else int(CANVAS_W * 0.68)
    prepared, block_w, block_h = prepare_text_block(draw, lines, is_cover=is_cover, max_width=text_area_width)

    box_padding_x = 28
    box_padding_y = 20

    if is_cover:
        box_x = 50
        box_y = int(CANVAS_H * 0.37)
    else:
        box_x = 50
        box_y = int(CANVAS_H * 0.60)

    rect_w = block_w + box_padding_x * 2
    rect_h = block_h + box_padding_y * 2

    # Полупрозрачная плашка 15–25%
    draw.rectangle(
        [box_x, box_y, box_x + rect_w, box_y + rect_h],
        fill=(0, 0, 0, 55)
    )

    if is_cover:
        font_bold = get_font(FONT_BOLD, 58)
        font_regular = get_font(FONT_REGULAR, 58)
        line_spacing = 8
    else:
        font_bold = get_font(FONT_BOLD, 42)
        font_regular = get_font(FONT_REGULAR, 42)
        line_spacing = 6

    cursor_x = box_x + box_padding_x
    cursor_y = box_y + box_padding_y

    for role, line in prepared:
        font = font_bold if role == "bold" else font_regular
        draw.text((cursor_x, cursor_y), line, fill=(255, 255, 255, 255), font=font)
        bbox = draw.textbbox((0, 0), line, font=font)
        h = bbox[3] - bbox[1]
        cursor_y += h + line_spacing

    return Image.alpha_composite(img, overlay).convert("RGB")


def fit_image_to_canvas(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")

    scale = max(CANVAS_W / image.width, CANVAS_H / image.height)
    new_w = int(image.width * scale)
    new_h = int(image.height * scale)

    resized = image.resize((new_w, new_h), Image.LANCZOS)

    left = max(0, (new_w - CANVAS_W) // 2)
    top = max(0, (new_h - CANVAS_H) // 2)
    right = left + CANVAS_W
    bottom = top + CANVAS_H

    return resized.crop((left, top, right, bottom))


def render_assembled_slide(
    raw_image_path: Path,
    slide_text: str,
    slide_num: int,
    total_slides: int,
    output_path: Path,
    is_cover: bool = False,
) -> None:
    source = Image.open(raw_image_path)
    source = fit_image_to_canvas(source)
    result = add_text_overlay(source, slide_text, slide_num, total_slides, is_cover=is_cover)
    result.save(output_path, format="PNG", quality=95)


# =========================================================
# MAIN PIPELINE
# =========================================================
def append_note(existing: str, addition: str) -> str:
    existing = existing or ""
    addition = addition or ""

    if existing.strip():
        return existing.strip() + "\n\n---\n\n" + addition.strip()

    return addition.strip()


def generate_reel_brief(record: Dict[str, Any]) -> Dict[str, Any]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    fields = record["fields"]
    source_context = build_source_context(fields)

    system_prompt = f"""
Ты — creative director и reel producer для {BRAND_NAME}.

Задача:
Сделать production-ready brief для Instagram Reel.

Важно:
- Пока НЕ генерируем видео.
- Пока НЕ запускаем Krea.
- Нужно создать структуру рилса, текст, сценарий, shot list и prompts для будущей генерации.
- Стиль должен продолжать visual language SV Fashion Media:
  quiet luxury, editorial intelligence, distance, negative space, fashion as context.
- Не делать TikTok-кричалку.
- Не делать рекламный ролик.
- Не делать глянцевую восторженность.
- Рилс должен ощущаться как короткая fashion-media колонка в движении.

Тон:
сухо, умно, точно, премиально.
"""

    user_prompt = f"""
Вот контекст исходного поста:

{source_context}

Сделай production package для Reel.

Верни строго валидный JSON без markdown.

Схема:
{{
  "job_title": "короткое название reel job",
  "chosen_format": "Reel",
  "visual_mode": "Hybrid",
  "visual_hook": "короткая визуальная идея",
  "visual_concept": "визуальная концепция рилса",
  "reel_hook": "первая фраза рилса, до 12 слов",
  "reel_duration": "30 sec",
  "reel_script": "voiceover script на русском, 90-130 слов, короткие фразы",
  "shot_list": "5-7 сцен с таймингом: 0-3 sec, 3-7 sec и т.д.",
  "on_screen_text": "короткие фразы для экрана, по одной на сцену",
  "krea_prompt_pack": "prompts на английском для keyframes / motion: cover frame, scene 1, scene 2, scene 3, final frame. Без текста внутри изображения.",
  "render_notes": "короткие notes: как собирать рилс, темп, музыка, движение камеры"
}}

Правила:
- Reel должен быть не пересказом поста, а усилением идеи.
- Визуал: объект, пустота, дистанция, тень, материал, фактура.
- Движение: медленное, почти неподвижное, дорогое.
- Не использовать людей, если они не нужны.
- Не просить генерировать текст внутри изображения.
- On-screen text должен быть коротким.
- Voiceover должен звучать как авторская fashion-колонка.
"""

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2200,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text

    print("Claude reel brief raw response:")
    print(response_text)

    brief = extract_json(response_text)

    required = [
        "job_title",
        "chosen_format",
        "visual_mode",
        "visual_hook",
        "visual_concept",
        "reel_hook",
        "reel_duration",
        "reel_script",
        "shot_list",
        "on_screen_text",
        "krea_prompt_pack",
        "render_notes",
    ]

    for key in required:
        if key not in brief:
            raise ValueError(f"Claude reel brief missing key: {key}")

    brief["chosen_format"] = "Reel"
    brief["visual_mode"] = brief.get("visual_mode") or "Hybrid"
    brief["reel_duration"] = brief.get("reel_duration") or "30 sec"

    return brief


def process_reel_brief_record(record: Dict[str, Any]) -> None:
    record_id = record["id"]
    fields = record.get("fields", {})
    existing_notes = safe_get(fields, "Render Notes", "")

    print("Reel Brief Mode detected.")
    print("Record ID:", record_id)
    print("Job Title:", safe_get(fields, "Job Title"))

    update_airtable_record(
        record_id,
        {
            "Visual Status": STATUS_RENDERING,
            "Render Notes": append_note(
                existing_notes,
                f"Reel Brief Mode started at {now_iso()}",
            ),
        },
    )

    brief = generate_reel_brief(record)

    output_links = f"""
Reel Brief Mode v1 output:
No video generated yet.
No Krea render generated yet.

This record is ready for reel review:
- Reel Hook
- Reel Script
- Shot List
- On-screen Text
- Krea Prompt Pack

Generated at:
{now_iso()}
""".strip()

    update_airtable_record(
        record_id,
        {
            "Job Title": brief["job_title"],
            "Format": "Reel",
            "Chosen Format": "Reel",
            "Visual Mode": brief["visual_mode"],
            "Visual Hook": brief["visual_hook"],
            "Visual Concept": brief["visual_concept"],
            "Reel Hook": brief["reel_hook"],
            "Reel Duration": brief["reel_duration"],
            "Reel Script": brief["reel_script"],
            "Shot List": brief["shot_list"],
            "On-screen Text": brief["on_screen_text"],
            "Krea Prompt Pack": brief["krea_prompt_pack"],
            "Krea Model Recommendation": "Manual Choice",
            "Output Links": output_links,
            "Render Notes": append_note(
                existing_notes,
                f"""
Reel Brief Mode v1 completed.

{brief["render_notes"]}

Status moved to Needs Visual Review.
Generated at {now_iso()}
""",
            ),
            "Visual Status": STATUS_NEEDS_REVIEW,
        },
    )

    print("Done. Reel brief generated and moved to Needs Visual Review.")


def download_reel_image(image_url: str, filename: str) -> str:
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    response = requests.get(image_url, timeout=120)

    if response.status_code != 200:
        raise RuntimeError(f"Could not download reel image: {image_url}")

    output_path = output_dir / filename
    output_path.write_bytes(response.content)

    print("Saved reel keyframe:", output_path)

    return str(output_path)


def build_reel_keyframe_prompts(fields: Dict[str, Any]) -> list[Dict[str, str]]:
    title = safe_get(fields, "Source Post Title") or safe_get(fields, "Job Title")
    visual_hook = safe_get(fields, "Visual Hook")
    visual_concept = safe_get(fields, "Visual Concept")
    reel_hook = safe_get(fields, "Reel Hook")
    krea_prompt_pack = safe_get(fields, "Krea Prompt Pack")

    shared_rules = f"""
Create ONE single full-screen vertical photograph.

This is not a storyboard.
This is not a moodboard.
This is not a contact sheet.
This is not a collage.
This is not a sequence.
This is not a set of images.

The entire 9:16 canvas must be one continuous photographic image from top to bottom.
One camera angle only.
One composition only.
One visual subject only.
No horizontal strips.
No panels.
No grid.
No split screen.
No multiple moments.
No multiple scenes inside the same image.

Editorial fashion still life.
Cold editorial light.
Matte surfaces.
Clear negative space.
Premium magazine background feel.
No text inside the image.
No letters.
No logos.
No people.
No hands.
No model.
No collage.
No storyboard.
No moodboard.
No contact sheet.

Topic: {title}
Visual hook: {visual_hook}
Visual concept: {visual_concept}
Opening thought: {reel_hook}
""".strip()

    def normalize_name(label: str, idx: int) -> str:
        cleaned = []
        for ch in label.lower():
            if ch.isalnum():
                cleaned.append(ch)
            else:
                cleaned.append("_")
        name = "".join(cleaned).strip("_")
        while "__" in name:
            name = name.replace("__", "_")
        return name or f"frame_{idx}"

    def parse_krea_prompt_pack(text: str) -> tuple[str, list[tuple[str, str]]]:
        if not text.strip():
            return "", []

        blocks = []
        current = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                if current:
                    blocks.append(" ".join(current).strip())
                    current = []
                continue
            current.append(line)

        if current:
            blocks.append(" ".join(current).strip())

        style_parts = []
        scene_parts = []

        for block in blocks:
            lower = block.lower()

            if lower.startswith("style rules:"):
                style_parts.append(block[len("style rules:"):].strip())
                continue

            if lower.startswith("global rules:"):
                style_parts.append(block[len("global rules:"):].strip())
                continue

            if ":" in block:
                label, desc = block.split(":", 1)
                label = label.strip()
                desc = desc.strip()

                lower_label = label.lower()
                if lower_label in {"cover frame", "scene 1", "scene 2", "scene 3", "scene 4", "scene 5", "scene 6", "final frame", "cover", "final"}:
                    scene_parts.append((label, desc))
                else:
                    # if it's not a scene label, treat it as style text
                    style_parts.append(block)
            else:
                style_parts.append(block)

        return " ".join(style_parts).strip(), scene_parts

    style_text, scene_parts = parse_krea_prompt_pack(krea_prompt_pack)

    if scene_parts:
        prompts = []

        for idx, (label, desc) in enumerate(scene_parts, start=1):
            prompt = f"""
{shared_rules}

Additional style rules:
{style_text}

Frame role: {label}

Scene instruction:
{desc}

Important:
- Generate exactly ONE image only.
- The result must be a single full-frame 9:16 photograph.
- Do not combine several scenes into one image.
- Do not create a collage, contact sheet, or storyboard.
- No repeated variations in one frame.
- Follow the scene instruction precisely.
""".strip()

            prompts.append(
                {
                    "name": normalize_name(label, idx),
                    "prompt": prompt,
                }
            )

        return prompts

    # Fallback if Krea Prompt Pack is empty
    return [
        {
            "name": "start",
            "prompt": f"""
{shared_rules}

Create a strong opening frame based on the topic and visual concept.
Use one clear visual subject only.
Make it iconic, minimal, editorial, and immediately readable.
""".strip(),
        },
        {
            "name": "middle",
            "prompt": f"""
{shared_rules}

Create a middle frame based on the topic and visual concept.
Show one symbolic object, material fragment, or coded visual detail.
Keep strong negative space and a quiet editorial feeling.
""".strip(),
        },
        {
            "name": "final",
            "prompt": f"""
{shared_rules}

Create a final frame based on the topic and visual concept.
The image should feel conclusive, restrained, and memorable.
One object or one surface only.
Very clean, very still, very editorial.
""".strip(),
        },
    ]

def process_reel_keyframes_record(record: Dict[str, Any]) -> None:
    record_id = record["id"]
    fields = record.get("fields", {})

    existing_links = safe_get(fields, "Output Links", "")
    existing_notes = safe_get(fields, "Render Notes", "")

    print("Reel Keyframes Mode detected.")
    print("Record ID:", record_id)
    print("Job Title:", safe_get(fields, "Job Title"))

    try:
        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_RENDERING,
                "Render Notes": append_note(
                    existing_notes,
                    f"Reel Keyframes Mode started at {now_iso()}",
                ),
            },
        )

        prompts = build_reel_keyframe_prompts(fields)

        results = []

        for index, item in enumerate(prompts, start=1):
            name = item["name"]
            prompt = item["prompt"]

            print("=" * 80)
            print(f"Rendering reel keyframe {index}: {name}")
            print(prompt)

            job_id = create_krea_image_job(
                prompt=prompt,
                aspect_ratio="9:16",
            )

            image_url = poll_krea_job(job_id)

            local_path = download_reel_image(
                image_url=image_url,
                filename=f"reel_keyframe_{index:02d}_{name}.png",
            )

            results.append(
                {
                    "index": index,
                    "name": name,
                    "image_url": image_url,
                    "job_id": job_id,
                    "local_path": local_path,
                }
            )

        output_lines = []

        if existing_links.strip():
            output_lines.append(existing_links.strip())
            output_lines.append("")
            output_lines.append("---")
            output_lines.append("")

        output_lines.append("Reel keyframes generated:")

        for item in results:
            output_lines.append(
                f"Keyframe {item['index']} — {item['name']}: {item['image_url']} | job_id: {item['job_id']}"
            )

        output_lines.append("")
        output_lines.append("Artifact: visual-production-output")
        output_lines.append(f"Generated at: {now_iso()}")

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_NEEDS_REVIEW,
                "Output Links": "\n".join(output_lines).strip(),
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Keyframes Mode completed.

Generated 3 vertical 9:16 keyframes:
1. Start frame
2. Middle frame
3. Final frame

No video generated yet.
Status moved to Needs Visual Review.

Generated at:
{now_iso()}
""",
                ),
            },
        )

        print("Done. Reel keyframes generated and moved to Needs Visual Review.")

    except Exception as exc:
        print("Reel Keyframes Mode failed:", repr(exc))

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_ERROR,
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Keyframes Mode failed.

Error:
{repr(exc)}

Failed at:
{now_iso()}
""",
                ),
            },
        )

        raise

def extract_reel_keyframe_urls(output_links: str) -> List[Dict[str, str]]:
    text = output_links or ""

    pattern = r"Keyframe\s*(\d+)[^\n:]*:\s*(https?://[^\s|]+)"
    matches = re.findall(pattern, text, re.IGNORECASE)

    if not matches:
        raise RuntimeError("Could not find reel keyframe image URLs in Output Links")

    seen = set()
    results: List[Dict[str, str]] = []

    for index_text, url in matches:
        index = int(index_text)

        if index in seen:
            continue

        seen.add(index)

        if index == 1:
            name = "start"
        elif index == 2:
            name = "middle"
        elif index == 3:
            name = "final"
        else:
            name = f"extra_{index}"

        results.append(
            {
                "index": str(index),
                "name": name,
                "url": url.strip().rstrip(".,)"),
            }
        )

    results.sort(key=lambda item: int(item["index"]))

    if len(results) < 3:
        raise RuntimeError(
            f"Expected 3 reel keyframe URLs, found {len(results)}: {results}"
        )

    return results
    
def parse_selected_frame_order(text: str, max_frame: int) -> list[int]:
    text = text or ""
    numbers = re.findall(r"\d+", text)

    result = []
    seen = set()

    for number_text in numbers:
        number = int(number_text)

        if number < 1 or number > max_frame:
            continue

        if number in seen:
            continue

        seen.add(number)
        result.append(number)

    if not result:
        return list(range(1, max_frame + 1))

    return result


def apply_selected_frame_order(
    keyframes: list[Dict[str, str]],
    selected_frame_order_text: str,
    ) -> list[Dict[str, str]]:
    selected_order = parse_selected_frame_order(
        selected_frame_order_text,
        max_frame=len(keyframes),
    )

    keyframe_by_index = {
        int(item["index"]): item
        for item in keyframes
        if str(item.get("index", "")).isdigit()
    }

    selected = []

    for index in selected_order:
        item = keyframe_by_index.get(index)
        if item:
            selected.append(item)

    if not selected:
        return keyframes

    print("Selected frame order:", ",".join(str(x) for x in selected_order))
    print("Selected keyframes:", [item["index"] for item in selected])

    return selected

def build_reel_motion_prompt(fields: Dict[str, Any]) -> str:
    title = safe_get(fields, "Source Post Title") or safe_get(fields, "Job Title")
    reel_hook = safe_get(fields, "Reel Hook")
    visual_concept = safe_get(fields, "Visual Concept")

    return f"""
Create a 5 second vertical fashion editorial video from the provided start image.

ABSOLUTE RULE:
The provided image is the locked visual reference.
Do not redesign it.
Do not reinterpret it.
Do not change the object.
Do not change the composition.
Do not change the material.
Do not change the shape.
Do not add new objects.

The video must preserve:
- the exact handbag / object identity
- the same silhouette
- the same leather texture
- the same stitching
- the same metal hardware
- the same color palette
- the same background
- the same negative space
- the same lighting mood
- the same editorial still-life atmosphere

Allowed movement only:
- extremely slow camera push-in
- very subtle parallax
- barely visible breathing in the light
- slight atmospheric depth
- tiny shadow movement
- almost still image

Forbidden:
- no morphing
- no transformation
- no object deformation
- no new fabric
- no extra folds
- no additional accessories
- no new handbag parts
- no people
- no hands
- no model
- no logo
- no text
- no letters
- no cuts
- no montage
- no fast movement
- no TikTok style
- no commercial advertising energy
- no product demo feeling
- no zoom jump
- no camera shake
- no fantasy effect

The video should feel like:
a moving fashion magazine still life,
quiet luxury,
editorial intelligence,
distance,
restraint,
negative space.

Topic:
{title}

Visual concept:
{visual_concept}

Reel hook:
{reel_hook}

Final direction:
Make the image feel alive only through camera and atmosphere.
The object itself must remain stable and unchanged.
""".strip()


def create_krea_video_job(start_image_url: str, prompt: str, duration: int = 5) -> str:
    url = f"{KREA_API_BASE}/generate/video/kling/kling-2.5"

    payload = {
        "prompt": prompt[:3000],
        "start_image": start_image_url,
        "aspect_ratio": "9:16",
        "duration": duration,
    }

    response = requests.post(
        url,
        headers=krea_headers(),
        json=payload,
        timeout=60,
    )

    print("Create Krea video job URL:", url)
    print("Create Krea video job status:", response.status_code)
    print("Create Krea video job preview:", shorten(response.text, 1200))

    response.raise_for_status()

    data = response.json()
    job_id = data.get("job_id")

    if not job_id:
        raise RuntimeError(f"Krea video did not return job_id: {data}")

    return job_id


def extract_video_url_from_krea_result(data: Dict[str, Any]) -> str:
    result = data.get("result") or {}

    candidates: List[str] = []

    def collect_urls(obj: Any) -> None:
        if isinstance(obj, str):
            if obj.startswith("http"):
                candidates.append(obj)
            return

        if isinstance(obj, list):
            for item in obj:
                collect_urls(item)
            return

        if isinstance(obj, dict):
            for value in obj.values():
                collect_urls(value)

    collect_urls(result)

    if not candidates:
        raise RuntimeError(f"Krea video completed but no URL found: {data}")

    for url in candidates:
        lowered = url.lower()
        if ".mp4" in lowered or "video" in lowered:
            return url

    return candidates[0]


def poll_krea_video_job(job_id: str, max_wait_seconds: int = 600) -> str:
    url = f"{KREA_API_BASE}/jobs/{job_id}"
    started = time.time()

    while time.time() - started < max_wait_seconds:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {KREA_API_KEY}"},
            timeout=60,
        )

        print("Poll Krea video URL:", url)
        print("Poll Krea video status:", response.status_code)
        print("Poll Krea video preview:", shorten(response.text, 1200))

        response.raise_for_status()

        data = response.json()
        status = data.get("status")

        if status == "completed":
            return extract_video_url_from_krea_result(data)

        if status in {"failed", "cancelled", "canceled"}:
            raise RuntimeError(f"Krea video job failed: {data}")

        time.sleep(8)

    raise TimeoutError(f"Krea video job timed out: {job_id}")


def download_reel_video(video_url: str, filename: str) -> str:
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    response = requests.get(video_url, timeout=180)

    if response.status_code != 200:
        raise RuntimeError(f"Could not download reel video: {video_url}")

    output_path = output_dir / filename
    output_path.write_bytes(response.content)

    print("Saved reel motion clip:", output_path)

    return str(output_path)


def process_reel_motion_record(record: Dict[str, Any]) -> None:
    record_id = record["id"]
    fields = record.get("fields", {})

    existing_links = safe_get(fields, "Output Links", "")
    existing_notes = safe_get(fields, "Render Notes", "")

    print("Reel Motion Mode detected.")
    print("Record ID:", record_id)
    print("Job Title:", safe_get(fields, "Job Title"))

    try:
        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_RENDERING,
                "Render Notes": append_note(
                    existing_notes,
                    f"Reel Motion Mode started at {now_iso()}",
                ),
            },
        )

        keyframes = extract_reel_keyframe_urls(existing_links)
        selected_frame_order_text = safe_get(fields, "Selected Frame Order", "")
        keyframes = apply_selected_frame_order(
        keyframes,
        selected_frame_order_text,
        )

        base_prompt = build_reel_motion_prompt(fields)

        results = []

        for item in keyframes:
            index = item["index"]
            name = item["name"]
            start_image_url = item["url"]

            prompt = f"""
{base_prompt}

This motion clip is based on keyframe {index}: {name}.
Create only this one short segment.
Preserve this exact source image.
Do not introduce visual elements from other keyframes.
""".strip()

            print("=" * 80)
            print(f"Rendering reel motion clip {index}: {name}")
            print("Using start image URL:", start_image_url)
            print("Motion prompt:")
            print(prompt)

            job_id = create_krea_video_job(
                start_image_url=start_image_url,
                prompt=prompt,
                duration=5,
            )

            video_url = poll_krea_video_job(job_id)

            local_path = download_reel_video(
                video_url=video_url,
                filename=f"reel_motion_clip_{int(index):02d}_{name}.mp4",
            )

            results.append(
                {
                    "index": index,
                    "name": name,
                    "video_url": video_url,
                    "job_id": job_id,
                    "local_path": local_path,
                }
            )

        output_lines = []

        if existing_links.strip():
            output_lines.append(existing_links.strip())
            output_lines.append("")
            output_lines.append("---")
            output_lines.append("")

        output_lines.append("Reel motion clips generated:")

        for item in results:
            output_lines.append(
                f"Motion clip {item['index']} — {item['name']}: {item['video_url']} | job_id: {item['job_id']}"
            )
            output_lines.append(f"Local file: {item['local_path']}")

        output_lines.append("")
        output_lines.append("Artifact: visual-production-outputs")
        output_lines.append(f"Generated at: {now_iso()}")

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_NEEDS_REVIEW,
                "Output Links": "\n".join(output_lines).strip(),
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Motion Mode completed.

Generated 3 vertical 5 sec motion clips:
1. Start
2. Middle
3. Final

No final reel assembly yet.
Status moved to Needs Visual Review.

Generated at:
{now_iso()}
""",
                ),
            },
        )

        print("Done. 3 reel motion clips generated and moved to Needs Visual Review.")

    except Exception as exc:
        print("Reel Motion Mode failed:", repr(exc))

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_ERROR,
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Motion Mode failed.

Error:
{repr(exc)}

Failed at:
{now_iso()}
""",
                ),
            },
        )

        raise
def extract_reel_motion_clip_urls(output_links: str) -> List[Dict[str, str]]:
    text = output_links or ""

    pattern = r"Motion clip\s*(\d+)[^\n:]*:\s*(https?://[^\s|]+)"
    matches = re.findall(pattern, text, re.IGNORECASE)

    if not matches:
        raise RuntimeError("Could not find reel motion clip URLs in Output Links")

    seen = set()
    results: List[Dict[str, str]] = []

    for index_text, url in matches:
        index = int(index_text)

        if index in seen:
            continue

        seen.add(index)

        if index == 1:
            name = "start"
        elif index == 2:
            name = "middle"
        elif index == 3:
            name = "final"
        else:
            name = f"extra_{index}"

        results.append(
            {
                "index": str(index),
                "name": name,
                "url": url.strip().rstrip(".,)"),
            }
        )

    results.sort(key=lambda item: int(item["index"]))

    if len(results) < 3:
        raise RuntimeError(
            f"Expected 3 reel motion clip URLs, found {len(results)}: {results}"
        )

    return results


def download_motion_clip(video_url: str, filename: str) -> str:
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    response = requests.get(video_url, timeout=180)

    if response.status_code != 200:
        raise RuntimeError(f"Could not download motion clip: {video_url}")

    output_path = output_dir / filename
    output_path.write_bytes(response.content)

    print("Downloaded motion clip:", output_path)

    return str(output_path)


def assemble_reel_with_ffmpeg(clip_paths: List[str], output_filename: str = "final_reel_v1.mp4") -> str:
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    concat_file = output_dir / "concat_list.txt"

    with concat_file.open("w", encoding="utf-8") as f:
        for path in clip_paths:
            absolute_path = Path(path).resolve()
            f.write(f"file '{absolute_path}'\n")

    output_path = output_dir / output_filename

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        str(output_path),
    ]

    print("Running ffmpeg concat:")
    print(" ".join(command))

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    print("ffmpeg stdout:")
    print(result.stdout)
    print("ffmpeg stderr:")
    print(result.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed with code {result.returncode}")

    print("Final reel assembled:", output_path)

    return str(output_path)


def process_reel_assembly_record(record: Dict[str, Any]) -> None:
    record_id = record["id"]
    fields = record.get("fields", {})

    existing_links = safe_get(fields, "Output Links", "")
    existing_notes = safe_get(fields, "Render Notes", "")

    print("Reel Assembly Mode detected.")
    print("Record ID:", record_id)
    print("Job Title:", safe_get(fields, "Job Title"))

    try:
        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_RENDERING,
                "Render Notes": append_note(
                    existing_notes,
                    f"Reel Assembly Mode started at {now_iso()}",
                ),
            },
        )

        clips = extract_reel_motion_clip_urls(existing_links)

        local_clip_paths = []

        for item in clips:
            index = int(item["index"])
            name = item["name"]
            url = item["url"]

            local_path = download_motion_clip(
                video_url=url,
                filename=f"source_motion_clip_{index:02d}_{name}.mp4",
            )

            local_clip_paths.append(local_path)

        final_reel_path = assemble_reel_with_ffmpeg(
            clip_paths=local_clip_paths,
            output_filename="final_reel_v1.mp4",
        )

        output_lines = []

        if existing_links.strip():
            output_lines.append(existing_links.strip())
            output_lines.append("")
            output_lines.append("---")
            output_lines.append("")

        output_lines.append("Final reel assembled:")
        output_lines.append(f"Local file: {final_reel_path}")
        output_lines.append("Artifact: visual-production-outputs")
        output_lines.append(f"Generated at: {now_iso()}")

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_NEEDS_REVIEW,
                "Output Links": "\n".join(output_lines).strip(),
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Assembly Mode completed.

Assembled 3 motion clips into final_reel_v1.mp4.
No text overlay yet.
No voiceover yet.
Status moved to Needs Visual Review.

Generated at:
{now_iso()}
""",
                ),
            },
        )

        print("Done. Final reel assembled and moved to Needs Visual Review.")

    except Exception as exc:
        print("Reel Assembly Mode failed:", repr(exc))

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_ERROR,
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Assembly Mode failed.

Error:
{repr(exc)}

Failed at:
{now_iso()}
""",
                ),
            },
        )

        raise   
def clean_overlay_line(line: str) -> str:
    line = line or ""
    line = line.strip()

    # remove bullets / numbering
    line = re.sub(r"^\s*[-•*]\s*", "", line)
    line = re.sub(r"^\s*\d+[.)]\s*", "", line)

    # remove scene / shot labels
    line = re.sub(
        r"^\s*(scene|shot|сцена|кадр)\s*\d+\s*[:.-]\s*",
        "",
        line,
        flags=re.IGNORECASE,
    )

    # remove timecodes at the beginning:
    # 0-3 sec:, 3–8 sec:, 0:03-0:08:, etc.
    line = re.sub(
        r"^\s*\d+\s*[-–]\s*\d+\s*(sec|seconds|с|сек|секунд)\s*[:.-]\s*",
        "",
        line,
        flags=re.IGNORECASE,
    )

    line = re.sub(
        r"^\s*\d+:\d+\s*[-–]\s*\d+:\d+\s*[:.-]\s*",
        "",
        line,
        flags=re.IGNORECASE,
    )

    # remove accidental remaining internal timecode
    line = re.sub(
        r"\b\d+\s*[-–]\s*\d+\s*(sec|seconds|с|сек|секунд)\s*[:.-]\s*",
        "",
        line,
        flags=re.IGNORECASE,
    )

    line = line.strip(" \"'“”«»|")
    line = re.sub(r"\s+", " ", line).strip()

    return line


def wrap_overlay_text(text: str, width: int = 30, max_lines: int = 2) -> str:
    text = clean_overlay_line(text)

    if not text:
        return ""

    wrapped = textwrap.wrap(
        text,
        width=width,
        break_long_words=False,
        replace_whitespace=False,
    )

    wrapped = wrapped[:max_lines]

    return "\n".join(wrapped)


def normalize_field_name(name: str) -> str:
    name = str(name or "").strip().lower()

    # protect against accidental Cyrillic letters in field names
    name = name.replace("о", "o")
    name = name.replace("е", "e")
    name = name.replace("а", "a")

    name = re.sub(r"[^a-z0-9а-яё]+", "", name)

    return name


def get_field_fuzzy(fields: Dict[str, Any], possible_names: List[str], default: str = "") -> str:
    wanted = {normalize_field_name(name) for name in possible_names}

    for actual_name, value in fields.items():
        if normalize_field_name(actual_name) in wanted:
            if value is None:
                return default
            if isinstance(value, list):
                return ", ".join(str(x) for x in value if x is not None)
            return str(value)

    return default


def split_overlay_text_blocks(raw_text: str) -> List[str]:
    raw_text = (raw_text or "").replace("\r\n", "\n").strip()

    if not raw_text:
        return []

    # Main separator: ---
    if re.search(r"(?m)^\s*-{3,}\s*$", raw_text):
        chunks = re.split(r"(?m)^\s*-{3,}\s*$", raw_text)
    else:
        # Fallback: empty line separates text blocks.
        chunks = re.split(r"\n\s*\n", raw_text)

    blocks: List[str] = []

    for chunk in chunks:
        lines: List[str] = []

        for raw_line in chunk.splitlines():
            line = raw_line.strip()

            if not line:
                continue

            # Remove bullets and numbering.
            line = re.sub(r"^\s*[-•*]\s*", "", line)
            line = re.sub(r"^\s*(?:Overlay\s*)?\d+\s*[:.)-]\s*", "", line, flags=re.I)

            if line:
                lines.append(line)

        text = "\n".join(lines).strip()

        if text:
            blocks.append(text)

    return blocks


def collect_overlay_texts(fields: Dict[str, Any]) -> List[str]:
    overlay_texts: List[str] = []

    # 1. Main universal field.
    overlay_script = safe_get(fields, "Overlay Script", "").strip()

    if overlay_script:
        overlay_texts = split_overlay_text_blocks(overlay_script)

    # 2. Fallback: existing On-screen Text field.
    if not overlay_texts:
        on_screen_text = safe_get(fields, "On-screen Text", "").strip()

        if on_screen_text:
            overlay_texts = split_overlay_text_blocks(on_screen_text)

    # 3. Fallback: old Overlay 1 / Overlay 2 / Overlay 3 / ... fields.
    # This is dynamic: it supports Overlay 1 through Overlay 20 without changing code.
    if not overlay_texts:
        numbered_overlays = []

        for key, value in fields.items():
            match = re.fullmatch(r"Overlay\s+(\d+)", str(key).strip(), flags=re.I)

            if not match:
                continue

            text = str(value or "").strip()

            if not text:
                continue

            index = int(match.group(1))
            numbered_overlays.append((index, text))

        numbered_overlays.sort(key=lambda item: item[0])

        overlay_texts = [text for _, text in numbered_overlays]

    # 4. Last fallback: current record hooks only.
    # No old hardcoded phrases. No previous reel topics.
    if not overlay_texts:
        for key in ["Reel Hook", "Visual Hook", "Source Hook", "Source Post Title", "Job Title"]:
            value = safe_get(fields, key, "").strip()

            if value:
                overlay_texts.append(value)
                break

    if not overlay_texts:
        overlay_texts = ["EDITORIAL NOTE"]

    # Remove exact duplicates.
    clean_texts: List[str] = []
    seen = set()

    for text in overlay_texts:
        text = str(text or "").strip()

        if not text:
            continue

        key = re.sub(r"\s+", " ", text).lower()

        if key in seen:
            continue

        seen.add(key)
        clean_texts.append(text)

    print("Overlay texts:")
    for idx, text in enumerate(clean_texts, start=1):
        print(f"{idx}: {text}")

    return clean_texts


# Backward-compatible wrapper.
# Old code may still call parse_on_screen_texts(...), but now it uses the universal logic.
def parse_on_screen_texts(fields: Dict[str, Any], expected_count: int = 0) -> List[str]:
    return collect_overlay_texts(fields)

def write_drawtext_file(text: str, filename: str) -> str:
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    path = output_dir / filename
    path.write_text(text, encoding="utf-8")

    return str(path.resolve())


def ffmpeg_escape_text(value: str) -> str:
    value = value or ""
    value = value.replace("\\", "\\\\")
    value = value.replace(":", "\\:")
    value = value.replace("'", "\\'")
    return value


def get_video_duration_seconds(input_video_path: str) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_video_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("Could not read video duration with ffprobe.")
        print(result.stderr)
        return 0.0

    try:
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def choose_reel_overlay_font_size(text: str) -> int:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    longest_line = max((len(line) for line in lines), default=len(str(text or "")))

    if longest_line <= 18:
        return 58

    if longest_line <= 26:
        return 52

    if longest_line <= 34:
        return 46

    return 40


def add_on_screen_text_to_reel(
    input_video_path: str,
    overlay_texts: List[str],
    output_filename: str = "final_reel_text_v1.mp4",
    segment_duration_seconds: float = 0.0,
) -> str:
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    output_path = output_dir / output_filename

    overlay_texts = [str(text or "").strip() for text in overlay_texts if str(text or "").strip()]

    if not overlay_texts:
        overlay_texts = ["EDITORIAL NOTE"]

    text_files = []

    for idx, text in enumerate(overlay_texts, start=1):
        text_files.append(
            write_drawtext_file(
                text=text,
                filename=f"onscreen_text_{idx:02d}.txt",
            )
        )

    total_segments = max(1, len(text_files))

    video_duration = get_video_duration_seconds(input_video_path)

    if video_duration <= 0:
        video_duration = total_segments * 5.0

    # Universal rule:
    # 2 texts = each gets half of the reel.
    # 5 texts = each gets one fifth.
    # 7 texts = each gets one seventh.
    segment_duration = video_duration / total_segments

    font_regular = globals().get(
        "FONT_REGULAR",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    )

    font_bold = globals().get(
        "FONT_BOLD",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    )

    brand_name = globals().get("BRAND_NAME", "SV FASHION MEDIA")
    instagram_handle = globals().get("INSTAGRAM_HANDLE", "@sv_fashionacademy")

    filters = []

    # Brand header
    filters.append(
        "drawtext="
        f"fontfile='{font_regular}':"
        f"text='{ffmpeg_escape_text(brand_name)}':"
        "x=80:"
        "y=70:"
        "fontsize=26:"
        "fontcolor=white@0.92"
    )

    # Small brand underline
    filters.append(
        "drawbox="
        "x=80:"
        "y=125:"
        "w=120:"
        "h=2:"
        "color=white@0.82:"
        "t=fill"
    )

    # Instagram handle
    filters.append(
        "drawtext="
        f"fontfile='{font_regular}':"
        f"text='{ffmpeg_escape_text(instagram_handle)}':"
        "x=80:"
        "y=h-95:"
        "fontsize=22:"
        "fontcolor=white@0.92"
    )

    for idx, text_file in enumerate(text_files, start=1):
        start = (idx - 1) * segment_duration

        if idx == total_segments:
            end = video_duration + 0.25
        else:
            end = idx * segment_duration

        counter = f"{idx:02d}/{total_segments:02d}"
        text = overlay_texts[idx - 1]
        font_size = choose_reel_overlay_font_size(text)

        # Counter top-right
        filters.append(
            "drawtext="
            f"fontfile='{font_regular}':"
            f"text='{counter}':"
            f"enable='between(t,{start:.2f},{end:.2f})':"
            "x=w-tw-80:"
            "y=70:"
            "fontsize=22:"
            "fontcolor=white@0.92"
        )

        # Main editorial text block
        filters.append(
            "drawtext="
            f"fontfile='{font_bold}':"
            f"textfile='{text_file}':"
            f"enable='between(t,{start:.2f},{end:.2f})':"
            "x=80:"
            "y=h*0.64:"
            f"fontsize={font_size}:"
            "fontcolor=white:"
            "line_spacing=8:"
            "box=1:"
            "boxcolor=black@0.20:"
            "boxborderw=24"
        )

    filter_chain = ",".join(filters)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_video_path),
        "-vf",
        filter_chain,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        str(output_path),
    ]

    print("Running ffmpeg text overlay:")
    print(" ".join(command))

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    print("ffmpeg stdout:")
    print(result.stdout)
    print("ffmpeg stderr:")
    print(result.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg text overlay failed with code {result.returncode}")

    print("Final reel with text created:", output_path)

    return str(output_path)

def process_reel_text_overlay_record(record: Dict[str, Any]) -> None:
    record_id = record["id"]
    fields = record.get("fields", {})

    existing_links = safe_get(fields, "Output Links", "")
    existing_notes = safe_get(fields, "Render Notes", "")

    print("Reel Text Overlay Mode detected.")
    print("Record ID:", record_id)
    print("Job Title:", safe_get(fields, "Job Title"))

    try:
        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_RENDERING,
                "Render Notes": append_note(
                    existing_notes,
                    f"Reel Text Overlay Mode started at {now_iso()}",
                ),
            },
        )

        # Re-download the 3 motion clips and assemble again in this runner
        clips = extract_reel_motion_clip_urls(existing_links)

        local_clip_paths = []

        for item in clips:
            index = int(item["index"])
            name = item["name"]
            url = item["url"]

            local_path = download_motion_clip(
                video_url=url,
                filename=f"text_source_motion_clip_{index:02d}_{name}.mp4",
            )

            local_clip_paths.append(local_path)

        final_reel_path = assemble_reel_with_ffmpeg(
            clip_paths=local_clip_paths,
            output_filename="final_reel_v1_for_text.mp4",
        )

        expected_overlay_count = len(local_clip_paths)

        overlay_texts = collect_overlay_texts(fields)

        print("Overlay texts:")
        for idx, text in enumerate(overlay_texts, start=1):
            print(f"{idx}: {text}")

        final_text_reel_path = add_on_screen_text_to_reel(
            input_video_path=final_reel_path,
            overlay_texts=overlay_texts,
            output_filename="final_reel_text_v1.mp4",
        )

        cover_title = (
            safe_get(fields, "Reel Cover Title")
            or safe_get(fields, "Source Post Title")
            or safe_get(fields, "Job Title")
            or "SV FASHION MEDIA"
        )

        reel_cover_path = create_reel_cover_from_keyframe(
            output_links=existing_links,
            title=cover_title,
            fields=fields,
        )

        output_lines = []

        if existing_links.strip():
            output_lines.append(existing_links.strip())
            output_lines.append("")
            output_lines.append("---")
            output_lines.append("")

        output_lines.append("Final reel with text generated:")
        output_lines.append(f"Local file: {final_text_reel_path}")
        output_lines.append("")
        output_lines.append("Reel cover generated:")
        output_lines.append(f"Local file: {reel_cover_path}")
        output_lines.append("")
        output_lines.append("Artifact: visual-production-outputs")
        output_lines.append(f"Generated at: {now_iso()}")

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_NEEDS_REVIEW,
                "Output Links": "\n".join(output_lines).strip(),
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Text Overlay Mode completed.

Created final_reel_text_v1.mp4.
Added 3 on-screen text overlays.
No voiceover yet.
No music yet.
Status moved to Needs Visual Review.

Generated at:
{now_iso()}
""",
                ),
            },
        )

        print("Done. Final reel with text generated and moved to Needs Visual Review.")

    except Exception as exc:
        print("Reel Text Overlay Mode failed:", repr(exc))

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_ERROR,
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Text Overlay Mode failed.

Error:
{repr(exc)}

Failed at:
{now_iso()}
""",
                ),
            },
        )

        raise 
def get_reel_cover_title(fields: Dict[str, Any], overlay_texts: List[str]) -> str:
    title = safe_get(fields, "Reel Cover Title", "")

    if not title:
        title = safe_get(fields, "Overlay 1", "")

    if not title and overlay_texts:
        title = overlay_texts[0]

    if not title:
        title = safe_get(fields, "Reel Hook", "")

    if not title:
        title = "Luxury продаёт дистанцию"

    title = clean_overlay_line(title)
    title = title.replace("\n", " ").strip()

    return title


def fit_cover_image_to_canvas(image: Image.Image, width: int = 1080, height: int = 1920) -> Image.Image:
    image = image.convert("RGB")

    scale = max(width / image.width, height / image.height)
    new_w = int(image.width * scale)
    new_h = int(image.height * scale)

    resized = image.resize((new_w, new_h), Image.LANCZOS)

    left = max(0, (new_w - width) // 2)
    top = max(0, (new_h - height) // 2)

    return resized.crop((left, top, left + width, top + height))


def draw_reel_cover_text(base: Image.Image, title: str) -> Image.Image:
    canvas = base.convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    width, height = canvas.size

    title = title.upper()
    font = get_font(FONT_BOLD, 64)

    max_text_width = int(width * 0.78)
    wrapped_lines = wrap_text_lines(draw, title, font, max_text_width)

    wrapped_lines = wrapped_lines[:3]

    line_spacing = 10
    line_heights = []

    max_line_width = 0
    total_text_height = 0

    for line in wrapped_lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]

        max_line_width = max(max_line_width, line_w)
        line_heights.append(line_h)
        total_text_height += line_h + line_spacing

    total_text_height = max(0, total_text_height - line_spacing)

    padding_x = 32
    padding_y = 24

    box_x = 70
    box_y = int(height * 0.66)

    box_w = max_line_width + padding_x * 2
    box_h = total_text_height + padding_y * 2

    draw.rectangle(
        [box_x, box_y, box_x + box_w, box_y + box_h],
        fill=(0, 0, 0, 78),
    )

    cursor_y = box_y + padding_y

    for idx, line in enumerate(wrapped_lines):
        draw.text(
            (box_x + padding_x, cursor_y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
        )

        cursor_y += line_heights[idx] + line_spacing

    result = Image.alpha_composite(canvas, overlay).convert("RGB")

    return result


def draw_reel_cover_text(image: Image.Image, title: str) -> Image.Image:
    image = image.convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")

    width, height = image.size

    brand_name = BRAND_NAME
    handle = INSTAGRAM_HANDLE
    title = (title or "").strip().upper()

    # Font paths
    regular_font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf"
    bold_font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"

    brand_font = ImageFont.truetype(regular_font_path, 30)
    handle_font = ImageFont.truetype(regular_font_path, 26)
    title_font = ImageFont.truetype(bold_font_path, 62)

    margin_x = 80

    # Subtle dark gradient for readability, not a heavy grey block.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay, "RGBA")

    for y in range(height):
        if y < int(height * 0.45):
            alpha = 0
        else:
            alpha = int(95 * ((y - height * 0.45) / (height * 0.55)))
        overlay_draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))

    image = Image.alpha_composite(image.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(image, "RGBA")

    # Brand header
    draw.text(
        (margin_x, 70),
        brand_name,
        font=brand_font,
        fill=(255, 255, 255, 235),
    )

    # Small line under brand
    draw.rectangle(
        (margin_x, 125, margin_x + 120, 128),
        fill=(255, 255, 255, 220),
    )

    # Handle bottom-left
    draw.text(
        (margin_x, height - 105),
        handle,
        font=handle_font,
        fill=(255, 255, 255, 235),
    )

    def wrap_text_by_pixels(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
        words = text.split()
        lines = []
        current = ""

        for word in words:
            test = word if not current else current + " " + word
            bbox = draw.textbbox((0, 0), test, font=font)
            test_width = bbox[2] - bbox[0]

            if test_width <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word

        if current:
            lines.append(current)

        return lines

    max_text_width = width - margin_x * 2
    title_lines = wrap_text_by_pixels(title, title_font, max_text_width)

    # Limit cover text to 3 lines.
    title_lines = title_lines[:3]

    line_height = 72
    text_block_height = len(title_lines) * line_height

    # Position: lower third, but with air.
    text_x = margin_x
    text_y = int(height * 0.66)

    # Very restrained black plaque, not grey.
    plaque_padding_x = 28
    plaque_padding_y = 22

    max_line_width = 0
    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        max_line_width = max(max_line_width, bbox[2] - bbox[0])

    plaque = (
        text_x - plaque_padding_x,
        text_y - plaque_padding_y,
        text_x + max_line_width + plaque_padding_x,
        text_y + text_block_height + plaque_padding_y,
    )

    draw.rectangle(
        plaque,
        fill=(0, 0, 0, 82),
    )

    for idx, line in enumerate(title_lines):
        draw.text(
            (text_x, text_y + idx * line_height),
            line,
            font=title_font,
            fill=(255, 255, 255, 255),
        )

    return image.convert("RGB")
def create_reel_cover_from_keyframe(
    output_links: str,
    title: str,
    output_filename: str = "reel_cover_v1.png",
    fields: Dict[str, Any] = None,
) -> str:
    fields = fields or {}

    keyframes = extract_reel_keyframe_urls(output_links)

    if not keyframes:
        raise RuntimeError("No keyframe URL found for reel cover")

    cover_frame_index_text = safe_get(fields, "Cover Frame Index", "").strip()

    cover_frame_index = None

    if cover_frame_index_text:
        match = re.search(r"\d+", cover_frame_index_text)
        if match:
            cover_frame_index = int(match.group(0))

    # If Cover Frame Index is empty, use first selected frame.
    if cover_frame_index is None:
        selected_frame_order_text = safe_get(fields, "Selected Frame Order", "").strip()
        selected_numbers = re.findall(r"\d+", selected_frame_order_text)

        if selected_numbers:
            cover_frame_index = int(selected_numbers[0])

    selected_keyframe = None

    if cover_frame_index is not None:
        for item in keyframes:
            item_index = str(item.get("index", ""))

            if item_index.isdigit() and int(item_index) == cover_frame_index:
                selected_keyframe = item
                break

    if selected_keyframe is None:
        selected_keyframe = keyframes[0]

    keyframe_url = selected_keyframe["url"]

    print("Reel cover selected keyframe:", selected_keyframe)

    response = requests.get(keyframe_url, timeout=120)

    if response.status_code != 200:
        raise RuntimeError(f"Could not download keyframe for reel cover: {keyframe_url}")

    cover_title = safe_get(fields, "Reel Cover Title", "").strip()

    if not cover_title:
        cover_title = title

    image = Image.open(BytesIO(response.content)).convert("RGB")
    image = fit_cover_image_to_canvas(image, width=1080, height=1920)
    image = draw_reel_cover_text(image, cover_title)

    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    output_path = output_dir / output_filename
    image.save(output_path, format="PNG", quality=95)

    print("Reel cover created:", output_path)

    return str(output_path)
def generate_final_reel_caption(record: Dict[str, Any]) -> Dict[str, Any]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    fields = record.get("fields", {})

    source_title = safe_get(fields, "Source Post Title")
    reel_hook = safe_get(fields, "Reel Hook")
    reel_script = safe_get(fields, "Reel Script")
    overlay_1 = safe_get(fields, "Overlay 1")
    overlay_2 = safe_get(fields, "Overlay 2")
    overlay_3 = safe_get(fields, "Overlay 3")
    overlay_4 = safe_get(fields, "Overlay 4")
    overlay_5 = safe_get(fields, "Overlay 5")
    visual_concept = safe_get(fields, "Visual Concept")

    system_prompt = f"""
Ты — fashion editor и автор Instagram caption для {BRAND_NAME}.

Стиль:
- умно
- сухо
- точно
- без глянцевой восторженности
- без emoji
- без продажного тона
- без "вдохновляемся"
- без "must-have"
- без дешёвых hashtags

Формат:
короткий Instagram caption для fashion-media reel.
"""

    user_prompt = f"""
Сделай caption к Instagram Reel.

Контекст:
Source title: {source_title}

Reel hook:
{reel_hook}

Visual concept:
{visual_concept}

Reel script:
{reel_script}

On-screen text:
1. {overlay_1}
2. {overlay_2}
3. {overlay_3}
4. {overlay_4}
5. {overlay_5}


Верни строго валидный JSON без markdown.

Схема:
{{
  "final_reel_caption": "готовый caption на русском"
}}

Требования к caption:
- 5–9 коротких строк
- без emoji
- без hashtags
- без прямой продажи
- звучит как короткая fashion-media колонка
- тема должна строго соответствовать Source title, Reel hook, Visual concept и Reel script
- не использовать старые темы из предыдущих рилов
- не добавлять luxury / дистанцию / желание / недоступность, если этого нет в текущем контексте
- финальная строка должна быть сильной, но не пафосной
"""

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1200,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text

    print("Claude final reel caption raw response:")
    print(response_text)

    data = extract_json(response_text)

    if "final_reel_caption" not in data:
        raise ValueError("Claude response missing final_reel_caption")

    data["final_reel_caption"] = str(data["final_reel_caption"]).strip()

    return data


def process_reel_caption_record(record: Dict[str, Any]) -> None:
    record_id = record["id"]
    fields = record.get("fields", {})

    existing_links = safe_get(fields, "Output Links", "")
    existing_notes = safe_get(fields, "Render Notes", "")

    print("Final Reel Caption Mode detected.")
    print("Record ID:", record_id)
    print("Job Title:", safe_get(fields, "Job Title"))

    try:
        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_RENDERING,
                "Render Notes": append_note(
                    existing_notes,
                    f"Final Reel Caption Mode started at {now_iso()}",
                ),
            },
        )

        caption_data = generate_final_reel_caption(record)
        final_caption = caption_data["final_reel_caption"]

        output_lines = []

        if existing_links.strip():
            output_lines.append(existing_links.strip())
            output_lines.append("")
            output_lines.append("---")
            output_lines.append("")

        output_lines.append("Final reel caption generated:")
        output_lines.append("Stored in Airtable field: Final Reel Caption")
        output_lines.append("")
        output_lines.append(build_ready_for_buffer_summary())
        output_lines.append("")
        output_lines.append(f"Generated at: {now_iso()}")

        update_airtable_record(
            record_id,
            {
                "Final Reel Caption": final_caption,
                "Visual Status": STATUS_READY_FOR_BUFFER,
                "Output Links": "\n".join(output_lines).strip(),
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Final Reel Caption Mode completed.

Final Reel Caption generated.
Reel package is ready for Buffer / manual posting.

Status moved to Ready for Buffer.

Generated at:
{now_iso()}
""",
                ),
            },
        )

        print("Done. Final reel caption generated and moved to Ready for Buffer.")
        print("Final caption:")
        print(final_caption)

    except Exception as exc:
        print("Final Reel Caption Mode failed:", repr(exc))

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_ERROR,
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Final Reel Caption Mode failed.

Error:
{repr(exc)}

Failed at:
{now_iso()}
""",
                ),
            },
        )

        raise
def build_ready_for_buffer_summary() -> str:
    lines = []

    lines.append("READY FOR BUFFER PACKAGE")
    lines.append("")
    lines.append("Files in GitHub Actions artifact:")
    lines.append("- final_reel_sound_v1.mp4")
    lines.append("- reel_cover_v1.png")
    lines.append("")
    lines.append("Artifact name:")
    lines.append("visual-production-outputs")
    lines.append("")

    if GITHUB_RUN_URL:
        lines.append("GitHub run:")
        lines.append(GITHUB_RUN_URL)
        lines.append("")

    if GITHUB_RUN_ID:
        lines.append(f"GitHub run id: {GITHUB_RUN_ID}")
        lines.append("")

    lines.append("Manual publishing checklist:")
    lines.append("1. Open the GitHub run.")
    lines.append("2. Download artifact: visual-production-outputs.")
    lines.append("3. Upload final_reel_sound_v1.mp4 to Buffer / Instagram.")
    lines.append("4. Use reel_cover_v1.png as cover.")
    lines.append("5. Copy Final Reel Caption.")
    lines.append("6. After scheduling, set Visual Status = Sent to Buffer.")

    return "\n".join(lines)
def add_ambient_sound_to_reel(
    input_video_path: str,
    output_filename: str = "final_reel_sound_v1.mp4",
) -> str:
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    output_path = output_dir / output_filename

    video_duration = get_video_duration_seconds(input_video_path)

    if video_duration <= 0:
        video_duration = 60.0

    # Make audio slightly longer than video so ffmpeg never cuts the video.
    audio_duration = video_duration + 1.0

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_video_path),

        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=color=brown:amplitude=0.28:duration={audio_duration:.2f}",

        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=82:sample_rate=48000:duration={audio_duration:.2f}",

        "-filter_complex",
        (
            "[1:a]lowpass=f=1400,highpass=f=55,volume=0.75[a1];"
            "[2:a]volume=0.07[a2];"
            "[a1][a2]amix=inputs=2:duration=longest,"
            "afade=t=in:st=0:d=1.0,"
            "alimiter=limit=0.85[aout]"
        ),

        "-map",
        "0:v:0",
        "-map",
        "[aout]",

        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",

        "-t",
        f"{video_duration:.2f}",

        str(output_path),
    ]

    print("Running ffmpeg ambient sound:")
    print(" ".join(command))
    print("Input video duration:", video_duration)
    print("Generated audio duration:", audio_duration)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    print("ffmpeg stdout:")
    print(result.stdout)
    print("ffmpeg stderr:")
    print(result.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg ambient sound failed with code {result.returncode}")

    print("Final reel with ambient sound created:", output_path)

    return str(output_path)


def process_reel_sound_record(record: Dict[str, Any]) -> None:
    record_id = record["id"]
    fields = record.get("fields", {})

    existing_links = safe_get(fields, "Output Links", "")
    existing_notes = safe_get(fields, "Render Notes", "")

    print("Reel Ambient Sound Mode detected.")
    print("Record ID:", record_id)
    print("Job Title:", safe_get(fields, "Job Title"))

    try:
        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_RENDERING,
                "Render Notes": append_note(
                    existing_notes,
                    f"Reel Ambient Sound Mode started at {now_iso()}",
                ),
            },
        )

        # Recreate final reel with text in the current runner
        clips = extract_reel_motion_clip_urls(existing_links)

        local_clip_paths = []

        for item in clips:
            index = int(item["index"])
            name = item["name"]
            url = item["url"]

            local_path = download_motion_clip(
                video_url=url,
                filename=f"sound_source_motion_clip_{index:02d}_{name}.mp4",
            )

            local_clip_paths.append(local_path)

        final_reel_path = assemble_reel_with_ffmpeg(
            clip_paths=local_clip_paths,
            output_filename="final_reel_v1_for_sound.mp4",
        )

        overlay_texts = parse_on_screen_texts(fields)

        final_text_reel_path = add_on_screen_text_to_reel(
            input_video_path=final_reel_path,
            overlay_texts=overlay_texts,
            output_filename="final_reel_text_v1_for_sound.mp4",
        )

        final_sound_reel_path = add_ambient_sound_to_reel(
            input_video_path=final_text_reel_path,
            output_filename="final_reel_sound_v1.mp4",
        )

        reel_cover_title = get_reel_cover_title(fields, overlay_texts)

        reel_cover_path = create_reel_cover_from_keyframe(
            output_links=existing_links,
            title=(
                safe_get(fields, "Reel Cover Title")
                or safe_get(fields, "Source Post Title")
                or safe_get(fields, "Job Title")
                or "SV FASHION MEDIA"
        ),
            fields=fields,
        )

        output_lines = []

        if existing_links.strip():
            output_lines.append(existing_links.strip())
            output_lines.append("")
            output_lines.append("---")
            output_lines.append("")

        output_lines.append("Final reel with sound generated:")
        output_lines.append(f"Local file: {final_sound_reel_path}")
        output_lines.append("")
        output_lines.append("Reel cover generated:")
        output_lines.append(f"Local file: {reel_cover_path}")
        output_lines.append("")
        output_lines.append("Sound style:")
        output_lines.append("Subtle ambient room tone + low drone. No voice. No beat.")
        output_lines.append("")
        output_lines.append("Artifact: visual-production-outputs")
        output_lines.append(f"Generated at: {now_iso()}")

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_NEEDS_REVIEW,
                "Output Links": "\n".join(output_lines).strip(),
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Ambient Sound Mode completed.

Created final_reel_sound_v1.mp4.
Added subtle ambient sound.
No voiceover.
No music track.
Status moved to Needs Visual Review.

Generated at:
{now_iso()}
""",
                ),
            },
        )

        print("Done. Final reel with ambient sound generated and moved to Needs Visual Review.")

    except Exception as exc:
        print("Reel Ambient Sound Mode failed:", repr(exc))

        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_ERROR,
                "Render Notes": append_note(
                    existing_notes,
                    f"""
Reel Ambient Sound Mode failed.

Error:
{repr(exc)}

Failed at:
{now_iso()}
""",
                ),
            },
        )

        raise    
def process_record(record: Dict[str, Any]) -> None:
    record_id = record["id"]
    fields = record["fields"]
    job_title = safe_get(fields, "Job Title", "Untitled Visual Job")

    print("=" * 80)
    print(f"Processing record: {record_id}")
    print(f"Job title: {job_title}")

    status_value = safe_get(fields, "Visual Status", "").strip()

    format_value = (
        safe_get(fields, "Format") or safe_get(fields, "Chosen Format")
    ).strip().lower()

    if "reel" in format_value and "carousel" not in format_value:
        output_links = safe_get(fields, "Output Links", "")

        if status_value == STATUS_QUEUED:
            process_reel_brief_record(record)
            return

        if status_value == STATUS_APPROVED:
            if "READY FOR BUFFER PACKAGE" in output_links:
                print("Ready for Buffer package already generated. Skipping.")
                return

            if "Final reel with sound generated" in output_links:
                process_reel_caption_record(record)
                return

            if "Final reel with text generated" in output_links:
                process_reel_sound_record(record)
                return

            if "Final reel assembled" in output_links:
                process_reel_text_overlay_record(record)
                return

            if "Reel motion clips generated" in output_links:
                process_reel_assembly_record(record)
                return

            if "Reel keyframes generated" in output_links:
                process_reel_motion_record(record)
                return

            process_reel_keyframes_record(record)
            return

    try:
        # 1. Brief
        update_airtable_record(record_id, {"Visual Status": STATUS_RENDERING})
        brief = generate_visual_brief(record)

        brief_fields = brief_to_airtable_fields(brief)
        brief_fields["Visual Status"] = STATUS_RENDERING
        update_airtable_record(record_id, brief_fields)

        slide_count = clamp_slide_count(brief["slide_count"])
        slide_texts = brief["slide_texts"]
        krea_prompts = brief["krea_prompts"]

        # 2. Krea render
        raw_dir = OUTPUT_DIR / record_id / "raw"
        assembled_dir = OUTPUT_DIR / record_id / "assembled"
        ensure_dir(raw_dir)
        ensure_dir(assembled_dir)

        raw_items: List[Dict[str, str]] = []
        assembled_paths: List[str] = []

        for idx in range(slide_count):
            slide_num = idx + 1
            prompt = krea_prompts[idx]

            print(f"Rendering slide {slide_num}/{slide_count}")
            print("Prompt:", prompt)

            job_id = create_krea_image_job(
                prompt=prompt,
                aspect_ratio=KREA_ASPECT_RATIO,
            )
            url = poll_krea_job(job_id)

            raw_path = raw_dir / f"slide_{slide_num:02d}_raw.png"
            download_image(url, raw_path)

            raw_items.append(
                {
                    "slide": str(slide_num),
                    "url": url,
                    "job_id": job_id,
                }
            )

        # 3. Assembly
        for idx in range(slide_count):
            slide_num = idx + 1
            raw_path = raw_dir / f"slide_{slide_num:02d}_raw.png"
            output_path = assembled_dir / f"assembled_slide_{slide_num:02d}.png"

            render_assembled_slide(
                raw_image_path=raw_path,
                slide_text=slide_texts[idx],
                slide_num=slide_num,
                total_slides=slide_count,
                output_path=output_path,
                is_cover=(slide_num == 1),
            )

            assembled_paths.append(str(output_path))

        # 4. Save result in Airtable
        output_links = build_output_links_text(raw_items, assembled_paths)

        final_fields = {
            "Visual Status": STATUS_NEEDS_REVIEW,
            "Output Links": output_links,
            "Slide Count": slide_count,
            "Slide Copy": format_slide_copy_for_airtable(slide_texts),
            "Render Notes": (
                f"{brief['render_notes']}\n\n"
                f"Final assembled carousel saved in GitHub Actions artifact and outputs folder.\n"
                f"Generated at: {now_iso()}"
            ),
        }

        update_airtable_record(record_id, final_fields)

        print(f"Done: {record_id}")
        print("Assembled files:")
        for path in assembled_paths:
            print(path)

    except Exception as error:
        print("ERROR while processing record:", error)
        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_ERROR,
                "Render Notes": f"Error at {now_iso()}:\n{str(error)}",
            },
        )
        raise
def main() -> None:
    print("Visual Production Bot v2 started:", now_iso())

    records = get_queued_visual_jobs(limit=1)

    if not records:
        print("No queued visual jobs found.")
        return

    for record in records:
        process_record(record)

    print("Visual Production Bot v2 finished.")


if __name__ == "__main__":
    main()

