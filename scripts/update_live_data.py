#!/usr/bin/env python3
"""Refresh public-news discovery data for the Davis data-center monitor.

Runs server-side (GitHub Actions or another scheduler), so the browser never has
to make cross-origin GDELT requests. Existing per-state data is preserved when
an upstream request fails.
"""
from __future__ import annotations
import json, re, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data'/'live.json'
API='https://api.gdeltproject.org/api/v2/doc/doc'
STATES=['Alabama','Alaska','Arizona','Arkansas','California','Colorado','Connecticut','Delaware','Florida','Georgia','Hawaii','Idaho','Illinois','Indiana','Iowa','Kansas','Kentucky','Louisiana','Maine','Maryland','Massachusetts','Michigan','Minnesota','Mississippi','Missouri','Montana','Nebraska','Nevada','New Hampshire','New Jersey','New Mexico','New York','North Carolina','North Dakota','Ohio','Oklahoma','Oregon','Pennsylvania','Rhode Island','South Carolina','South Dakota','Tennessee','Texas','Utah','Vermont','Virginia','Washington','West Virginia','Wisconsin','Wyoming']
SIGNALS=[('Pause / Ban',re.compile(r'moratorium|pause|ban|freeze|halt',re.I)),('Power',re.compile(r'power|electric|grid|utility|interconnection|transmission|ratepayer|tariff',re.I)),('Water',re.compile(r'water|aquifer|groundwater|cooling',re.I)),('Permitting',re.compile(r'permit|zoning|land use|setback|hearing',re.I)),('Economics',re.compile(r'tax|incentive|cost allocation|infrastructure cost|community benefit',re.I)),('Politics',re.compile(r'governor|mayor|attorney general|council|commission|senator|politic',re.I)),('Litigation',re.compile(r'lawsuit|litigation|court',re.I)),('Project',re.compile(r'withdraw|reject|approval|project',re.I))]
BAD_TITLE=re.compile(r'stock|shares|earnings|nasdaq|dow|market today|price target|investor|portfolio|cryptocurrency|bitcoin|chip stocks|nvidia',re.I)
BAD_DOMAIN=re.compile(r'prnewswire|globenewswire|businesswire|stock\.|marketscreener|benzinga|seekingalpha',re.I)

def load_old():
    try:return json.loads(OUT.read_text())
    except Exception:return {'meta':{},'national':{'articles':[]},'states':{}}

def http_json(params, tries=3):
    url=API+'?'+urlencode(params)
    err=None
    for i in range(tries):
        try:
            req=Request(url,headers={'User-Agent':'DavisDataCenterMonitor/1.0 (+public-policy research)'})
            with urlopen(req,timeout=25) as r:
                return json.loads(r.read().decode('utf-8','replace'))
        except Exception as e:
            err=e; time.sleep(2**i)
    raise err

def query(state=None):
    # One OR block only; GDELT DOC supports exact phrases + OR blocks.
    q='"data center"'
    if state:q+=f' "{state}"'
    q+=' (moratorium OR zoning OR permit OR legislation OR regulator OR utility OR electricity OR water OR tariff OR interconnection OR ratepayer OR governor OR mayor OR county OR commission OR lawsuit OR incentive OR approval OR rejected OR withdraw)'
    return http_json({'query':q,'mode':'artlist','maxrecords':'75','timespan':'15d','sort':'datedesc','format':'json'})

def ascii_ratio(s):
    if not s:return 1
    return sum(ord(c)<128 for c in s)/len(s)

def score(a,state=None):
    title=(a.get('title') or '').strip(); dom=(a.get('domain') or '').lower(); lang=(a.get('language') or '').lower(); country=(a.get('sourcecountry') or '').lower()
    if not title or ascii_ratio(title)<.92:return -99
    if lang and lang!='english':return -99
    if country and country not in ('united states','us'):return -99
    if BAD_TITLE.search(title) or BAD_DOMAIN.search(dom):return -99
    low=title.lower(); sc=0
    sc+=5 if re.search(r'data[ -]?cent(er|re)s?',low) else -3
    if state and state.lower() in low:sc+=3
    sc+=1.6*sum(bool(rx.search(title)) for _,rx in SIGNALS)
    if '.gov' in dom:sc+=5
    if dom in ('reuters.com','apnews.com') or dom.endswith('.reuters.com'):sc+=4
    if re.search(r'governor|legislature|commission|council|county|utility|permit|moratorium|zoning|ratepayer|water|power|tariff|interconnection',title,re.I):sc+=2
    return sc

def normalize(s):return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9 ]',' ',s.lower())).strip()

def clean(raw,state=None):
    arr=[]
    for a in raw.get('articles',[]):
        sc=score(a,state)
        if sc<6:continue
        title=(a.get('title') or '').strip(); n=normalize(title)
        if any(n==x or n in x or x in n for x in [z['_n'] for z in arr]):continue
        tags=[name for name,rx in SIGNALS if rx.search(title)][:4]
        dom=(a.get('domain') or '').lower()
        tier='Primary source' if '.gov' in dom else ('Credible reporting' if ('reuters.com' in dom or 'apnews.com' in dom) else 'Discovery')
        arr.append({'_n':n,'title':title,'url':a.get('url'),'domain':a.get('domain'),'seendate':a.get('seendate'),'date_display':a.get('seendate'),'tier':tier,'tags':tags,'score':round(sc,1)})
        if len(arr)>=8:break
    for a in arr:a.pop('_n',None)
    return arr

def infer_state(title):
    lo=title.lower()
    for s in STATES:
        if s.lower() in lo:return s
    return None

def main():
    old=load_old(); now=datetime.now(timezone.utc); stamp=now.isoformat(); display=now.strftime('%b. %d, %Y %H:%M UTC')
    out={'meta':{'generated_at':stamp,'generated_at_display':display,'lookback_days':15,'stale':False,'source':'GDELT DOC 2.0 API via scheduled server-side job'},'national':{},'states':{}}
    failures=[]
    try:
        arts=clean(query(None),None)
        states=Counter(filter(None,(infer_state(a['title']) for a in arts)))
        themes=Counter(t for a in arts for t in a['tags'])
        out['national']={'articles':arts,'top_states':[x for x,_ in states.most_common(5)],'top_themes':[x for x,_ in themes.most_common(4)]}
    except Exception as e:
        failures.append('national'); out['national']=old.get('national',{'articles':[]})
    for i,state in enumerate(STATES):
        try:
            arts=clean(query(state),state)
            out['states'][state]={'articles':arts,'refreshed_at':stamp,'refreshed_at_display':display,'stale':False}
        except Exception as e:
            failures.append(state)
            prev=old.get('states',{}).get(state,{'articles':[]})
            prev=dict(prev); prev['stale']=True
            out['states'][state]=prev
        time.sleep(.15)
    out['meta']['stale']=bool(failures); out['meta']['failed_feeds']=failures
    OUT.write_text(json.dumps(out,indent=2,ensure_ascii=False))
    print(f'Wrote {OUT}; failures={len(failures)}')
    # Do not fail the deployment if some feeds fail: old data is preserved.
    return 0
if __name__=='__main__':raise SystemExit(main())
