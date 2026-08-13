import os
import json
import base64
import requests
from pathlib import Path

EBAY_APP_ID = os.environ["EBAY_APP_ID"]
EBAY_CERT_ID = os.environ["EBAY_CERT_ID"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MARKETPLACE = "EBAY_US"
MAX_RESULTS = 50

SEEN_FILE = Path("seen.json")
SEARCHES_FILE = Path("searches.json")

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def get_ebay_token():
    creds = f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Basic " + base64.b64encode(creds).decode(),
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    r = requests.post(EBAY_OAUTH_URL, headers=headers, data=data, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def search_ebay(token, search):
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
    }
    params = {"q": search["q"], "sort": "newlyListed", "limit": MAX_RESULTS}
    if search.get("filter"):
        params["filter"] = search["filter"]
    if search.get("category_ids"):
        params["category_ids"] = search["category_ids"]
    r = requests.get(EBAY_SEARCH_URL, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("itemSummaries") or []


def send_telegram(text):
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(TELEGRAM_URL, json=payload, timeout=30)
    r.raise_for_status()


def format_item(item, label):
    price = item.get("price", {})
    price_str = f'{price.get("value", "?")} {price.get("currency", "")}'.strip()
    lines = [
        f"\U0001F0CF <b>New listing</b> \u2014 {label}",
        f"<b>{item.get('title', 'Untitled')}</b>",
        f"\U0001F4B5 {price_str}",
    ]
    if item.get("condition"):
        lines.append(f"\U0001F4E6 {item['condition']}")
    lines.append(item.get("itemWebUrl", ""))
    return "\n".join(lines)


def main():
    searches = json.loads(SEARCHES_FILE.read_text())

    first_run = not SEEN_FILE.exists()
    seen = set() if first_run else set(json.loads(SEEN_FILE.read_text()))

    token = get_ebay_token()
    new_count = 0

    for search in searches:
        label = search.get("label", search["q"])
        try:
            items = search_ebay(token, search)
        except requests.HTTPError as e:
            print(f"Search failed for '{label}': {e}")
            continue

        for item in items:
            item_id = item.get("itemId")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            if not first_run:
                try:
                    send_telegram(format_item(item, label))
                    new_count += 1
                except requests.HTTPError as e:
                    print(f"Telegram send failed: {e}")

    SEEN_FILE.write_text(json.dumps(sorted(seen)))

    if first_run:
        send_telegram("\u2705 eBay monitor is live. I'll ping you when new matching listings appear.")
        print(f"First run - seeded {len(seen)} items, no alerts sent.")
    else:
        print(f"Done. Sent {new_count} alerts. Tracking {len(seen)} items.")


if __name__ == "__main__":
    main()
