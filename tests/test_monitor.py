"""End-to-end tests for monitor.py.

The monitor talks over real HTTP to a small stand-in for eBay, Telegram and
GitHub that runs on this machine, with a fake clock so a whole day of runs
takes seconds. Nothing here touches the real services.

Run from the repository folder:  python -m unittest discover -s tests -b
"""

import json
import os
import re
import sys
import tempfile
import threading
import time as real_time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class Clock:
    """Stands in for the time module inside monitor.py."""

    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0)


class World:
    """Everything the stand-in services know."""

    def __init__(self, clock):
        self.clock = clock
        self.reset()

    def reset(self):
        self.listings = {}          # search keywords -> items, newest first
        self.search_calls = []      # (clock time, keywords, filter)
        self.search_script = []     # statuses to answer the next searches with
        self.token_script = []      # statuses to answer the next logins with
        self.rejected_keywords = set()
        self.sent = []              # Telegram messages accepted
        self.telegram_script = []   # (status, body) answers to use first
        self.telegram_limit = 20    # messages a minute, like a real group
        self.runs = []              # GitHub's run history, newest first
        self.runs_status = 200
        self.slow_search = 0        # real seconds to stall searches


def html_is_valid(text):
    """Telegram refuses HTML with stray <, > or & in it."""
    bare = re.sub(r'</?(b|i|code)>|<a href="[^"<>]*">|</a>', "", text)
    if "<" in bare or ">" in bare:
        return False
    return not re.search(r"&(?!(lt|gt|amp|quot|#\d+);)", bare)


class Handler(BaseHTTPRequestHandler):
    world = None

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        world, url = self.world, urlparse(self.path)
        query = parse_qs(url.query)
        if url.path.endswith("/actions/runs"):
            if world.runs_status != 200:
                return self.reply(world.runs_status, {"message": "nope"})
            return self.reply(200, {"workflow_runs": world.runs[:40]})
        if url.path == "/buy/browse/v1/item_summary/search":
            keywords = query["q"][0]
            world.search_calls.append((world.clock.now, keywords, query.get("filter", [""])[0]))
            if world.slow_search:
                real_time.sleep(world.slow_search)
            if world.search_script:
                status = world.search_script.pop(0)
                if status != 200:
                    return self.reply(status, {"errors": [{"message": f"scripted {status}"}]})
            if keywords in world.rejected_keywords:
                return self.reply(400, {"errors": [{"message": "The filter is invalid."}]})
            items = world.listings.get(keywords, [])[: int(query["limit"][0])]
            return self.reply(200, {"itemSummaries": items} if items else {"total": 0})
        self.reply(404, {})

    def do_POST(self):
        world = self.world
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/identity/v1/oauth2/token":
            status = world.token_script.pop(0) if world.token_script else 200
            if status != 200:
                return self.reply(status, {"error": "invalid_client", "error_description": "client authentication failed"})
            return self.reply(200, {"access_token": "token"})
        if self.path.endswith("/sendMessage"):
            message = json.loads(body)
            if world.telegram_script:
                status, answer = world.telegram_script.pop(0)
                if status == "accept-then-stall":
                    message["_at"] = world.clock.now
                    world.sent.append(message)
                    real_time.sleep(0.8)
                    return self.reply(200, {"ok": True})
                if status != 200:
                    return self.reply(status, answer)
            recent = [m for m in world.sent if m["_at"] > world.clock.now - 60]
            if len(recent) >= world.telegram_limit:
                wait = int(60 - (world.clock.now - recent[-world.telegram_limit]["_at"])) + 1
                return self.reply(429, {
                    "ok": False, "error_code": 429,
                    "description": f"Too Many Requests: retry after {wait}",
                    "parameters": {"retry_after": wait},
                })
            if message.get("parse_mode") == "HTML" and not html_is_valid(message["text"]):
                return self.reply(400, {"ok": False, "description": "Bad Request: can't parse entities"})
            message["_at"] = world.clock.now
            world.sent.append(message)
            return self.reply(200, {"ok": True})
        self.reply(404, {})


CLOCK = Clock()
WORLD = World(CLOCK)
Handler.world = WORLD
SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"

os.environ.update({
    "EBAY_APP_ID": "app", "EBAY_CERT_ID": "cert-secret", "TELEGRAM_TOKEN": "111:telegram-secret",
    "TELEGRAM_CHAT_ID": "-100", "EBAY_API_URL": BASE, "TELEGRAM_API_URL": BASE,
    "GITHUB_API_URL": BASE, "GITHUB_REPOSITORY": "someone/monitor", "GITHUB_TOKEN": "gh-secret",
})
os.environ.pop("TELEGRAM_ADMIN_CHAT_ID", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402

monitor.time = CLOCK
monitor.TIMEOUT = (2, 0.4)
EBAY_DAILY_LIMIT = 5000


def item(number, title="Union Arena card", price="150.00", **extra):
    listing = {
        "itemId": f"v1|{number}|0", "legacyItemId": str(number), "title": title,
        "price": {"value": price, "currency": "USD"}, "condition": "Ungraded",
        "itemWebUrl": f"https://www.ebay.com/itm/{number}?_skw=union+arena&hash=item{number}:g:x",
        "buyingOptions": ["FIXED_PRICE"],
    }
    listing.update(extra)
    return listing


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MonitorTest(unittest.TestCase):
    def setUp(self):
        WORLD.reset()
        self.folder = tempfile.TemporaryDirectory()
        self.old_folder = os.getcwd()
        os.chdir(self.folder.name)
        monitor.EVENT = "repository_dispatch"
        self.run_number = 0

    def tearDown(self):
        os.chdir(self.old_folder)
        self.folder.cleanup()

    # ------------------------------------------------------------- helpers

    def searches(self, *keywords, **extra):
        entries = [dict({"label": f"Label {k}", "q": k}, **extra) for k in keywords]
        Path("searches.json").write_text(json.dumps(entries))
        return entries

    def run_monitor(self, event="repository_dispatch", advance=120):
        """One workflow run, as GitHub would start it."""
        CLOCK.now += advance
        self.run_number += 1
        entry = {
            "id": self.run_number, "event": event, "status": "in_progress", "conclusion": None,
            "created_at": iso(CLOCK.now), "run_started_at": iso(CLOCK.now),
            "path": ".github/workflows/monitor.yml",
        }
        WORLD.runs.insert(0, entry)
        monitor.EVENT = event
        monitor.GITHUB_RUN_ID = str(self.run_number)
        code = monitor.main()
        entry.update(status="completed", conclusion="success" if code == 0 else "failure")
        return code

    def established(self, *keywords, **extra):
        """A monitor that has already been running with these searches."""
        self.searches(*keywords, **extra)
        Path("seen.json").write_text("[]")
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
        WORLD.sent.clear()
        WORLD.search_calls.clear()

    def texts(self):
        return [m["text"] for m in WORLD.sent]

    def seen(self):
        return set(json.loads(Path("seen.json").read_text()))

    # --------------------------------------------------------------- tests

    def test_first_ever_run_records_listings_without_alerting(self):
        self.searches("alpha")
        WORLD.listings["alpha"] = [item(1), item(2)]
        self.assertEqual(self.run_monitor(), 0)
        self.assertEqual(len(WORLD.sent), 1)
        self.assertIn("eBay monitor is live", WORLD.sent[0]["text"])
        self.assertEqual(self.seen(), {"v1|1|0", "v1|2|0"})

    def test_deleting_seen_file_starts_over_quietly(self):
        self.established("alpha")
        WORLD.listings["alpha"] = [item(n) for n in range(60, 90)]
        Path("seen.json").unlink()
        self.assertEqual(self.run_monitor(), 0)
        self.assertEqual(len(WORLD.sent), 1)
        self.assertIn("eBay monitor is live", WORLD.sent[0]["text"])
        self.assertEqual(len(self.seen()), 30)

    def test_damaged_seen_file_starts_over_quietly_instead_of_flooding(self):
        self.established("alpha")
        WORLD.listings["alpha"] = [item(n) for n in range(60, 90)]
        Path("seen.json").write_text("<<<<<<< not json")
        self.assertEqual(self.run_monitor(), 0)
        self.assertEqual([t for t in self.texts() if "New listing" in t or "more new" in t], [])
        self.assertEqual(len(self.seen()), 30)

    def test_upgrade_from_first_version_does_not_reseed_or_spam(self):
        self.searches("alpha")
        Path("seen.json").write_text(json.dumps(["v1|1|0"]))    # old single-line format, no state.json
        WORLD.listings["alpha"] = [item(2), item(1)]
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
        self.assertEqual(len(WORLD.sent), 1)
        self.assertIn("New listing", WORLD.sent[0]["text"])
        self.assertIn("/itm/2", WORLD.sent[0]["text"])
        self.assertTrue(Path("state.json").exists())

    def test_new_listing_alerts_once(self):
        self.established("alpha")
        WORLD.listings["alpha"] = [item(7, title="Pochita R* Foil")]
        self.run_monitor(event="workflow_dispatch")
        self.run_monitor(event="workflow_dispatch")
        self.assertEqual(len(WORLD.sent), 1)
        text = WORLD.sent[0]["text"]
        self.assertIn("<b>Pochita R* Foil</b>", text)
        self.assertIn("150.00 USD", text)
        self.assertIn("Label alpha", text)

    def test_special_characters_in_label_and_title_still_deliver(self):
        # A "<" in a label used to make Telegram refuse the whole message.
        self.searches("alpha")
        entries = json.loads(Path("searches.json").read_text())
        entries[0]["label"] = "4. UA Booster Box Case (< $1000)"
        Path("searches.json").write_text(json.dumps(entries))
        Path("seen.json").write_text("[]")
        self.run_monitor(event="workflow_dispatch")
        WORLD.sent.clear()
        WORLD.listings["alpha"] = [item(9, title="Tom & Jerry <rare> 3>2")]
        self.run_monitor(event="workflow_dispatch")
        self.assertEqual(len(WORLD.sent), 1)
        self.assertEqual(WORLD.sent[0].get("parse_mode"), "HTML")
        self.assertIn("(&lt; $1000)", WORLD.sent[0]["text"])
        self.assertIn("Tom &amp; Jerry &lt;rare&gt;", WORLD.sent[0]["text"])

    def test_big_batch_is_summarised_and_nothing_is_lost(self):
        self.established("alpha", "beta")
        WORLD.listings["alpha"] = [item(n) for n in range(100, 150)]
        WORLD.listings["beta"] = [item(n) for n in range(200, 246)]
        self.run_monitor(event="workflow_dispatch")
        full = [t for t in self.texts() if "New listing" in t]
        summaries = [t for t in self.texts() if "more new listing" in t]
        self.assertEqual(len(full), 10)
        self.assertEqual(len(summaries), 9)                      # 86 remaining, 10 per message
        self.assertLessEqual(len(WORLD.sent), 20)                # inside Telegram's per-minute limit
        linked = set(re.findall(r"/itm/(\d+)", "\n".join(self.texts())))
        self.assertEqual(len(linked), 96)
        self.assertEqual(len(self.seen()), 96)

    def test_telegram_rate_limit_waits_then_carries_the_rest_to_the_next_run(self):
        monitor.FULL_ALERTS_PER_RUN, monitor.SUMMARY_SIZE = 10, 1   # force many messages
        try:
            self.established("alpha")
            WORLD.listings["alpha"] = [item(n) for n in range(300, 350)]
            for _ in range(6):
                self.run_monitor(event="workflow_dispatch")
        finally:
            monitor.FULL_ALERTS_PER_RUN, monitor.SUMMARY_SIZE = 10, 10
        linked = re.findall(r"/itm/(\d+)", "\n".join(self.texts()))
        self.assertEqual(len(set(linked)), 50, "every listing must arrive")
        self.assertEqual(len(linked), 50, "and only once")

    def test_search_timeout_is_retried_and_never_crashes(self):
        self.established("alpha", "beta")
        WORLD.listings["beta"] = [item(5)]
        WORLD.slow_search = 0.8                 # longer than the timeout
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
        self.assertEqual(WORLD.sent, [])
        WORLD.slow_search = 0
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
        self.assertEqual(len(WORLD.sent), 1)

    def test_server_error_on_a_search_is_retried_within_the_run(self):
        self.established("alpha")
        WORLD.listings["alpha"] = [item(5)]
        WORLD.search_script = [503]
        self.run_monitor(event="workflow_dispatch")
        self.assertEqual(len(WORLD.sent), 1)

    def test_ebay_daily_limit_stops_the_run_and_warns_once(self):
        self.established("alpha", "beta", "gamma")
        WORLD.search_script = [429]
        self.run_monitor(event="workflow_dispatch")
        self.assertEqual(len(WORLD.search_calls), 1, "no point hammering eBay after a 429")
        self.assertEqual(len(WORLD.sent), 1)
        self.assertIn("daily search limit", WORLD.sent[0]["text"])
        WORLD.search_script = [429]
        self.run_monitor(event="workflow_dispatch")
        self.assertEqual(len(WORLD.sent), 1, "warned once, not every run")

    def test_login_blip_is_retried_silently(self):
        self.established("alpha")
        WORLD.listings["alpha"] = [item(5)]
        WORLD.token_script = [401, 503]
        self.run_monitor(event="workflow_dispatch")
        self.assertEqual(len(WORLD.sent), 1)
        self.assertIn("New listing", WORLD.sent[0]["text"])

    def test_lasting_login_failure_warns_after_three_runs_then_reports_recovery(self):
        self.established("alpha")
        WORLD.token_script = [401] * 9
        for expected in (0, 0, 1):
            self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
            self.assertEqual(len(WORLD.sent), expected)
        self.assertIn("can't log in to eBay", WORLD.sent[0]["text"])
        self.assertNotIn("cert-secret", Path("state.json").read_text())
        self.run_monitor(event="workflow_dispatch")
        self.assertIn("login is working again", WORLD.sent[-1]["text"])

    def test_one_bad_search_warns_once_and_the_others_keep_working(self):
        self.established("alpha", "beta")
        WORLD.rejected_keywords = {"alpha"}
        WORLD.listings["beta"] = [item(5)]
        self.run_monitor(event="workflow_dispatch")
        self.run_monitor(event="workflow_dispatch")
        warnings = [t for t in self.texts() if "eBay rejected the search" in t]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(len([t for t in self.texts() if "New listing" in t]), 1)

    def test_broken_searches_file_warns_and_keeps_watching_the_last_good_list(self):
        self.established("alpha")
        Path("searches.json").write_text('[{"label": "x", "q": "alpha"},]')   # trailing comma
        WORLD.listings["alpha"] = [item(5)]
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
        self.assertEqual(len([t for t in self.texts() if "searches.json has a mistake" in t]), 1)
        self.assertEqual(len([t for t in self.texts() if "New listing" in t]), 1)
        self.searches("alpha")
        self.run_monitor(event="workflow_dispatch")
        self.assertIn("valid again", WORLD.sent[-1]["text"])

    def test_added_search_starts_quietly_with_one_confirmation(self):
        self.established("alpha")
        self.searches("alpha", "beta")
        WORLD.listings["beta"] = [item(n) for n in range(10, 40)]
        WORLD.listings["alpha"] = [item(99)]
        self.run_monitor()
        confirmations = [t for t in self.texts() if "Now watching" in t]
        self.assertEqual(len(confirmations), 1)
        self.assertIn("Skipped 30 listings", confirmations[0])
        self.assertEqual(len([t for t in self.texts() if "New listing" in t]), 1)   # alpha's real find
        WORLD.sent.clear()
        WORLD.listings["beta"].insert(0, item(41))
        self.run_monitor(event="workflow_dispatch")
        self.assertEqual(len(WORLD.sent), 1)
        self.assertIn("/itm/41", WORLD.sent[0]["text"])

    def test_renaming_a_label_does_not_restart_a_search(self):
        self.established("alpha")
        entries = json.loads(Path("searches.json").read_text())
        entries[0]["label"] = "Renamed"
        Path("searches.json").write_text(json.dumps(entries))
        WORLD.listings["alpha"] = [item(5)]
        self.run_monitor(event="workflow_dispatch")
        self.assertEqual(len(WORLD.sent), 1)
        self.assertIn("Renamed", WORLD.sent[0]["text"])

    def test_message_that_times_out_is_sent_again_rather_than_lost(self):
        self.established("alpha")
        WORLD.listings["alpha"] = [item(5), item(6)]
        WORLD.telegram_script = [("accept-then-stall", None)]
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
        linked = re.findall(r"/itm/(\d+)", "\n".join(self.texts()))
        self.assertEqual(set(linked), {"5", "6"})
        self.assertEqual(self.seen(), {"v1|5|0", "v1|6|0"})

    def test_telegram_refusing_the_bot_fails_the_run_and_keeps_listings_for_later(self):
        self.established("alpha")
        WORLD.listings["alpha"] = [item(5)]
        WORLD.telegram_script = [(401, {"ok": False, "description": "Unauthorized"})]
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 1)
        self.assertEqual(self.seen(), set())
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
        self.assertEqual(len(WORLD.sent), 1)

    def test_group_upgraded_by_telegram_is_followed(self):
        self.established("alpha")
        WORLD.listings["alpha"] = [item(5)]
        WORLD.telegram_script = [(400, {
            "ok": False, "description": "Bad Request: group chat was upgraded to a supergroup chat",
            "parameters": {"migrate_to_chat_id": -100777},
        })]
        self.assertEqual(self.run_monitor(event="workflow_dispatch"), 0)
        self.assertEqual(WORLD.sent[0]["chat_id"], -100777)
        self.assertIn("New listing", WORLD.sent[0]["text"])
        self.assertIn("TELEGRAM_CHAT_ID", WORLD.sent[1]["text"])

    def test_auction_listings_can_be_switched_on_per_search(self):
        self.established("alpha", filter="price:[100..],priceCurrency:USD")
        self.run_monitor(event="workflow_dispatch")
        self.assertEqual(WORLD.search_calls[-1][2], "price:[100..],priceCurrency:USD")
        self.searches("alpha", filter="price:[100..],priceCurrency:USD", auctions=True)
        self.run_monitor()
        self.assertEqual(
            WORLD.search_calls[-1][2],
            "price:[100..],priceCurrency:USD,buyingOptions:{FIXED_PRICE|AUCTION}",
        )
        WORLD.sent.clear()
        WORLD.listings["alpha"] = [item(
            8, buyingOptions=["AUCTION"], currentBidPrice={"value": "120.00", "currency": "USD"},
        )]
        self.run_monitor(event="workflow_dispatch")
        self.assertIn("120.00 USD (current bid)", WORLD.sent[0]["text"])
        self.assertIn("Auction", WORLD.sent[0]["text"])

    def test_backup_schedule_warns_when_the_main_trigger_goes_quiet(self):
        self.established("alpha")
        self.run_monitor()                                   # cron-job.org fires normally
        self.run_monitor(event="schedule", advance=600)
        self.assertEqual(WORLD.sent, [])
        self.run_monitor(event="schedule", advance=2400)     # 50 minutes of silence
        self.run_monitor(event="schedule", advance=900)
        warnings = [t for t in self.texts() if "cron-job.org trigger has stopped" in t]
        self.assertEqual(len(warnings), 1)
        self.run_monitor()
        self.assertIn("trigger is working again", WORLD.sent[-1]["text"])

    def test_checks_continue_if_github_history_is_unavailable(self):
        self.established("alpha", "beta", "gamma")
        WORLD.runs_status = 500
        calls_before = len(WORLD.search_calls)
        for _ in range(20):
            self.assertEqual(self.run_monitor(), 0)
        self.assertGreater(len(WORLD.search_calls), calls_before)

    # ------------------------------------------------- staying inside quota

    def day_of_runs(self, keywords, every_seconds):
        self.established(*keywords)
        start = CLOCK.now
        while CLOCK.now - start < 86400:
            self.assertEqual(self.run_monitor(advance=every_seconds), 0)
        return [call for call in WORLD.search_calls if call[0] > start]

    def test_ten_searches_every_two_minutes_stay_inside_the_daily_limit(self):
        keywords = [f"search {n}" for n in range(10)]
        budget = monitor.DAILY_CALL_BUDGET
        calls = self.day_of_runs(keywords, 120)
        self.assertLess(len(calls), EBAY_DAILY_LIMIT)
        self.assertLessEqual(len(calls), budget + 10)
        self.assertGreater(len(calls), budget - 200, "should use the allowance, not waste it")
        for keyword in keywords:                              # every search checked all day, evenly
            times = [at for at, q, _ in calls if q == keyword]
            gaps = [b - a for a, b in zip(times, times[1:])]
            self.assertLessEqual(max(gaps), 360)
            self.assertAlmostEqual(len(times), budget / 10, delta=5)

    def test_few_searches_are_all_checked_every_run(self):
        calls = self.day_of_runs(["a", "b", "c", "d"], 120)
        self.assertEqual(len(calls), 4 * 720)

    def test_fast_trigger_with_many_searches_still_stays_inside_the_limit(self):
        calls = self.day_of_runs([f"search {n}" for n in range(25)], 60)
        self.assertLess(len(calls), EBAY_DAILY_LIMIT)
        self.assertGreater(len(calls), monitor.DAILY_CALL_BUDGET - 200)

    def test_manual_run_checks_everything(self):
        self.established(*[f"search {n}" for n in range(10)])
        self.run_monitor(event="workflow_dispatch", advance=5)
        self.assertEqual(len(WORLD.search_calls), 10)


if __name__ == "__main__":
    unittest.main()
