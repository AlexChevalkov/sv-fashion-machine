"""
Фоторедактор SV Fashion Media.

Ищет РЕАЛЬНЫЕ фото по теме поста вместо генерации:
  1. картинки из исходной статьи (og:image, фото в тексте);
  2. Google Картинки через Serper.dev (если задан SERPER_API_KEY);
  3. Wikimedia Commons (бесплатно, без ключа) — запасной источник.
Потом Claude смотрит на кандидатов и оставляет только подходящие:
на фото именно этот показ / человек / вещь, хорошее качество, без
водяных знаков, коллажей и скриншотов. Код обрезает под 4:5
(1080×1350), не отрезая главный объект, и ставит подпись источника.

Модуль никогда не роняет конвейер: при любой ошибке возвращает
столько фото, сколько удалось найти (в том числе ноль).

Переменные окружения:
  SERPER_API_KEY         — ключ serper.dev (необязательно)
  PHOTO_EDITOR_ENABLED   — "0", чтобы выключить (по умолчанию включён)
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

CANVAS_W, CANVAS_H = 1080, 1350
MIN_SIDE = 600          # меньше — не берём, будет мыло
MAX_BYTES = 15_000_000
MAX_CANDIDATES = 16     # столько показываем Claude
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
CREDIT_FONT = os.environ.get("PHOTO_CREDIT_FONT", "fonts/Montserrat-SemiBold.ttf")

_SKIP_URL_PARTS = (
    "logo", "icon", "avatar", "sprite", "placeholder", "favicon",
    "banner-ad", "/ads/", "pixel", "badge", "emoji",
)


def photo_editor_enabled() -> bool:
    return os.environ.get("PHOTO_EDITOR_ENABLED", "1").strip() not in ("0", "false", "no")


def _log(*args):
    print("[photo_editor]", *args)


def _domain(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in response")
    return json.loads(text[start : end + 1])


def _anthropic_client(client=None):
    if client is not None:
        return client
    import anthropic

    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _model(model: str = "") -> str:
    return model or os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# 1. Поисковые запросы
# ---------------------------------------------------------------------------

def make_queries(title: str, text: str, client=None, model: str = "") -> Dict:
    """Claude превращает тему в 2–3 точных поисковых запроса по-английски."""
    fallback = {"queries": [title[:120]] if title else [], "subject": title}
    try:
        client = _anthropic_client(client)
        prompt = f"""
Тема поста fashion-медиа: {title}

Текст поста:
{text[:2500]}

Нужно найти РЕАЛЬНЫЕ фотографии для иллюстрации: конкретный показ,
коллекцию, человека, вещь, магазин или событие из текста.

Верни СТРОГО JSON:
{{"subject": "что именно должно быть на фото, 1 строка по-русски",
  "queries": ["2–3 поисковых запроса по-английски для Google Картинок, максимально конкретно: бренд + сезон + runway / имя + designer / название вещи"]}}
Пример запросов: "Glenn Martens Diesel spring 2027 runway", "Anthony Vaccarello portrait", "Chanakya International embroidery atelier Mumbai".
"""
        message = client.messages.create(
            model=_model(model),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_json(message.content[0].text)
        queries = [str(q).strip() for q in data.get("queries") or [] if str(q).strip()][:3]
        if not queries:
            return fallback
        return {"queries": queries, "subject": str(data.get("subject") or title)}
    except Exception as exc:
        _log("make_queries failed:", repr(exc))
        return fallback


# ---------------------------------------------------------------------------
# 2. Кандидаты
# ---------------------------------------------------------------------------

def _good_url(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith("http"):
        return False
    if low.endswith(".svg") or ".svg?" in low or low.endswith(".gif"):
        return False
    return not any(part in low for part in _SKIP_URL_PARTS)


def _largest_from_srcset(srcset: str) -> str:
    best, best_w = "", 0
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                width = 0
        if width >= best_w:
            best, best_w = bits[0], width
    return best


def article_candidates(source_url: str, limit: int = 8) -> List[Dict]:
    """Картинки из самой статьи-источника: самые релевантные по определению."""
    if not source_url or not source_url.startswith("http"):
        return []
    try:
        from bs4 import BeautifulSoup

        resp = requests.get(source_url, headers={"User-Agent": UA}, timeout=20)
        if resp.status_code != 200:
            _log("article fetch", resp.status_code, source_url)
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        urls: List[str] = []

        for key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"):
            for tag in soup.find_all("meta", attrs={"property": key}) + soup.find_all(
                "meta", attrs={"name": key}
            ):
                if tag.get("content"):
                    urls.append(urljoin(source_url, tag["content"]))

        scope = soup.find("article") or soup.find("main") or soup
        for img in scope.find_all("img"):
            src = (
                _largest_from_srcset(img.get("srcset") or img.get("data-srcset") or "")
                or img.get("data-src")
                or img.get("data-original")
                or img.get("src")
                or ""
            )
            if src:
                urls.append(urljoin(source_url, src))

        out, seen = [], set()
        for url in urls:
            if url in seen or not _good_url(url):
                continue
            seen.add(url)
            out.append({"url": url, "credit": _domain(source_url), "page": source_url, "origin": "article"})
            if len(out) >= limit:
                break
        _log(f"article: {len(out)} candidates from {_domain(source_url)}")
        return out
    except Exception as exc:
        _log("article_candidates failed:", repr(exc))
        return []


def serper_candidates(queries: List[str], per_query: int = 6) -> List[Dict]:
    """Google Картинки через serper.dev. Без ключа — пусто."""
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        return []
    out = []
    for query in queries:
        try:
            resp = requests.post(
                "https://google.serper.dev/images",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": query, "num": 10},
                timeout=20,
            )
            if resp.status_code != 200:
                _log("serper", resp.status_code, resp.text[:200])
                continue
            taken = 0
            for item in resp.json().get("images", []):
                url = item.get("imageUrl", "")
                w, h = item.get("imageWidth") or 0, item.get("imageHeight") or 0
                if not _good_url(url) or (w and h and min(w, h) < MIN_SIDE):
                    continue
                out.append(
                    {
                        "url": url,
                        "credit": item.get("domain") or _domain(item.get("link", "")) or item.get("source", ""),
                        "page": item.get("link", ""),
                        "origin": "google",
                        "title": item.get("title", ""),
                    }
                )
                taken += 1
                if taken >= per_query:
                    break
        except Exception as exc:
            _log("serper failed:", repr(exc))
    _log(f"google: {len(out)} candidates")
    return out


def commons_candidates(queries: List[str], per_query: int = 4) -> List[Dict]:
    """Wikimedia Commons: свободная лицензия, хорошо для портретов и архива."""
    out = []
    for query in queries:
        try:
            resp = requests.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "generator": "search",
                    "gsrnamespace": 6,
                    "gsrsearch": query,
                    "gsrlimit": per_query * 2,
                    "prop": "imageinfo",
                    "iiprop": "url|size|mime",
                    "iiurlwidth": 1600,
                },
                headers={"User-Agent": "SVFashionMedia/1.0 (photo editor bot)"},
                timeout=20,
            )
            pages = (resp.json().get("query") or {}).get("pages") or {}
            taken = 0
            for page in pages.values():
                info = (page.get("imageinfo") or [{}])[0]
                if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
                    continue
                if min(info.get("width") or 0, info.get("height") or 0) < MIN_SIDE:
                    continue
                url = info.get("thumburl") or info.get("url")
                out.append(
                    {
                        "url": url,
                        "credit": "Wikimedia Commons",
                        "page": info.get("descriptionurl", ""),
                        "origin": "commons",
                        "title": page.get("title", ""),
                    }
                )
                taken += 1
                if taken >= per_query:
                    break
        except Exception as exc:
            _log("commons failed:", repr(exc))
    _log(f"commons: {len(out)} candidates")
    return out


# ---------------------------------------------------------------------------
# 3. Загрузка, фильтр, дубли
# ---------------------------------------------------------------------------

def _download(candidate: Dict) -> Optional[Dict]:
    url = candidate["url"]
    reason = ""
    for headers in (
        {"User-Agent": UA, "Referer": candidate.get("page") or "", "Accept": "image/avif,image/webp,image/*,*/*"},
        {"User-Agent": UA, "Accept": "image/*,*/*"},
    ):
        try:
            resp = requests.get(url, headers=headers, timeout=20, stream=True)
            if resp.status_code != 200:
                reason = f"HTTP {resp.status_code}"
                continue
            data = resp.raw.read(MAX_BYTES + 1, decode_content=True)
            if len(data) > MAX_BYTES or len(data) < 15_000:
                reason = f"size {len(data)}"
                continue
            image = Image.open(io.BytesIO(data))
            image = ImageOps.exif_transpose(image).convert("RGB")
            w, h = image.size
            if min(w, h) < MIN_SIDE:
                reason = f"small {w}x{h}"
                break
            ratio = w / h
            if ratio > 2.2 or ratio < 0.4:   # баннеры и полоски
                reason = f"ratio {w}x{h}"
                break
            return {**candidate, "image": image}
        except Exception as exc:
            reason = repr(exc)[:120]
    _log(f"skip ({reason}): {url[:110]}")
    return None


def _ahash(image: Image.Image) -> int:
    small = image.convert("L").resize((8, 8), Image.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for value in pixels:
        bits = (bits << 1) | (1 if value > avg else 0)
    return bits


def _dedupe(items: List[Dict]) -> List[Dict]:
    kept, hashes = [], []
    for item in items:
        h = _ahash(item["image"])
        if any(bin(h ^ other).count("1") <= 6 for other in hashes):
            continue
        hashes.append(h)
        kept.append(item)
    return kept


# ---------------------------------------------------------------------------
# 4. Отбор глазами Claude
# ---------------------------------------------------------------------------

def _thumb_b64(image: Image.Image, max_side: int = 640) -> str:
    thumb = image.copy()
    thumb.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def select_with_vision(
    items: List[Dict], title: str, subject: str, text: str, want: int, client=None, model: str = ""
) -> List[Dict]:
    """Claude смотрит на кандидатов и выбирает лучшие с точкой фокуса."""
    client = _anthropic_client(client)
    content = [
        {
            "type": "text",
            "text": (
                f"Ты фоторедактор русскоязычного fashion-медиа.\n"
                f"Тема поста: {title}\nЧто должно быть на фото: {subject}\n"
                f"Текст поста (кратко): {text[:1200]}\n\n"
                f"Ниже {len(items)} фото-кандидатов, у каждого номер. Выбери до {want} лучших для карусели "
                f"Instagram 4:5, лучшее первым (оно пойдёт на обложку).\n"
                "Бери фото, ТОЛЬКО если:\n"
                "- на нём именно предмет поста: этот показ, эта коллекция, этот человек, эта вещь или место;\n"
                "- хорошее качество, не мыло;\n"
                "- нет водяных знаков, крупного текста, логотипов на весь кадр, коллажей, скриншотов, рекламных баннеров;\n"
                "- кадр выдержит обрезку до вертикали 4:5.\n"
                "Не подходит ни одно — верни пустой список, это нормально.\n"
                "Для каждого выбранного укажи точку фокуса (лицо или главный объект) в долях ширины и высоты.\n"
                'Верни СТРОГО JSON: {"selected": [{"index": 0, "focus_x": 0.5, "focus_y": 0.35, "why": "коротко"}]}'
            ),
        }
    ]
    for idx, item in enumerate(items):
        content.append({"type": "text", "text": f"Фото №{idx}  (источник: {item.get('credit', '')})"})
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": _thumb_b64(item["image"])},
            }
        )

    message = client.messages.create(
        model=_model(model),
        max_tokens=1200,
        messages=[{"role": "user", "content": content}],
    )
    data = _extract_json(message.content[0].text)
    chosen = []
    for entry in data.get("selected") or []:
        try:
            idx = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(items) and all(c["_idx"] != idx for c in chosen):
            fx = min(1.0, max(0.0, float(entry.get("focus_x", 0.5))))
            fy = min(1.0, max(0.0, float(entry.get("focus_y", 0.4))))
            chosen.append({**items[idx], "_idx": idx, "focus": (fx, fy), "why": entry.get("why", "")})
        if len(chosen) >= want:
            break
    for c in chosen:
        _log(f"selected #{c['_idx']} {c.get('credit')}: {c.get('why')}")
    return chosen


# ---------------------------------------------------------------------------
# 5. Обрезка 4:5 и подпись источника
# ---------------------------------------------------------------------------

def crop_to_canvas(image: Image.Image, focus=(0.5, 0.4), width: int = CANVAS_W, height: int = CANVAS_H) -> Image.Image:
    """Обрезка под заданный формат вокруг точки фокуса."""
    w, h = image.size
    target = width / height
    if w / h > target:            # слишком широкая — режем по бокам
        new_w = int(h * target)
        left = int(focus[0] * w - new_w / 2)
        left = max(0, min(w - new_w, left))
        box = (left, 0, left + new_w, h)
    else:                         # слишком высокая — режем сверху/снизу
        new_h = int(w / target)
        top = int(focus[1] * h - new_h * 0.42)   # объект чуть выше центра
        top = max(0, min(h - new_h, top))
        box = (0, top, w, top + new_h)
    return image.crop(box).resize((width, height), Image.LANCZOS)


def draw_credit(image: Image.Image, credit: str) -> Image.Image:
    if not credit:
        return image
    img = image.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype(CREDIT_FONT, 20)
    except Exception:
        font = ImageFont.load_default()
    label = f"Фото: {credit}"
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = img.width - tw - 36, img.height - th - 34
    draw.text((x + 1, y + 1), label, font=font, fill=(0, 0, 0, 150))
    draw.text((x, y), label, font=font, fill=(255, 255, 255, 200))
    return Image.alpha_composite(img, overlay).convert("RGB")


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

def find_photos(
    title: str,
    text: str,
    source_url: str,
    want: int,
    out_dir,
    client=None,
    model: str = "",
    with_credit: bool = True,
) -> List[Dict]:
    """
    Найти до `want` реальных фото по теме.
    Возвращает список {"path", "credit", "page", "origin", "url"}.
    Первое фото — лучшее (для обложки). Никогда не бросает исключений.
    """
    if want <= 0 or not photo_editor_enabled():
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        plan = make_queries(title, text, client=client, model=model)
        queries, subject = plan["queries"], plan["subject"]
        _log("queries:", queries)

        candidates = article_candidates(source_url)
        candidates += serper_candidates(queries)
        if len(candidates) < 8:
            candidates += commons_candidates(queries)

        # уникальные URL, порядок сохраняется (статья — первой)
        seen, unique = set(), []
        for c in candidates:
            if c["url"] not in seen:
                seen.add(c["url"])
                unique.append(c)

        with ThreadPoolExecutor(max_workers=8) as pool:
            downloaded = [d for d in pool.map(_download, unique[:40]) if d]
        downloaded = _dedupe(downloaded)[:MAX_CANDIDATES]
        _log(f"usable candidates: {len(downloaded)}")
        if not downloaded:
            return []

        try:
            chosen = select_with_vision(downloaded, title, subject, text, want, client=client, model=model)
        except Exception as exc:
            # Без «глаз» берём только картинки из самой статьи — они точно по теме.
            _log("vision failed, falling back to article images:", repr(exc))
            chosen = [
                {**d, "focus": (0.5, 0.4)} for d in downloaded if d.get("origin") == "article"
            ][:want]

        results = []
        for n, item in enumerate(chosen, start=1):
            canvas = crop_to_canvas(item["image"], item.get("focus", (0.5, 0.4)))
            if with_credit:
                canvas = draw_credit(canvas, item.get("credit", ""))
            path = out_dir / f"photo_{n:02d}.jpg"
            canvas.save(path, format="JPEG", quality=92)
            results.append(
                {
                    "path": str(path),
                    "credit": item.get("credit", ""),
                    "page": item.get("page", ""),
                    "origin": item.get("origin", ""),
                    "url": item.get("url", ""),
                }
            )
        _log(f"done: {len(results)} photo(s)")
        return results
    except Exception as exc:
        _log("find_photos failed:", repr(exc))
        return []
