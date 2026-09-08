#!/usr/bin/env python3
"""
Davis Data Center Monitor — resilient live-news updater.

Primary source: Google News RSS search
Fallback source: Bing News RSS search

Why RSS instead of GDELT:
- GitHub-hosted runners can inherit shared-IP rate limits from GDELT.
- RSS endpoints are simpler for scheduled server-side discovery.
- The script preserves the last-good feed whenever a source fails.

This remains a DISCOVERY layer, not the verified regulatory record.
"""

from __future__ import annotations

import html
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "live.json"

LOOKBACK_DAYS = 15
REQUEST_TIMEOUT = 20
MIN_REQUEST_INTERVAL = 2.25
MAX_ATTEMPTS = 2
MAX_ARTICLES = 8

STATES = [
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut","Delaware","Florida","Georgia",
    "Hawaii","Idaho","Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts",
    "Michigan","Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada","New Hampshire","New Jersey",
    "New Mexico","New York","North Carolina","North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
    "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont","Virginia","Washington","West Virginia",
    "Wisconsin","Wyoming"
]

SIGNALS = [
    ("Pause / Ban", re.compile(r"\bmoratorium\b|\bpause\b|\bban\b|\bfreeze\b|\bhalt\b", re.I)),
    ("Power", re.compile(r"\bpower\b|\belectric(?:ity)?\b|\bgrid\b|\butility\b|\binterconnection\b|\btransmission\b|\bratepayer\b|\btariff\b", re.I)),
    ("Water", re.compile(r"\bwater\b|\baquifer\b|\bgroundwater\b|\bcooling\b", re.I)),
    ("Permitting", re.compile(r"\bpermit(?:ting)?\b|\bzoning\b|\bland use\b|\bsetback\b|\bhearing\b", re.I)),
    ("Economics", re.compile(r"\btax\b|\bincentive\b|\bcost allocation\b|\binfrastructure cost\b|\bcommunity benefit\b", re.I)),
    ("Politics", re.compile(r"\bgovernor\b|\bmayor\b|\battorney general\b|\bcouncil\b|\bcommission\b|\bsenator\b|\bpolitic", re.I)),
    ("Litigation", re.compile(r"\blawsuit\b|\blitigation\b|\bcourt\b", re.I)),
    ("Project", re.compile(r"\bwithdraw|\breject|\bapproval\b|\bproject\b", re.I)),
]

BAD_TITLE = re.compile(
    r"\bstock\b|\bshares\b|\bearnings\b|\bnasdaq\b|\bdow\b|\bprice target\b|\binvestor\b|\bportfolio\b|"
    r"\bcryptocurrency\b|\bbitcoin\b|\bnvidia\b|\bchip stocks\b",
    re.I,
)
BAD_SOURCE = re.compile(
    r"prnewswire|globenewswire|businesswire|marketscreener|benzinga|seekingalpha",
    re.I,
)

_last_request = 0.0


def load_old():
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {"meta": {}, "national": {"articles": []}, "states": {}}


def wait_gap():
    global _last_request
    elapsed = time.monotonic() - _last_request
    if _last_request and elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request = time.monotonic()


def request_bytes(url: str) -> bytes:
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait_gap()
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; DavisDataCenterMonitor/4.0; +https://github.com/)",
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                },
            )
            with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return r.read()
        except (HTTPError, URLError, TimeoutError) as e:
            last = e
            if attempt < MAX_ATTEMPTS:
                delay = 8 * attempt
                print(f"    {type(e).__name__}: {e}; retrying in {delay}s", flush=True)
                time.sleep(delay)
    raise last


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return s


def domain_from_url(url: str | None) -> str:
    try:
        return urlparse(url or "").netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def parse_rss(xml_bytes: bytes, provider: str):
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.findall(".//item"):
        title = clean_text(item.findtext("title"))
        link = clean_text(item.findtext("link"))
        desc = clean_text(item.findtext("description"))
        pub = parse_date(item.findtext("pubDate"))

        source_el = item.find("source")
        source_name = clean_text(source_el.text if source_el is not None else "")
        source_url = source_el.attrib.get("url", "") if source_el is not None else ""
        domain = domain_from_url(source_url) or domain_from_url(link) or source_name.lower()

        items.append({
            "title": title,
            "url": link,
            "description": desc,
            "domain": domain,
            "publisher": source_name,
            "seendate": pub,
            "provider": provider,
        })
    return items


def google_news_url(state: str | None) -> str:
    geo = f' "{state}"' if state else ""
    q = (
        f'"data center"{geo} '
        '(moratorium OR zoning OR permit OR permitting OR utility OR electricity OR power OR water OR tariff '
        'OR interconnection OR ratepayer OR governor OR mayor OR county OR commission OR lawsuit OR incentive '
        f'OR approval OR rejected OR withdraw) when:{LOOKBACK_DAYS}d'
    )
    return (
        "https://news.google.com/rss/search?q=" + quote_plus(q) +
        "&hl=en-US&gl=US&ceid=US:en"
    )


def bing_news_url(state: str | None) -> str:
    geo = f' "{state}"' if state else ""
    q = (
        f'"data center"{geo} '
        '(moratorium OR zoning OR permit OR utility OR power OR water OR tariff OR interconnection '
        'OR governor OR county OR lawsuit OR incentive)'
    )
    return (
        "https://www.bing.com/news/search?q=" + quote_plus(q) +
        "&format=rss&setlang=en-us&cc=us"
    )


def fetch_feed(state: str | None):
    errors = []
    for provider, url in (
        ("Google News RSS", google_news_url(state)),
        ("Bing News RSS", bing_news_url(state)),
    ):
        try:
            return parse_rss(request_bytes(url), provider), provider, None
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {e}")
    return None, None, " | ".join(errors)


def normalize(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def article_score(a, state=None):
    title = a.get("title", "")
    desc = a.get("description", "")
    blob = f"{title} {desc}"
    source_blob = f"{a.get('domain','')} {a.get('publisher','')}"

    if not title or BAD_TITLE.search(title) or BAD_SOURCE.search(source_blob):
        return -99

    sc = 0
    if re.search(r"data[ -]?cent(?:er|re)s?", blob, re.I):
        sc += 5
    else:
        return -99

    if state and state.lower() in blob.lower():
        sc += 2

    tags = [name for name, rx in SIGNALS if rx.search(blob)]
    sc += 1.5 * len(tags)

    if re.search(r"\.gov\b|government|legislature|commission|council|county|utility|permit|moratorium|zoning|ratepayer|water|power|tariff|interconnection", blob, re.I):
        sc += 2

    publisher = (a.get("publisher") or "").lower()
    domain = (a.get("domain") or "").lower()
    if "reuters" in publisher or "associated press" in publisher or "ap news" in publisher or "reuters" in domain or "apnews" in domain:
        sc += 3

    return sc


def article_tier(a):
    domain = (a.get("domain") or "").lower()
    pub = (a.get("publisher") or "").lower()

    if ".gov" in domain or domain.endswith(".gov"):
        return "Primary source"
    if "reuters" in domain or "reuters" in pub or "apnews" in domain or "associated press" in pub or "ap news" in pub:
        return "Credible reporting"
    return "Discovery"


def clean_articles(raw, state=None):
    scored = []
    for a in raw or []:
        sc = article_score(a, state)
        if sc < 6:
            continue

        blob = f"{a.get('title','')} {a.get('description','')}"
        tags = [name for name, rx in SIGNALS if rx.search(blob)][:4]
        a = dict(a)
        a["score"] = round(sc, 1)
        a["tags"] = tags
        a["tier"] = article_tier(a)
        a["date_display"] = a.get("seendate")
        scored.append(a)

    scored.sort(key=lambda x: (-x["score"], x.get("seendate") or ""))

    out = []
    seen = []
    for a in scored:
        n = normalize(a["title"])
        if any(n == x or n in x or x in n for x in seen):
            continue
        seen.append(n)
        a.pop("description", None)
        out.append(a)
        if len(out) >= MAX_ARTICLES:
            break
    return out


def infer_state(text):
    lo = (text or "").lower()
    for s in STATES:
        if s.lower() in lo:
            return s
    return None


def main():
    old = load_old()
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    display = now.strftime("%b. %d, %Y %H:%M UTC")

    out = {
        "meta": {
            "generated_at": stamp,
            "generated_at_display": display,
            "lookback_days": LOOKBACK_DAYS,
            "stale": False,
            "source": "Google News RSS with Bing News RSS fallback via scheduled GitHub Actions job",
        },
        "national": {},
        "states": {},
    }

    jobs = [None] + STATES
    failures = []
    results = {}

    print(
        f"Starting {len(jobs)} discovery feeds; primary=Google News RSS; "
        f"fallback=Bing News RSS; min_interval={MIN_REQUEST_INTERVAL}s",
        flush=True,
    )

    for i, state in enumerate(jobs, 1):
        label = state or "NATIONAL"
        started = time.monotonic()
        raw, provider, err = fetch_feed(state)

        if err:
            results[state] = (None, None, err)
            failures.append(label)
            print(f"[{i:02d}/{len(jobs)}] {label}: FAIL — {err}", flush=True)
            continue

        arts = clean_articles(raw, state)
        results[state] = (arts, provider, None)
        secs = time.monotonic() - started
        print(f"[{i:02d}/{len(jobs)}] {label}: OK {len(arts)} articles via {provider} in {secs:.1f}s", flush=True)

    national_arts, national_provider, national_err = results.get(None, (None, None, "missing"))
    if national_err or national_arts is None:
        out["national"] = old.get("national", {"articles": []})
    else:
        state_counts = Counter(
            filter(None, (infer_state((a.get("title") or "") + " " + (a.get("publisher") or "")) for a in national_arts))
        )
        themes = Counter(t for a in national_arts for t in a.get("tags", []))
        out["national"] = {
            "articles": national_arts,
            "top_states": [x for x, _ in state_counts.most_common(5)],
            "top_themes": [x for x, _ in themes.most_common(4)],
            "provider": national_provider,
        }

    for state in STATES:
        arts, provider, err = results.get(state, (None, None, "missing"))
        if err or arts is None:
            prev = dict(old.get("states", {}).get(state, {"articles": []}))
            prev["stale"] = True
            out["states"][state] = prev
        else:
            out["states"][state] = {
                "articles": arts,
                "refreshed_at": stamp,
                "refreshed_at_display": display,
                "provider": provider,
                "stale": False,
            }

    out["meta"]["stale"] = bool(failures)
    out["meta"]["failed_feeds"] = failures
    out["meta"]["successful_feeds"] = len(jobs) - len(failures)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))

    print(
        f"Wrote {OUT}; successful_feeds={len(jobs)-len(failures)}; "
        f"failed_feeds={len(failures)}",
        flush=True,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
