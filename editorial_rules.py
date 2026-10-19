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
- НАЗВАНИЯ И ИМЕНА. Событие, место, премию, человека при первом упоминании
  пиши так, чтобы их можно было найти: устоявшееся русское название и
  оригинал латиницей в скобках — «Фестиваль моды в Йере (Festival d'Hyères)»,
  «Юи Чен (Yui Chen)». Не изобретай транслитерацию: если не уверен в
  устоявшемся русском написании — пиши оригиналом латиницей.
- УСТОЯВШИЕСЯ НАЗВАНИЯ (не придумывай свои): Hyères — Йер, «фестиваль в Йере»
  (не «Гиер»); Met Gala — Met Gala / бал Института костюма; LVMH Prize —
  премия LVMH; Fashion Week — неделя моды; Haute Couture Week — неделя
  высокой моды; Central Saint Martins — колледж Central Saint Martins.
- СНАЧАЛА ФАКТ. Читатель, который не слышал новость, после первых двух
  абзацев должен суметь пересказать: что произошло, кто, где, когда, чем
  это закончилось. Мнение и выводы — только после этого.
- КОНКРЕТИКА ВМЕСТО АБСТРАКЦИЙ. Фразы вроде «честный разговор индустрии»,
  «жюри выбирает язык», «содержательный сигнал», «точка опоры» без
  конкретного факта рядом — запрещены. Если речь о показе, конкурсе или
  коллекции — назови, что именно сделано: вещи, материалы, силуэт, идея.
- Год, номер события, должности и названия бери из источника, не по памяти.
- Факты, цифры, имена и цитаты не меняй. НОВЫХ фактов не добавляй:
  никаких деталей коллекции, событий, дат, цифр и имён, которых нет в
  тексте автора. Разрешено только короткое пояснение «кто это / что это»
  (роль человека, тип компании, перевод термина) — и только если ты в нём
  уверен. Не уверен — пиши нейтрально («дизайнер», «бренд») или не поясняй.
"""

_ADAPTOR_SYSTEM = f"""
Ты — выпускающий редактор русскоязычного fashion-медиа @sv_fashionacademy.
Автор текста — инсайдер с 20-летним опытом в высокой моде. Его голос:
ироничный, точный, короткие фразы, юмор над индустрией. Голос сохраняешь.

Твоя единственная задача — сделать текст понятным широкой русскоязычной
аудитории, не превращая его в лекцию.
{AUDIENCE_RULES}
ПОРЯДОК РАБОТЫ:
1. Прочитай текст глазами человека, который НЕ слышал эту новость. Выпиши
   до 5 вопросов, которые у него возникнут («что это за фестиваль?», «кто
   победил и с чем?», «кто эти люди?», «что значит это слово?»).
2. Ответь на каждый вопрос прямо в тексте, коротко, фактами из ТЕКСТА
   ИСТОЧНИКА ниже. Если ответа в источнике нет — перепиши или убери фразу,
   которая без этого ответа непонятна. Не додумывай.
3. Проверь имена и названия по правилу выше и по написанию в источнике.

ФОРМА:
- Первый абзац — короткий заголовок-хук до 12 слов, отдельной строкой.
  Пояснения в хук не вставляй — они идут дальше.
- Абзацы по 1–3 предложения, между абзацами пустая строка.
- Структуру автора сохраняй, насколько можно: абзацы не склеивай.
  Если первые два абзаца не объясняют, что произошло, — добавь второй
  абзац с фактами (кто, что, где, когда) из источника.
- Эмодзи: только те, что уже есть у автора, на тех же местах. Новых
  эмодзи не добавляй никогда.
- Длина — не больше исходной плюс 35%. Пояснения короткие, без лекций.
- Запрещены: конструкция «не X, а Y» в любом виде; риторический вопрос с
  мгновенным ответом; триады рубленых фраз; финал-афоризм с тире;
  связки «и вот почему», «важно другое», «но есть нюанс», «на самом деле».
- Без морали в конце, если её не было у автора.

Хэштеги: предложи ровно 4 релевантных (бренд, человек, тема), без
#sv_fashion_media — его добавит система. В текст хэштеги не вставляй.

Верни СТРОГО JSON без markdown:
{{"reader_questions": ["вопрос читателя", "..."], "text": "...", "hashtags": ["#...", "#...", "#...", "#..."], "explained": ["что пояснил", "..."]}}
"""


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


_EMOJI_CHAR_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\u2B00-\u2BFF\u25A0-\u25FF]\uFE0F?"
)


def _drop_new_emoji(original: str, new_text: str) -> str:
    """Убрать эмодзи, которых не было в тексте автора."""
    allowed = set(m.group(0).rstrip("\uFE0F") for m in _EMOJI_CHAR_RE.finditer(original))

    def keep(match):
        return match.group(0) if match.group(0).rstrip("\uFE0F") in allowed else ""

    cleaned = _EMOJI_CHAR_RE.sub(keep, new_text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return "\n".join(line.strip() for line in cleaned.split("\n")).strip()


_ARTICLE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


def fetch_article_text(url: str, limit: int = 7000) -> str:
    """
    Текст статьи-источника (заголовок, лид и абзацы), чтобы автор и редактор
    писали по фактам, а не по двум строкам из RSS. При ошибке — пустая строка.
    """
    if not url or not str(url).startswith("http"):
        return ""
    try:
        import requests
        from bs4 import BeautifulSoup

        resp = requests.get(url, headers={"User-Agent": _ARTICLE_UA}, timeout=25)
        if resp.status_code != 200:
            print(f"Article fetch: HTTP {resp.status_code} for {url}")
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        parts = []
        title = soup.find("meta", attrs={"property": "og:title"})
        if title and title.get("content"):
            parts.append(title["content"].strip())
        desc = soup.find("meta", attrs={"property": "og:description"}) or soup.find(
            "meta", attrs={"name": "description"}
        )
        if desc and desc.get("content"):
            parts.append(desc["content"].strip())
        scope = soup.find("article") or soup.find("main") or soup
        for p in scope.find_all(["p", "h2", "h3"]):
            line = p.get_text(" ", strip=True)
            if len(line) >= 40 and line not in parts:
                parts.append(line)
            if sum(len(x) for x in parts) > limit:
                break
        text = "\n".join(parts)[:limit]
        print(f"Article fetch: {len(text)} chars from {url}")
        return text
    except Exception as exc:
        print("Article fetch failed:", repr(exc))
        return ""


def adapt_caption(
    text: str,
    title: str = "",
    source_url: str = "",
    client=None,
    model: str = "",
    source_text: str = "",
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

        if not source_text and source_url:
            source_text = fetch_article_text(source_url)
        min_len, max_len = int(len(body) * 0.6), int(len(body) * 1.45) + 150

        data, new_text = {}, ""
        for attempt in (1, 2):
            limit_note = (
                f"\n\nОГРАНИЧЕНИЕ ДЛИНЫ: итоговый текст от {min_len} до {max_len} знаков. "
                "Если текст автора уже понятен — меняй минимально."
            )
            if attempt == 2:
                limit_note += (
                    f"\nПрошлый вариант был {len(new_text)} знаков — это вне рамок. "
                    "Сократи пояснения, оставь только самое нужное."
                )
            user = (
                f"Тема: {title}\nИсточник: {source_url}\n\n"
                f"ТЕКСТ ИСТОЧНИКА (факты бери только отсюда):\n{source_text or 'недоступен — новых фактов не добавляй'}\n\n"
                f"Текст автора ({len(body)} знаков):\n{body}{limit_note}"
            )
            message = client.messages.create(
                model=model,
                max_tokens=4000,
                system=_ADAPTOR_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            data = _extract_json(message.content[0].text)
            new_text = str(data.get("text") or "").strip()
            if new_text and min_len <= len(new_text) <= max_len:
                break
            print(f"Adaptor: попытка {attempt} вне рамок длины ({len(new_text)} при {len(body)}).")
        else:
            # Защита от поломки: оставляем текст автора, но хэштеги берём.
            print("Adaptor: правка отклонена по длине, оставлен исходный текст.")
            hashtags = [str(t) for t in (data.get("hashtags") or []) if str(t).strip()]
            return {**fallback, "hashtags": hashtags}

        new_text = _drop_new_emoji(body, new_text)
        hashtags = [str(t) for t in (data.get("hashtags") or []) if str(t).strip()]
        explained = [str(x) for x in (data.get("explained") or [])]
        questions = [str(x) for x in (data.get("reader_questions") or [])]
        print("Adaptor: вопросы читателя —", " | ".join(questions) or "нет")
        print("Adaptor: пояснено —", ", ".join(explained) or "ничего")
        return {"text": new_text, "hashtags": hashtags, "explained": explained, "ok": True}
    except Exception as exc:  # конвейер не должен падать из-за редактора
        print("Adaptor failed, keeping original text:", repr(exc))
        return fallback
