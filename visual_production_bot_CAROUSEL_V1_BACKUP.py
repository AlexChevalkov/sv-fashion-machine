import os
import re
import json
import math
import time
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
STATUS_BRIEF_READY = "In Production"
STATUS_RENDERING = "In Production"
STATUS_NEEDS_REVIEW = "Needs Visual Review"
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
            "filterByFormula": "{Visual Status}='Queued'"
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
        "Visual Status": STATUS_BRIEF_READY,
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

def process_record(record: Dict[str, Any]) -> None:
    record_id = record["id"]
    fields = record["fields"]
    job_title = safe_get(fields, "Job Title", "Untitled Visual Job")

    print("=" * 80)
    print(f"Processing record: {record_id}")
    print(f"Job title: {job_title}")

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

            job_id = create_krea_image_job(prompt=prompt, aspect_ratio=KREA_ASPECT_RATIO)
            url = poll_krea_job(job_id)

            raw_path = raw_dir / f"slide_{slide_num:02d}_raw.png"
            download_image(url, raw_path)

            raw_items.append({
                "slide": str(slide_num),
                "url": url,
                "job_id": job_id
            })

        # 3. Assembly
        for idx in range(slide_count):
            slide_num = idx + 1
            raw_path = raw_dir / f"slide_{slide_num:02d}_raw.png"
            output_path = assembled_dir / f"assembled_slide_{slide_num:02d}.png"

            source = Image.open(raw_path)
            source = fit_image_to_canvas(source)
            result = add_text_overlay(
                source,
                slide_texts[idx],
                slide_num,
                slide_count,
                is_cover=(slide_num == 1),
            )
            result.save(output_path, format="PNG", quality=95)

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
            )
        }

        update_airtable_record(record_id, final_fields)

        print(f"Done: {record_id}")
        print("Assembled files:")
        for p in assembled_paths:
            print(p)

    except Exception as error:
        print("ERROR while processing record:", error)
        update_airtable_record(
            record_id,
            {
                "Visual Status": STATUS_ERROR,
                "Render Notes": f"Error at {now_iso()}:\n{str(error)}"
            }
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
