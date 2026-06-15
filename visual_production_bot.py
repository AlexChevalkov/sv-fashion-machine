import os
import re
import json
import time
import traceback
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import anthropic
from PIL import Image, ImageDraw, ImageFont, ImageOps


# ============================================================
# ENV
# ============================================================

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
KREA_API_KEY = os.environ["KREA_API_KEY"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

CONTENT_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Content Inbox")
VISUAL_TABLE_NAME = os.environ.get("AIRTABLE_VISUAL_TABLE_NAME", "Visual Jobs")

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

KREA_API_BASE = "https://api.krea.ai"

OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

W, H = 1080, 1350
FINAL_SLIDES = 3
BODY_IMAGES_TO_RENDER = 2


# ============================================================
# SV FASHION MEDIA — CAROUSEL STYLE SYSTEM v1
# ============================================================

STYLE_CONFIG = {
    "fonts": {
        "regular_candidates": [
            "assets/fonts/RobotoCondensed-Regular.ttf",
            "assets/fonts/IBMPlexSansCondensed-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ],
        "bold_candidates": [
            "assets/fonts/RobotoCondensed-Bold.ttf",
            "assets/fonts/IBMPlexSansCondensed-Medium.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ],
    },
    "colors": {
        "white": (255, 255, 255, 255),
        "meta": (255, 255, 255, 175),
        "meta_soft": (255, 255, 255, 135),
        "plate": (0, 0, 0, int(255 * 0.24)),
    },
    "meta": {
        "x": 78,
        "y": 66,
        "brand": "SV FASHION MEDIA",
        "brand_size": 24,
        "number_size": 22,
        "handle_size": 22,
        "tracking": 4,
        "line_y": 126,
        "line_width": 96,
        "line_height": 1,
        "handle_y": 1248,
    },
    "cover": {
        "x": 78,
        "y": 520,
        "width": 690,
        "font_size": 56,
        "min_font_size": 40,
        "line_height": 1.12,
        "max_lines": 4,
        "bold": False,
        "plate_padding_x": 28,
        "plate_padding_y": 20,
    },
    "body": {
        "x": 78,
        "y": 830,
        "width": 720,
        "font_size": 42,
        "min_font_size": 30,
        "line_height": 1.16,
        "max_lines": 4,
        "bold": False,
        "plate_padding_x": 28,
        "plate_padding_y": 20,
    },
}


# ============================================================
# AIRTABLE
# ============================================================

def airtable_table_url(table_name: str) -> str:
    table_encoded = quote(table_name, safe="")
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"


def airtable_headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def fetch_queued_visual_job() -> dict | None:
    url = airtable_table_url(VISUAL_TABLE_NAME)

    params = {
        "pageSize": 1,
        "filterByFormula": (
            "OR("
            "{Visual Status} = 'Queued', "
            "{Visual Status} = 'queued'"
            ")"
        ),
    }

    response = requests.get(
        url,
        headers=airtable_headers(),
        params=params,
        timeout=30,
    )

    print("Read Visual Jobs status:", response.status_code)
    print("Read Visual Jobs preview:", response.text[:1200])

    if response.status_code != 200:
        raise RuntimeError("Could not read Visual Jobs")

    records = response.json().get("records", [])

    if not records:
        print("No Queued Visual Jobs found.")
        return None

    return records[0]


def update_visual_job_fields(record_id: str, fields: dict) -> None:
    url = f"{airtable_table_url(VISUAL_TABLE_NAME)}/{record_id}"

    payload = {
        "fields": fields,
        "typecast": True,
    }

    response = requests.patch(
        url,
        headers=airtable_headers(),
        json=payload,
        timeout=30,
    )

    print("Update Visual Job status:", response.status_code)
    print("Update Visual Job response:", response.text[:1200])

    if response.status_code not in [200, 201, 202]:
        raise RuntimeError("Could not update Visual Job")


def append_render_notes(existing: str, addition: str) -> str:
    existing = existing or ""
    addition = addition or ""

    if existing.strip():
        return f"{existing.strip()}\n\n---\n\n{addition.strip()}"

    return addition.strip()


def normalize_title(title: str) -> str:
    title = (title or "").lower().strip()
    title = re.sub(r"[^\w\sа-яА-ЯёЁ]", " ", title)
    title = re.sub(r"\s+", " ", title)
    return title


def fetch_content_posts() -> list[dict]:
    url = airtable_table_url(CONTENT_TABLE_NAME)

    params = [
        ("pageSize", "100"),
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

    print("Read Content Inbox status:", response.status_code)

    if response.status_code != 200:
        print("Content Inbox read failed. Using Visual Job only.")
        print(response.text[:1000])
        return []

    return response.json().get("records", [])


def find_matching_post(source_post_title: str) -> dict:
    posts = fetch_content_posts()
    target = normalize_title(source_post_title)

    for record in posts:
        fields = record.get("fields", {})
        title = fields.get("Title", "")

        if normalize_title(title) == target:
            print("Matched Content Inbox post:", title)
            return fields

    print("No exact Content Inbox match. Using Visual Job data only.")
    return {}


# ============================================================
# CLAUDE VISUAL BRIEF
# ============================================================

def extract_json(text: str) -> dict:
    text = text.strip()

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
            "No complete JSON object found in Claude response.\n"
            f"Response preview:\n{text[:2000]}"
        )

    return json.loads(text[start:end + 1])


def generate_visual_brief(job_fields: dict, post_fields: dict) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    source_post_title = job_fields.get("Source Post Title", "")
    job_title = job_fields.get("Job Title", "")
    chosen_format = (
        job_fields.get("Chosen Format")
        or job_fields.get("Format")
        or "Carousel"
    )
    visual_mode = job_fields.get("Visual Mode", "Hybrid")

    post_title = post_fields.get("Title", source_post_title)
    hook = post_fields.get("HOOK", job_fields.get("Visual Hook", ""))
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
Не тренды. Контекст моды.
Архивы, показы, бренды, вкус, индустрия.

Задача:
Сделать visual brief для карусели Instagram в гибридном стиле:
fashion-media intelligence + art/image-driven visual appeal.

Тон:
умно, сухо, премиально, без глянцевой восторженности.
Не делать случайную красивость.
Не делать stock-photo fashion.
Не делать Canva-постер.

Пиши по-русски, но Krea prompts пиши на английском.
"""

    user_prompt = f"""
Данные Visual Job:

Job Title: {job_title}
Source Post Title: {source_post_title}
Chosen Format: {chosen_format}
Visual Mode: {visual_mode}

Данные исходного поста:

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

Сделай production-ready Visual Brief для карусели.

Правила:
- Итоговая карусель v1 = 3 слайда: cover + 2 body slides.
- Slide Copy должен быть коротким.
- Каждый слайд: 1-2 короткие смысловые фразы.
- Не перегружай текстом.
- Krea Prompt Pack должен быть компактным, но конкретным.
- Krea prompts должны быть physical, visual, production-ready.
- Не проси Krea писать текст в изображении.
- Фоны должны оставлять место для типографики.

Krea Prompt Pack должен содержать строго такие секции:
STYLE RULES:
NEGATIVE PROMPTS:
COVER IMAGE:
CAROUSEL IMAGES:
Slide 2:
Slide 3:

Верни строго валидный JSON без markdown.

Схема:
{{
  "Visual Hook": "короткая визуальная идея",
  "Visual Concept": "общее арт-директорское описание",
  "Visual Mode": "Hybrid",
  "Carousel Cover": "короткий cover title",
  "Slide Count": 3,
  "Slide Structure": "структура 3 слайдов",
  "Slide Copy": "Слайд 1: ... Слайд 2: ... Слайд 3: ...",
  "Krea Prompt Pack": "STYLE RULES: ... NEGATIVE PROMPTS: ... COVER IMAGE: ... CAROUSEL IMAGES: Slide 2: ... Slide 3: ...",
  "Krea Model Recommendation": "Manual Choice",
  "Render Notes": "краткие производственные заметки"
}}
"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=4200,
        temperature=0.25,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text

    print("\n=== Claude Visual Brief raw response ===")
    print(response_text)

    brief = extract_json(response_text)

    required = [
        "Visual Hook",
        "Visual Concept",
        "Visual Mode",
        "Carousel Cover",
        "Slide Count",
        "Slide Structure",
        "Slide Copy",
        "Krea Prompt Pack",
        "Krea Model Recommendation",
        "Render Notes",
    ]

    for field in required:
        if field not in brief:
            raise ValueError(f"Missing field from Claude response: {field}")

    brief["Visual Mode"] = brief.get("Visual Mode") or "Hybrid"
    brief["Slide Count"] = 3
    brief["Krea Model Recommendation"] = "Manual Choice"

    return brief


# ============================================================
# KREA
# ============================================================

def krea_headers() -> dict:
    return {
        "Authorization": f"Bearer {KREA_API_KEY}",
        "Content-Type": "application/json",
    }


def create_krea_image_job(prompt: str) -> str:
    url = f"{KREA_API_BASE}/generate/image/krea/krea-2/medium"

    payload = {
        "prompt": prompt[:4000],
        "aspect_ratio": "4:5",
        "resolution": "1K",
        "creativity": "low",
    }

    response = requests.post(
        url,
        headers=krea_headers(),
        json=payload,
        timeout=60,
    )

    print("Create Krea image job status:", response.status_code)
    print("Create Krea image job response:", response.text[:1500])

    if response.status_code not in [200, 201, 202]:
        raise RuntimeError("Krea image job creation failed")

    data = response.json()
    job_id = data.get("job_id")

    if not job_id:
        raise RuntimeError("No job_id returned from Krea")

    return job_id


def wait_for_krea_job(job_id: str, max_wait_seconds: int = 360) -> dict:
    url = f"{KREA_API_BASE}/jobs/{job_id}"
    started = time.time()

    while True:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {KREA_API_KEY}"},
            timeout=60,
        )

        print("Poll status:", response.status_code)
        print("Poll response preview:", response.text[:1000])

        if response.status_code != 200:
            raise RuntimeError("Krea job polling failed")

        data = response.json()
        status = data.get("status")

        print("Krea job status:", status)

        if status == "completed":
            return data

        if status in ["failed", "cancelled", "canceled"]:
            raise RuntimeError(f"Krea job failed: {data}")

        if time.time() - started > max_wait_seconds:
            raise TimeoutError("Krea job timed out")

        time.sleep(5)


def get_image_url(job_data: dict) -> str:
    result = job_data.get("result") or {}
    urls = result.get("urls") or []

    if not urls:
        raise RuntimeError(f"No image URLs found in Krea job result: {job_data}")

    return urls[0]


def download_image(image_url: str) -> Image.Image:
    response = requests.get(image_url, timeout=120)

    if response.status_code != 200:
        raise RuntimeError(f"Could not download image: {image_url}")

    return Image.open(BytesIO(response.content)).convert("RGB")


def save_image_from_url(image_url: str, path: Path) -> None:
    response = requests.get(image_url, timeout=120)

    if response.status_code != 200:
        raise RuntimeError(f"Could not download generated image: {image_url}")

    path.write_bytes(response.content)


# ============================================================
# PROMPT PARSING
# ============================================================

def extract_block(text: str, start_patterns: list[str], end_patterns: list[str]) -> str:
    if not text:
        return ""

    upper = text.upper()
    start_index = -1

    for pattern in start_patterns:
        idx = upper.find(pattern.upper())
        if idx != -1:
            start_index = idx
            break

    if start_index == -1:
        return ""

    end_index = len(text)

    for pattern in end_patterns:
        idx = upper.find(pattern.upper(), start_index + 1)
        if idx != -1:
            end_index = min(end_index, idx)

    return text[start_index:end_index].strip()


def extract_style_rules(prompt_pack: str) -> str:
    return extract_block(
        prompt_pack,
        ["STYLE RULES"],
        ["NEGATIVE PROMPTS", "COVER IMAGE", "CAROUSEL IMAGES", "REEL SCENES"],
    )


def extract_negative_prompts(prompt_pack: str) -> str:
    return extract_block(
        prompt_pack,
        ["NEGATIVE PROMPTS"],
        ["COVER IMAGE", "CAROUSEL IMAGES", "REEL SCENES"],
    )


def extract_cover_prompt(brief: dict) -> str:
    prompt_pack = brief.get("Krea Prompt Pack", "")
    style_rules = extract_style_rules(prompt_pack)
    negative_prompts = extract_negative_prompts(prompt_pack)

    cover_block = extract_block(
        prompt_pack,
        ["COVER IMAGE"],
        ["CAROUSEL IMAGES", "REEL SCENES"],
    )

    if not cover_block:
        cover_block = brief.get("Visual Concept", "")

    final_prompt = f"""
{cover_block}

{style_rules}

{negative_prompts}

Additional production rules:
The object must look like a deliberate fashion editorial symbol, not a random product still life.
Composition should feel like a magazine cover background, with clear negative space reserved for typography.
No text inside the image. No logos. No fake brand names.
The mood should be intelligent, restrained, premium, editorial, not commercial stock photography.
Vertical 4:5 composition for Instagram carousel cover.
""".strip()

    return final_prompt[:4000]


def extract_carousel_block(prompt_pack: str) -> str:
    return extract_block(
        prompt_pack,
        ["CAROUSEL IMAGES"],
        ["REEL SCENES", "VIDEO", "STYLE RULES"],
    )


def parse_slide_prompts(carousel_block: str) -> list[dict]:
    if not carousel_block:
        return []

    text = carousel_block.replace("—", "\n—")

    pattern = (
        r"(?:Slide|Слайд)\s*(\d+)\s*[:\-]\s*"
        r"(.*?)(?=\n\s*[—-]?\s*(?:Slide|Слайд)\s*\d+\s*[:\-]|\Z)"
    )

    matches = re.findall(pattern, text, flags=re.IGNORECASE | re.DOTALL)

    result = []

    for number, prompt in matches:
        clean = re.sub(r"\s+", " ", prompt).strip()

        if len(clean) < 20:
            continue

        result.append(
            {
                "slide_number": int(number),
                "prompt": clean,
            }
        )

    return result


def fallback_slide_prompts(brief: dict) -> list[dict]:
    concept = brief.get("Visual Concept", "")
    hook = brief.get("Visual Hook", "")

    return [
        {
            "slide_number": 2,
            "prompt": (
                f"Premium fashion editorial background image. Visual hook: {hook}. "
                f"Concept: {concept}. One symbolic fashion object in large negative space, "
                "cold editorial light, matte textures, no text, no logos."
            ),
        },
        {
            "slide_number": 3,
            "prompt": (
                f"Premium fashion editorial contrast image. Visual hook: {hook}. "
                "One side suggests mass fashion speed and accessibility, the other side shows "
                "restrained luxury silence and distance. No text, no logos, no stock photo look."
            ),
        },
    ]


def build_carousel_image_prompt(base_prompt: str, brief: dict, slide_number: int) -> str:
    prompt_pack = brief.get("Krea Prompt Pack", "")
    style_rules = extract_style_rules(prompt_pack)
    negative_prompts = extract_negative_prompts(prompt_pack)

    final_prompt = f"""
CAROUSEL SLIDE {slide_number} BACKGROUND IMAGE:

{base_prompt}

{style_rules}

{negative_prompts}

Additional production rules:
Create a visual background for an Instagram carousel slide, not a finished poster.
Do not put any text inside the image.
No logos. No fake brand names. No random letters.
Leave clean negative space where typography can be added later.
The image must feel like intelligent fashion media, not stock photography.
The subject must look deliberate and symbolic.
Vertical 4:5 composition.
Premium editorial lighting. Matte textures. Controlled palette.
""".strip()

    return final_prompt[:4000]


# ============================================================
# TYPOGRAPHY / ASSEMBLY
# ============================================================

def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        STYLE_CONFIG["fonts"]["bold_candidates"]
        if bold
        else STYLE_CONFIG["fonts"]["regular_candidates"]
    )

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue

    return ImageFont.load_default()


def fit_image(img: Image.Image) -> Image.Image:
    return ImageOps.fit(
        img,
        (W, H),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def parse_slide_copy(slide_copy: str) -> dict[int, str]:
    result = {}

    if not slide_copy:
        return result

    pattern = (
        r"(?:Слайд|Slide)\s*(\d+)\s*[:：]\s*"
        r"(.*?)(?=(?:\s*(?:Слайд|Slide)\s*\d+\s*[:：])|$)"
    )

    matches = re.findall(
        pattern,
        slide_copy,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for num, text in matches:
        clean = re.sub(r"\s+", " ", text).strip()
        clean = clean.strip(" .")
        clean = clean.strip("«»\"“”'")
        clean = clean.replace(" / @sv_fashionacademy", "")
        clean = clean.replace("@sv_fashionacademy", "")

        result[int(num)] = clean

    return result


def normalize_display_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("«»\"“”'")
    text = text.replace(" / @sv_fashionacademy", "")
    text = text.replace("@sv_fashionacademy", "")

    return text.strip()


def split_into_phrases(text: str) -> list[str]:
    text = normalize_display_text(text)

    if not text:
        return []

    parts = re.split(r"(?<=[.!?])\s+", text)
    cleaned = []

    for part in parts:
        part = part.strip()
        part = part.strip(" .")

        if part:
            cleaned.append(part)

    return cleaned or [text]


def prepare_slide_text(text: str) -> str:
    phrases = split_into_phrases(text)

    if not phrases:
        return ""

    phrases = phrases[:2]
    phrases[0] = phrases[0].upper()

    return "\n".join(phrases)


def wrap_text_by_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    paragraphs = text.split("\n")
    wrapped_lines = []

    for para in paragraphs:
        para = para.strip()

        if not para:
            continue

        words = para.split()

        if not words:
            continue

        current = words[0]

        for word in words[1:]:
            candidate = current + " " + word
            bbox = draw.textbbox((0, 0), candidate, font=font)
            width = bbox[2] - bbox[0]

            if width <= max_width:
                current = candidate
            else:
                wrapped_lines.append(current)
                current = word

        wrapped_lines.append(current)

    return wrapped_lines


def fit_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    preferred_size: int,
    min_size: int,
    max_width: int,
    max_lines: int,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    size = preferred_size

    while size >= min_size:
        font = load_font(size, bold=False)
        lines = wrap_text_by_width(draw, text, font, max_width)

        if len(lines) <= max_lines:
            return font, lines, size

        size -= 2

    font = load_font(min_size, bold=False)
    lines = wrap_text_by_width(draw, text, font, max_width)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,!?:;") + "…"

    return font, lines, min_size


def split_emphasis(line: str) -> tuple[str, str]:
    line = line.strip()

    if not line:
        return "", ""

    colon_pos = line.find(":")

    if colon_pos != -1 and colon_pos <= max(18, len(line) * 0.45):
        return line[:colon_pos + 1], line[colon_pos + 1:].lstrip()

    parts = line.split(" ", 1)

    if len(parts) == 1:
        return parts[0], ""

    return parts[0], parts[1]


def draw_mixed_weight_line(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    line: str,
    regular_font: ImageFont.FreeTypeFont,
    bold_font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    emphasize: bool,
) -> None:
    x, y = xy

    if not emphasize:
        draw.text((x, y), line, font=regular_font, fill=fill)
        return

    emphasis, rest = split_emphasis(line)

    if not emphasis:
        draw.text((x, y), line, font=regular_font, fill=fill)
        return

    draw.text((x, y), emphasis, font=bold_font, fill=fill)

    bbox = draw.textbbox((0, 0), emphasis, font=bold_font)
    emphasis_w = bbox[2] - bbox[0]

    if rest:
        draw.text(
            (x + emphasis_w + 10, y),
            rest,
            font=regular_font,
            fill=fill,
        )


def draw_text_plate(base: Image.Image, x: int, y: int, w: int, h: int) -> Image.Image:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    overlay_draw.rectangle(
        [x, y, x + w, y + h],
        fill=STYLE_CONFIG["colors"]["plate"],
    )

    return Image.alpha_composite(base.convert("RGBA"), overlay)


def render_main_text(base: Image.Image, text: str, is_cover: bool) -> Image.Image:
    cfg = STYLE_CONFIG["cover"] if is_cover else STYLE_CONFIG["body"]
    prepared = prepare_slide_text(text)

    if not prepared:
        return base

    draw = ImageDraw.Draw(base)

    regular_font, lines, actual_size = fit_text_block(
        draw=draw,
        text=prepared,
        preferred_size=cfg["font_size"],
        min_size=cfg["min_font_size"],
        max_width=cfg["width"],
        max_lines=cfg["max_lines"],
    )

    bold_font = load_font(actual_size, bold=True)

    if not lines:
        return base

    line_height = int(actual_size * cfg["line_height"])
    max_line_width = 0

    for index, line in enumerate(lines):
        if index == 0:
            emphasis, rest = split_emphasis(line)
            emphasis_bbox = draw.textbbox((0, 0), emphasis, font=bold_font)
            emphasis_w = emphasis_bbox[2] - emphasis_bbox[0]

            if rest:
                rest_bbox = draw.textbbox((0, 0), rest, font=regular_font)
                rest_w = rest_bbox[2] - rest_bbox[0]
                line_w = emphasis_w + 10 + rest_w
            else:
                line_w = emphasis_w
        else:
            bbox = draw.textbbox((0, 0), line, font=regular_font)
            line_w = bbox[2] - bbox[0]

        max_line_width = max(max_line_width, line_w)

    text_height = line_height * len(lines)

    plate_x = cfg["x"] - cfg["plate_padding_x"]
    plate_y = cfg["y"] - cfg["plate_padding_y"]
    plate_w = max_line_width + cfg["plate_padding_x"] * 2
    plate_h = text_height + cfg["plate_padding_y"] * 2

    plate_w = min(plate_w, W - plate_x - 40)
    plate_h = min(plate_h, H - plate_y - 40)

    composed = draw_text_plate(
        base=base,
        x=plate_x,
        y=plate_y,
        w=plate_w,
        h=plate_h,
    )

    draw = ImageDraw.Draw(composed)
    cursor_y = cfg["y"]

    for index, line in enumerate(lines):
        draw_mixed_weight_line(
            draw=draw,
            xy=(cfg["x"], cursor_y),
            line=line,
            regular_font=regular_font,
            bold_font=bold_font,
            fill=STYLE_CONFIG["colors"]["white"],
            emphasize=(index == 0),
        )
        cursor_y += line_height

    return composed


def draw_tracking_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    tracking: int,
) -> None:
    x, y = xy

    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), char, font=font)
        x += (bbox[2] - bbox[0]) + tracking


def render_meta(base: Image.Image, slide_number: int, total_slides: int) -> Image.Image:
    draw = ImageDraw.Draw(base)
    meta_cfg = STYLE_CONFIG["meta"]

    brand_font = load_font(meta_cfg["brand_size"], bold=False)
    num_font = load_font(meta_cfg["number_size"], bold=False)
    handle_font = load_font(meta_cfg["handle_size"], bold=False)

    meta_color = STYLE_CONFIG["colors"]["meta"]
    meta_soft = STYLE_CONFIG["colors"]["meta_soft"]

    draw_tracking_text(
        draw,
        (meta_cfg["x"], meta_cfg["y"]),
        meta_cfg["brand"],
        brand_font,
        meta_color,
        tracking=meta_cfg["tracking"],
    )

    draw.rectangle(
        [
            meta_cfg["x"],
            meta_cfg["line_y"],
            meta_cfg["x"] + meta_cfg["line_width"],
            meta_cfg["line_y"] + meta_cfg["line_height"],
        ],
        fill=meta_soft,
    )

    slide_label = f"{slide_number:02d}/{total_slides:02d}"
    bbox = draw.textbbox((0, 0), slide_label, font=num_font)
    num_w = bbox[2] - bbox[0]

    draw.text(
        (W - meta_cfg["x"] - num_w, meta_cfg["y"]),
        slide_label,
        font=num_font,
        fill=meta_color,
    )

    draw.text(
        (meta_cfg["x"], meta_cfg["handle_y"]),
        "@sv_fashionacademy",
        font=handle_font,
        fill=meta_soft,
    )

    return base


def draw_slide(img: Image.Image, slide_number: int, text: str, total_slides: int) -> Image.Image:
    base = fit_image(img).convert("RGBA")

    is_cover = slide_number == 1

    base = render_main_text(
        base=base,
        text=text,
        is_cover=is_cover,
    )

    base = render_meta(
        base=base,
        slide_number=slide_number,
        total_slides=total_slides,
    )

    return base.convert("RGB")


# ============================================================
# PRODUCTION PIPELINE
# ============================================================

def render_krea_image(prompt: str, filename: str) -> dict:
    job_id = create_krea_image_job(prompt)
    completed_job = wait_for_krea_job(job_id)
    image_url = get_image_url(completed_job)

    output_path = OUTPUT_DIR / filename
    save_image_from_url(image_url, output_path)

    return {
        "job_id": job_id,
        "image_url": image_url,
        "output_path": str(output_path),
    }


def assemble_carousel(
    cover_url: str,
    body_results: list[dict],
    brief: dict,
) -> list[str]:
    slide_copy = brief.get("Slide Copy", "")
    texts_by_slide = parse_slide_copy(slide_copy)

    if brief.get("Carousel Cover"):
        texts_by_slide[1] = brief.get("Carousel Cover")

    slide_items = [
        {
            "url": cover_url,
            "text": texts_by_slide.get(1, brief.get("Carousel Cover", "")),
        }
    ]

    for index, result in enumerate(body_results, start=2):
        text = texts_by_slide.get(index, brief.get("Visual Hook", "Fashion is context."))
        slide_items.append(
            {
                "url": result["image_url"],
                "text": text,
            }
        )

    slide_items = slide_items[:FINAL_SLIDES]
    total = len(slide_items)
    rendered_files = []

    for display_index, item in enumerate(slide_items, start=1):
        print(f"Rendering assembled slide {display_index}")
        print("Image URL:", item["url"])
        print("Text:", item["text"])

        img = download_image(item["url"])

        slide = draw_slide(
            img=img,
            slide_number=display_index,
            text=item["text"],
            total_slides=total,
        )

        output_path = OUTPUT_DIR / f"assembled_carousel_slide_{display_index:02d}.png"
        slide.save(output_path, quality=95)

        print("Saved assembled slide:", output_path)

        rendered_files.append(str(output_path))

    return rendered_files


def run_pipeline() -> None:
    print("Visual Production Bot started:", datetime.now(timezone.utc).isoformat())

    job = fetch_queued_visual_job()

    if not job:
        return

    record_id = job["id"]
    job_fields = job.get("fields", {})

    existing_render_notes = job_fields.get("Render Notes", "")

    update_visual_job_fields(
        record_id,
        {
            "Visual Status": "In Production",
            "Render Notes": append_render_notes(
                existing_render_notes,
                f"Visual Production Bot v1 started at {datetime.now(timezone.utc).isoformat()}",
            ),
        },
    )

    try:
        source_post_title = job_fields.get("Source Post Title", "")
        post_fields = find_matching_post(source_post_title)

        brief = generate_visual_brief(job_fields, post_fields)

        print("\n=== Generated brief ===")
        print(json.dumps(brief, ensure_ascii=False, indent=2))

        update_visual_job_fields(
            record_id,
            {
                "Visual Hook": brief.get("Visual Hook", ""),
                "Visual Concept": brief.get("Visual Concept", ""),
                "Visual Mode": brief.get("Visual Mode", "Hybrid"),
                "Carousel Cover": brief.get("Carousel Cover", ""),
                "Slide Count": brief.get("Slide Count", 3),
                "Slide Structure": brief.get("Slide Structure", ""),
                "Slide Copy": brief.get("Slide Copy", ""),
                "Krea Prompt Pack": brief.get("Krea Prompt Pack", ""),
                "Krea Model Recommendation": brief.get("Krea Model Recommendation", "Manual Choice"),
                "Render Notes": append_render_notes(
                    existing_render_notes,
                    "Visual brief generated by Visual Production Bot v1.",
                ),
            },
        )

        cover_prompt = extract_cover_prompt(brief)

        print("\n=== Cover prompt ===")
        print(cover_prompt)

        cover_result = render_krea_image(
            prompt=cover_prompt,
            filename="production_cover_background.png",
        )

        carousel_block = extract_carousel_block(brief.get("Krea Prompt Pack", ""))
        slide_prompts = parse_slide_prompts(carousel_block)

        if not slide_prompts:
            print("No slide prompts parsed. Using fallback prompts.")
            slide_prompts = fallback_slide_prompts(brief)

        slide_prompts = slide_prompts[:BODY_IMAGES_TO_RENDER]

        body_results = []

        for item in slide_prompts:
            slide_number = item["slide_number"]
            base_prompt = item["prompt"]

            final_prompt = build_carousel_image_prompt(
                base_prompt=base_prompt,
                brief=brief,
                slide_number=slide_number,
            )

            print(f"\n=== Body slide {slide_number} prompt ===")
            print(final_prompt)

            result = render_krea_image(
                prompt=final_prompt,
                filename=f"production_carousel_bg_slide_{slide_number}.png",
            )

            result["slide_number"] = slide_number
            body_results.append(result)

        rendered_files = assemble_carousel(
            cover_url=cover_result["image_url"],
            body_results=body_results,
            brief=brief,
        )

        now = datetime.now(timezone.utc).isoformat()

        output_lines = [
            "Visual Production Bot v1 output:",
            "",
            f"Cover background: {cover_result['image_url']} | job_id: {cover_result['job_id']}",
        ]

        for result in body_results:
            output_lines.append(
                f"Carousel background slide {result['slide_number']}: "
                f"{result['image_url']} | job_id: {result['job_id']}"
            )

        output_lines.append("")
        output_lines.append("Assembled carousel files are in GitHub artifact: sv-visual-production-output")
        output_lines.append(f"Generated at: {now}")

        final_render_notes = append_render_notes(
            job_fields.get("Render Notes", ""),
            f"""
Visual Production Bot v1 completed:
- visual brief generated;
- cover background generated;
- {len(body_results)} carousel background images generated;
- {len(rendered_files)} assembled carousel PNG slides created;
- status moved to Needs Visual Review.
Generated at: {now}
""",
        )

        update_visual_job_fields(
            record_id,
            {
                "Visual Status": "Needs Visual Review",
                "Output Links": "\n".join(output_lines),
                "Render Notes": final_render_notes,
            },
        )

        print("Done. Visual Job moved to Needs Visual Review.")

    except Exception as error:
        error_text = traceback.format_exc()
        print(error_text)

        fail_notes = append_render_notes(
            job_fields.get("Render Notes", ""),
            f"""
Visual Production Bot v1 FAILED:
{str(error)}

Traceback:
{error_text[:2500]}
""",
        )

        update_visual_job_fields(
            record_id,
            {
                "Visual Status": "Failed",
                "Render Notes": fail_notes,
            },
        )

        raise


if __name__ == "__main__":
    run_pipeline()
