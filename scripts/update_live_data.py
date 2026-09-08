#!/usr/bin/env python3
"""Fast scheduled refresh for the Davis Data Center Monitor.

Queries national + 50 state feeds concurrently, applies strict timeouts, preserves
last-good data on failures, and prints progress for GitHub Actions logs.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "live.json"
API = "https://api.gdeltproject.org/api/v2/doc/doc"
WORKERS = 10
REQUEST_TIMEOUT = 12
TRIES = 2

STATES = [
    'Alabama','Alaska','Arizona','Arkansas','California','Colorado','Connecticut','Delaware','Florida','Georgia',
    'Hawaii','Idaho','Illinois','Indiana','Iowa','Kansas','Kentucky','Louisiana','Maine','Maryland','Massachusetts',
    'Michigan','Minnesota','Mississippi','Missouri','Montana','Nebraska','Nevada','New Hampshire','New Jersey',
    'New Mexico','New York','North Carolina','North Dakota','Ohio','Oklahoma','Oregon','Pennsylvania','Rhode Island',
    'South Carolina','South Dakota','Tennessee','Texas','Utah','Vermont','Virginia','Washington','West Virginia',
    'Wisconsin','Wyoming'
]

SIGNALS = [
    ('Pause / Ban', re.compile(r'moratorium|pause|ban|freeze|halt', re.I)),
    ('Power', re.compile(r'power|electric|grid|utility|interconnection|transmission|ratepayer|tariff', re.I)),
    ('Water', re.compile(r'water|aquifer|groundwater|cooling', re.I)),
    ('Permitting', re.compile(r'permit|zoning|land use|setback|hearing', re.I)),
    ('Economics', re.compile(r'tax|incentive|cost allocation|infrastructure cost|community benefit', re.I)),
    ('Politics', re.compile(r'governor|mayor|attorney general|council|commission|senator|politic', re.I)),
    ('Litigation', re.compile(r'lawsuit|litigation|court', re.I)),
    ('Project', re.compile(r'withdraw|reject|approval|project', re.I)),
]
BAD_TITLE = re.compile(r'stock|shares|earnings|nasdaq|dow|market today|price target|investor|portfolio|cryptocurrency|bitcoin|chip stocks|nvidia', re.I)
BAD_DOMAIN = re.compile(r'prnewswire|globenewswire|businesswire|stock\.|marketscreener|benzinga|seekingalpha', re.I)


def load_old():
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {'meta': {}, 'national': {'articles': []}, 'states': {}}


def http_json(params):
    url = API + '?' + urlencode(params)
    err = None
    for attempt in range(TRIES):
        try:
            req = Request(url, headers={'User-Agent': 'DavisDataCenterMonitor/2.0 (+public-policy research)'})
            with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode('utf-8', 'replace'))
        except Exception as e:
            err = e
            if attempt + 1 < TRIES:
                time.sleep(0.8 * (attempt + 1))
    raise err


def query(state=None):
    q = '"data center"'
    if state:
        q += f' "{state}"'
    q += ' (moratorium OR zoning OR permit OR legislation OR regulator OR utility OR electricity OR water OR tariff OR interconnection OR ratepayer OR governor OR mayor OR county OR commission OR lawsuit OR incentive OR approval OR rejected OR withdraw)'
    return http_json({'query': q, 'mode': 'artlist', 'maxrecords': '75', 'timespan': '15d', 'sort': 'datedesc', 'format': 'json'})


def ascii_ratio(s):
    return 1 if not s else sum(ord(c) < 128 for c in s) / len(s)


def score(a, state=None):
    title = (a.get('title') or '').strip()
    dom = (a.get('domain') or '').lower()
    lang = (a.get('language') or '').lower()
    country = (a.get('sourcecountry') or '').lower()
    if not title or ascii_ratio(title) < .92:
        return -99
    if lang and lang != 'english':
        return -99
    if country and country not in ('united states', 'us'):
        return -99
    if BAD_TITLE.search(title) or BAD_DOMAIN.search(dom):
        return -99
    low = title.lower()
    sc = 5 if re.search(r'data[ -]?cent(er|re)s?', low) else -3
    if state and state.lower() in low:
        sc += 3
    sc += 1.6 * sum(bool(rx.search(title)) for _, rx in SIGNALS)
    if '.gov' in dom:
        sc += 5
    if dom in ('reuters.com', 'apnews.com') or dom.endswith('.reuters.com'):
        sc += 4
    if re.search(r'governor|legislature|commission|council|county|utility|permit|moratorium|zoning|ratepayer|water|power|tariff|interconnection', title, re.I):
        sc += 2
    return sc


def normalize(s):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', ' ', s.lower())).strip()


def clean(raw, state=None):
    candidates = []
    for a in raw.get('articles', []):
        sc = score(a, state)
        if sc < 6:
            continue
        title = (a.get('title') or '').strip()
        n = normalize(title)
        if any(n == x['_n'] or n in x['_n'] or x['_n'] in n for x in candidates):
            continue
        tags = [name for name, rx in SIGNALS if rx.search(title)][:4]
        dom = (a.get('domain') or '').lower()
        tier = 'Primary source' if '.gov' in dom else ('Credible reporting' if ('reuters.com' in dom or 'apnews.com' in dom) else 'Discovery')
        candidates.append({
            '_n': n, 'title': title, 'url': a.get('url'), 'domain': a.get('domain'),
            'seendate': a.get('seendate'), 'date_display': a.get('seendate'), 'tier': tier,
            'tags': tags, 'score': round(sc, 1)
        })
        if len(candidates) >= 8:
            break
    for a in candidates:
        a.pop('_n', None)
    return candidates


def infer_state(title):
    lo = title.lower()
    for s in STATES:
        if s.lower() in lo:
            return s
    return None


def fetch_one(state):
    label = state or 'NATIONAL'
    started = time.time()
    try:
        arts = clean(query(state), state)
        return state, arts, None, time.time() - started
    except Exception as e:
        return state, None, f'{type(e).__name__}: {e}', time.time() - started


def main():
    old = load_old()
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    display = now.strftime('%b. %d, %Y %H:%M UTC')
    out = {
        'meta': {
            'generated_at': stamp,
            'generated_at_display': display,
            'lookback_days': 15,
            'stale': False,
            'source': 'GDELT DOC 2.0 API via scheduled server-side job'
        },
        'national': {},
        'states': {}
    }
    failures = []
    jobs = [None] + STATES
    results = {}

    print(f'Starting {len(jobs)} feeds with {WORKERS} workers; timeout={REQUEST_TIMEOUT}s; tries={TRIES}', flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        future_map = {pool.submit(fetch_one, state): state for state in jobs}
        done = 0
        for fut in as_completed(future_map):
            state, arts, err, secs = fut.result()
            label = state or 'NATIONAL'
            done += 1
            if err:
                print(f'[{done:02d}/{len(jobs)}] {label}: FAIL after {secs:.1f}s — {err}', flush=True)
                failures.append(label)
            else:
                print(f'[{done:02d}/{len(jobs)}] {label}: OK {len(arts)} articles in {secs:.1f}s', flush=True)
            results[state] = (arts, err)

    national_arts, national_err = results.get(None, (None, 'missing'))
    if national_err or national_arts is None:
        out['national'] = old.get('national', {'articles': []})
    else:
        state_counts = Counter(filter(None, (infer_state(a['title']) for a in national_arts)))
        themes = Counter(t for a in national_arts for t in a['tags'])
        out['national'] = {
            'articles': national_arts,
            'top_states': [x for x, _ in state_counts.most_common(5)],
            'top_themes': [x for x, _ in themes.most_common(4)]
        }

    for state in STATES:
        arts, err = results.get(state, (None, 'missing'))
        if err or arts is None:
            prev = dict(old.get('states', {}).get(state, {'articles': []}))
            prev['stale'] = True
            out['states'][state] = prev
        else:
            out['states'][state] = {
                'articles': arts,
                'refreshed_at': stamp,
                'refreshed_at_display': display,
                'stale': False
            }

    out['meta']['stale'] = bool(failures)
    out['meta']['failed_feeds'] = failures
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f'Wrote {OUT}; failed_feeds={len(failures)}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
