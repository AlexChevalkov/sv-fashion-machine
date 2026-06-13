import os
import sys
import json
import requests
from urllib.parse import quote
from datetime import datetime

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME")

headers = {
    "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    "Content-Type": "application/json",
}

def print_response(label, response):
    print(f"\n--- {label} ---")
    print("Status code:", response.status_code)
    try:
        data = response.json()
        print(json.dumps(data, ensure_ascii=False, indent=2)[:5000])
    except Exception:
        print(response.text[:5000])

print("\n=== Airtable diagnostic ===")
print("AIRTABLE_API_KEY present:", bool(AIRTABLE_API_KEY))
print("AIRTABLE_BASE_ID present:", bool(AIRTABLE_BASE_ID))
print("AIRTABLE_TABLE_NAME present:", bool(AIRTABLE_TABLE_NAME))
print("BASE_ID starts with app:", AIRTABLE_BASE_ID.startswith("app") if AIRTABLE_BASE_ID else False)
print("TABLE starts with tbl:", AIRTABLE_TABLE_NAME.startswith("tbl") if AIRTABLE_TABLE_NAME else False)
print("BASE_ID length:", len(AIRTABLE_BASE_ID) if AIRTABLE_BASE_ID else 0)
print("TABLE length:", len(AIRTABLE_TABLE_NAME) if AIRTABLE_TABLE_NAME else 0)

# 1. Check whether token can list accessible bases
bases_url = "https://api.airtable.com/v0/meta/bases"
bases_response = requests.get(bases_url, headers=headers)
print_response("1. LIST ACCESSIBLE BASES", bases_response)

# 2. Check whether token can read schema of the target base
schema_url = f"https://api.airtable.com/v0/meta/bases/{AIRTABLE_BASE_ID}/tables"
schema_response = requests.get(schema_url, headers=headers)
print_response("2. READ TARGET BASE SCHEMA", schema_response)

# 3. If schema is visible, print table names and IDs
if schema_response.status_code == 200:
    schema = schema_response.json()
    print("\n--- TABLES FOUND IN TARGET BASE ---")
    for table in schema.get("tables", []):
        print(f"Table name: {table.get('name')} | Table id: {table.get('id')}")

# 4. Try minimal record creation: only Title
table_encoded = quote(AIRTABLE_TABLE_NAME, safe="")
create_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"

payload = {
    "fields": {
        "Title": f"DIAG TEST: {datetime.now().isoformat()}"
    }
}

create_response = requests.post(create_url, headers=headers, json=payload)
print_response("3. CREATE MINIMAL RECORD", create_response)

if create_response.status_code in [200, 201]:
    print("\n✅ SUCCESS: GitHub can write to Airtable.")
    sys.exit(0)

print("\n❌ FAILED: Airtable write test did not pass.")
sys.exit(1)
