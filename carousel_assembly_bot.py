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


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []

    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        ]
    else:
        candidates += [
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
    return ImageOps.fit(img, (W, H), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def add_overlays(img: Image.Image) -> Image.Image:
    base = img.convert("RGBA")

    # лёгкое затемнение всего кадра
    veil = Image.new("RGBA", (W, H), (0, 0, 0, 55))
    base = Image.alpha_composite(base, veil)

    # нижний градиент для текста
    gradient = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pix = gradient.load()

    for y in range(H):
        if y > int(H * 0.48):
            alpha = int(min(170, (y - H * 0.48) / (H * 0.52) * 170))
            for x in range(W):
                pix[x, y] = (0, 0, 0, alpha)

    base = Image.alpha_composite(base, gradient)

    return base


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
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


def parse_slide_copy(slide_copy: str) -> dict[int, str]:
    if not slide_copy:
        return {}

    pattern = r"(?:Слайд|Slide)\s*(\d+)\s*[:：]\s*(.*?)(?=(?:\s*(?:Слайд|Slide)\s*\d+\s*[:：])|$)"
    matches = re.findall(pattern, slide_copy, flags=re.IGNORECASE | re.DOTALL)

    result = {}

    for num, text in matches:
        clean = re.sub(r"\s+", " ", text).strip()
        clean = clean.strip(" .")
        clean = clean.strip("«»\"“”'")
        result[int(num)] = clean

    return result


def extract_image_urls(output_links: str) -> dict[int, str]:
    """
    Возвращает:
    1 -> cover url
    2 -> slide 2 url
    3 -> slide 3 url
    и т.д.
    """
    urls_by_slide = {}

    cover_match = re.search(
        r"Krea cover image generated:\s*(https?://[^\s]+)",
        output_links,
        flags=re.IGNORECASE,
    )

    if cover_match:
        urls_by_slide[1] = cover_match.group(1).strip()

    for num, url in re.findall(
        r"Slide\s+(\d+):\s*(https?://[^\s|]+)",
        output_links,
        flags=re.IGNORECASE,
    ):
        urls_by_slide[int(num)] = url.strip()

    if not urls_by_slide:
        all_urls = re.findall(r"https?://[^\s|]+", output_links)
        for index, url in enumerate(all_urls[:MAX_SLIDES], start=1):
            urls_by_slide[index] = url.strip()

    return urls_by_slide


def draw_slide(img: Image.Image, slide_number: int, text: str, total_slides: int) -> Image.Image:
    canvas = add_overlays(fit_image(img))
    draw = ImageDraw.Draw(canvas)

    cream = (244, 240, 230, 255)
    muted = (210, 205, 194, 255)

    label_font = load_font(28, bold=False)
    number_font = load_font(24, bold=False)

    if slide_number == 1:
        main_font = load_font(68, bold=True)
        max_lines = 5
        text_y = 760
    else:
        main_font = load_font(48, bold=False)
        max_lines = 8
        text_y = 830

    margin_x = 76
    max_width = W - margin_x * 2

    # header
    draw.text(
        (margin_x, 72),
        "SV FASHION MEDIA",
        font=label_font,
        fill=muted,
    )

    draw.text(
        (W - 160, 72),
        f"{slide_number:02d}/{total_slides:02d}",
        font=number_font,
        fill=muted,
    )

    # короткая линия
    draw.line(
        (margin_x, 128, margin_x + 130, 128),
        fill=muted,
        width=2,
    )

    # если текст слишком длинный — уменьшаем шрифт
    lines = wrap_text(draw, text, main_font, max_width)

    while len(lines) > max_lines and main_font.size > 34:
        main_font = load_font(main_font.size - 4, bold=(slide_number == 1))
        lines = wrap_text(draw, text, main_font, max_width)

    lines = lines[:max_lines]

    line_height = int(main_font.size * 1.25)

    for i, line in enumerate(lines):
        draw.text(
            (margin_x, text_y + i * line_height),
            line,
            font=main_font,
            fill=cream,
            stroke_width=1,
            stroke_fill=(0, 0, 0, 120),
        )

    # footer
    draw.text(
        (margin_x, H - 86),
        "@sv_fashionacademy",
        font=label_font,
        fill=muted,
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
