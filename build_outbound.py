#!/usr/bin/env python3
"""
Build final outbound campaign CSVs:
  1. Classify all 197 emails → true_personal vs generic
  2. Verify generic_emails.csv through Reoon (bulk)
  3. Re-enrich 16 invalid personal emails via Serper
  4. Verify re-enriched emails
  5. Output:
       outbound_personal.csv  — safe+catch_all personal emails
       outbound_generic.csv   — safe+catch_all generic emails
       phone_only.csv         — already exists (no change)
       reinrich_results.csv   — re-enriched 16 (for review)
"""

import csv, json, os, re, time, requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE         = Path(__file__).resolve().parent
REOON_URL    = 'https://emailverifier.reoon.com/api/v1/verify'
BULK_CREATE  = 'https://emailverifier.reoon.com/api/v1/create-bulk-verification-task/'
BULK_RESULT  = 'https://emailverifier.reoon.com/api/v1/get-result-bulk-verification-task/'
SERPER_URL   = 'https://google.serper.dev/search'
REOON_KEY    = os.getenv('REOON_API_KEY')

_serper_keys = [v for k in ['SERPER_API_KEY','SERPER_API_KEY_2','SERPER_API_KEY_3']
                if (v := os.getenv(k,'').strip())]
_serper_idx  = 0

BULK_SCORES = {'safe': 100, 'catch_all': 70, 'role_account': 60, 'unknown': 30, 'invalid': 0}

# ── Email classifier ───────────────────────────────────────────────────────────
EMAIL_PREFIX_GENERICS = [
    'email','noreply','no-reply','donotreply','do-not-reply',
]

ROLE_WORDS = {
    # Standard roles
    'info','sales','contact','support','admin','mail','hello','store','shop',
    'service','office','customerservice','contactus','orders','events','questions',
    'online','feedback','webmaster','billing','accounting','hr','jobs','careers',
    'press','media','help','inquiry','inquiries','quote','returns','warranty',
    'shipping','wholesale','dealer','dispatch','fulfillment','team','staff','crew',
    # Gun-industry roles
    'gunsmith','ffl','transfers','transfer','firearms','guns','ammo','range',
    'shooting','armory','arsenal','tactical','defense','arms','gun','pawn',
    # Business roles
    'manager','management','ceo','cfo','coo','president','director','vp',
    'legal','technical','receiving','contracting','onlinestore','uniforms',
    'directions','reply','apply','web','deals','sot','operation','operations',
    # Brand/location-as-email
    'voodoo','winchester','durango','bolsaguns','voxtac','goldstar',
    'rockystore','omanager','management','onlinestore',
}

FREE_PROVIDERS = {
    'gmail.com','yahoo.com','hotmail.com','outlook.com','icloud.com',
    'aol.com','msn.com','live.com','me.com','mac.com','protonmail.com',
}

def _name_tokens(name: str) -> set:
    """Split contact name into lowercase tokens."""
    if not name: return set()
    return {t.lower() for t in re.split(r'[\s.\-_,]+', name) if len(t) >= 2}

def classify_email(email: str, contact_name: str = '') -> str:
    """Return 'personal' or 'generic'."""
    email = email.lower().strip()
    if '@' not in email: return 'generic'
    local, _, domain = email.rpartition('@')

    # Free provider: only personal if local matches a name token
    if domain in FREE_PROVIDERS:
        tokens = _name_tokens(contact_name)
        local_clean = re.sub(r'[^a-z]', '', local)
        if tokens and any(t in local_clean or local_clean in t for t in tokens if len(t) >= 3):
            return 'personal'
        return 'generic'

    # Starts with email* prefix → always generic
    for prefix in EMAIL_PREFIX_GENERICS:
        if local.startswith(prefix):
            return 'generic'

    # Exact role word match
    local_base = re.sub(r'[^a-z]', '', local)
    if local_base in ROLE_WORDS:
        return 'generic'

    # Has a dot → likely first.last
    if '.' in local:
        parts = local.split('.')
        if all(re.match(r'^[a-z]+$', p) for p in parts if p):
            # Check if looks like name.name or initial.name
            return 'personal'

    # Matches contact name tokens
    tokens = _name_tokens(contact_name)
    if tokens:
        local_alpha = re.sub(r'[^a-z]', '', local)
        for tok in tokens:
            if len(tok) >= 3 and (tok in local_alpha or local_alpha.startswith(tok[:3])):
                return 'personal'

    # Initials+lastname pattern: 1-2 letters followed by longer string (jcannon, jsmith, eburnett)
    m = re.match(r'^([a-z]{1,2})([a-z]{3,})$', local_base)
    if m:
        return 'personal'

    # Short alphabetic string that could be a first name (≤12 chars, no numbers)
    if re.match(r'^[a-z]{2,12}$', local_base) and local_base not in ROLE_WORDS:
        return 'personal'

    return 'generic'


# ── Reoon bulk verify ──────────────────────────────────────────────────────────
def bulk_verify(emails: list) -> dict:
    """Submit emails to Reoon bulk API. Returns {email: {status, score}}."""
    unique = list(dict.fromkeys(e.lower().strip() for e in emails if e.strip()))
    if not unique:
        return {}
    print(f'  Submitting {len(unique)} emails to Reoon...')
    resp = requests.post(BULK_CREATE, json={'emails': unique, 'key': REOON_KEY}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if data.get('status') != 'success':
        raise RuntimeError(f"Bulk task failed: {data.get('reason')}")
    task_id = data['task_id']
    print(f'  Task {task_id} queued ({data["count_processing"]} emails)')
    interval = 5
    while True:
        time.sleep(interval)
        r = requests.get(BULK_RESULT, params={'key': REOON_KEY, 'task_id': task_id}, timeout=60)
        d = r.json()
        pct = d.get('progress_percentage', 0)
        print(f'  {d["count_checked"]}/{d["count_total"]} ({pct:.0f}%)', end='\r', flush=True)
        if d.get('status') == 'completed':
            print()
            break
        interval = min(interval * 1.5, 30)
    out = {}
    for em, res in d.get('results', {}).items():
        status = res.get('status', 'unknown')
        out[em.lower()] = {'status': status, 'score': BULK_SCORES.get(status, 0)}
    return out


# ── Serper re-enrichment ───────────────────────────────────────────────────────
def serper_search(query):
    global _serper_idx
    for attempt in range(3):
        try:
            r = requests.post(SERPER_URL,
                json={'q': query, 'num': 10},
                headers={'X-API-KEY': _serper_keys[_serper_idx], 'Content-Type': 'application/json'},
                timeout=(5, 15))
            if r.status_code in (401, 403):
                _serper_idx = min(_serper_idx + 1, len(_serper_keys) - 1)
                continue
            if r.status_code != 200: return {}
            return r.json()
        except Exception:
            time.sleep(3 * (attempt + 1))
    return {}

EMAIL_RE    = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_RE    = re.compile(r'(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}')
PHONE_NOISE = re.compile(r'^(?:0{7,}|1{7,}|\d{5,6}$)')
NOISE_DOMS  = {'gmail.com','yahoo.com','hotmail.com','outlook.com','icloud.com',
               'example.com','yourdomain.com','domain.com','wiza.co','hunter.io'}
GENERIC_PFX = ['info@','sales@','contact@','support@','admin@','mail@','hello@','store@',
               'shop@','service@','office@','noreply@','no-reply@','donotreply@']

def extract_from_serper(data, domain):
    texts = []
    for r in data.get('organic', []):
        texts += [r.get('snippet',''), r.get('title','')]
        for s in r.get('sitelinks',[]): texts.append(s.get('snippet',''))
    texts.append(data.get('knowledgeGraph',{}).get('description',''))
    full = ' '.join(filter(None, texts))
    domain_root = domain.split('.')[0] if domain else ''
    best_e = ''; generic_e = ''; best_p = ''
    for em in EMAIL_RE.findall(full):
        em = em.lower().strip('.,;:()')
        em_dom = em.split('@')[-1] if '@' in em else ''
        if not em_dom or em_dom in NOISE_DOMS: continue
        if domain_root and domain_root not in em_dom: continue
        if any(em.startswith(p) for p in GENERIC_PFX):
            if not generic_e: generic_e = em
            continue
        best_e = em; break
    if not best_e: best_e = generic_e
    for ph in PHONE_RE.findall(full):
        ph = re.sub(r'[^\d+]', '', ph)
        if len(ph) >= 10 and not PHONE_NOISE.match(ph):
            best_p = ph; break
    return best_e, best_p

def extract_domain(url):
    if not url: return ''
    url = re.sub(r'^https?://', '', url).strip('/')
    return re.sub(r'^www\.', '', url).split('/')[0].lower()

def reenrich_serper(name, domain):
    queries = [
        f'"{name}" owner email contact',
        f'site:{domain} email owner manager',
        f'{domain} contact email owner phone',
    ]
    email = ''; phone = ''
    for q in queries:
        data = serper_search(q)
        e, p = extract_from_serper(data, domain)
        if e and not email: email = e
        if p and not phone: phone = p
        if email and phone: break
        time.sleep(0.3)
    return email, phone


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # ── 1. Load all 197 emails ─────────────────────────────────────────────────
    personal_rows = list(csv.DictReader(open(BASE / 'personal_emails_verified.csv')))
    generic_rows  = list(csv.DictReader(open(BASE / 'generic_emails.csv')))

    print(f'Loaded {len(personal_rows)} personal (verified) + {len(generic_rows)} generic')

    # ── 2. Re-classify: some "personal" are actually generic ───────────────────
    true_personal = []
    moved_to_generic = []

    for r in personal_rows:
        email = r.get('contact_email','').strip()
        name  = r.get('contact_name','').strip()
        cat   = classify_email(email, name)
        if cat == 'personal':
            true_personal.append(r)
        else:
            moved_to_generic.append(r)

    print(f'\nRe-classification:')
    print(f'  True personal       : {len(true_personal)}')
    print(f'  Moved to generic    : {len(moved_to_generic)}')
    if moved_to_generic:
        for r in moved_to_generic:
            print(f'    → {r["contact_email"]}')

    # ── 3. Verify generic_emails.csv through Reoon ─────────────────────────────
    print(f'\nVerifying {len(generic_rows)} generic emails...')
    generic_emails_list = [r['contact_email'].strip() for r in generic_rows if r.get('contact_email','').strip()]
    moved_emails_list   = [r['contact_email'].strip() for r in moved_to_generic if r.get('contact_email','').strip() and r.get('best_email_score','') in ('','unknown/30')]

    to_verify = list(dict.fromkeys(generic_emails_list + moved_emails_list))
    gen_results = bulk_verify(to_verify) if to_verify else {}

    # ── 4. Re-enrich the 16 invalid personal emails ────────────────────────────
    invalid_rows = [r for r in personal_rows if r.get('best_email_score','').startswith('invalid')]
    print(f'\nRe-enriching {len(invalid_rows)} invalid emails via Serper...')
    reinrich_cache = {}
    for r in invalid_rows:
        name   = r['dealer_name']
        domain = extract_domain(r.get('website',''))
        if not domain: continue
        print(f'  {name[:40]:40s}', end='  ', flush=True)
        new_email, new_phone = reenrich_serper(name, domain)
        reinrich_cache[name] = {'email': new_email, 'phone': new_phone,
                                'old_email': r['contact_email'], 'domain': domain, 'listings': r['total_listings']}
        print(f'  → {new_email or "not found"} / {new_phone or "-"}')
        time.sleep(0.3)

    # Verify newly found emails
    new_emails = [v['email'] for v in reinrich_cache.values() if v['email']]
    print(f'\nVerifying {len(new_emails)} re-enriched emails...')
    reinrich_results = bulk_verify(new_emails) if new_emails else {}
    (BASE / 'cache/reinrich_results.json').write_text(json.dumps(reinrich_results, indent=2))

    # ── 5. Build output CSVs ───────────────────────────────────────────────────
    GOOD = {'safe', 'catch_all'}

    OUT_FIELDS = [
        'dealer_name','website','total_listings','new_listings','used_listings',
        'contact_name','contact_title','contact_email','contact_phone',
        'email_source','phone_source','name_source','email_status','email_score',
    ]

    def status_from_score(score_str):
        if not score_str: return '', ''
        parts = score_str.split('/')
        return parts[0], parts[1] if len(parts) > 1 else ''

    # Personal outbound: true personal, safe or catch_all
    outbound_personal = []
    for r in true_personal:
        status, score = status_from_score(r.get('best_email_score',''))
        if status in GOOD:
            outbound_personal.append({
                'dealer_name':   r['dealer_name'],
                'website':       r.get('website',''),
                'total_listings': r.get('total_listings',''),
                'new_listings':  r.get('new_listings',''),
                'used_listings': r.get('used_listings',''),
                'contact_name':  r.get('contact_name',''),
                'contact_title': r.get('contact_title',''),
                'contact_email': r.get('contact_email',''),
                'contact_phone': r.get('contact_phone',''),
                'email_source':  r.get('email_source',''),
                'phone_source':  r.get('phone_source',''),
                'name_source':   r.get('name_source',''),
                'email_status':  status,
                'email_score':   score,
            })

    # Also add re-enriched invalids that came back safe/catch_all
    for dealer, info in reinrich_cache.items():
        if not info['email']: continue
        res = reinrich_results.get(info['email'].lower(), {})
        if res.get('status') in GOOD:
            orig = next((r for r in invalid_rows if r['dealer_name'] == dealer), {})
            outbound_personal.append({
                'dealer_name':   dealer,
                'website':       f"https://{info['domain']}",
                'total_listings': info['listings'],
                'new_listings':  orig.get('new_listings',''),
                'used_listings': orig.get('used_listings',''),
                'contact_name':  orig.get('contact_name',''),
                'contact_title': orig.get('contact_title',''),
                'contact_email': info['email'],
                'contact_phone': info['phone'] or orig.get('contact_phone',''),
                'email_source':  'serper_rerun',
                'phone_source':  '',
                'name_source':   '',
                'email_status':  res['status'],
                'email_score':   str(res['score']),
            })

    outbound_personal.sort(key=lambda r: int(r.get('total_listings') or 0), reverse=True)

    # Generic outbound: all generic (original + moved from personal), safe or catch_all
    all_generic = []
    for r in generic_rows:
        email  = r.get('contact_email','').strip()
        res    = gen_results.get(email.lower(), {})
        status = res.get('status','')
        if status in GOOD:
            all_generic.append({
                'dealer_name':   r['dealer_name'],
                'website':       r.get('website',''),
                'total_listings': r.get('total_listings',''),
                'new_listings':  r.get('new_listings',''),
                'used_listings': r.get('used_listings',''),
                'contact_name':  r.get('contact_name',''),
                'contact_title': r.get('contact_title',''),
                'contact_email': email,
                'contact_phone': r.get('contact_phone',''),
                'email_source':  r.get('email_source',''),
                'phone_source':  r.get('phone_source',''),
                'name_source':   r.get('name_source',''),
                'email_status':  status,
                'email_score':   str(res.get('score','')),
            })

    for r in moved_to_generic:
        email  = r.get('contact_email','').strip()
        # Check original verified score first
        orig_status, orig_score = status_from_score(r.get('best_email_score',''))
        if orig_status in GOOD:
            status, score = orig_status, orig_score
        else:
            res    = gen_results.get(email.lower(), {})
            status = res.get('status','')
            score  = str(res.get('score',''))
        if status in GOOD:
            all_generic.append({
                'dealer_name':   r['dealer_name'],
                'website':       r.get('website',''),
                'total_listings': r.get('total_listings',''),
                'new_listings':  r.get('new_listings',''),
                'used_listings': r.get('used_listings',''),
                'contact_name':  r.get('contact_name',''),
                'contact_title': r.get('contact_title',''),
                'contact_email': email,
                'contact_phone': r.get('contact_phone',''),
                'email_source':  r.get('email_source',''),
                'phone_source':  r.get('phone_source',''),
                'name_source':   r.get('name_source',''),
                'email_status':  status,
                'email_score':   score,
            })

    all_generic.sort(key=lambda r: int(r.get('total_listings') or 0), reverse=True)

    # Write outputs
    for path, rows, label in [
        (BASE/'outbound_personal.csv', outbound_personal, 'outbound_personal'),
        (BASE/'outbound_generic.csv',  all_generic,       'outbound_generic'),
    ]:
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f'\n{label}.csv: {len(rows)} rows → {path}')

    # Re-enrichment results CSV
    reinrich_out = []
    for dealer, info in reinrich_cache.items():
        res = reinrich_results.get(info['email'].lower(), {}) if info['email'] else {}
        reinrich_out.append({
            'dealer_name':  dealer,
            'listings':     info['listings'],
            'old_email':    info['old_email'],
            'new_email':    info['email'],
            'new_phone':    info['phone'],
            'email_status': res.get('status','not_found'),
        })
    reinrich_out.sort(key=lambda r: int(r.get('listings') or 0), reverse=True)
    with open(BASE/'reinrich_results.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['dealer_name','listings','old_email','new_email','new_phone','email_status'])
        w.writeheader()
        w.writerows(reinrich_out)

    # ── Summary ────────────────────────────────────────────────────────────────
    from collections import Counter
    gen_statuses = Counter(gen_results.get(r.get('contact_email','').lower(),{}).get('status','unverified')
                           for r in generic_rows)
    print(f'\n{"="*55}')
    print(f'  FINAL OUTBOUND SUMMARY')
    print(f'{"="*55}')
    print(f'  outbound_personal.csv  : {len(outbound_personal):>4} (safe+catch_all personal)')
    print(f'  outbound_generic.csv   : {len(all_generic):>4} (safe+catch_all generic)')
    print(f'  phone_only.csv         :  155 (no email)')
    print(f'\n  Generic email verification:')
    for s, cnt in gen_statuses.most_common():
        print(f'    {s:<15} {cnt}')
    print(f'\n  Re-enrichment ({len(invalid_rows)} invalid):')
    found_new = sum(1 for v in reinrich_cache.values() if v["email"])
    good_new  = sum(1 for v in reinrich_cache.values()
                    if reinrich_results.get((v["email"] or "").lower(),{}).get("status") in GOOD)
    print(f'    New email found : {found_new}')
    print(f'    Safe/catch_all  : {good_new}')

if __name__ == '__main__':
    main()
