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
        "filterByFormula": "OR({Visual Status} = 'Carousel Images Approved', {Visual Status} = 'Carousel images approved')",
    }

    response = requests.get(
        url,
        headers=airtable_headers(),
        params=params,
        timeout=30,
    )

    print("Read Visual Jobs status:", response.status_code)
    print("Read Visual Jobs preview:", response.text[:1000])

    if response.status_code != 200:
        raise RuntimeError("Could not read Visual Jobs")

    records = response.json().get("records", [])

    if not records:
        print("No Carousel Images Approved Visual Jobs found.")
        return None

    return records[0]


STYLE_CONFIG = {
    "colors": {
        "cream": (238, 234, 224, 255),
        "muted": (196, 191, 181, 255),
        "soft_muted": (170, 166, 158, 255),
        "shadow": (0, 0, 0, 95),
    },
    "layout": {
        "margin_x": 92,
        "header_y": 78,
        "rule_y": 132,
        "rule_width": 96,
        "footer_y": 1268,
    },
    "cover": {
        "x": 92,
        "y": 520,
        "width": 760,
        "font_size": 56,
        "min_font_size": 42,
        "line_height": 1.12,
        "max_lines": 4,
        "bold": False,
    },
    "body": {
        "x": 92,
        "y": 835,
        "width": 780,
        "font_size": 40,
        "min_font_size": 32,
        "line_height": 1.16,
        "max_lines": 5,
        "bold": False,
    },
    "small": {
        "header_size": 24,
        "number_size": 22,
        "footer_size": 22,
        "tracking": 3,
    },
    "overlay": {
        "global_dark_alpha": 24,
        "bottom_gradient_start": 0.62,
        "bottom_gradient_max_alpha": 135,
    },
}


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """
    Пытаемся взять более нейтральный sans.
    Если Liberation нет на runner, fallback на DejaVu.
    """
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
        ]

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass

    return ImageFont.load_default()


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


def add_overlays(img: Image.Image) -> Image.Image:
    base = img.convert("RGBA")

    overlay_cfg = STYLE_CONFIG["overlay"]

    # Очень лёгкое затемнение всего кадра.
    # Было грубее; теперь оставляем изображению больше воздуха.
    veil = Image.new(
        "RGBA",
        (W, H),
        (0, 0, 0, overlay_cfg["global_dark_alpha"]),
    )
    base = Image.alpha_composite(base, veil)

    # Нижний градиент теперь начинается ниже.
    # Это меньше похоже на стандартный social-media затемнитель.
    gradient = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pix = gradient.load()

    start_y = int(H * overlay_cfg["bottom_gradient_start"])
    max_alpha = overlay_cfg["bottom_gradient_max_alpha"]

    for y in range(H):
        if y > start_y:
            ratio = (y - start_y) / max(1, H - start_y)
            alpha = int(min(max_alpha, ratio * max_alpha))

            for x in range(W):
                pix[x, y] = (0, 0, 0, alpha)

    base = Image.alpha_composite(base, gradient)

    return base


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        width = bbox[2] - bbox[0]

        if width <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def draw_tracking_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    tracking: int = 2,
) -> None:
    x, y = xy

    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), char, font=font)
        char_width = bbox[2] - bbox[0]
        x += char_width + tracking


def draw_text_with_shadow(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
) -> None:
    x, y = xy
    shadow = STYLE_CONFIG["colors"]["shadow"]

    # Мягкая техническая тень вместо дешёвой обводки stroke.
    draw.text((x + 2, y + 2), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def normalize_display_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("«»\"“”'")

    # Убираем слишком длинные служебные подписи из одного слайда.
    text = text.replace(" / @sv_fashionacademy", "")
    text = text.replace("@sv_fashionacademy", "")

    return text.strip()


def draw_main_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    cfg: dict,
    color: tuple[int, int, int, int],
) -> None:
    font_size = cfg["font_size"]
    min_size = cfg["min_font_size"]
    is_bold = cfg.get("bold", False)

    text = normalize_display_text(text)

    font = load_font(font_size, bold=is_bold)
    lines = wrap_text(draw, text, font, cfg["width"])

    while len(lines) > cfg["max_lines"] and font_size > min_size:
        font_size -= 2
        font = load_font(font_size, bold=is_bold)
        lines = wrap_text(draw, text, font, cfg["width"])

    # Если всё ещё слишком много строк — режем.
    # Это лучше, чем убить композицию.
    lines = lines[: cfg["max_lines"]]

    line_height = int(font_size * cfg["line_height"])

    for i, line in enumerate(lines):
        draw_text_with_shadow(
            draw,
            (cfg["x"], cfg["y"] + i * line_height),
            line,
            font,
            color,
        )


def draw_slide(
    img: Image.Image,
    slide_number: int,
    text: str,
    total_slides: int,
) -> Image.Image:
    canvas = add_overlays(fit_image(img))
    draw = ImageDraw.Draw(canvas)

    colors = STYLE_CONFIG["colors"]
    layout = STYLE_CONFIG["layout"]
    small = STYLE_CONFIG["small"]

    cream = colors["cream"]
    muted = colors["muted"]
    soft_muted = colors["soft_muted"]

    header_font = load_font(small["header_size"], bold=False)
    number_font = load_font(small["number_size"], bold=False)
    footer_font = load_font(small["footer_size"], bold=False)

    margin_x = layout["margin_x"]

    # Header: тише, меньше, с лёгким tracking.
    draw_tracking_text(
        draw,
        (margin_x, layout["header_y"]),
        "SV FASHION MEDIA",
        header_font,
        muted,
        tracking=small["tracking"],
    )

    draw.text(
        (W - 160, layout["header_y"]),
        f"{slide_number:02d}/{total_slides:02d}",
        font=number_font,
        fill=muted,
    )

    # Линия стала короче и тоньше.
    draw.line(
        (
            margin_x,
            layout["rule_y"],
            margin_x + layout["rule_width"],
            layout["rule_y"],
        ),
        fill=soft_muted,
        width=1,
    )

    if slide_number == 1:
        text_cfg = STYLE_CONFIG["cover"]
    else:
        text_cfg = STYLE_CONFIG["body"]

    draw_main_text_block(
        draw=draw,
        text=text,
        cfg=text_cfg,
        color=cream,
    )

    # Footer: меньше и спокойнее.
    draw.text(
        (margin_x, layout["footer_y"]),
        "@sv_fashionacademy",
        font=footer_font,
        fill=soft_muted,
    )

    return canvas.convert("RGB")

def update_visual_job(record_id: str, fields: dict, rendered_files: list[str]) -> None:
    url = f"{airtable_table_url(VISUAL_TABLE_NAME)}/{record_id}"

    existing_render_notes = fields.get("Render Notes", "")
    now = datetime.now(timezone.utc).isoformat()

    new_render_note = f"""
{existing_render_notes}

---

Carousel Assembly Bot v1:
Assembled {len(rendered_files)} carousel PNG slides with typography overlay.
Files are available in the GitHub artifact: krea-carousel-assembled.
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
def extract_image_urls(output_links: str) -> dict[int, str]:
    """
    Из поля Output Links вытаскивает ссылки на изображения.
    Возвращает словарь:
    1 = cover image
    2 = slide 2
    3 = slide 3
    4 = slide 4
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

    return result

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

    # собираем slide 1 + первые два image slides, если есть
    for slide_number in [1, 2, 3]:
        url = urls_by_slide.get(slide_number)

        # если slide 3 нет, пробуем взять slide 4 как третий визуальный слайд
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
                "slide_number": slide_number,
                "url": url,
                "text": text,
            }
        )

    if not slide_items:
        raise RuntimeError("No slide images found in Output Links")

    total = len(slide_items)
    rendered_files = []

    for display_index, item in enumerate(slide_items, start=1):
        print(f"Rendering assembled slide {display_index} from source slide {item['slide_number']}")
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
