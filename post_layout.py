"""
Вёрстка текстового поста (формат Post) — версия 2.

Что исправлено по сравнению с render_all_post_slides():
- Текст делится по целым предложениям и абзацам, фраза никогда не рвётся
  посреди предложения на следующую карточку.
- Обложка верстается отдельно и больше не ограничивает остальные карточки
  (раньше заголовок на первой карточке задавал ёмкость для всех — отсюда
  полупустые карточки по 5–7 строк).
- Карточки заполнены ровно: подбирается самый крупный кегль, при котором
  пост укладывается в 5–7 карточек вместе с фото, и текст распределяется
  так, чтобы самая полная и самая пустая карточки отличались минимально.
- Реальные фото: первое — на обложку (фото + заголовок), остальные встают
  отдельными карточками внутри цепочки.
- Несколько цветов фона (палитры), выбор по полю или по очереди.
- Эмодзи вычищаются из карточек (шрифт их не рисует), в подписи остаются.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350
LEFT, RIGHT = 110, 110
TEXT_TOP, TEXT_BOTTOM = 215, H - 150
MAX_TEXT_W = W - LEFT - RIGHT

FONT_REGULAR = "fonts/Montserrat-Regular.ttf"
FONT_SEMIBOLD = "fonts/Montserrat-SemiBold.ttf"
FONT_BOLD = "fonts/Montserrat-Bold.ttf"

BODY_SIZES = (56, 53, 50, 47, 44, 41, 38, 35, 33)
TARGET_MAX_SLIDES = 7

# name -> (фон, текст)
PALETTES: Dict[str, Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = {
    "green": ((18, 35, 30), (255, 255, 255)),
    "black": ((16, 16, 16), (244, 240, 232)),
    "burgundy": ((70, 20, 30), (250, 240, 230)),
    "graphite": ((46, 48, 54), (245, 245, 245)),
    "ivory": ((240, 233, 222), (28, 26, 24)),
}
ROTATION = ("green", "black", "burgundy", "ivory")

# Русские названия для поля в Airtable / ответа в Telegram.
PALETTE_ALIASES = {
    "зелёный": "green", "зеленый": "green", "green": "green",
    "чёрный": "black", "черный": "black", "black": "black",
    "бордо": "burgundy", "бордовый": "burgundy", "burgundy": "burgundy",
    "графит": "graphite", "серый": "graphite", "graphite": "graphite",
    "слоновая кость": "ivory", "айвори": "ivory", "белый": "ivory", "ivory": "ivory",
}

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-⛿✀-➿⬀-⯿"
    "■-◿️‍⃣]"
)
_SENT_SPLIT_RE = re.compile(r"(?<=[\.\!\?…»])\s+(?=[«\"“A-ZА-ЯЁ0-9—–\-→])")


def pick_palette(requested: str = "", seed: str = "") -> str:
    key = PALETTE_ALIASES.get((requested or "").strip().lower())
    if key:
        return key
    digest = int(hashlib.md5((seed or "x").encode("utf-8")).hexdigest(), 16)
    return ROTATION[digest % len(ROTATION)]


def strip_emoji(text: str) -> str:
    cleaned = _EMOJI_RE.sub("", text or "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.split("\n")]
    # строки, где остались только разделители, считаем пустыми
    lines = ["" if not re.search(r"[0-9A-Za-zА-Яа-яЁё]", line) else line for line in lines]
    return "\n".join(lines).strip()


@lru_cache(maxsize=64)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    for candidate in (path, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int = MAX_TEXT_W) -> List[str]:
    words = text.split()
    if not words:
        return []
    lines, current = [], words[0]
    for word in words[1:]:
        trial = current + " " + word
        if font.getlength(trial) <= max_w:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Разбиение текста на карточки
# ---------------------------------------------------------------------------

def split_units(body: str) -> List[Tuple[str, int]]:
    """Текст -> список (предложение, номер абзаца). Любой перенос строки — абзац."""
    units: List[Tuple[str, int]] = []
    paragraphs = [p.strip() for p in re.split(r"\n+", body or "") if p.strip()]
    for pid, paragraph in enumerate(paragraphs):
        for sentence in _SENT_SPLIT_RE.split(paragraph):
            sentence = sentence.strip()
            if sentence:
                units.append((sentence, pid))
    return units


def _group_paragraphs(units: Sequence[Tuple[str, int]]) -> List[str]:
    paragraphs: List[str] = []
    last_pid = None
    for sentence, pid in units:
        if pid == last_pid and paragraphs:
            paragraphs[-1] += " " + sentence
        else:
            paragraphs.append(sentence)
        last_pid = pid
    return paragraphs


class _Metrics:
    def __init__(self, units, size):
        self.units = units
        self.font = _font(FONT_REGULAR, size)
        self.line_h = int(size * 1.45)
        self.gap = int(self.line_h * 0.55)
        self._cache: Dict[Tuple[int, int], int] = {}

    def height(self, i: int, j: int) -> int:
        key = (i, j)
        if key not in self._cache:
            paragraphs = _group_paragraphs(self.units[i:j])
            lines = sum(len(_wrap(p, self.font)) for p in paragraphs)
            self._cache[key] = lines * self.line_h + max(0, len(paragraphs) - 1) * self.gap
        return self._cache[key]


def _best_partition(metrics: _Metrics, k: int, capacity: int) -> Optional[List[int]]:
    """
    Разбить единицы на k карточек подряд так, чтобы всё влезло и карточки
    были заполнены ровно (около 90% высоты; последняя может быть короче).
    Разрыв абзаца между карточками штрафуется — абзац стараемся держать
    целым. Возвращает индексы начал карточек.
    """
    n = len(metrics.units)
    if k > n:
        return None
    INF = float("inf")
    target = capacity * 0.9
    last_min = capacity * 0.4
    mid_penalty = (capacity * 0.3) ** 2

    def slide_cost(h: int, is_last: bool) -> float:
        if is_last:
            # одинокая фраза на последней карточке выглядит как ошибка вёрстки
            return 2.5 * max(0.0, last_min - h) ** 2
        return (target - h) ** 2 if h < target else 0.0

    dp = [[INF] * (n + 1) for _ in range(k + 1)]
    back = [[-1] * (n + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for s in range(1, k + 1):
        for j in range(s, n + 1):
            if s == k and j != n:
                continue
            for i in range(s - 1, j):
                prev = dp[s - 1][i]
                if prev == INF:
                    continue
                h = metrics.height(i, j)
                if h > capacity:
                    continue
                mid = mid_penalty if (i > 0 and metrics.units[i][1] == metrics.units[i - 1][1]) else 0.0
                cand = prev + slide_cost(h, s == k) + mid
                if cand < dp[s][j]:
                    dp[s][j] = cand
                    back[s][j] = i
    if dp[k][n] == INF:
        return None
    starts, j = [], n
    for s in range(k, 0, -1):
        i = back[s][j]
        starts.append(i)
        j = i
    return list(reversed(starts))


def plan_text_slides(
    body: str, max_text_slides: int, min_text_slides: int = 3
) -> Tuple[int, List[List[str]]]:
    """
    Подобрать кегль и разбиение. Возвращает (кегль, [абзацы карточки 1], ...).
    Берётся самый крупный кегль, при котором текст укладывается в
    max_text_slides карточек, и минимальное число карточек для него.
    Короткий текст раскладывается минимум на min_text_slides карточек,
    если каждая при этом заполнена хотя бы наполовину.
    """
    units = split_units(body)
    if not units:
        return BODY_SIZES[-1], []
    capacity = TEXT_BOTTOM - TEXT_TOP

    def build(starts):
        bounds = starts + [len(units)]
        return [_group_paragraphs(units[bounds[t]:bounds[t + 1]]) for t in range(len(starts))]

    for size in BODY_SIZES:
        metrics = _Metrics(units, size)
        for k in range(1, max_text_slides + 1):
            starts = _best_partition(metrics, k, capacity)
            if starts is None:
                continue
            # Слишком мало карточек? Пробуем разложить шире без полупустых.
            for wider in range(min(min_text_slides, max_text_slides), k, -1):
                alt = _best_partition(metrics, wider, capacity)
                if alt is None:
                    continue
                bounds = alt + [len(units)]
                heights = [metrics.height(bounds[t], bounds[t + 1]) for t in range(wider)]
                if min(heights) >= capacity * 0.5:
                    return size, build(alt)
            return size, build(starts)

    # Текст огромный: самый мелкий кегль, жадно по предложениям.
    size = BODY_SIZES[-1]
    metrics = _Metrics(units, size)
    slides, start = [], 0
    for end in range(1, len(units) + 1):
        if metrics.height(start, end) > capacity and end - 1 > start:
            slides.append(_group_paragraphs(units[start:end - 1]))
            start = end - 1
    slides.append(_group_paragraphs(units[start:]))
    return size, slides


# ---------------------------------------------------------------------------
# Рисование
# ---------------------------------------------------------------------------

def _muted(color, bg):
    return tuple(int(c * 0.55 + b * 0.45) for c, b in zip(color, bg))


def _draw_brand(draw, brand, color, shadow=False):
    font = _font(FONT_BOLD, 24)
    if shadow:
        draw.text((LEFT + 1, 81), brand, font=font, fill=(0, 0, 0, 140))
    draw.text((LEFT, 80), brand, font=font, fill=color)
    draw.line([(LEFT, 120), (LEFT + 110, 120)], fill=color, width=2)


def _draw_counter(draw, n, total, color, shadow=False):
    font = _font(FONT_REGULAR, 22)
    label = f"{n:02d}/{total:02d}"
    if shadow:
        draw.text((LEFT + 1, H - 89), label, font=font, fill=(0, 0, 0, 140))
    draw.text((LEFT, H - 90), label, font=font, fill=color)


def _fit_title(title: str, sizes, max_lines: int, max_w: int = MAX_TEXT_W):
    for size in sizes:
        font = _font(FONT_BOLD, size)
        lines = _wrap(title, font, max_w)
        if len(lines) <= max_lines:
            return font, lines, int(size * 1.18)
    font = _font(FONT_BOLD, sizes[-1])
    return font, _wrap(title, font, max_w)[:max_lines], int(sizes[-1] * 1.18)


def render_text_cover(title, palette, brand, total) -> Image.Image:
    bg, fg = PALETTES[palette]
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    _draw_brand(draw, brand, fg)
    font, lines, line_h = _fit_title(title, (86, 78, 70, 63, 57, 52), 7)
    block = len(lines) * line_h
    y = TEXT_TOP + max(0, (TEXT_BOTTOM - TEXT_TOP - block) // 2) - 30
    draw.line([(LEFT, y - 40), (LEFT + 70, y - 40)], fill=fg, width=4)
    for line in lines:
        draw.text((LEFT, y), line, font=font, fill=fg)
        y += line_h
    _draw_counter(draw, 1, total, _muted(fg, bg))
    return img


def render_photo_cover(photo_path, title, brand, total) -> Image.Image:
    base = Image.open(photo_path).convert("RGB").resize((W, H))
    img = base.convert("RGBA")
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    start = int(H * 0.40)
    for y in range(start, H):
        alpha = int(235 * ((y - start) / (H - start)) ** 1.15)
        sd.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    for y in range(0, 200):                       # лёгкая тень сверху под бренд
        sd.line([(0, y), (W, y)], fill=(0, 0, 0, int(90 * (1 - y / 200))))
    img = Image.alpha_composite(img, shade)
    draw = ImageDraw.Draw(img)
    _draw_brand(draw, brand, (255, 255, 255, 255), shadow=True)
    font, lines, line_h = _fit_title(title, (80, 72, 65, 58, 52), 5)
    y = H - 170 - len(lines) * line_h
    for line in lines:
        draw.text((LEFT + 2, y + 2), line, font=font, fill=(0, 0, 0, 160))
        draw.text((LEFT, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h
    _draw_counter(draw, 1, total, (255, 255, 255, 210), shadow=True)
    return img.convert("RGB")


def render_text_slide(paragraphs, size, palette, brand, n, total) -> Image.Image:
    bg, fg = PALETTES[palette]
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    _draw_brand(draw, brand, fg)
    font = _font(FONT_REGULAR, size)
    line_h = int(size * 1.45)
    gap = int(line_h * 0.55)
    wrapped = [_wrap(p, font) for p in paragraphs]
    block = sum(len(w) for w in wrapped) * line_h + max(0, len(wrapped) - 1) * gap
    # блок чуть выше оптического центра, чтобы карточка не выглядела полупустой
    y = TEXT_TOP + max(0, int((TEXT_BOTTOM - TEXT_TOP - block) * 0.4))
    for p_index, paragraph in enumerate(paragraphs):
        if p_index:
            y += gap
        for line in _wrap(paragraph, font):
            draw.text((LEFT, y), line, font=font, fill=fg)
            y += line_h
    _draw_counter(draw, n, total, _muted(fg, bg))
    return img


def render_photo_slide(photo_path, brand, n, total) -> Image.Image:
    img = Image.open(photo_path).convert("RGB").resize((W, H)).convert("RGBA")
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for y in range(0, 190):
        sd.line([(0, y), (W, y)], fill=(0, 0, 0, int(95 * (1 - y / 190))))
    for y in range(H - 160, H):
        sd.line([(0, y), (W, y)], fill=(0, 0, 0, int(110 * ((y - (H - 160)) / 160))))
    img = Image.alpha_composite(img, shade)
    draw = ImageDraw.Draw(img)
    _draw_brand(draw, brand, (255, 255, 255, 255), shadow=True)
    _draw_counter(draw, n, total, (255, 255, 255, 220), shadow=True)
    return img.convert("RGB")


def render_post_carousel(
    out_dir,
    title: str,
    body: str,
    photos: Optional[List[Dict]] = None,
    palette: str = "green",
    brand: str = "SV FASHION MEDIA",
) -> List[str]:
    """
    Собрать карусель поста. photos — результат photo_editor.find_photos().
    Порядок: обложка (фото+заголовок или цветная), текстовые карточки,
    между ними фото-карточки. Возвращает пути к PNG по порядку.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    photos = [p for p in (photos or []) if p.get("path")]
    palette = palette if palette in PALETTES else "green"

    title = strip_emoji(title)
    body = strip_emoji(body)

    cover_photo = photos[0]["path"] if photos else None
    inline = [p["path"] for p in photos[1:3]]

    # Подбор: всего карточек ≤ 7, если получается; иначе убираем фото
    # внутри, потом разрешаем больше текстовых карточек.
    size, text_slides = None, None
    max_total = TARGET_MAX_SLIDES if photos else TARGET_MAX_SLIDES - 1
    for n_inline in range(len(inline), -1, -1):
        max_text = max_total - 1 - n_inline
        if max_text < 1:
            continue
        s, slides = plan_text_slides(body, max_text)
        if slides and len(slides) <= max_text and s >= 38:
            size, text_slides, inline = s, slides, inline[:n_inline]
            break
    if text_slides is None:
        size, text_slides = plan_text_slides(body, TARGET_MAX_SLIDES)
        inline = []

    # Где встают фото внутри: после 1-й текстовой и ближе к концу,
    # но никогда не после последней.
    order: List[Tuple[str, object]] = [("cover", None)]
    positions = []
    if inline and len(text_slides) >= 2:
        positions = [1] if len(inline) == 1 else [1, max(2, len(text_slides) - 1)]
    photo_iter = iter(inline)
    for t_index, paragraphs in enumerate(text_slides):
        if t_index in positions:
            path = next(photo_iter, None)
            if path:
                order.append(("photo", path))
        order.append(("text", paragraphs))

    total = len(order)
    paths = []
    for n, (kind, payload) in enumerate(order, start=1):
        if kind == "cover":
            img = (
                render_photo_cover(cover_photo, title, brand, total)
                if cover_photo
                else render_text_cover(title, palette, brand, total)
            )
        elif kind == "photo":
            img = render_photo_slide(payload, brand, n, total)
        else:
            img = render_text_slide(payload, size, palette, brand, n, total)
        path = out_dir / f"post_slide_{n:02d}.png"
        img.save(path, format="PNG")
        paths.append(str(path))
    print(f"[post_layout] {total} slides (text {len(text_slides)}, body {size}px, palette {palette}, "
          f"cover {'photo' if cover_photo else 'text'}, inline photos {len(inline)})")
    return paths
