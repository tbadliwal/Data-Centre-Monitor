#!/usr/bin/env python3
"""Rate-limited scheduled refresh for the Davis Data Center Monitor.

Designed for GitHub Actions + GDELT DOC 2.0.
- Queries NATIONAL + all 50 states sequentially.
- Enforces a conservative minimum interval between requests to avoid HTTP 429s.
- Honors Retry-After when present and uses exponential backoff.
- Preserves the last-good state feed when a request fails.
- Prints state-by-state progress in GitHub Actions logs.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "live.json"
API = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT is sensitive to bursty traffic. Keep this deliberately conservative.
MIN_REQUEST_INTERVAL = 6.5  # seconds between starts of API requests
REQUEST_TIMEOUT = 20
MAX_ATTEMPTS = 3
BACKOFF_BASE = 12  # seconds; grows 12, 24, 48 on retries
MAX_RECORDS = 75

STATES = [
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut","Delaware","Florida","Georgia",
    "Hawaii","Idaho","Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts",
    "Michigan","Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada","New Hampshire","New Jersey",
    "New Mexico","New York","North Carolina","North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
    "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont","Virginia","Washington","West Virginia",
    "Wisconsin","Wyoming"
]

SIGNALS = [
    ("Pause / Ban", re.compile(r"moratorium|pause|ban|freeze|halt", re.I)),
    ("Power", re.compile(r"power|electric|grid|utility|interconnection|transmission|ratepayer|tariff", re.I)),
    ("Water", re.compile(r"water|aquifer|groundwater|cooling", re.I)),
    ("Permitting", re.compile(r"permit|zoning|land use|setback|hearing", re.I)),
    ("Economics", re.compile(r"tax|incentive|cost allocation|infrastructure cost|community benefit", re.I)),
    ("Politics", re.compile(r"governor|mayor|attorney general|council|commission|senator|politic", re.I)),
    ("Litigation", re.compile(r"lawsuit|litigation|court", re.I)),
    ("Project", re.compile(r"withdraw|reject|approval|project", re.I)),
]
BAD_TITLE = re.compile(
    r"stock|shares|earnings|nasdaq|dow|market today|price target|investor|portfolio|cryptocurrency|bitcoin|chip stocks|nvidia",
    re.I,
)
BAD_DOMAIN = re.compile(
    r"prnewswire|globenewswire|businesswire|stock\.|marketscreener|benzinga|seekingalpha",
    re.I,
)

_last_request_started = 0.0


def load_old():
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {"meta": {}, "national": {"articles": []}, "states": {}}


def wait_for_rate_limit():
    global _last_request_started
    elapsed = time.monotonic() - _last_request_started
    if _last_request_started and elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_started = time.monotonic()


def http_json(params):
    url = API + "?" + urlencode(params)
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait_for_rate_limit()
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "DavisDataCenterMonitor/3.0 (public-policy research; GitHub Actions)",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
            with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                body = r.read().decode("utf-8", "replace").strip()
                if not body:
                    raise ValueError("empty response body")
                return json.loads(body)

        except HTTPError as e:
            last_error = e
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                try:
                    delay = max(float(retry_after), BACKOFF_BASE * (2 ** (attempt - 1))) if retry_after else BACKOFF_BASE * (2 ** (attempt - 1))
                except Exception:
                    delay = BACKOFF_BASE * (2 ** (attempt - 1))
                if attempt < MAX_ATTEMPTS:
                    print(f"    HTTP 429; backing off {delay:.0f}s before retry {attempt + 1}/{MAX_ATTEMPTS}", flush=True)
                    time.sleep(delay)
                    continue
            raise

        except (URLError, TimeoutError, json.JSONDecodeError, ValueError) as e:
            last_error = e
            if attempt < MAX_ATTEMPTS:
                delay = BACKOFF_BASE * (2 ** (attempt - 1))
                print(f"    {type(e).__name__}; backing off {delay:.0f}s before retry {attempt + 1}/{MAX_ATTEMPTS}", flush=True)
                time.sleep(delay)
                continue
            raise

    raise last_error


def query(state=None):
    q = '"data center"'
    if state:
        q += f' "{state}"'
    q += (
        " (moratorium OR zoning OR permit OR legislation OR regulator OR utility OR electricity OR water OR tariff "
        "OR interconnection OR ratepayer OR governor OR mayor OR county OR commission OR lawsuit OR incentive OR approval "
        "OR rejected OR withdraw)"
    )
    return http_json(
        {
            "query": q,
            "mode": "artlist",
            "maxrecords": str(MAX_RECORDS),
            "timespan": "15d",
            "sort": "datedesc",
            "format": "json",
        }
    )


def ascii_ratio(s):
    return 1 if not s else sum(ord(c) < 128 for c in s) / len(s)


def score(a, state=None):
    title = (a.get("title") or "").strip()
    dom = (a.get("domain") or "").lower()
    lang = (a.get("language") or "").lower()
    country = (a.get("sourcecountry") or "").lower()
    if not title or ascii_ratio(title) < 0.92:
        return -99
    if lang and lang != "english":
        return -99
    if country and country not in ("united states", "us"):
        return -99
    if BAD_TITLE.search(title) or BAD_DOMAIN.search(dom):
        return -99

    low = title.lower()
    sc = 5 if re.search(r"data[ -]?cent(er|re)s?", low) else -3
    if state and state.lower() in low:
        sc += 3
    sc += 1.6 * sum(bool(rx.search(title)) for _, rx in SIGNALS)
    if ".gov" in dom:
        sc += 5
    if dom in ("reuters.com", "apnews.com") or dom.endswith(".reuters.com"):
        sc += 4
    if re.search(
        r"governor|legislature|commission|council|county|utility|permit|moratorium|zoning|ratepayer|water|power|tariff|interconnection",
        title,
        re.I,
    ):
        sc += 2
    return sc


def normalize(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def clean(raw, state=None):
    candidates = []
    for a in raw.get("articles", []):
        sc = score(a, state)
        if sc < 6:
            continue
        title = (a.get("title") or "").strip()
        n = normalize(title)
        if any(n == x["_n"] or n in x["_n"] or x["_n"] in n for x in candidates):
            continue
        tags = [name for name, rx in SIGNALS if rx.search(title)][:4]
        dom = (a.get("domain") or "").lower()
        tier = (
            "Primary source"
            if ".gov" in dom
            else ("Credible reporting" if ("reuters.com" in dom or "apnews.com" in dom) else "Discovery")
        )
        candidates.append(
            {
                "_n": n,
                "title": title,
                "url": a.get("url"),
                "domain": a.get("domain"),
                "seendate": a.get("seendate"),
                "date_display": a.get("seendate"),
                "tier": tier,
                "tags": tags,
                "score": round(sc, 1),
            }
        )
        if len(candidates) >= 8:
            break
    for a in candidates:
        a.pop("_n", None)
    return candidates


def infer_state(title):
    lo = title.lower()
    for s in STATES:
        if s.lower() in lo:
            return s
    return None


def fetch_one(state):
    label = state or "NATIONAL"
    started = time.monotonic()
    try:
        arts = clean(query(state), state)
        return arts, None, time.monotonic() - started
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.monotonic() - started


def main():
    old = load_old()
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    display = now.strftime("%b. %d, %Y %H:%M UTC")

    out = {
        "meta": {
            "generated_at": stamp,
            "generated_at_display": display,
            "lookback_days": 15,
            "stale": False,
            "source": "GDELT DOC 2.0 API via scheduled rate-limited server-side job",
        },
        "national": {},
        "states": {},
    }

    failures = []
    jobs = [None] + STATES
    results = {}

    est_minutes = (len(jobs) * MIN_REQUEST_INTERVAL) / 60
    print(
        f"Starting {len(jobs)} feeds sequentially; min_interval={MIN_REQUEST_INTERVAL}s; "
        f"timeout={REQUEST_TIMEOUT}s; attempts={MAX_ATTEMPTS}; baseline≈{est_minutes:.1f} min",
        flush=True,
    )

    for i, state in enumerate(jobs, start=1):
        label = state or "NATIONAL"
        arts, err, secs = fetch_one(state)
        results[state] = (arts, err)
        if err:
            failures.append(label)
            print(f"[{i:02d}/{len(jobs)}] {label}: FAIL after {secs:.1f}s — {err}", flush=True)
        else:
            print(f"[{i:02d}/{len(jobs)}] {label}: OK {len(arts)} articles in {secs:.1f}s", flush=True)

    national_arts, national_err = results.get(None, (None, "missing"))
    if national_err or national_arts is None:
        out["national"] = old.get("national", {"articles": []})
    else:
        state_counts = Counter(filter(None, (infer_state(a["title"]) for a in national_arts)))
        themes = Counter(t for a in national_arts for t in a["tags"])
        out["national"] = {
            "articles": national_arts,
            "top_states": [x for x, _ in state_counts.most_common(5)],
            "top_themes": [x for x, _ in themes.most_common(4)],
        }

    for state in STATES:
        arts, err = results.get(state, (None, "missing"))
        if err or arts is None:
            prev = dict(old.get("states", {}).get(state, {"articles": []}))
            prev["stale"] = True
            out["states"][state] = prev
        else:
            out["states"][state] = {
                "articles": arts,
                "refreshed_at": stamp,
                "refreshed_at_display": display,
                "stale": False,
            }

    out["meta"]["stale"] = bool(failures)
    out["meta"]["failed_feeds"] = failures
    out["meta"]["successful_feeds"] = len(jobs) - len(failures)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(
        f"Wrote {OUT}; successful_feeds={len(jobs)-len(failures)}; failed_feeds={len(failures)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
