"""
Общие редакционные правила для всех ботов SV Fashion Media.

1. adapt_caption() — «Редактор-адаптатор»: второй проход после автора.
   Делает текст понятным русскоязычной аудитории: поясняет бренды, имена,
   аббревиатуры и английские термины при первом упоминании, упрощает
   «слишком умные» фразы, сохраняя голос автора. Предлагает хэштеги.

2. normalize_hashtags() — жёсткое правило в коде, а не просьба в промпте:
   не больше 5 хэштегов на пост, #sv_fashion_media всегда первый.
   Хэштеги внутри текста превращаются в обычные слова, чтобы общий счёт
   никогда не превысил лимит Instagram/Buffer.

Модуль безопасен: если Claude недоступен или ответ сломан, adapt_caption()
возвращает исходный текст — конвейер не падает.
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, List, Optional

BRAND_TAG = "#sv_fashion_media"
MAX_HASHTAGS = 5

# Свои старые варианты бренда — не тратим на них слоты.
_OWN_TAG_VARIANTS = {
    "#sv_fashion_media",
    "#svfashionmedia",
    "#sv_fashionacademy",
    "#svfashionacademy",
    "#sv_fashion_academy",
    "#svfashion",
}

_TAG_RE = re.compile(r"#[0-9A-Za-zА-Яа-яЁё_]*[A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_]*")
_TAG_ONLY_LINE_RE = re.compile(r"^[\s\.\,\·\•\|\-—–]*((#[^\s#]+)[\s\.\,\·\•\|\-—–]*)+$")


def _clean_tag(tag: str) -> str:
    tag = tag.strip().rstrip(".,;:!?»)")
    if not tag.startswith("#"):
        tag = "#" + tag
    return tag


def _is_tag_only_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    without_tags = _TAG_RE.sub("", stripped)
    return bool(_TAG_RE.search(stripped)) and not re.search(r"[0-9A-Za-zА-Яа-яЁё]", without_tags)


def normalize_hashtags(text: str, extra_tags: Optional[Iterable[str]] = None) -> str:
    """
    Привести хэштеги поста к правилу: максимум 5, первым #sv_fashion_media.

    - Хвостовой блок хэштегов (строки только из хэштегов в конце) собирается.
    - Хэштеги в конце последней строки текста тоже собираются.
    - Хэштеги внутри текста превращаются в обычные слова (#Dior -> Dior),
      чтобы общий счёт хэштегов в подписи не превысил лимит.
    - extra_tags добавляются, если остались свободные слоты.
    """
    lines = str(text or "").rstrip().split("\n")
    collected: List[str] = []

    # 1. Хвостовые строки, состоящие только из хэштегов (и пустые).
    while lines:
        last = lines[-1]
        if not last.strip():
            lines.pop()
            continue
        if _is_tag_only_line(last):
            collected = _TAG_RE.findall(last) + collected
            lines.pop()
            continue
        break

    # 2. Хэштеги в самом конце последней строки текста.
    if lines:
        last = lines[-1]
        match = re.search(r"(\s+#[^\s#]+)+\s*$", last)
        if match and _TAG_RE.search(match.group(0)):
            collected = _TAG_RE.findall(match.group(0)) + collected
            lines[-1] = last[: match.start()].rstrip()

    # 3. Хэштеги внутри текста -> обычные слова.
    body = "\n".join(lines)
    body = _TAG_RE.sub(lambda m: m.group(0)[1:].replace("_", " "), body).rstrip()

    # 4. Сборка финального списка.
    final: List[str] = [BRAND_TAG]
    seen = {BRAND_TAG.lower()}
    for tag in list(collected) + list(extra_tags or []):
        tag = _clean_tag(str(tag))
        if not _TAG_RE.fullmatch(tag):
            continue
        key = tag.lower()
        if key in seen or key in _OWN_TAG_VARIANTS:
            continue
        seen.add(key)
        final.append(tag)
        if len(final) >= MAX_HASHTAGS:
            break

    return (body + "\n\n" + " ".join(final)).strip()


def strip_all_trailing_hashtags(text: str) -> str:
    """Текст без хвостового блока хэштегов (для карточек)."""
    normalized = normalize_hashtags(text)
    parts = normalized.rsplit("\n\n", 1)
    return parts[0].strip() if len(parts) == 2 else normalized


# ---------------------------------------------------------------------------
# Редактор-адаптатор
# ---------------------------------------------------------------------------

AUDIENCE_RULES = """
АУДИТОРИЯ И ПОНЯТНОСТЬ (обязательно):
- Читатель — русскоязычный: дизайнеры, владельцы небольших ателье и брендов,
  студенты, любители моды. Проверка каждой фразы: владелица ателье в
  региональном городе, 45–50 лет, понимает её с первого прочтения.
- Каждый бренд, дом моды, компанию, человека, аббревиатуру и отраслевой
  термин при ПЕРВОМ упоминании поясни коротко, 2–6 словами, естественно:
  «Kering — концерн, которому принадлежат Gucci и Saint Laurent»,
  «Жюльен Дасье, бывший креативный директор Balenciaga».
  Не поясняй то, что знает любой, кто хоть немного следит за модой:
  Chanel, Dior, Gucci, Prada, Louis Vuitton, Hermès, Zara, H&M.
- Английские слова и жаргон заменяй русскими или объясняй:
  landed cost → «полная себестоимость с доставкой и пошлинами»,
  trend report → «прогноз трендов», SS27 → «весна-лето 2027»,
  FW → «осень-зима», CEO → «гендиректор», drop → «выпуск коллекции».
- Абстрактные «умные» фразы переписывай конкретно: кто, что сделал,
  сколько, что это меняет. Одна мысль — одно предложение, до 25 слов.
- Факты, цифры, имена и цитаты не меняй и не добавляй новых. Пояснение
  бери из самого текста, из источника или из общеизвестных фактов, в
  которых уверен. Не уверен — пиши нейтрально («дизайнер», «бренд»).
"""

_ADAPTOR_SYSTEM = f"""
Ты — выпускающий редактор русскоязычного fashion-медиа @sv_fashionacademy.
Автор текста — инсайдер с 20-летним опытом в высокой моде. Его голос:
ироничный, точный, короткие фразы, юмор над индустрией. Голос сохраняешь.

Твоя единственная задача — сделать текст понятным широкой русскоязычной
аудитории, не превращая его в лекцию.
{AUDIENCE_RULES}
ФОРМА:
- Первый абзац — короткий заголовок-хук до 12 слов, отдельной строкой.
  Пояснения в хук не вставляй — они идут дальше.
- Абзацы по 1–3 предложения, между абзацами пустая строка.
- Эмодзи-маркеры автора (▪️ ⚜️ ⚡️ ➕ 🖤) сохраняй, новых не добавляй.
- Длина — не больше исходной плюс 20%.
- Запрещены: конструкция «не X, а Y» в любом виде; риторический вопрос с
  мгновенным ответом; триады рубленых фраз; финал-афоризм с тире;
  связки «и вот почему», «важно другое», «но есть нюанс», «на самом деле».
- Без морали в конце, если её не было у автора.

Хэштеги: предложи ровно 4 релевантных (бренд, человек, тема), без
#sv_fashion_media — его добавит система. В текст хэштеги не вставляй.

Верни СТРОГО JSON без markdown:
{{"text": "...", "hashtags": ["#...", "#...", "#...", "#..."], "explained": ["что пояснил", "..."]}}
"""


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


def adapt_caption(
    text: str,
    title: str = "",
    source_url: str = "",
    client=None,
    model: str = "",
) -> dict:
    """
    Прогнать текст через редактора-адаптатора.

    Возвращает {"text": str, "hashtags": list, "explained": list, "ok": bool}.
    Никогда не бросает исключений: при сбое возвращает исходный текст.
    """
    original = str(text or "").strip()
    fallback = {"text": original, "hashtags": [], "explained": [], "ok": False}
    if not original:
        return fallback

    body = strip_all_trailing_hashtags(original)

    try:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        model = model or os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6"

        user = (
            f"Тема: {title}\nИсточник: {source_url}\n\n"
            f"Текст автора:\n{body}"
        )
        message = client.messages.create(
            model=model,
            max_tokens=3000,
            system=_ADAPTOR_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        data = _extract_json(message.content[0].text)
        new_text = str(data.get("text") or "").strip()

        # Защита от поломки: пустой, слишком короткий или раздутый ответ.
        if not new_text or len(new_text) < len(body) * 0.6 or len(new_text) > len(body) * 1.6:
            print("Adaptor: ответ отклонён по длине, оставлен исходный текст.")
            return fallback

        hashtags = [str(t) for t in (data.get("hashtags") or []) if str(t).strip()]
        explained = [str(x) for x in (data.get("explained") or [])]
        print("Adaptor: пояснено —", ", ".join(explained) or "ничего")
        return {"text": new_text, "hashtags": hashtags, "explained": explained, "ok": True}
    except Exception as exc:  # конвейер не должен падать из-за редактора
        print("Adaptor failed, keeping original text:", repr(exc))
        return fallback
