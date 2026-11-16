"""Union Arena eBay listing monitor.

Each run checks the saved searches in searches.json for listings it has not
seen before and sends them to Telegram.

How it works:

* eBay allows 5,000 searches a day. Checks are spread evenly across the whole
  day so the allowance never runs out, however many searches there are and
  however often the monitor is triggered.
* Telegram accepts about 20 messages a minute in a group. Alerts are paced,
  a large batch is sent as a few summary messages, and a listing is only
  marked as seen once its alert has actually been delivered.
* An alert is a photo card showing the card's name, star rarity, grade and
  price, read from the seller's title, with a button to the listing.
* Network hiccups are retried and never crash a run. Lasting problems are
  reported in Telegram once instead of failing silently.
"""

import base64
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- settings

# eBay's limit is 5,000 Browse API calls a day. Staying a little under leaves
# room for retries and manual runs. Raise this only if eBay raises the limit.
DAILY_CALL_BUDGET = 4800

MAX_RESULTS = 50          # newest listings fetched per search
MARKETPLACE = "EBAY_US"

# eBay returns Buy It Now listings only unless auctions are asked for.
# True adds auctions to every search; a search can also set "auctions": true.
INCLUDE_AUCTIONS = False

# Sealed product (booster boxes, cases, packs, decks) is skipped: any listing
# the seller filed under one of eBay's "Sealed" categories is ignored. A search
# can opt back in with "sealed": true.
SKIP_SEALED_PRODUCTS = True

FULL_ALERTS_PER_RUN = 10  # beyond this, the rest of a run's finds are summarised
SUMMARY_SIZE = 10         # listings per summary message

# ------------------------------------------------------------- environment

EBAY_APP_ID = os.environ["EBAY_APP_ID"]
EBAY_CERT_ID = os.environ["EBAY_CERT_ID"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
# Optional: where warnings about the monitor itself go. Defaults to the main chat.
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or TELEGRAM_CHAT_ID

EBAY_API = os.environ.get("EBAY_API_URL", "https://api.ebay.com")
TELEGRAM_API = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org")
GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
EVENT = os.environ.get("GITHUB_EVENT_NAME", "")

SEEN_FILE = Path("seen.json")
STATE_FILE = Path("state.json")
SEARCHES_FILE = Path("searches.json")

TIMEOUT = (10, 20)            # seconds to connect, seconds to wait for a reply
TELEGRAM_GAP = 1.1            # seconds between Telegram messages
TELEGRAM_WAIT_BUDGET = 75     # most seconds one run will wait out a rate limit
AUTH_FAILURES_BEFORE_WARNING = 3
TRIGGER_SILENCE_LIMIT = 30 * 60


class Transient(Exception):
    """A request that failed for a reason likely to pass (timeout, 5xx)."""


class RateLimited(Exception):
    """eBay's daily call allowance is used up."""


class EbayAuthError(Exception):
    """eBay rejected the app's credentials."""


class SearchRejected(Exception):
    """eBay refused one search as written (bad keywords or filter)."""


class TelegramBusy(Exception):
    """Telegram is rate-limiting; whatever is left goes out next run."""


class TelegramDown(Exception):
    """Telegram can't be reached or is refusing the bot."""


class MessageRejected(Exception):
    """Telegram refused one specific message."""


# ------------------------------------------------------------------ helpers

def scrub(text):
    """Keep credentials out of anything that is logged, saved or sent."""
    text = str(text)
    for secret in (TELEGRAM_TOKEN, EBAY_CERT_ID, GITHUB_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return text


def esc(text):
    return html.escape(str(text), quote=False)


def pacific_date(now):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return (datetime.fromtimestamp(now, timezone.utc) - timedelta(hours=8)).date().isoformat()


def parse_time(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def http(method, url, attempts=1, retry_statuses=(500, 502, 503, 504), **kwargs):
    """Make a request, retrying timeouts, dropped connections and server errors."""
    problem = "no attempt made"
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 * attempt)
        try:
            response = requests.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as error:
            problem = type(error).__name__
            continue
        if response.status_code in retry_statuses and attempt < attempts - 1:
            problem = f"HTTP {response.status_code}"
            continue
        return response
    raise Transient(problem)


def ebay_error(response):
    """The short reason eBay gave for refusing a request."""
    try:
        body = response.json()
        first = (body.get("errors") or [{}])[0]
        reason = first.get("message") or body.get("error_description") or body.get("error")
    except ValueError:
        reason = None
    return scrub(f"HTTP {response.status_code}" + (f": {reason}" if reason else ""))[:300]


# --------------------------------------------------------------------- eBay

def get_ebay_token():
    credentials = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    # eBay's login endpoint occasionally answers 401 or 5xx for valid
    # credentials, so those are retried before being treated as real.
    response = http(
        "POST", f"{EBAY_API}/identity/v1/oauth2/token",
        attempts=3, retry_statuses=(401, 429, 500, 502, 503, 504),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
    )
    if response.status_code != 200:
        raise EbayAuthError(ebay_error(response))
    try:
        return response.json()["access_token"]
    except (ValueError, KeyError):
        raise Transient("eBay login reply was not readable")


def search_filter(search):
    wanted = str(search.get("filter") or "").strip()
    if search.get("auctions", INCLUDE_AUCTIONS) and "buyingOptions" not in wanted:
        wanted = ",".join(p for p in (wanted, "buyingOptions:{FIXED_PRICE|AUCTION}") if p)
    return wanted


def signature(search):
    """What a search matches. Changing only the label keeps the same signature."""
    parts = (search["q"], search_filter(search), search.get("category_ids") or "")
    return " | ".join(" ".join(str(part).lower().split()) for part in parts if part)


def search_ebay(token, search):
    params = {"q": search["q"], "sort": "newlyListed", "limit": MAX_RESULTS}
    if search_filter(search):
        params["filter"] = search_filter(search)
    if search.get("category_ids"):
        params["category_ids"] = search["category_ids"]
    response = http(
        "GET", f"{EBAY_API}/buy/browse/v1/item_summary/search", attempts=2,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        },
        params=params,
    )
    status = response.status_code
    if status == 200:
        try:
            return response.json().get("itemSummaries") or []
        except ValueError:
            raise Transient("eBay search reply was not readable")
    if status == 429:
        raise RateLimited(ebay_error(response))
    if status in (401, 403):
        raise EbayAuthError(ebay_error(response))
    if 400 <= status < 500:
        raise SearchRejected(ebay_error(response))
    raise Transient(f"HTTP {status}")


def category(item):
    """The eBay category a listing was filed under, most specific first."""
    names = [str(c.get("categoryName") or c.get("categoryId") or "") for c in item.get("categories") or []]
    return names[0] if names and names[0] else "unknown category"


def is_sealed_product(item):
    return any(
        "sealed" in str(c.get("categoryName") or "").lower() for c in item.get("categories") or []
    )


# ------------------------------------------------------- reading a listing
#
# eBay only gives the seller's title, so the card's name, rarity and grade are
# worked out from it. When something can't be worked out it is left off the
# alert rather than guessed.

GRADERS = "PSA|BGS|CGC|SGC|TAG|ARS|ACE|AGS|BVG|HGA|GMA"
GRADE = re.compile(
    rf"\b({GRADERS})\b[\s:#-]*(?:GEM[\s-]*(?:MINT|MT)[\s-]*|PRISTINE[\s-]*|MINT[\s-]*|GRADED[\s-]*)?"
    r"(10|[1-9](?:\.5)?)(?![\d.])"
    r"(?!\s*(?:\?|candidate|contender|potential|ready|worthy|quality|possible|chance|hopeful))",
    re.IGNORECASE,
)
STAR_WORDS = {"one": 1, "single": 1, "1": 1, "two": 2, "double": 2, "2": 2, "three": 3, "triple": 3, "3": 3}
RARITY_CODES = "SR|UR|SEC|SP|AP|PR|R|U|C"
CARD_NUMBER = re.compile(
    r"\b(?:[A-Z]{2}\d{2}[A-Z]{2}|UAPR|[A-Z]{2}PR)\s?[/-]\s?[A-Z0-9]{2,4}-\d-\d{2,3}[A-Z]?\b"   # UA22BT/CSM-1-067
    r"|\b[A-Z]{2}\d{2}(?:BT|ST|EX|PR|NC)\b"                                                    # UA22BT
    r"|\b[A-Z]{2,4}-\d-\d{2,3}[A-Z]?\b",                                                       # CSM-1-067
    re.IGNORECASE,
)
# Series whose name is not normally part of a card's name. Longest first.
SERIES = sorted([
    "Bleach Thousand-Year Blood War", "Bleach Thousand Year Blood War", "Thousand-Year Blood War",
    "Thousand Year Blood War", "Bleach TYBW", "TYBW", "Bleach", "Jujutsu Kaisen", "JJK",
    "Hunter x Hunter", "Hunter \u00D7 Hunter", "HunterxHunter", "HxH", "Demon Slayer Kimetsu no Yaiba",
    "Kimetsu no Yaiba", "Demon Slayer", "Chainsaw Man", "Solo Leveling", "Blue Lock",
    "Code Geass Lelouch of the Rebellion", "Lelouch of the Rebellion", "Code Geass", "My Hero Academia",
    "MHA", "That Time I Got Reincarnated as a Slime", "Reincarnated as a Slime", "Tensura",
    "Sword Art Online", "SAO", "Black Clover", "Attack on Titan", "AOT", "Haikyu!!", "Haikyuu", "Haikyu",
    "Dr. Stone", "Dr Stone", "Gintama", "Yu Yu Hakusho", "YuYu Hakusho", "Fullmetal Alchemist Brotherhood",
    "Fullmetal Alchemist", "One Punch Man", "One-Punch Man", "Undead Unluck", "Kaiju No. 8", "Kaiju No 8",
    "Goddess of Victory NIKKE", "Goddess of Victory", "NIKKE", "Arknights", "Tekken 7", "Tales of Arise",
    "SynDuality Noir", "SynDuality", "The Idolmaster Shiny Colors", "Idolmaster Shiny Colors",
    "Shiny Colors", "Gakuen Idolmaster", "Idolmaster", "Rurouni Kenshin", "2.5 Dimensional Seduction",
    "The 100 Girlfriends Who Really Really Really Really Really Love You", "The 100 Girlfriends",
    "100 Girlfriends", "To Love-Ru Darkness", "To Love Ru Darkness", "To Love-Ru", "To Love Ru",
    "Puella Magi Madoka Magica", "Madoka Magica", "Sakamoto Days", "Re:Zero", "Re Zero",
    "Beyond Journey's End", "Mobile Suit Gundam", "Macross Frontier", "Trigun Stampede",
], key=len, reverse=True)
SERIES_PATTERN = re.compile(
    "|".join(rf"(?<![\w'])" + re.escape(name).replace(r"\ ", r"[\s:-]+") + r"(?![\w'])" for name in SERIES),
    re.IGNORECASE,
)
NOISE_PHRASES = re.compile(
    r"\b(?:union\s*arena|trading\s+card\s+game|card\s+game|near\s+mint|pack\s+fresh|alt(?:ernate)?\s+art|"
    r"full\s+art|super\s+rare|secret\s+rare|gem\s+(?:mint|mt)|free\s+shipping|fast\s+shipping|in\s+hand|"
    r"ships?\s+(?:today|fast|now)|us(?:a)?\s+seller|box\s+fresh|pop\s*\d+|vol(?:ume)?\.?\s*\d+|"
    r"booster\s+pack|starter\s+deck|extra\s+booster|new\s+card\s+selection|bandai\s+namco|"
    r"nm\s*/\s*m|m\s*/\s*nm|nm-mt)\b",
    re.IGNORECASE,
)
NOISE_WORDS = {
    "ua", "bandai", "tcg", "ccg", "card", "cards", "japanese", "japan", "jp", "jpn", "english", "en", "eng",
    "nm", "nm-m", "nm+", "mint", "lp", "mp", "holo", "foil", "parallel", "rare", "new", "hot", "fresh",
    "graded", "slab", "pop", "sr", "r", "u", "c", "ur", "sp", "sec", "ap", "pr", "star", "stars",
    "trigger", "vol", "volume", "official", "authentic", "aa", "cert", "pristine", "carddass", "namco",
    "candidate", "contender", "potential", "ready", "worthy", "quality", "possible", "hopeful",
}
ACRONYMS = {"OBC", "SAO", "MHA", "JJK", "AOT", "HXH", "TYBW", "II", "III", "IV", "DX", "EX", "GT"}
SMALL_WORDS = {"of", "the", "no", "x", "and", "de", "von", "van", "to", "a", "an", "in", "on", "from", "with", "for"}


def stars(title):
    """How many stars the card's rarity has (1 to 3), or None if the title doesn't say."""
    text = re.sub("[\u2606\u2B50\u2730\u272A\u2605\U0001F31F]\uFE0F?", "\u2605", title)
    found = re.search(r"\b(one|two|three|single|double|triple|[123])[\s-]*stars?\b", text, re.IGNORECASE)
    if found:
        return STAR_WORDS[found.group(1).lower()]
    found = re.search(r"(?<![\d.])([123])\s?[\u2605*](?![\u2605*\w])", text)
    if found:
        return int(found.group(1))
    groups = re.findall("\u2605+", text)
    if groups:
        return min(max(len(group) for group in groups), 3)
    found = re.search(rf"\b(?:{RARITY_CODES})\s?(\*{{1,3}})(?![*\w])", text)
    return len(found.group(1)) if found else None


def grade(item):
    """'PSA 10', 'Graded' or 'Raw'. eBay's own graded/ungraded flag wins over the title."""
    condition = str(item.get("condition") or "").lower()
    found = GRADE.search(str(item.get("title") or ""))
    slab = f"{found.group(1).upper()} {found.group(2)}" if found else None
    if condition == "graded" or str(item.get("conditionId")) == "2750":
        return slab or "Graded"
    if condition == "ungraded" or str(item.get("conditionId")) == "4000":
        return "Raw"
    return slab or "Raw"


def price(item):
    amount = item.get("price") or {}
    if item.get("currentBidPrice") and "FIXED_PRICE" not in (item.get("buyingOptions") or []):
        amount = item["currentBidPrice"]
    try:
        value = float(amount.get("value"))
    except (TypeError, ValueError):
        return "price not shown"
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    text = f"${text}" if amount.get("currency") in ("USD", None) else f"{text} {amount['currency']}"
    return f"{text} bid" if amount is item.get("currentBidPrice") else text


def card_name(title):
    """The seller's title with everything that isn't the card's name taken out."""
    title = str(title or "")
    letters = [c for c in title if c.isalpha()]
    shouting = bool(letters) and sum(c.isupper() for c in letters) > 0.7 * len(letters)
    text = re.sub("[\u2606\u2B50\u2730\u272A\u2605\U0001F31F]\uFE0F?", "\u2605", title)
    # Grades, including hopeful ones like "PSA 10?", then rarity and card numbers.
    text = re.sub(
        rf"\b(?:{GRADERS})\b[\s:#-]*(?:(?:GEM|MINT|MT|PRISTINE|GRADED)[\s-]*)*(?:\d{{1,2}}(?:\.5)?\+?)?\s*\??",
        " ", text, flags=re.IGNORECASE,
    )
    text = CARD_NUMBER.sub(" ", text)
    text = re.sub(r"\b(?:one|two|three|single|double|triple|[123])[\s-]*stars?\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<![\d.])[123]\s?[\u2605*]+", " ", text)
    text = re.sub(rf"\b(?:{RARITY_CODES})\s?[\u2605*]+", " ", text)
    text = re.sub("[\u2605*]+", " ", text)
    text = re.sub("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", " ", text)      # emoji
    text = NOISE_PHRASES.sub(" ", text)
    with_series = text
    text = SERIES_PATTERN.sub(" ", text)

    def tidy(value):
        value = re.sub(
            r"(?<!\d)/(?!\d)|[|\\,;:()\[\]{}~\u2022\u00B7!?#\u3010\u3011\u300C\u300D\u300E\u300F]+|\s-+\s|\s-+$|^-+\s",
            " ", value,
        )
        words = []
        for word in value.split():
            bare = word.strip("-.").lower()
            if word.lower() in NOISE_WORDS or bare in NOISE_WORDS or re.fullmatch(r"\d{3,}|-*", bare):
                continue
            if word.upper() in ACRONYMS:
                word = word.upper()
            elif re.fullmatch(r"[A-Z][A-Z'.-]+", word) and (shouting or len(word) > 3) and "." not in word:
                word = word.title()                      # ICHIGO -> Ichigo, HAE-IN -> Hae-In
            elif word.islower() and word not in SMALL_WORDS:
                word = word.capitalize()
            words.append(word)
        return " ".join(words)[:60].strip(" -.") if len(" ".join(words)) > 60 else " ".join(words).strip(" -")

    return tidy(text) or tidy(with_series) or title[:60] or "Untitled"


def describe(item):
    """The four things an alert shows: name, rarity, grade and price."""
    title = str(item.get("title") or "")
    count = stars(title)
    return {
        "name": card_name(title),
        "detail": "  \u00B7  ".join(part for part in ("\u2605" * count if count else "", grade(item)) if part),
        "price": price(item),
    }


def item_url(item):
    return item.get("itemWebUrl") or f'https://www.ebay.com/itm/{item.get("legacyItemId", "")}'


def photo_urls(item):
    """The listing photo, large first, then exactly as eBay gave it."""
    original = (item.get("image") or {}).get("imageUrl")
    if not original:
        return []
    large = re.sub(r"/s-l\d+\.", "/s-l800.", original.replace("/thumbs/images/", "/images/"))
    return [large, original] if large != original else [original]


def format_item(item):
    card = describe(item)
    return f"<b>{esc(card['name'])}</b>\n{esc(card['detail'])}\n<b>{esc(card['price'])}</b>"


def format_summary(entries, remaining_after):
    lines = [f"<b>+{len(entries)} more</b>"]
    for item, _ in entries:
        card = describe(item)
        legacy = item.get("legacyItemId")
        url = f"https://www.ebay.com/itm/{legacy}" if legacy else item_url(item)
        lines.append(
            f'\u2022 <a href="{html.escape(url, quote=True)}">{esc(card["name"])}</a>'
            f"  \u00B7  {esc(card['detail'])}  \u00B7  <b>{esc(card['price'])}</b>"
        )
    if remaining_after:
        lines.append(f"({remaining_after} more to follow)")
    return "\n".join(lines)


# ----------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self):
        self.last_sent = None
        self.wait_left = TELEGRAM_WAIT_BUDGET
        self.moved_to = None      # new chat ID if Telegram upgraded the group

    def send(self, text, chat_id=None, preview=True, photos=(), link=None):
        """Send one message. With photos and a link it is a photo card with a button.

        If Telegram refuses a version of the message (it can't fetch the photo,
        or dislikes the formatting) the next simpler version is tried, ending
        with plain text, so an alert is not lost over presentation.
        """
        chat = chat_id or TELEGRAM_CHAT_ID
        if self.moved_to and str(chat) == str(TELEGRAM_CHAT_ID):
            chat = self.moved_to
        button = {"inline_keyboard": [[{"text": "View on eBay", "url": link}]]} if link else None
        with_link = f"{text}\n{esc(link)}" if link else text
        versions = [
            ("sendPhoto", {"photo": url, "caption": text, "parse_mode": "HTML", "reply_markup": button})
            for url in (photos if link else ())
        ]
        versions.append(("sendMessage", {
            "text": with_link, "parse_mode": "HTML", "disable_web_page_preview": not preview,
        }))
        versions.append(("sendMessage", {
            "text": html.unescape(re.sub(r"<[^>]+>", "", with_link)), "disable_web_page_preview": not preview,
        }))
        network_failures = 0
        while True:
            method, payload = versions[0]
            payload = dict(payload, chat_id=chat)
            if self.last_sent is not None:
                gap = TELEGRAM_GAP - (time.monotonic() - self.last_sent)
                if gap > 0:
                    time.sleep(gap)
            try:
                response = requests.post(
                    f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/{method}", json=payload, timeout=TIMEOUT,
                )
            except requests.RequestException as error:
                self.last_sent = time.monotonic()
                network_failures += 1
                if network_failures >= 3:
                    raise TelegramDown(type(error).__name__)
                time.sleep(2 * network_failures)
                continue
            self.last_sent = time.monotonic()
            status = response.status_code
            if status == 200:
                return
            try:
                body = response.json()
            except ValueError:
                body = {}
            reason = scrub(body.get("description") or f"HTTP {status}")
            moved = (body.get("parameters") or {}).get("migrate_to_chat_id")
            if status == 429:
                wait = int((body.get("parameters") or {}).get("retry_after") or 5) + 1
                if wait > self.wait_left:
                    raise TelegramBusy(reason)
                self.wait_left -= wait
                time.sleep(wait)
            elif status >= 500:
                network_failures += 1
                if network_failures >= 3:
                    raise TelegramDown(reason)
                time.sleep(2 * network_failures)
            elif status == 400 and moved and moved != chat:
                # Telegram upgraded the group, which changes its ID. Follow it.
                self.moved_to = chat = moved
            elif status == 400 and "chat" not in reason.lower():
                versions.pop(0)
                if not versions:
                    raise MessageRejected(reason)
                print(f"  Telegram refused the {method} version ({reason}); trying a simpler one.")
            else:
                raise TelegramDown(reason)


def deliver(telegram, fresh, seen):
    """Send alerts, marking each listing seen only once Telegram has accepted it.

    Returns (listings delivered, whether Telegram is unusable).
    """
    full_count = len(fresh) if len(fresh) <= FULL_ALERTS_PER_RUN + 2 else FULL_ALERTS_PER_RUN
    batches = [(True, [entry]) for entry in fresh[:full_count]]
    batches += [
        (False, fresh[start:start + SUMMARY_SIZE])
        for start in range(full_count, len(fresh), SUMMARY_SIZE)
    ]
    delivered = 0
    try:
        for is_full, batch in batches:
            remaining = len(fresh) - delivered - len(batch)
            try:
                if is_full:
                    item = batch[0][0]
                    telegram.send(format_item(item), photos=photo_urls(item), link=item_url(item))
                else:
                    telegram.send(format_summary(batch, remaining), preview=False)
            except MessageRejected as error:
                print(f"Telegram refused one alert ({error}); skipping it.")
            for item, _ in batch:
                seen.add(item["itemId"])
            delivered += len(batch)
    except TelegramBusy:
        print(f"Telegram is rate-limiting. {len(fresh) - delivered} listings will go out next run.")
    except TelegramDown as error:
        print(f"Telegram is not accepting messages: {error}")
        return delivered, True
    return delivered, False


# -------------------------------------------------------- saved state/notes

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_if_changed(path, text):
    try:
        if path.read_text() == text:
            return
    except OSError:
        pass
    path.write_text(text)


def warn_once(telegram, state, key, text, fingerprint="sent"):
    """Report a problem with the monitor itself, once per distinct problem."""
    notes = state.setdefault("notices", {})
    if notes.get(key) == fingerprint:
        return
    try:
        telegram.send(text, chat_id=TELEGRAM_ADMIN_CHAT_ID, preview=False)
    except (TelegramBusy, TelegramDown, MessageRejected) as error:
        print(f"Could not send a warning ({error}); will retry next run.")
        return
    notes[key] = fingerprint


def settle(telegram, state, key, text=None):
    """A problem that was reported is over."""
    if state.get("notices", {}).pop(key, None) is None or not text:
        return
    try:
        telegram.send(text, chat_id=TELEGRAM_ADMIN_CHAT_ID, preview=False)
    except (TelegramBusy, TelegramDown, MessageRejected):
        pass


def load_searches(telegram, state):
    """Read searches.json. If it is broken, say so and keep the last good list."""
    problems = []
    searches = []
    try:
        raw = json.loads(SEARCHES_FILE.read_text())
        if not isinstance(raw, list):
            raise ValueError("the file must be a list of searches inside [ ]")
        for position, entry in enumerate(raw, start=1):
            if isinstance(entry, dict) and str(entry.get("q") or "").strip():
                searches.append(entry)
            else:
                problems.append(f'search #{position} has no "q" keywords')
    except (OSError, ValueError) as error:
        problems = [str(error)]
        searches = []

    if not problems:
        settle(telegram, state, "searches_file", "\u2705 searches.json is valid again.")
        state["last_good_searches"] = searches
        return searches

    detail = scrub("; ".join(problems))[:400]
    if searches:
        consequence = "The other searches are still being watched."
    elif state.get("last_good_searches"):
        searches = state["last_good_searches"]
        consequence = "Still watching the last working version of the list until it is fixed."
    else:
        consequence = "Nothing is being watched until it is fixed."
    warn_once(
        telegram, state, "searches_file",
        f"\u26A0\uFE0F <b>searches.json has a mistake</b>: {esc(detail)}\n{consequence}",
        fingerprint=detail,
    )
    return searches


# --------------------------------------------------------------- scheduling

def run_times(now):
    """When the previous run and this run started, and when cron-job.org last fired.

    Read from GitHub's own run history, so nothing has to be saved between runs.
    """
    unknown = (now - 60, now, None)
    if not GITHUB_REPOSITORY:
        return unknown
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        response = http(
            "GET", f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/actions/runs",
            attempts=2, headers=headers, params={"per_page": 40},
        )
        if response.status_code != 200:
            raise Transient(f"HTTP {response.status_code}")
        runs = response.json()["workflow_runs"]
        started = lambda run: parse_time(run.get("run_started_at") or run["created_at"])
        mine = next((run for run in runs if str(run["id"]) == GITHUB_RUN_ID), None)
        this = started(mine) if mine else now
        if mine:
            runs = [run for run in runs if run.get("path") == mine.get("path")]
        earlier = [
            started(run) for run in runs
            if run.get("conclusion") in ("success", "failure") and started(run) < this
        ]
        external = [started(run) for run in runs if run.get("event") == "repository_dispatch"]
        previous = max(earlier) if earlier else this - 86400
        return previous, this, (max(external) if external else None)
    except (Transient, ValueError, KeyError, TypeError) as error:
        print(f"Could not read the run history ({scrub(error)}); assuming a 1-minute gap.")
        return unknown


def check_period(count):
    """Seconds between checks of any one search that keeps a day inside the budget."""
    return count * 86400 / DAILY_CALL_BUDGET


def due_searches(searches, previous, this):
    """The searches whose turn came up between the previous run and this one.

    Every search gets a check time once per period, staggered so the load is
    even. A run takes the turns that have come up since the last run, which
    keeps the daily total inside the budget at any trigger speed.
    """
    count = len(searches)
    period = check_period(count)
    due = []
    for index, search in enumerate(searches):
        offset = period * index / count
        if (this - offset) // period > (previous - offset) // period:
            due.append(search)
    return due


# --------------------------------------------------------------------- main

def run(state, seen, first_run, now):
    telegram = Telegram()
    searches = load_searches(telegram, state)
    if not searches:
        print("No usable searches in searches.json.")
        return 1

    previous, this, last_external = run_times(now)

    if EVENT == "repository_dispatch":
        settle(telegram, state, "trigger", "\u2705 The cron-job.org trigger is working again.")
    elif EVENT == "schedule" and (last_external is None or this - last_external > TRIGGER_SILENCE_LIMIT):
        warn_once(
            telegram, state, "trigger",
            "\u26A0\uFE0F <b>The cron-job.org trigger has stopped reaching GitHub.</b>\n"
            "The usual cause is an expired GitHub access token in the cron-job.org job. "
            "Checks are continuing on a slower backup schedule until it is fixed.",
            fingerprint=pacific_date(now),
        )

    seeded = state.setdefault("seeded", {})
    current = {signature(search) for search in searches}
    for old in [sig for sig in seeded if sig not in current]:
        del seeded[old]
    for key in [k for k in state.get("notices", {}) if k.startswith("search:") and k[7:] not in current]:
        del state["notices"][key]

    if first_run or EVENT == "workflow_dispatch" or not GITHUB_REPOSITORY:
        due = list(searches)
    else:
        due = due_searches(searches, previous, this)
        due += [s for s in searches if signature(s) not in seeded and s not in due]
    # Established searches first, so a search being added can't swallow their alerts.
    due.sort(key=lambda s: signature(s) not in seeded)

    period = check_period(len(searches))
    if not due:
        print(f"Done. No search was due this run. Tracking {len(seen)} items.")
        return 0

    auth_problem = None
    try:
        token = get_ebay_token()
    except (EbayAuthError, Transient) as error:
        token = None
        auth_problem = str(error)

    fresh, queued, added, checked, misses = [], set(), [], 0, 0
    for search in due if token else []:
        label = str(search.get("label") or search["q"])
        sig = signature(search)
        try:
            items = search_ebay(token, search)
        except RateLimited:
            print("eBay's daily search limit is used up; stopping until it resets.")
            warn_once(
                telegram, state, "ebay_limit",
                "\u26A0\uFE0F <b>eBay's daily search limit is used up.</b>\n"
                "Checks are paused until it resets at midnight Pacific. "
                "Listings posted in the meantime will be sent then.",
                fingerprint=pacific_date(now),
            )
            break
        except EbayAuthError as error:
            auth_problem = str(error)
            break
        except SearchRejected as error:
            print(f"eBay rejected the search '{label}': {error}")
            warn_once(
                telegram, state, f"search:{sig}",
                f"\u26A0\uFE0F <b>eBay rejected the search \u201C{esc(label)}\u201D</b>: {esc(error)}\n"
                "Check its keywords and filter in searches.json. The other searches are unaffected.",
                fingerprint=str(error),
            )
            continue
        except Transient as error:
            print(f"Search '{label}' did not go through ({error}); it will be retried.")
            misses += 1
            if misses >= 2:
                print("eBay is not answering; leaving the rest for the next run.")
                break
            continue

        checked += 1
        misses = 0
        state.get("notices", {}).pop(f"search:{sig}", None)
        new_items = [
            item for item in items
            if item.get("itemId") and item["itemId"] not in seen and item["itemId"] not in queued
        ]
        if SKIP_SEALED_PRODUCTS and not search.get("sealed"):
            for item in [item for item in new_items if is_sealed_product(item)]:
                if sig in seeded:
                    print(f"  skipped sealed product: {category(item)} | {str(item.get('title'))[:70]}")
                seen.add(item["itemId"])
                new_items.remove(item)
        if sig not in seeded:
            # A search being watched for the first time: note what is already
            # listed without alerting, so only listings from now on are sent.
            seen.update(item["itemId"] for item in new_items)
            seeded[sig] = pacific_date(now)
            if not first_run:
                added.append((label, len(new_items)))
        else:
            queued.update(item["itemId"] for item in new_items)
            fresh += [(item, label) for item in new_items]
            for item in new_items:
                card = describe(item)
                print(
                    f"  new: {card['name']} | {card['detail']} | {card['price']}"
                    f"  <-  {item.get('title')}  [{label}; {category(item)}]"
                )

    if checked:
        if state.pop("auth_failures", 0) >= AUTH_FAILURES_BEFORE_WARNING:
            settle(telegram, state, "ebay_login", "\u2705 eBay login is working again.")
    elif auth_problem:
        state["auth_failures"] = state.get("auth_failures", 0) + 1
        print(f"Could not log in to eBay ({auth_problem}). Failed runs in a row: {state['auth_failures']}")
        if state["auth_failures"] >= AUTH_FAILURES_BEFORE_WARNING:
            warn_once(
                telegram, state, "ebay_login",
                f"\u26A0\uFE0F <b>The monitor can't log in to eBay</b> ({esc(auth_problem)}).\n"
                "No listings are being checked. If the Cert ID was rotated or has expired, "
                "update EBAY_CERT_ID in the repository's Actions secrets.",
            )

    delivered, telegram_down = deliver(telegram, fresh, seen)

    if telegram.moved_to:
        warn_once(
            telegram, state, "chat_moved",
            "\u26A0\uFE0F Telegram upgraded this group, which changed its chat ID. Alerts still "
            f"arrive, but update the TELEGRAM_CHAT_ID secret to <code>{telegram.moved_to}</code>.",
            fingerprint=str(telegram.moved_to),
        )

    if not telegram_down:
        try:
            for label, skipped in added:
                telegram.send(
                    f"\U0001F440 Now watching <b>{esc(label)}</b>. Skipped {skipped} listings "
                    "already on eBay; you'll be pinged when new ones appear.",
                    preview=False,
                )
            if first_run and checked:
                telegram.send("\u2705 eBay monitor is live. I'll ping you when new matching listings appear.")
        except TelegramDown as error:
            print(f"Telegram is not accepting messages: {error}")
            telegram_down = True
        except (TelegramBusy, MessageRejected):
            pass

    print(
        f"Done. Checked {checked} of {len(searches)} searches "
        f"(paced at one check per search every {period / 60:.1f} min). "
        f"Sent {delivered} of {len(fresh)} new listings. Tracking {len(seen)} items."
    )
    return 1 if telegram_down else 0


def main():
    # No seen.json (or an unreadable one) means starting fresh: everything
    # currently listed is recorded quietly instead of being sent as new.
    saved = load_json(SEEN_FILE, None)
    first_run = not isinstance(saved, list)
    seen = set() if first_run else set(saved)
    migrating = not STATE_FILE.exists() and not first_run
    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    if first_run:
        state["seeded"] = {}
    if migrating:
        # Upgrading from the first version: the searches already running have
        # nothing to catch up on, so they are not treated as newly added.
        today = pacific_date(time.time())
        existing = load_json(SEARCHES_FILE, [])
        if not isinstance(existing, list):
            existing = []
        state["seeded"] = {
            signature(s): today for s in existing if isinstance(s, dict) and s.get("q")
        }

    try:
        return run(state, seen, first_run, time.time())
    finally:
        save_if_changed(SEEN_FILE, json.dumps(sorted(seen), indent=0) + "\n")
        save_if_changed(STATE_FILE, json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    sys.exit(main())
