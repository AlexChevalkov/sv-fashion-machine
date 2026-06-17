import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

import requests


AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

CONTENT_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Content Inbox")
VISUAL_TABLE_NAME = os.environ.get("AIRTABLE_VISUAL_TABLE_NAME", "Visual Jobs")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def airtable_table_url(table_name: str) -> str:
    table_encoded = quote(table_name, safe="")
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"


def airtable_headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def normalize_title(title: str) -> str:
    title = (title or "").lower().strip()
    title = re.sub(r"[^\w\sа-яА-ЯёЁ]", " ", title)
    title = re.sub(r"\s+", " ", title)
    return title


def get_table_schema(table_name: str) -> dict:
    url = f"https://api.airtable.com/v0/meta/bases/{AIRTABLE_BASE_ID}/tables"

    response = requests.get(
        url,
        headers=airtable_headers(),
        timeout=30,
    )

    if response.status_code != 200:
        print("Schema read failed:", response.status_code, response.text[:500])
        return {}

    data = response.json()

    for table in data.get("tables", []):
        if table.get("name") == table_name:
            return {
                field.get("name"): field.get("type")
                for field in table.get("fields", [])
            }

    return {}


def filter_fields_for_table(fields: dict, table_schema: dict) -> dict:
    if not table_schema:
        return fields

    return {
        key: value
        for key, value in fields.items()
        if key in table_schema
    }


def fetch_approved_content_posts() -> list[dict]:
    url = airtable_table_url(CONTENT_TABLE_NAME)

    params = {
        "pageSize": 20,
        "filterByFormula": "{Status} = 'Approved'",
    }

    response = requests.get(
        url,
        headers=airtable_headers(),
        params=params,
        timeout=30,
    )

    print("Read Approved Content status:", response.status_code)
    print("Read Approved Content preview:", response.text[:700])

    if response.status_code != 200:
        raise RuntimeError("Could not read approved Content Inbox posts")

    return response.json().get("records", [])


def visual_job_already_exists(content_record_id: str, title: str) -> bool:
    url = airtable_table_url(VISUAL_TABLE_NAME)

    title_safe = title.replace("'", "\\'")
    formula = (
        f"OR("
        f"{{Source Content ID}} = '{content_record_id}', "
        f"{{Source Post Title}} = '{title_safe}'"
        f")"
    )

    response = requests.get(
        url,
        headers=airtable_headers(),
        params={
            "pageSize": 1,
            "filterByFormula": formula,
        },
        timeout=30,
    )

    if response.status_code != 200:
        print("Duplicate check failed, will continue carefully.")
        print(response.status_code, response.text[:700])
        return False

    return bool(response.json().get("records", []))


def build_visual_job_fields(content_record: dict, visual_schema: dict) -> dict:
    content_id = content_record["id"]
    fields = content_record.get("fields", {})

    title = fields.get("Title", "Untitled Content")
    hook = fields.get("HOOK", "")
    visual_headline = fields.get("Visual Headline", "")
    final_caption = fields.get("Final Caption", "")
    raw_text = fields.get("Raw Text", "")
    rubric = fields.get("Rubric", "")
    source_url = fields.get("Source URL", "")
    format_value = fields.get("Format", "Reel + Carousel")

    source_content_value = content_id

    if visual_schema.get("Source Content ID") == "multipleRecordLinks":
        source_content_value = [content_id]

    visual_fields = {
        "Job Title": title,
        "Source Post Title": title,
        "Source Content ID": source_content_value,
        "Source Raw Text": raw_text,
        "Source Final Caption": final_caption,
        "Source Hook": hook,
        "Source URL": source_url,
        "Rubric": rubric,
        "Format": format_value,
        "Chosen Format": format_value,
        "Visual Headline": visual_headline,
        "Visual Mode": "Hybrid",
        "Visual Status": "Queued",
        "Render Notes": (
            "Created automatically from approved Content Inbox post.\n\n"
            f"Content record: {content_id}\n"
            f"Created at: {now_iso()}"
        ),
    }

    return filter_fields_for_table(visual_fields, visual_schema)


def create_visual_job(content_record: dict, visual_schema: dict) -> str:
    url = airtable_table_url(VISUAL_TABLE_NAME)

    visual_fields = build_visual_job_fields(content_record, visual_schema)

    response = requests.post(
        url,
        headers=airtable_headers(),
        json={
            "fields": visual_fields,
            "typecast": True,
        },
        timeout=30,
    )

    print("Create Visual Job status:", response.status_code)
    print("Create Visual Job preview:", response.text[:1000])

    if response.status_code not in [200, 201, 202]:
        raise RuntimeError("Could not create Visual Job")

    return response.json()["id"]


def update_content_after_transfer(content_record_id: str, visual_job_id: str, content_schema: dict) -> None:
    update_fields = {}

    if "Visual Job Created" in content_schema:
        update_fields["Visual Job Created"] = True

    if "Visual Job ID" in content_schema:
        update_fields["Visual Job ID"] = visual_job_id

    if not update_fields:
        print("No transfer marker fields found in Content Inbox. Skipping content update.")
        return

    url = f"{airtable_table_url(CONTENT_TABLE_NAME)}/{content_record_id}"

    response = requests.patch(
        url,
        headers=airtable_headers(),
        json={
            "fields": update_fields,
            "typecast": True,
        },
        timeout=30,
    )

    print("Update Content Inbox status:", response.status_code)
    print("Update Content Inbox preview:", response.text[:700])

    if response.status_code not in [200, 201, 202]:
        raise RuntimeError("Could not update Content Inbox after transfer")


def main() -> None:
    print("Transfer to Visual Bot started:", now_iso())

    content_schema = get_table_schema(CONTENT_TABLE_NAME)
    visual_schema = get_table_schema(VISUAL_TABLE_NAME)

    approved_posts = fetch_approved_content_posts()

    if not approved_posts:
        print("No Approved Content Inbox posts found.")
        return

    created_count = 0
    skipped_count = 0

    for content_record in approved_posts:
        content_id = content_record["id"]
        fields = content_record.get("fields", {})
        title = fields.get("Title", "Untitled Content")

        print("=" * 80)
        print("Content:", title)
        print("Record:", content_id)

        if fields.get("Visual Job Created") is True:
            print("Skipped: Visual Job Created already checked.")
            skipped_count += 1
            continue

        if visual_job_already_exists(content_id, title):
            print("Skipped: Visual Job already exists.")
            skipped_count += 1
            continue

        visual_job_id = create_visual_job(content_record, visual_schema)

        update_content_after_transfer(
            content_record_id=content_id,
            visual_job_id=visual_job_id,
            content_schema=content_schema,
        )

        created_count += 1

    print("=" * 80)
    print("Transfer finished.")
    print("Created Visual Jobs:", created_count)
    print("Skipped:", skipped_count)


if __name__ == "__main__":
    main()
