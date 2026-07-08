#!/usr/bin/env python3
"""bookmark-eXport — back up your X (Twitter) bookmarks to a JSONL file, fully enriched.

Runs a real headless browser (Playwright + Chromium) logged into *your* X session with
your own cookies, opens x.com/i/bookmarks, and intercepts the GraphQL responses the page
already fetches. No paid X API, no scraping-service, no LLM in the loop. $0.

Why a real browser and not a plain HTTP request? X gates its GraphQL behind an anti-bot
header, `x-client-transaction-id`, that only its own JavaScript can generate. HTTP-only
libraries (twikit, agent-twitter-client) fail with "Couldn't get KEY_BYTE indices". A real
browser runs that JS for free, so the token is valid and we just read the responses.

What "enriched" means — each bookmark is captured with:
  - the FULL tweet text (long "note tweets" beat the truncated legacy field)
  - quoted tweet, media, and expanded links inline
  - the author's FULL thread (their self-reply chain in the conversation)
  - the FULL article body, for X Articles (decoded from the Draft.js content_state)
The thread + article body come from a one-time per-bookmark detail fetch. Once a bookmark is
detail-fetched its record is marked `enriched:true` and re-runs skip it (append + dedup), so
running this repeatedly is cheap and only does real work on new bookmarks.

Auth: set X_AUTH_TOKEN and X_CT0 (your `auth_token` and `ct0` cookies). See the README for
how to grab them. This script never stores or transmits them anywhere but x.com.

Usage:
  python scrape.py                    # scrape bookmarks → ./bookmarks.jsonl
  python scrape.py --scrolls 60       # scroll deeper to reach older bookmarks (backfill)
  python scrape.py --out mybm.jsonl   # write somewhere else
"""
import os, sys, json, argparse, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

AUTH = os.environ.get("X_AUTH_TOKEN")
CT0 = os.environ.get("X_CT0")
ENRICH_LIMIT = int(os.environ.get("BM_ENRICH_LIMIT", "400"))   # max detail fetches per run


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def cookies_for(domain):
    return [
        {"name": "auth_token", "value": AUTH, "domain": domain, "path": "/", "httpOnly": True, "secure": True},
        {"name": "ct0", "value": CT0, "domain": domain, "path": "/", "secure": True},
    ]


# ── JSON walking / extraction ────────────────────────────────────────────────
def find_tweets(obj):
    """Recursively pull every Tweet node out of an arbitrary GraphQL response."""
    out = []

    def rec(o):
        if isinstance(o, dict):
            t = o
            if t.get("__typename") == "TweetWithVisibilityResults" and t.get("tweet"):
                t = t["tweet"]
            if t.get("__typename") == "Tweet" and t.get("legacy"):
                out.append(t)
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)

    rec(obj)
    return out


def screen_name(tw):
    u = (((tw.get("core") or {}).get("user_results") or {}).get("result") or {})
    return (u.get("core") or {}).get("screen_name") or (u.get("legacy") or {}).get("screen_name") or ""


def display_name(tw):
    u = (((tw.get("core") or {}).get("user_results") or {}).get("result") or {})
    return (u.get("core") or {}).get("name") or (u.get("legacy") or {}).get("name")


def best_text(tw):
    """Full text — note_tweet (long tweets) beats truncated legacy.full_text."""
    nt = ((tw.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result")
    if nt and nt.get("text"):
        return nt["text"]
    return (tw.get("legacy") or {}).get("full_text", "")


def extract_quoted(tw):
    q = (tw.get("quoted_status_result") or {}).get("result")
    if not q:
        return None
    if q.get("__typename") == "TweetWithVisibilityResults":
        q = q.get("tweet", q)
    if not q.get("legacy"):
        return None
    return {"handle": screen_name(q), "text": best_text(q)}


def extract_urls(tw):
    urls = ((tw.get("legacy") or {}).get("entities") or {}).get("urls") or []
    return [u["expanded_url"] for u in urls if u.get("expanded_url")]


def extract_media(tw):
    leg = tw.get("legacy") or {}
    media = ((leg.get("extended_entities") or {}).get("media")) \
        or ((leg.get("entities") or {}).get("media")) or []
    return [{"type": m.get("type"), "url": m.get("media_url_https")}
            for m in media if m.get("media_url_https")]


def article_meta(tw):
    ar = ((tw.get("article") or {}).get("article_results") or {}).get("result")
    if not ar:
        return None
    return {"title": (ar.get("title") or "").strip(), "preview": ar.get("preview_text"), "body": None}


def article_body_from(tw):
    """Full article body from Draft.js content_state (present in detail responses)."""
    ar = ((tw.get("article") or {}).get("article_results") or {}).get("result") or {}
    cs = ar.get("content_state")
    if isinstance(cs, str):
        try:
            cs = json.loads(cs)
        except Exception:
            cs = None
    if not isinstance(cs, dict):
        return None
    blocks = cs.get("blocks") or []
    body = "\n".join(b.get("text", "") for b in blocks).strip()
    return body or None


def base_record(tw):
    sid = tw.get("rest_id")
    leg = tw.get("legacy") or {}
    handle = screen_name(tw)
    full = best_text(tw)
    return {
        "statusId": sid, "handle": handle, "displayName": display_name(tw),
        "timestamp": leg.get("created_at"),
        "url": f"https://x.com/{handle}/status/{sid}",
        "text": full,
        "text_was_truncated": leg.get("full_text", "") != full and bool(leg.get("full_text")),
        "engagement": {
            "reply": leg.get("reply_count"), "retweet": leg.get("retweet_count"),
            "like": leg.get("favorite_count"), "quote": leg.get("quote_count"),
            "bookmark": leg.get("bookmark_count"),
            "views": int((tw.get("views") or {}).get("count"))
                     if str((tw.get("views") or {}).get("count", "")).isdigit() else None,
        },
        "quoted": extract_quoted(tw), "urls": extract_urls(tw), "media": extract_media(tw),
        "article": article_meta(tw), "thread": None, "enriched": False,
    }


# ── browser harness ──────────────────────────────────────────────────────────
class Session:
    def __init__(self):
        self._p = sync_playwright().start()
        self.b = self._p.chromium.launch(headless=True)
        ctx = self.b.new_context()
        ctx.add_cookies(cookies_for(".x.com") + cookies_for(".twitter.com"))
        self.page = ctx.new_page()

    def grab(self, url, op_filter, scrolls=0, wait=5000):
        """Open a URL, intercept matching GraphQL responses, scroll to lazy-load more."""
        cap = []

        def on_resp(r):
            if "/graphql/" in r.url and (not op_filter or any(f in r.url for f in op_filter)):
                try:
                    cap.append(r.json())
                except Exception:
                    pass

        self.page.on("response", on_resp)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self.page.wait_for_timeout(wait)
            for _ in range(scrolls):
                self.page.mouse.wheel(0, 3200)
                self.page.wait_for_timeout(2200)
        except Exception as e:
            print("  nav note:", repr(e))
        self.page.remove_listener("response", on_resp)
        return cap

    def close(self):
        try:
            self.b.close()
        finally:
            self._p.stop()


def enrich(sess, rec):
    """Detail-fetch one bookmark: fill in the full thread + full article body."""
    payloads = sess.grab(rec["url"], ["TweetDetail", "TweetResultByRestId"], wait=4000)
    author = rec["handle"]
    root = rec["statusId"]
    # full article body — look at the root tweet's article content_state
    for j in payloads:
        for t in find_tweets(j):
            if t.get("rest_id") == root:
                if rec.get("article"):
                    body = article_body_from(t)
                    if body:
                        rec["article"]["body"] = body
                if len(best_text(t)) > len(rec["text"]):
                    rec["text"] = best_text(t)
    # full thread = author's tweets in this conversation, chronological
    chain = {}
    for j in payloads:
        for t in find_tweets(j):
            if screen_name(t) == author:
                chain[t["rest_id"]] = best_text(t)
    if len(chain) > 1:
        ordered = sorted(chain.items(), key=lambda kv: int(kv[0]))
        rec["thread"] = [{"id": i, "url": f"https://x.com/{author}/status/{i}", "text": txt}
                         for i, txt in ordered]
    rec["enriched"] = True
    return rec


# ── persistence (append + dedup, keyed by tweet id) ───────────────────────────
def load_existing(path):
    by_id = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                d = json.loads(line)
                by_id[d["statusId"]] = d
            except Exception:
                pass
    return by_id


def write_all(path, by_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for d in by_id.values():
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def scrape_bookmarks(out_path, scrolls=10):
    existing = load_existing(out_path)
    sess = Session()
    payloads = sess.grab("https://x.com/i/bookmarks", ["Bookmarks"], scrolls=scrolls, wait=6000)
    seen = {}
    for j in payloads:
        for t in find_tweets(j):
            if t.get("rest_id"):
                seen[t["rest_id"]] = t
    if not seen:
        sess.close()
        print("[bookmarks] captured 0 — this almost always means your cookies expired. "
              "Re-grab X_AUTH_TOKEN / X_CT0 (see the README) and try again.")
        return
    # merge: keep already-enriched records (just refresh their engagement), enrich the rest
    new_ids = []
    to_enrich = []
    for sid, tw in seen.items():
        if sid in existing and existing[sid].get("enriched"):
            rec = existing[sid]
            rec["engagement"] = base_record(tw)["engagement"]   # cheap refresh, keep thread/article
        else:
            rec = base_record(tw)
            new_ids.append(sid)
            to_enrich.append(rec)                               # every new bookmark gets one detail fetch
        existing[sid] = rec
    # enrich (bounded so a huge first run can't spin forever)
    n_thread = n_article = 0
    for rec in to_enrich[:ENRICH_LIMIT]:
        try:
            enrich(sess, rec)
            if rec.get("thread"):
                n_thread += 1
            if rec.get("article") and rec["article"].get("body"):
                n_article += 1
        except Exception as e:
            rec["enriched"] = True
            print("  enrich note", rec["statusId"], repr(e))
    sess.close()
    write_all(out_path, existing)
    print(f"[bookmarks] captured {len(seen)} | {len(new_ids)} new | "
          f"enriched {min(len(to_enrich), ENRICH_LIMIT)} "
          f"(threads:{n_thread}, article-bodies:{n_article}) | file total {len(existing)} → {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Back up your X bookmarks to enriched JSONL.")
    ap.add_argument("--out", default="bookmarks.jsonl", help="output JSONL path (default: ./bookmarks.jsonl)")
    ap.add_argument("--scrolls", type=int, default=10,
                    help="how many times to scroll the bookmarks page (higher = older bookmarks; default 10)")
    args = ap.parse_args()
    if not (AUTH and CT0):
        sys.exit("Missing X_AUTH_TOKEN / X_CT0. Export them first — see the README "
                 "(x.com → DevTools → Application → Cookies → copy auth_token and ct0).")
    scrape_bookmarks(Path(args.out).expanduser(), scrolls=args.scrolls)
