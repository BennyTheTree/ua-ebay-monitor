# ua-ebay-monitor

Watches eBay for new Union Arena listings and sends them to a Telegram chat.

cron-job.org pings GitHub every few minutes. Each ping starts a short run that
checks the searches in `searches.json` and sends anything it has not seen
before. Nothing runs on your own computer.

## Changing what it watches

Edit `searches.json` on GitHub and commit. The next run picks it up.

```json
{
  "label": "Bleach UA ($100+)",
  "q": "Bleach Union Arena",
  "filter": "price:[100..],priceCurrency:USD"
}
```

| Field | Needed | What it does |
| --- | --- | --- |
| `q` | yes | Keywords, as you would type them in eBay's search box. Every word must match. |
| `label` | no | The name shown on the alert. |
| `filter` | no | Price limits. `price:[100..]` is $100 and up, `price:[..1000]` is up to $1,000, `price:[50..200]` is between. Always follow it with `,priceCurrency:USD`. |
| `auctions` | no | `true` adds auction listings for this search. Without it only Buy It Now listings are returned. |
| `sealed` | no | `true` lets this search alert on sealed product. See below. |

When you add a search or change its keywords or filter, the monitor replies
"Now watching ..." within a few minutes and skips what is already listed, so
you only hear about listings from that point on. Renaming a label changes
nothing else.

If the file has a typo, the monitor says so in Telegram and keeps using the
last version that worked.

Sealed product (booster boxes, cases, packs, decks) is skipped in every
search. The monitor goes by the eBay category the seller chose, so a box that
a seller filed as a single card can still come through.

## How often each search is checked

eBay allows 5,000 searches a day. The monitor spreads 4,800 of them evenly over
24 hours, so the pace depends on how many searches there are:

| Searches | Each one is checked about every |
| --- | --- |
| 6 or fewer | 2 minutes (every run) |
| 8 | 2.4 minutes |
| 9 | 2.7 minutes |
| 10 | 3 minutes |
| 15 | 4.5 minutes |

Triggering more often than this on cron-job.org does not make checks faster.
Fewer searches does.

## Messages you may see

- **New listing**: one message per listing, with a link preview.
- **N more new listings**: when a run finds more than 12, the first 10 get
  their own message and the rest are grouped, ten per message. Telegram only
  accepts about 20 messages a minute, so this keeps anything from being lost.
- **Now watching ...**: a search was added or changed.
- **Warnings (⚠️)**: something needs attention, such as a typo in
  `searches.json`, eBay refusing the login, or the cron-job.org trigger going
  quiet. Each is sent once, with a follow-up when it clears.

Warnings go to the same chat as alerts. To send them somewhere else, add a
repository secret named `TELEGRAM_ADMIN_CHAT_ID` with that chat's ID.

## Checking on it

Open the **Actions** tab. Each run ends with a line like:

```
Done. Checked 7 of 10 searches (paced at one check per search every 3.0 min). Sent 2 of 2 new listings. Tracking 11920 items.
```

Runs marked "cancelled" are normal: a run that arrived while another was still
working was replaced by a newer one.

The **Run workflow** button checks every search immediately.

## Starting over

Deleting `seen.json` makes the next run record everything currently listed
without alerting, then carry on from there.

## Files

- `monitor.py`: the monitor. Settings are at the top of the file.
- `searches.json`: your searches.
- `seen.json`, `state.json`: the monitor's memory, saved by each run. No need to edit these.
- `.github/workflows/monitor.yml`: when and how GitHub runs it.
- `tests/`: checks for the script. Run with `python -m unittest discover -s tests -b`.

## Secrets it needs

`EBAY_APP_ID`, `EBAY_CERT_ID`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, set under
Settings → Secrets and variables → Actions.

cron-job.org uses a GitHub access token to send its ping. When that token
expires the pings stop. A slower backup schedule built into the workflow keeps
checks going and the monitor sends a warning, but regenerate the token and
paste it into the cron-job.org job to get back to full speed.
