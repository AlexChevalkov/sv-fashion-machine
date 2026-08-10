"""
Рил 05 — текстовый рилс с эффектом печатной машинки.

Берёт готовый текст поста из Visual Jobs, режет его на экраны, собирает
вертикальное видео в Remotion (папка reel05/), кладёт файл в R2 и создаёт
черновик в Buffer.

Ветка экспериментальная и намеренно отделена от остального конвейера:
у неё свой статус, свой запуск и свой скрипт, так что сломать ею
работающие посты, карусели и рилсы нельзя.

Как запускается:
    Visual Jobs → Visual Status = "Reel 05"

Что делает дальше:
    успех  → "Sent for Buffer" (черновик в Buffer)
    ошибка → "Ready for Buffer" (причина в Render Notes)

Переменные окружения — те же, что у остальных ботов: AIRTABLE_API_KEY,
AIRTABLE_BASE_ID, R2_*, BUFFER_ACCESS_TOKEN.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from buffer_publish import buffer_is_configured, create_instagram_draft
from r2_storage import r2_is_configured, upload_file_to_r2


AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
VISUAL_TABLE = os.environ.get("AIRTABLE_VISUAL_TABLE_NAME", "Visual Jobs")

STATUS_TRIGGER = "Reel 05"
STATUS_SENT = "Sent for Buffer"
STATUS_MANUAL = "Ready for Buffer"

PROJECT_DIR = Path(__file__).parent / "reel05"
OUTPUT_DIR = Path("outputs")

# Сколько знаков влезает на экран, не мельча шрифт. Подобрано по живому
# материалу: у первого экрана бюджет меньше, потому что сверху стоит заголовок.
# Жёсткий предел — сколько физически помещается в текстовую область при
# кегле 54: около восемнадцати строк по три десятка знаков.
MAX_BODY_CHARS = 220
MAX_BODY_CHARS_HARD = 420
FIRST_SCREEN_CHARS = 130
MAX_SCREENS = 8


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- Airtable ---

def airtable_url(record_id: str = "") -> str:
    table = quote(VISUAL_TABLE, safe="")
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}"
    return f"{url}/{record_id}" if record_id else url


def airtable_headers(write: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    if write:
        headers["Content-Type"] = "application/json"
    return headers


def fetch_queued_jobs(limit: int = 1) -> list:
    response = requests.get(
        airtable_url(),
        headers=airtable_headers(),
        params={
            "maxRecords": limit,
            "filterByFormula": f"{{Visual Status}}='{STATUS_TRIGGER}'",
        },
        timeout=30,
    )
    print("Read Visual Jobs status:", response.status_code)
    response.raise_for_status()
    return response.json().get("records", [])


def update_job(record_id: str, fields: dict) -> None:
    response = requests.patch(
        airtable_url(record_id),
        headers=airtable_headers(write=True),
        json={"fields": fields, "typecast": True},
        timeout=30,
    )
    print("Update Visual Job:", response.status_code, list(fields))
    response.raise_for_status()


def field(fields: dict, name: str, default: str = "") -> str:
    value = fields.get(name, default)
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get("name", default)
    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x is not None)
    return str(value)


# ------------------------------------------------------------ текст → экраны ---

def split_sentences(text: str) -> list:
    """Режет абзац по границам предложений, сохраняя знак в конце."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def pack_units(units: list, first_budget: int, budget: int) -> list:
    """
    Складывает предложения в экраны по бюджету знаков.

    units — список пар (номер абзаца, предложение). Предложения одного абзаца
    склеиваются пробелом, разные абзацы на одном экране — пустой строкой.
    Предложение целиком не разрывается: лучше экран чуть плотнее, чем фраза,
    оборванная посередине.
    """
    screens, current, current_para = [], "", None

    for para, sentence in units:
        limit = first_budget if not screens else budget
        joiner = "" if not current else ("\n\n" if para != current_para else " ")
        candidate = f"{current}{joiner}{sentence}"

        if current and len(candidate) > limit:
            screens.append(current)
            current, current_para = sentence, para
        else:
            current, current_para = candidate, para

    if current:
        screens.append(current)
    return screens


def build_slides(title: str, body: str) -> list:
    """
    Готовый текст поста → список экранов.

    Единица разбивки — предложение, а не абзац: абзацы у постов слишком
    разного размера, и разбивка по ним даёт то экран в три строки, то экран
    в одну. Первому экрану отводится меньше знаков, потому что над ним стоит
    заголовок.

    Экраны не выбрасываются никогда: если текста много, растёт бюджет на
    экран, а не теряется концовка — в ней обычно вся мысль.
    """
    units = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    for index, paragraph in enumerate(paragraphs):
        for sentence in split_sentences(paragraph):
            units.append((index, sentence))

    if not units:
        return [{"title": title, "body": ""}] if title else []

    first_budget = FIRST_SCREEN_CHARS if title else MAX_BODY_CHARS
    budget = MAX_BODY_CHARS

    screens = pack_units(units, first_budget, budget)
    while len(screens) > MAX_SCREENS and budget < MAX_BODY_CHARS_HARD:
        budget = min(MAX_BODY_CHARS_HARD, int(budget * 1.25))
        first_budget = min(budget, int(first_budget * 1.25))
        screens = pack_units(units, first_budget, budget)

    slides = [{"title": title, "body": screens[0]} if title else {"body": screens[0]}]
    slides.extend({"body": s} for s in screens[1:])
    return slides


def pick_text(fields: dict) -> tuple:
    """Заголовок и текст поста из карточки Visual Jobs."""
    title = (
        field(fields, "Source Hook")
        or field(fields, "Source Post Title")
        or field(fields, "Job Title")
    ).strip()

    body = (
        field(fields, "Source Final Caption")
        or field(fields, "Final Reel Caption")
        or field(fields, "Source Raw Text")
    ).strip()

    # Заголовок часто повторяется первой строкой текста — не печатаем дважды.
    if body.startswith(title):
        body = body[len(title):].strip()

    return title, body


# ------------------------------------------------------------------ рендер ---

def render_reel(slides: list, record_id: str) -> str:
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = (OUTPUT_DIR / f"reel05_{record_id}.mp4").resolve()
    props_path = (PROJECT_DIR / "props.json").resolve()
    props_path.write_text(
        json.dumps({"slides": slides}, ensure_ascii=False), encoding="utf-8"
    )

    print(f"Экранов: {len(slides)}")
    for i, slide in enumerate(slides, start=1):
        preview = (slide.get("title", "") + " | " + slide["body"])[:90]
        print(f"  {i:02d}: {preview}")

    steps = []
    # В GitHub Actions зависимости ставит отдельный шаг воркфлоу; локально их
    # обычно нет, поэтому ставим сами.
    if not (PROJECT_DIR / "node_modules").exists():
        steps.append(["npm", "ci", "--no-audit", "--no-fund"])

    steps += [
        ["node", "scripts/make-font-data.mjs"],
        [
            "npx", "remotion", "render", "Reel05", str(out_path),
            f"--props={props_path}",
            "--log=error",
        ],
    ]
    browser = os.environ.get("REMOTION_BROWSER_EXECUTABLE")
    if browser:
        steps[-1].append(f"--browser-executable={browser}")

    for step in steps:
        print("+", " ".join(step))
        result = subprocess.run(step, cwd=PROJECT_DIR, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Шаг не выполнен: {' '.join(step)}")

    if not out_path.exists():
        raise RuntimeError("Remotion отработал, но файла нет: " + str(out_path))

    print("Готов файл:", out_path, out_path.stat().st_size // 1024, "КБ")
    return str(out_path)


def job_folder(record: dict) -> str:
    created = str(record.get("createdTime", ""))[:10] or "0000-00-00"
    return f"{created}_reel05_{record['id']}"


def process(record: dict) -> None:
    record_id = record["id"]
    fields = record.get("fields", {})
    notes = field(fields, "Render Notes")

    title, body = pick_text(fields)
    if not body:
        raise RuntimeError(
            "Нет текста поста: пусты и Source Final Caption, и Final Reel Caption."
        )

    slides = build_slides(title, body)
    video_path = render_reel(slides, record_id)

    if not r2_is_configured():
        raise RuntimeError("R2 не настроен — некуда положить файл для Buffer.")

    video_url = upload_file_to_r2(
        video_path, f"bot-output/{job_folder(record)}/reel05.mp4"
    )

    if not buffer_is_configured():
        raise RuntimeError("Buffer не настроен.")

    caption = field(fields, "Source Final Caption") or body
    ok, info = create_instagram_draft(caption=caption, video_url=video_url)
    print("Buffer:", ok, info)

    if ok:
        update_job(record_id, {
            "Visual Status": STATUS_SENT,
            "Output Links": f"Рил 05 собран: {video_url}\nGenerated at: {now_iso()}",
            "Render Notes": notes + f"\n\n---\n\nРил 05: {len(slides)} экранов. {info}",
        })
    else:
        update_job(record_id, {
            "Visual Status": STATUS_MANUAL,
            "Output Links": f"Рил 05 собран: {video_url}\nGenerated at: {now_iso()}",
            "Render Notes": notes + f"\n\n---\n\nРил 05 собран, но черновик в Buffer "
                                    f"не создан ({info}). Выложи вручную из ссылки выше.",
        })


def main() -> None:
    print("Reel 05 Bot started:", now_iso())

    jobs = fetch_queued_jobs(limit=1)
    if not jobs:
        print(f"Карточек со статусом «{STATUS_TRIGGER}» нет.")
        return

    record = jobs[0]
    record_id = record["id"]
    print("Карточка:", record_id, field(record.get("fields", {}), "Job Title"))

    try:
        process(record)
    except Exception as error:
        print("ОШИБКА:", repr(error))
        update_job(record_id, {
            "Visual Status": STATUS_MANUAL,
            "Render Notes": f"Рил 05 не собрался {now_iso()}:\n{error}",
        })
        raise

    print("Reel 05 Bot finished.")


if __name__ == "__main__":
    main()
