"""
Проверка перестройки на реальных темах — БЕЗ записи в Airtable и Buffer.

Для каждой записи Visual Jobs (только чтение):
  1. редактор-адаптатор: текст до / после, что пояснено;
  2. хэштеги по правилу;
  3. фоторедактор: какие фото найдены;
  4. вёрстка: Post — новая карусель; Carousel — фото со старым оверлеем.
Результат: outputs/smoke/<запись>/... и outputs/smoke/REPORT.md
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from editorial_rules import adapt_caption, normalize_hashtags, strip_all_trailing_hashtags  # noqa: E402
from photo_editor import find_photos  # noqa: E402
from post_layout import pick_palette, render_post_carousel  # noqa: E402

BASE = os.environ["AIRTABLE_BASE_ID"]
KEY = os.environ["AIRTABLE_API_KEY"]
RECORDS = [r for r in os.environ.get("SMOKE_RECORDS", "").split(",") if r.strip()]
OUT = Path("outputs/smoke")
OUT.mkdir(parents=True, exist_ok=True)


def fetch(record_id):
    url = f"https://api.airtable.com/v0/{BASE}/{quote('Visual Jobs')}/{record_id}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
    resp.raise_for_status()
    return resp.json()["fields"]


def sheet(paths, dest, w=360, h=450):
    ims = [Image.open(p).convert("RGB") for p in paths]
    canvas = Image.new("RGB", (w * len(ims), h), "white")
    for i, im in enumerate(ims):
        canvas.paste(im.resize((w, h)), (i * w, 0))
    canvas.save(dest, quality=88)


report = ["# Smoke test\n", f"SERPER_API_KEY: {'есть' if os.environ.get('SERPER_API_KEY') else 'нет'}\n"]

for rid in RECORDS:
    fields = fetch(rid.strip())
    title = fields.get("Job Title", rid)
    fmt = (fields.get("Format") or fields.get("Chosen Format") or "").strip()
    src = fields.get("Source URL", "")
    caption = fields.get("Source Final Caption") or fields.get("Final Reel Caption") or ""
    folder = OUT / rid
    folder.mkdir(parents=True, exist_ok=True)

    report.append(f"\n## {title}\nФормат: {fmt} · источник: {src}\n")

    adapted = adapt_caption(caption, title=title, source_url=src)
    final_caption = normalize_hashtags(adapted["text"], adapted["hashtags"])
    (folder / "caption_before.txt").write_text(caption, encoding="utf-8")
    (folder / "caption_after.txt").write_text(final_caption, encoding="utf-8")
    report.append(f"Адаптатор: {'ok' if adapted['ok'] else 'НЕ СРАБОТАЛ'}; пояснено: {', '.join(adapted['explained']) or '—'}\n")
    report.append("\n**После:**\n\n```\n" + final_caption + "\n```\n")

    want = 3 if fmt.lower() == "post" else int(fields.get("Slide Count") or 6)
    photos = find_photos(title, strip_all_trailing_hashtags(final_caption), src, want, folder / "photos")
    report.append(f"Фото: {len(photos)} — " + "; ".join(f"{p['credit']} ({p['origin']})" for p in photos) + "\n")

    if fmt.lower() == "post":
        body = strip_all_trailing_hashtags(final_caption)
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        head, rest = (paras[0], "\n\n".join(paras[1:])) if len(paras) > 1 else (title, body)
        paths = render_post_carousel(folder / "slides", head, rest, photos, pick_palette("", rid))
    else:
        os.environ.setdefault("KREA_API_KEY", "unused")
        import visual_production_bot as vpb  # noqa: E402

        slide_copy = [s for s in (fields.get("Slide Copy") or "").split("\n") if s.strip()]
        paths = []
        (folder / "slides").mkdir(exist_ok=True)
        for i, photo in enumerate(photos):
            img = Image.open(photo["path"])
            text = slide_copy[i] if i < len(slide_copy) else ""
            out = vpb.add_text_overlay(img, text, i + 1, len(photos), is_cover=(i == 0))
            p = folder / "slides" / f"slide_{i + 1:02d}.png"
            out.convert("RGB").save(p)
            paths.append(str(p))
    if paths:
        sheet(paths, OUT / f"sheet_{rid}.jpg")
    report.append(f"Карточек: {len(paths)}\n")

(OUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
print("\n".join(report))
