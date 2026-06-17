import os
import json
from urllib.parse import quote

import requests


AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

VISUAL_TABLE_NAME = "Visual Jobs"


def main():
    table_encoded = quote(VISUAL_TABLE_NAME, safe="")
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"

    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    }

    params = {
        "pageSize": 10,
        "filterByFormula": "{Visual Status} = 'Queued'",
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    print("Airtable status:", response.status_code)
    print("Response preview:", response.text[:1000])

    if response.status_code != 200:
        raise RuntimeError("Could not read Visual Jobs table")

    data = response.json()
    records = data.get("records", [])

    print(f"Queued visual jobs found: {len(records)}")

    for record in records:
        fields = record.get("fields", {})

        print("\n--- Visual Job ---")
        print("Record ID:", record.get("id"))
        print("Job Title:", fields.get("Job Title"))
        print("Source Post Title:", fields.get("Source Post Title"))
        print("Visual Status:", fields.get("Visual Status"))
        print("Recommended Format:", fields.get("Recommended Format"))
        print("Chosen Format:", fields.get("Chosen Format"))
        print("Visual Mode:", fields.get("Visual Mode"))

    print("\nDone. Visual Jobs read test completed.")


if __name__ == "__main__":
    main()
