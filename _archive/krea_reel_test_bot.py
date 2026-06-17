import os
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote

import requests


KREA_API_KEY = os.environ["KREA_API_KEY"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

VISUAL_TABLE_NAME = os.environ.get("AIRTABLE_VISUAL_TABLE_NAME", "Visual Jobs")

KREA_API_BASE = "https://api.krea.ai"

OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

OUTPUT_VIDEO_PATH = OUTPUT_DIR / "krea_reel_clip_test.mp4"


def airtable_table_url(table_name: str) -> str:
    table_encoded = quote(table_name, safe="")
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"


def airtable_headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def krea_headers() -> dict:
    return {
        "Authorization": f"Bearer {KREA_API_KEY}",
        "Content-Type": "application/json",
    }


def fetch_ready_for_reel_job() -> dict | None:
    url = airtable_table_url(VISUAL_TABLE_NAME)

    params = {
        "pageSize": 1,
        "filterByFormula": (
            "OR("
            "{Visual Status} = 'Ready for Reel Test', "
            "{Visual Status} = 'Ready For Reel Test', "
            "{Visual Status} = 'ready for reel test'"
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
        print("No Ready for Reel Test Visual Jobs found.")
        return None

    return records[0]


def extract_cover_image_url(output_links: str) -> str:
    if not output_links:
        return ""

    cover_match = re.search(
        r"Krea cover image generated:\s*(https?://[^\s|]+)",
        output_links,
        re.IGNORECASE,
    )

    if cover_match:
        return cover_match.group(1).strip()

    # fallback: first image URL
    urls = re.findall(r"https?://[^\s|]+", output_links)

    for url in urls:
        if any(ext in url.lower() for ext in [".png", ".jpg", ".jpeg", ".webp"]):
            return url.strip()

    return urls[0].strip() if urls else ""


def build_reel_prompt(fields: dict) -> str:
    source_title = fields.get("Source Post Title", "")
    visual_hook = fields.get("Visual Hook", "")
    reel_hook = fields.get("Reel Hook", "")
    visual_concept = fields.get("Visual Concept", "")
    shot_list = fields.get("Shot List", "")

    prompt = f"""
Animate this fashion editorial still image into a quiet luxury reel opening.

Concept:
{source_title}

Visual hook:
{visual_hook}

Reel hook:
{reel_hook}

Art direction:
{visual_concept}

Motion:
A very slow cinematic pull-back.
The object should become slightly smaller in the frame.
The surrounding empty space should become more dominant.
No fast camera movement.
No flying camera.
No rotation.
No morphing of the object.
No new objects.
No people.
No text inside the video.
No logos.
No glossy advertising style.
No chaotic motion.

Camera:
Locked premium editorial camera, subtle slow dolly-out, almost still.
Movement should feel expensive, restrained, silent, museum-like.

Lighting:
Keep the same cold editorial light, matte textures, deep shadows and quiet negative space.

Mood:
Luxury as distance. Silence. Pause. Controlled desire.

Use the start image as the exact visual reference.
""".strip()

    # Не перегружаем Krea слишком длинным текстом.
    return prompt[:2500]


def create_krea_video_job(start_image_url: str, prompt: str) -> str:
    url = f"{KREA_API_BASE}/generate/video/kling/kling-2.5"

    payload = {
        "prompt": prompt,
        "start_image": start_image_url,
        "aspect_ratio": "9:16",
        "duration": 5,
    }

    response = requests.post(
        url,
        headers=krea_headers(),
        json=payload,
        timeout=60,
    )

    print("Create Krea video job status:", response.status_code)
    print("Create Krea video job response:", response.text[:1500])

    if response.status_code not in [200, 201, 202]:
        raise RuntimeError("Krea video job creation failed")

    data = response.json()
    job_id = data.get("job_id")

    if not job_id:
        raise RuntimeError("No job_id returned from Krea")

    return job_id


def wait_for_krea_job(job_id: str, max_wait_seconds: int = 900) -> dict:
    url = f"{KREA_API_BASE}/jobs/{job_id}"
    started = time.time()

    while True:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {KREA_API_KEY}"},
            timeout=60,
        )

        print("Poll status:", response.status_code)
        print("Poll response preview:", response.text[:1200])

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
            raise TimeoutError("Krea video job timed out")

        time.sleep(10)


def collect_urls_from_obj(obj) -> list[str]:
    urls = []

    if isinstance(obj, str):
        if obj.startswith("http://") or obj.startswith("https://"):
            urls.append(obj)

    elif isinstance(obj, list):
        for item in obj:
            urls.extend(collect_urls_from_obj(item))

    elif isinstance(obj, dict):
        for value in obj.values():
            urls.extend(collect_urls_from_obj(value))

    return urls


def get_video_url(job_data: dict) -> str:
    result = job_data.get("result") or {}

    urls = collect_urls_from_obj(result)

    if not urls:
        raise RuntimeError(f"No URLs found in completed Krea job result: {job_data}")

    # Prefer mp4 / video-looking URLs
    for url in urls:
        lower = url.lower()
        if ".mp4" in lower or "video" in lower:
            return url

    return urls[0]


def download_video(video_url: str) -> Path:
    response = requests.get(video_url, timeout=300)

    if response.status_code != 200:
        raise RuntimeError("Could not download generated video")

    OUTPUT_VIDEO_PATH.write_bytes(response.content)

    print("Saved video to:", OUTPUT_VIDEO_PATH)

    return OUTPUT_VIDEO_PATH


def update_visual_job(record_id: str, fields: dict, video_url: str, job_id: str, prompt: str) -> None:
    url = f"{airtable_table_url(VISUAL_TABLE_NAME)}/{record_id}"

    existing_output_links = fields.get("Output Links", "")
    existing_render_notes = fields.get("Render Notes", "")

    now = datetime.now(timezone.utc).isoformat()

    new_output_entry = f"""
Krea reel clip test generated:
{video_url}

Krea video job_id:
{job_id}

Generated at:
{now}
""".strip()

    new_render_note = f"""
{existing_render_notes}

---

Krea Reel Test Bot v1:
Generated one 5-second vertical image-to-video reel clip from the cover image.
Status moved to Reel Clip Ready.
Prompt used:
{prompt[:1200]}
""".strip()

    payload = {
        "fields": {
            "Visual Status": "Reel Clip Ready",
            "Output Links": f"{existing_output_links}\n\n{new_output_entry}".strip(),
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
    print("Krea Reel Test Bot started:", datetime.now(timezone.utc).isoformat())

    job = fetch_ready_for_reel_job()

    if not job:
        return

    record_id = job["id"]
    fields = job.get("fields", {})

    print("Record ID:", record_id)
    print("Job Title:", fields.get("Job Title"))
    print("Source Post Title:", fields.get("Source Post Title"))

    output_links = fields.get("Output Links", "")
    cover_url = extract_cover_image_url(output_links)

    if not cover_url:
        raise RuntimeError("No cover image URL found in Output Links")

    print("Cover image URL:", cover_url)

    prompt = build_reel_prompt(fields)

    print("\n=== Reel prompt sent to Krea ===")
    print(prompt)

    job_id = create_krea_video_job(
        start_image_url=cover_url,
        prompt=prompt,
    )

    print("Krea video job_id:", job_id)

    completed_job = wait_for_krea_job(job_id)
    video_url = get_video_url(completed_job)

    print("Krea video URL:", video_url)

    download_video(video_url)

    update_visual_job(
        record_id=record_id,
        fields=fields,
        video_url=video_url,
        job_id=job_id,
        prompt=prompt,
    )

    print("Done. Reel clip test generated and Visual Job moved to Reel Clip Ready.")


if __name__ == "__main__":
    main()
