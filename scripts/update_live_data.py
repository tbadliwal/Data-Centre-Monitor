#!/usr/bin/env python3
"""
Davis Data Center Monitor — production discovery updater.

Primary source: Google News RSS
Fallback source: Bing News RSS

Key guarantees:
- Enforces a TRUE rolling 15-day cutoff locally, even if an upstream feed returns older stories.
- Sorts articles chronologically descending (newest first).
- Keeps up to 24 high-relevance articles per state.
- Preserves the last-good state feed when a request fails.
- Produces transparent automated interpretation:
    * state 15-day development-friction signal
    * governor 15-day signal
  These are discovery/interpretive overlays, NOT verified regulatory conclusions.
"""

from __future__ import annotations
import html, json, re, time, xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone, timedelta
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
MAX_ARTICLES = 24

STATES = [
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut","Delaware","Florida","Georgia",
    "Hawaii","Idaho","Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts",
    "Michigan","Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada","New Hampshire","New Jersey",
    "New Mexico","New York","North Carolina","North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
    "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont","Virginia","Washington","West Virginia",
    "Wisconsin","Wyoming"
]

GOVERNORS = {
    "Alabama":"Kay Ivey","Alaska":"Mike Dunleavy","Arizona":"Katie Hobbs","Arkansas":"Sarah Huckabee Sanders",
    "California":"Gavin Newsom","Colorado":"Jared Polis","Connecticut":"Ned Lamont","Delaware":"Matt Meyer",
    "Florida":"Ron DeSantis","Georgia":"Brian Kemp","Hawaii":"Josh Green","Idaho":"Brad Little","Illinois":"JB Pritzker",
    "Indiana":"Mike Braun","Iowa":"Kim Reynolds","Kansas":"Laura Kelly","Kentucky":"Andy Beshear","Louisiana":"Jeff Landry",
    "Maine":"Janet Mills","Maryland":"Wes Moore","Massachusetts":"Maura Healey","Michigan":"Gretchen Whitmer",
    "Minnesota":"Tim Walz","Mississippi":"Tate Reeves","Missouri":"Mike Kehoe","Montana":"Greg Gianforte",
    "Nebraska":"Jim Pillen","Nevada":"Joe Lombardo","New Hampshire":"Kelly Ayotte","New Jersey":"Mikie Sherrill",
    "New Mexico":"Michelle Lujan Grisham","New York":"Kathy Hochul","North Carolina":"Josh Stein","North Dakota":"Kelly Armstrong",
    "Ohio":"Mike DeWine","Oklahoma":"Kevin Stitt","Oregon":"Tina Kotek","Pennsylvania":"Josh Shapiro","Rhode Island":"Dan McKee",
    "South Carolina":"Henry McMaster","South Dakota":"Larry Rhoden","Tennessee":"Bill Lee","Texas":"Greg Abbott",
    "Utah":"Spencer Cox","Vermont":"Phil Scott","Virginia":"Abigail Spanberger","Washington":"Bob Ferguson",
    "West Virginia":"Patrick Morrisey","Wisconsin":"Tony Evers","Wyoming":"Mark Gordon"
}

SIGNALS = [
    ("Pause / Ban", re.compile(r"\bmoratorium\b|\bpause\b|\bban\b|\bfreeze\b|\bhalt\b", re.I)),
    ("Power", re.compile(r"\bpower\b|\belectric(?:ity)?\b|\bgrid\b|\butility\b|\binterconnection\b|\btransmission\b|\bratepayer\b|\btariff\b", re.I)),
    ("Water", re.compile(r"\bwater\b|\baquifer\b|\bgroundwater\b|\bcooling\b", re.I)),
    ("Permitting", re.compile(r"\bpermit(?:ting)?\b|\bzoning\b|\bland use\b|\bsetback\b|\bhearing\b", re.I)),
    ("Economics", re.compile(r"\btax\b|\bincentive\b|\bcost allocation\b|\binfrastructure cost\b|\bcommunity benefit\b", re.I)),
    ("Politics", re.compile(r"\bgovernor\b|\bmayor\b|\battorney general\b|\bcouncil\b|\bcommission\b|\bsenator\b|\bpolitic", re.I)),
    ("Litigation", re.compile(r"\blawsuit\b|\blitigation\b|\bcourt\b|\bappeal\b", re.I)),
    ("Project", re.compile(r"\bwithdraw|\breject|\bapproval\b|\bproject\b", re.I)),
]

RESTRICTIVE_PATTERNS = [
    re.compile(r"\bmoratorium\b|\bban\b|\bpause\b|\bfreeze\b|\bhalt\b", re.I),
    re.compile(r"\boppos(?:e|ed|ition|ing)\b|\breject(?:ed|ion|s)?\b.*\bdata center", re.I),
    re.compile(r"\blawsuit\b|\blitigation\b|\bappeal\b|\bcourt\b", re.I),
    re.compile(r"\brestriction\b|\brestrictive\b|\btighter\b|\bguardrail", re.I),
    re.compile(r"\bratepayer\b|\bcost[- ]allocation\b|\bpay .* grid\b|\bown infrastructure costs?\b", re.I),
    re.compile(r"\bwater\b.*\bconcern|\bgroundwater\b|\baquifer\b", re.I),
]
SUPPORTIVE_PATTERNS = [
    re.compile(r"\breject(?:ed|s)?\b.*\bmoratorium\b|\bmoratorium\b.*\breject", re.I),
    re.compile(r"\bapprove(?:d|s)?\b.*\bdata center|\bdata center\b.*\bapprove", re.I),
    re.compile(r"\bpermit\b.*\bapproved\b|\bapproval\b.*\bproject\b", re.I),
    re.compile(r"\bsupport(?:s|ed|ing)?\b.*\bdata center|\bwelcome(?:s|d)?\b.*\bdata center", re.I),
    re.compile(r"\bfast[- ]track\b|\bincentive\b|\btax credit\b|\battract\b.*\bdata center", re.I),
]

BAD_TITLE = re.compile(
    r"\bstock\b|\bshares\b|\bearnings\b|\bnasdaq\b|\bdow\b|\bprice target\b|\binvestor\b|\bportfolio\b|"
    r"\bcryptocurrency\b|\bbitcoin\b|\bnvidia\b|\bchip stocks\b",
    re.I,
)
BAD_SOURCE = re.compile(r"prnewswire|globenewswire|businesswire|marketscreener|benzinga|seekingalpha", re.I)

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
                    "User-Agent": "Mozilla/5.0 (compatible; DavisDataCenterMonitor/5.0; +https://github.com/)",
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


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None


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
        pub_dt = parse_date(item.findtext("pubDate"))

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
            "published_at": pub_dt.isoformat() if pub_dt else None,
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
    return "https://news.google.com/rss/search?q=" + quote_plus(q) + "&hl=en-US&gl=US&ceid=US:en"


def bing_news_url(state: str | None) -> str:
    geo = f' "{state}"' if state else ""
    q = (
        f'"data center"{geo} '
        '(moratorium OR zoning OR permit OR utility OR power OR water OR tariff OR interconnection '
        'OR governor OR county OR lawsuit OR incentive)'
    )
    return "https://www.bing.com/news/search?q=" + quote_plus(q) + "&format=rss&setlang=en-us&cc=us"


def fetch_feed(state: str | None):
    errors = []
    for provider, url in (("Google News RSS", google_news_url(state)), ("Bing News RSS", bing_news_url(state))):
        try:
            return parse_rss(request_bytes(url), provider), provider, None
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {e}")
    return None, None, " | ".join(errors)


def normalize(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def article_tier(a):
    domain = (a.get("domain") or "").lower()
    pub = (a.get("publisher") or "").lower()
    if ".gov" in domain or domain.endswith(".gov"):
        return "Primary source"
    if "reuters" in domain or "reuters" in pub or "apnews" in domain or "associated press" in pub or "ap news" in pub:
        return "Credible reporting"
    return "Discovery"


def article_score(a, state=None):
    title = a.get("title", "")
    desc = a.get("description", "")
    blob = f"{title} {desc}"
    source_blob = f"{a.get('domain','')} {a.get('publisher','')}"
    if not title or BAD_TITLE.search(title) or BAD_SOURCE.search(source_blob):
        return -99
    if not re.search(r"data[ -]?cent(?:er|re)s?", blob, re.I):
        return -99

    sc = 5
    if state and state.lower() in blob.lower():
        sc += 2
    tags = [name for name, rx in SIGNALS if rx.search(blob)]
    sc += 1.5 * len(tags)
    if re.search(r"\.gov\b|government|legislature|commission|council|county|utility|permit|moratorium|zoning|ratepayer|water|power|tariff|interconnection", blob, re.I):
        sc += 2
    if article_tier(a) in ("Primary source", "Credible reporting"):
        sc += 3
    return sc


def clean_articles(raw, state=None, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    scored = []
    for a in raw or []:
        pub_dt = parse_date(a.get("published_at"))
        # Enforce the 15-day window locally. Undated or old fallback items are excluded.
        if pub_dt is None or pub_dt < cutoff or pub_dt > now + timedelta(hours=6):
            continue

        sc = article_score(a, state)
        if sc < 6:
            continue

        blob = f"{a.get('title','')} {a.get('description','')}"
        tags = [name for name, rx in SIGNALS if rx.search(blob)][:4]
        item = dict(a)
        item["score"] = round(sc, 1)
        item["tags"] = tags
        item["tier"] = article_tier(a)
        item["published_at"] = pub_dt.isoformat()
        item["date_display"] = pub_dt.strftime("%b %d, %Y")
        item["age_days"] = max(0, (now.date() - pub_dt.date()).days)
        # Keep a short summary for transparent signal derivation.
        item["summary"] = clean_text(a.get("description"))[:500]
        scored.append(item)

    # Chronology is the primary sort. Relevance breaks same-day ties.
    scored.sort(key=lambda x: (x["published_at"], x["score"]), reverse=True)

    out, seen = [], []
    for a in scored:
        n = normalize(a["title"])
        if any(n == x or n in x or x in n for x in seen):
            continue
        seen.append(n)
        out.append(a)
        if len(out) >= MAX_ARTICLES:
            break
    return out


def directional_score(text: str):
    restrictive = sum(1 for rx in RESTRICTIVE_PATTERNS if rx.search(text))
    supportive = sum(1 for rx in SUPPORTIVE_PATTERNS if rx.search(text))
    return restrictive, supportive


def derive_state_signal(articles):
    if not articles:
        return {
            "direction": "No clear signal", "tone": "quiet", "confidence": "Low",
            "restrictive_points": 0, "supportive_points": 0, "dominant_themes": [], "evidence": []
        }

    r_total = s_total = 0
    themes = Counter()
    directional_evidence = []
    for a in articles:
        text = f"{a.get('title','')} {a.get('summary','')}"
        r, s = directional_score(text)
        weight = 1.5 if a.get("tier") in ("Primary source", "Credible reporting") else 1.0
        r_total += r * weight
        s_total += s * weight
        for tag in a.get("tags", []):
            themes[tag] += 1
        if r or s:
            directional_evidence.append(a)

    delta = r_total - s_total
    if r_total == 0 and s_total == 0:
        direction, tone = "No clear signal", "quiet"
    elif abs(delta) < 0.5:
        direction, tone = "Mixed / contested", "mixed"
    elif delta > 0:
        direction, tone = "Friction rising", "rising"
    else:
        direction, tone = "Development conditions easing", "easing"

    evidence_count = len(directional_evidence)
    confidence = "High" if evidence_count >= 5 else ("Medium" if evidence_count >= 2 else "Low")
    return {
        "direction": direction,
        "tone": tone,
        "confidence": confidence,
        "restrictive_points": round(r_total, 1),
        "supportive_points": round(s_total, 1),
        "dominant_themes": [x for x, _ in themes.most_common(4)],
        "evidence": [
            {"title": a["title"], "url": a.get("url"), "date": a.get("published_at"), "tier": a.get("tier")}
            for a in directional_evidence[:4]
        ],
    }


def derive_governor_signal(state, articles):
    governor = GOVERNORS.get(state)
    if not governor:
        return {"direction": "No new signal", "tone": "quiet", "confidence": "Low", "evidence": []}
    surname = governor.split()[-1].lower()
    matches = []
    r_total = s_total = 0
    for a in articles:
        text = f"{a.get('title','')} {a.get('summary','')}"
        lo = text.lower()
        if governor.lower() not in lo and surname not in lo:
            continue
        r, s = directional_score(text)
        if not (r or s):
            continue
        weight = 1.5 if a.get("tier") in ("Primary source", "Credible reporting") else 1.0
        r_total += r * weight
        s_total += s * weight
        matches.append(a)

    if not matches:
        return {"direction": "No new signal", "tone": "quiet", "confidence": "Low", "evidence": []}

    delta = r_total - s_total
    if abs(delta) < 0.5:
        direction, tone = "Mixed / unclear", "mixed"
    elif delta > 0:
        direction, tone = "More restrictive signal", "rising"
    else:
        direction, tone = "More supportive signal", "easing"
    confidence = "High" if len(matches) >= 3 else ("Medium" if len(matches) >= 2 else "Low")
    return {
        "direction": direction,
        "tone": tone,
        "confidence": confidence,
        "evidence": [
            {"title": a["title"], "url": a.get("url"), "date": a.get("published_at"), "tier": a.get("tier")}
            for a in matches[:3]
        ],
    }


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
    display = now.strftime("%b %d, %Y %H:%M UTC")

    out = {
        "meta": {
            "generated_at": stamp,
            "generated_at_display": display,
            "lookback_days": LOOKBACK_DAYS,
            "stale": False,
            "source": "Google News RSS with Bing News RSS fallback via scheduled GitHub Actions",
            "interpretation_note": "Automated 15-day signals are screening indicators, not verified legal or political conclusions."
        },
        "national": {},
        "states": {},
    }

    jobs = [None] + STATES
    failures, results = [], {}

    print(f"Starting {len(jobs)} feeds; strict {LOOKBACK_DAYS}-day cutoff; max_articles={MAX_ARTICLES}", flush=True)

    for i, state in enumerate(jobs, 1):
        label = state or "NATIONAL"
        started = time.monotonic()
        raw, provider, err = fetch_feed(state)
        if err:
            results[state] = (None, None, err)
            failures.append(label)
            print(f"[{i:02d}/{len(jobs)}] {label}: FAIL — {err}", flush=True)
            continue
        arts = clean_articles(raw, state, now)
        results[state] = (arts, provider, None)
        print(f"[{i:02d}/{len(jobs)}] {label}: OK {len(arts)} current articles via {provider} in {time.monotonic()-started:.1f}s", flush=True)

    # Populate state feeds and signals first.
    all_articles = []
    signal_counts = Counter()
    for state in STATES:
        arts, provider, err = results.get(state, (None, None, "missing"))
        if err or arts is None:
            prev = dict(old.get("states", {}).get(state, {"articles": []}))
            prev["stale"] = True
            out["states"][state] = prev
            continue

        sig = derive_state_signal(arts)
        gov_sig = derive_governor_signal(state, arts)
        signal_counts[sig["tone"]] += 1
        all_articles.extend([{**a, "_state": state} for a in arts])
        out["states"][state] = {
            "articles": arts,
            "refreshed_at": stamp,
            "refreshed_at_display": display,
            "provider": provider,
            "stale": False,
            "signal": sig,
            "governor_signal": gov_sig,
            "article_count": len(arts),
            "latest_article_date": arts[0]["published_at"] if arts else None,
        }

    national_arts, national_provider, national_err = results.get(None, (None, None, "missing"))
    if national_err or national_arts is None:
        national_arts = old.get("national", {}).get("articles", [])
        national_provider = old.get("national", {}).get("provider")

    # Merge national and state discoveries to build a current high-signal national feed.
    merged = []
    seen = set()
    for a in list(national_arts or []) + sorted(all_articles, key=lambda x: x.get("published_at") or "", reverse=True):
        key = normalize(a.get("title", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(a)
    merged.sort(key=lambda x: (x.get("published_at") or "", x.get("score", 0)), reverse=True)
    merged = merged[:40]

    state_counts = Counter(filter(None, (infer_state(a.get("title", "")) for a in merged)))
    themes = Counter(t for a in merged for t in a.get("tags", []))
    out["national"] = {
        "articles": merged[:24],
        "high_signal_articles": merged[:16],
        "top_states": [x for x, _ in state_counts.most_common(6)],
        "top_themes": [x for x, _ in themes.most_common(5)],
        "provider": national_provider,
        "state_signal_counts": dict(signal_counts),
    }

    out["meta"]["stale"] = bool(failures)
    out["meta"]["failed_feeds"] = failures
    out["meta"]["successful_feeds"] = len(jobs) - len(failures)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Wrote {OUT}; successful_feeds={len(jobs)-len(failures)}; failed_feeds={len(failures)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
