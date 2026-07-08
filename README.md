# bookmark-eXport

Back up your **X (Twitter) bookmarks** to a clean JSONL file — fully enriched with the complete
tweet text, the author's whole thread, quoted tweets, media, links, and even the **full body of X
Articles**. No paid API, no third-party scraping service, no LLM. It runs entirely on your own
machine against your own logged-in session, and costs $0.

X gives you no export for bookmarks and no real API access to them. This owns your data back.

```
python scrape.py                 # → ./bookmarks.jsonl
python scrape.py --scrolls 60    # scroll deeper to reach older bookmarks
```

Each line of `bookmarks.jsonl` is one bookmark:

```jsonc
{
  "statusId": "1780000000000000000",
  "handle": "some_author",
  "displayName": "Some Author",
  "timestamp": "Wed Apr 17 12:34:56 +0000 2024",
  "url": "https://x.com/some_author/status/1780000000000000000",
  "text": "the FULL tweet text, not the truncated version…",
  "text_was_truncated": true,
  "engagement": { "reply": 12, "retweet": 40, "like": 830, "quote": 3, "bookmark": 210, "views": 95000 },
  "quoted":  { "handle": "another", "text": "the quoted tweet, inline" },
  "urls":    ["https://example.com/the-real-expanded-link"],
  "media":   [{ "type": "photo", "url": "https://pbs.twimg.com/media/…" }],
  "article": { "title": "…", "preview": "…", "body": "the ENTIRE article body…" },
  "thread":  [{ "id": "…", "url": "…", "text": "reply 1 of the author's thread" }, …],
  "enriched": true
}
```

## How it works (and why it's a real browser)

X hides its bookmarks behind an internal GraphQL API that is gated by an anti-bot header,
`x-client-transaction-id`. That token can only be produced by running X's own JavaScript — so plain
HTTP scrapers (`twikit`, `agent-twitter-client`, raw `curl`) get rejected with *"Couldn't get
KEY_BYTE indices."*

So instead of faking the request, this tool **lets X make the request for you**. It launches a
headless Chromium (via [Playwright](https://playwright.dev/python/)) with your session cookies,
opens `x.com/i/bookmarks`, and simply **reads the GraphQL responses the page fetches anyway**. The
anti-bot token is valid because X's own page generated it. We just listen.

Two passes:

1. **List pass** — scroll the bookmarks timeline, collecting every tweet the page loads.
2. **Enrich pass** — for each *new* bookmark, open its permalink once to pull the parts the timeline
   omits: the author's full self-reply **thread**, and the full **article body** (decoded from X's
   Draft.js `content_state`). Enriched records are marked `"enriched": true`.

Runs are **append + dedup**, keyed by tweet id. Run it as often as you like — it only does the slow
enrich work on bookmarks it hasn't seen before, and just refreshes engagement counts on the rest.

## Setup

Requires Python 3.9+.

```bash
git clone https://github.com/pauljump/bookmark-eXport
cd bookmark-eXport

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## Authentication — two cookies

The tool authenticates as *you*, using two cookies from your logged-in X session: `auth_token` and
`ct0`. Grab them once:

1. Open **x.com** in your browser, logged in.
2. Open DevTools (`F12` or `Cmd+Opt+I`) → **Application** tab (Chrome) or **Storage** tab (Firefox).
3. **Cookies** → `https://x.com`.
4. Copy the **Value** of the `auth_token` cookie and the **Value** of the `ct0` cookie.

Then either export them:

```bash
export X_AUTH_TOKEN="paste_auth_token_here"
export X_CT0="paste_ct0_here"
python scrape.py
```

…or copy `.env.example` to `.env`, paste them in, and load it:

```bash
cp .env.example .env      # edit .env with your values
set -a && source .env && set +a
python scrape.py
```

`.env` and `bookmarks.jsonl` are gitignored — **your cookies and your bookmarks never get
committed.** These cookies are your live session; treat them like a password and don't share them.

### When it stops working

If a run reports `captured 0`, your cookies have almost certainly **expired** — X rotates
`auth_token` whenever you log out and back in. Just re-grab the two cookie values and run again.
This is the one recurring gotcha of the cookie approach.

## Options

| flag / env | default | what it does |
|---|---|---|
| `--out PATH` | `bookmarks.jsonl` | where to write the JSONL |
| `--scrolls N` | `10` | how many times to scroll the bookmarks page. Higher reaches older bookmarks — use `--scrolls 60` for a one-time deep backfill |
| `BM_ENRICH_LIMIT` | `400` | max per-bookmark detail fetches per run (bounds a huge first run) |

## Notes & etiquette

- These are **your own bookmarks**, pulled through **your own session** — the same data X shows you
  in the app. Use it to back up and own your reading. Don't hammer it; the defaults are gentle on
  purpose.
- Nothing is sent anywhere except x.com. There is no server, no telemetry, no API key.
- Unofficial: this relies on X's internal GraphQL, which X can change at any time. If a field moves,
  the extraction helpers in `scrape.py` are small and easy to patch.

## License

MIT — see [LICENSE](LICENSE).
