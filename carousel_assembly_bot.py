import os
import re
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps


AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

VISUAL_TABLE_NAME = os.environ.get("AIRTABLE_VISUAL_TABLE_NAME", "Visual Jobs")

OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

W, H = 1080, 1350
MAX_SLIDES = 3


# ============================================================
# SV FASHION MEDIA — CAROUSEL STYLE SYSTEM
# ============================================================

STYLE_CONFIG = {
    "fonts": {
        # Можно позже положить свои шрифты сюда:
        # assets/fonts/RobotoCondensed-Regular.ttf
        # assets/fonts/RobotoCondensed-Bold.ttf
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
        "plate": (0, 0, 0, int(255 * 0.24)),  # 24% black plate
        "subtle_veil": (0, 0, 0, 0),
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


def fetch_job() -> dict | None:
    url = airtable_table_url(VISUAL_TABLE_NAME)

    params = {
        "pageSize": 1,
        "filterByFormula": (
            "OR("
            "{Visual Status} = 'Carousel Images Approved', "
            "{Visual Status} = 'Carousel images approved'"
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
        print("No Carousel Images Approved Visual Jobs found.")
        return None

    return records[0]


def update_visual_job(record_id: str, fields: dict, rendered_files: list[str]) -> None:
    url = f"{airtable_table_url(VISUAL_TABLE_NAME)}/{record_id}"

    existing_render_notes = fields.get("Render Notes", "")
    now = datetime.now(timezone.utc).isoformat()

    new_render_note = f"""
{existing_render_notes}

---

Carousel Assembly Bot v2:
Assembled {len(rendered_files)} carousel PNG slides with the updated SV Fashion Media typography system:
- condensed sans typography;
- white text only;
- semi-transparent black local plate under main text;
- first line uppercase;
- sentence / meaning-block line breaks;
- max 4 text lines per slide.
Generated at: {now}
""".strip()

    payload = {
        "fields": {
            "Visual Status": "Carousel Assembled",
            "Render Notes": new_render_note,
        },
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


# ============================================================
# FONTS
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


# ============================================================
# IMAGE HELPERS
# ============================================================

def download_image(url: str) -> Image.Image:
    response = requests.get(url, timeout=120)

    if response.status_code != 200:
        raise RuntimeError(f"Could not download image: {url}")

    return Image.open(BytesIO(response.content)).convert("RGB")


def fit_image(img: Image.Image) -> Image.Image:
    return ImageOps.fit(
        img,
        (W, H),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def apply_subtle_veil(img: Image.Image) -> Image.Image:
    base = img.convert("RGBA")
    veil_color = STYLE_CONFIG["colors"]["subtle_veil"]

    if veil_color[3] <= 0:
        return base

    veil = Image.new("RGBA", (W, H), veil_color)
    return Image.alpha_composite(base, veil)


# ============================================================
# PARSING
# ============================================================

def parse_slide_copy(slide_copy: str) -> dict[int, str]:
    """
    Разбирает поле Slide Copy из Airtable.

    Пример:
    Слайд 1: ...
    Слайд 2: ...
    Slide 3: ...

    Возвращает:
    {
        1: "текст первого слайда",
        2: "текст второго слайда",
    }
    """

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

        # Убираем служебные хвосты, которые лучше не класть на слайд.
        clean = clean.replace(" / @sv_fashionacademy", "")
        clean = clean.replace("@sv_fashionacademy", "")

        result[int(num)] = clean

    return result


def extract_image_urls(output_links: str) -> dict[int, str]:
    """
    Из поля Output Links вытаскивает ссылки на изображения.

    Возвращает:
    1 = cover image
    2 = carousel slide 2
    3 = carousel slide 3
    4 = carousel slide 4
    """

    result = {}

    if not output_links:
        return result

    cover_match = re.search(
        r"Krea cover image generated:\s*(https?://[^\s|]+)",
        output_links,
        re.IGNORECASE,
    )

    if cover_match:
        result[1] = cover_match.group(1).strip()

    slide_matches = re.findall(
        r"Slide\s+(\d+):\s*(https?://[^\s|]+)",
        output_links,
        re.IGNORECASE,
    )

    for slide_num, url in slide_matches:
        result[int(slide_num)] = url.strip()

    # Fallback: если формат Output Links изменился
    if not result:
        all_urls = re.findall(r"https?://[^\s|]+", output_links)
        for index, url in enumerate(all_urls[:MAX_SLIDES], start=1):
            result[index] = url.strip()

    return result


# ============================================================
# TEXT SYSTEM
# ============================================================

def normalize_display_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("«»\"“”'")

    text = text.replace(" / @sv_fashionacademy", "")
    text = text.replace("@sv_fashionacademy", "")

    return text.strip()


def split_into_phrases(text: str) -> list[str]:
    """
    Делим текст на смысловые фразы.
    Базовое правило: каждое предложение — с новой строки.
    """

    text = normalize_display_text(text)

    if not text:
        return []

    # Сначала делим по точкам / вопросам / восклицаниям.
    parts = re.split(r"(?<=[.!?])\s+", text)

    cleaned = []

    for part in parts:
        part = part.strip()
        part = part.strip(" .")

        if not part:
            continue

        cleaned.append(part)

    # Если точек не было, оставляем одну фразу.
    if not cleaned and text:
        cleaned = [text]

    return cleaned


def prepare_slide_text(text: str) -> str:
    phrases = split_into_phrases(text)

    if not phrases:
        return ""

    # Для базовой premium-системы не перегружаем слайд.
    # Берём максимум 2 смысловые фразы, иначе всё становится текстовым постером.
    phrases = phrases[:2]

    # Первая строка — uppercase.
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
    bold: bool = False,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    size = preferred_size

    while size >= min_size:
        font = load_font(size, bold=bold)
        lines = wrap_text_by_width(draw, text, font, max_width)

        if len(lines) <= max_lines:
            return font, lines, size

        size -= 2

    font = load_font(min_size, bold=bold)
    lines = wrap_text_by_width(draw, text, font, max_width)

    if len(lines) > max_lines:
        lines = lines[:max_lines]

        if lines:
            lines[-1] = lines[-1].rstrip(" .,!?:;") + "…"

    return font, lines, min_size


def draw_tracking_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    tracking: int = 3,
) -> None:
    x, y = xy

    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), char, font=font)
        char_width = bbox[2] - bbox[0]
        x += char_width + tracking


def draw_text_plate(
    base: Image.Image,
    x: int,
    y: int,
    w: int,
    h: int,
) -> Image.Image:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    overlay_draw.rectangle(
        [x, y, x + w, y + h],
        fill=STYLE_CONFIG["colors"]["plate"],
    )

    return Image.alpha_composite(base.convert("RGBA"), overlay)


def split_emphasis(line: str) -> tuple[str, str]:
    """
    Возвращает:
    emphasis = часть строки, которую рисуем bold
    rest = остальная часть строки regular

    Правило:
    - если есть двоеточие в первой трети строки — bold всё до двоеточия включительно
    - иначе bold первое слово
    """

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
    emphasize: bool = False,
) -> None:
    """
    Рисует строку с bold-акцентом в начале.
    """

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


def render_main_text(
    base: Image.Image,
    text: str,
    is_cover: bool,
) -> Image.Image:
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
        bold=False,
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

    line_height = int(actual_size * cfg["line_height"])

    max_line_width = 0

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        max_line_width = max(max_line_width, bbox[2] - bbox[0])

    text_height = line_height * len(lines)

    plate_x = cfg["x"] - cfg["plate_padding_x"]
    plate_y = cfg["y"] - cfg["plate_padding_y"]
    plate_w = max_line_width + cfg["plate_padding_x"] * 2
    plate_h = text_height + cfg["plate_padding_y"] * 2

    # Страховка, чтобы плашка не вышла за край.
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

    for line in lines:
        draw.text(
            (cfg["x"], cursor_y),
            line,
            font=font,
            fill=STYLE_CONFIG["colors"]["white"],
        )
        cursor_y += line_height

    return composed


def render_meta(
    base: Image.Image,
    slide_number: int,
    total_slides: int,
) -> Image.Image:
    draw = ImageDraw.Draw(base)

    meta_cfg = STYLE_CONFIG["meta"]

    brand_font = load_font(meta_cfg["brand_size"], bold=False)
    num_font = load_font(meta_cfg["number_size"], bold=False)
    handle_font = load_font(meta_cfg["handle_size"], bold=False)

    meta_color = STYLE_CONFIG["colors"]["meta"]
    meta_soft = STYLE_CONFIG["colors"]["meta_soft"]

    # Brand label with tracking
    draw_tracking_text(
        draw,
        (meta_cfg["x"], meta_cfg["y"]),
        meta_cfg["brand"],
        brand_font,
        meta_color,
        tracking=meta_cfg["tracking"],
    )

    # Thin rule
    draw.rectangle(
        [
            meta_cfg["x"],
            meta_cfg["line_y"],
            meta_cfg["x"] + meta_cfg["line_width"],
            meta_cfg["line_y"] + meta_cfg["line_height"],
        ],
        fill=meta_soft,
    )

    # Slide number
    slide_label = f"{slide_number:02d}/{total_slides:02d}"
    bbox = draw.textbbox((0, 0), slide_label, font=num_font)
    num_w = bbox[2] - bbox[0]

    draw.text(
        (W - meta_cfg["x"] - num_w, meta_cfg["y"]),
        slide_label,
        font=num_font,
        fill=meta_color,
    )

    # Handle
    draw.text(
        (meta_cfg["x"], meta_cfg["handle_y"]),
        "@sv_fashionacademy",
        font=handle_font,
        fill=meta_soft,
    )

    return base


def draw_slide(
    img: Image.Image,
    slide_number: int,
    text: str,
    total_slides: int,
) -> Image.Image:
    base = fit_image(img).convert("RGBA")
    base = apply_subtle_veil(base)

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
# MAIN
# ============================================================

def main() -> None:
    print("Carousel Assembly Bot started:", datetime.now(timezone.utc).isoformat())

    job = fetch_job()

    if not job:
        return

    record_id = job["id"]
    fields = job.get("fields", {})

    print("Record ID:", record_id)
    print("Job Title:", fields.get("Job Title"))

    output_links = fields.get("Output Links", "")
    slide_copy = fields.get("Slide Copy", "")
    carousel_cover = fields.get("Carousel Cover", "")

    urls_by_slide = extract_image_urls(output_links)
    texts_by_slide = parse_slide_copy(slide_copy)

    if carousel_cover:
        texts_by_slide[1] = carousel_cover

    print("URLs by slide:", urls_by_slide)
    print("Texts by slide:", texts_by_slide)

    slide_items = []

    # Собираем cover + первые два визуальных слайда.
    for slide_number in [1, 2, 3]:
        url = urls_by_slide.get(slide_number)

        # Если slide 3 нет, берём slide 4 как третий assembled slide.
        if not url and slide_number == 3:
            url = urls_by_slide.get(4)

        if not url:
            continue

        text = texts_by_slide.get(slide_number)

        if not text and slide_number == 3:
            text = texts_by_slide.get(4)

        if not text:
            text = fields.get("Visual Hook", "Fashion is not trend. Fashion is context.")

        slide_items.append(
            {
                "source_slide_number": slide_number,
                "url": url,
                "text": text,
            }
        )

    if not slide_items:
        raise RuntimeError("No slide images found in Output Links")

    total = len(slide_items)
    rendered_files = []

    for display_index, item in enumerate(slide_items, start=1):
        print(
            f"Rendering assembled slide {display_index} "
            f"from source slide {item['source_slide_number']}"
        )
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

        print("Saved:", output_path)

        rendered_files.append(str(output_path))

    update_visual_job(record_id, fields, rendered_files)

    print("Done. Carousel slides assembled.")


if __name__ == "__main__":
    main()
