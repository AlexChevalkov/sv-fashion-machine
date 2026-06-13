import os
import requests
from datetime import datetime

WEBHOOK_URL = os.environ["AIRTABLE_WEBHOOK_URL"]

payload = {
    "title": "WEBHOOK TEST: GitHub → Airtable",
    "status": "Needs Review",
    "format": "Single Post",
    "rubric": "Fashion Context",
    "hook": "GitHub отправил карточку через webhook.",
    "visual_headline": "Webhook Test",
    "final_caption": "Это тестовая карточка. Если она появилась в Alex Review, значит путь GitHub → Airtable Automation → Content Inbox работает.",
    "raw_text": f"Тест отправлен автоматически: {datetime.now().isoformat()}",
    "source_url": "https://github.com/AlexChevalkov/-fashion-content-bot"
}

response = requests.post(WEBHOOK_URL, json=payload)

print("Status code:", response.status_code)
print("Response:", response.text)

if response.status_code not in [200, 201, 202]:
    raise Exception("Webhook request failed")
