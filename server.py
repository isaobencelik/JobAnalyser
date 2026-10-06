"""
Job Analyser — LinkedIn Server
Fetches jobs directly from LinkedIn using your account credentials.
"""

from flask import Flask, request, jsonify, send_file, make_response
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor, as_completed
import time, os, json, re, random, datetime as _dt
import requests
from bs4 import BeautifulSoup
import db
import paths

app = Flask(__name__)
CORS(app, origins=['null', 'file://', 'http://localhost:5000', 'http://127.0.0.1:5000'])
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024   # 2 MB request cap (public safety; plenty for local use)

# ═══════════════════════════════════════════════════════════
#  GUEST MODE — no LinkedIn login, no password stored.
#  Jobs are read from LinkedIn's public guest endpoints.
#  Only the optional Gemini key (for AI summaries) is stored,
#  via env var or config.json (git-ignored — never commit it).
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_KEY")
AI_ENABLED     = True   # user can pause AI (keeps the key) so testing doesn't spend Gemini quota
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
#  PUBLIC MODE — set PUBLIC_MODE=1 when hosting for visitors
#  (e.g. jobanalyser.obencelik.com). The Gemini key comes ONLY
#  from the GEMINI_API_KEY secret, admin routes are switched off,
#  favorites/auto-collect are disabled (one shared server), and
#  LinkedIn + Gemini usage is rate-limited per visitor and capped
#  per day so a public demo can't run up a bill or get banned.
#  Unset (default) = your normal local app, behaviour unchanged.
PUBLIC_MODE        = os.environ.get('PUBLIC_MODE', '0') == '1'
PUBLIC_GEMINI_DAY  = int(os.environ.get('PUBLIC_GEMINI_DAILY_CAP', '300'))  # Gemini calls / day, all visitors
# ═══════════════════════════════════════════════════════════

CONFIG_FILE = os.path.join(paths.DATA_DIR, 'config.json')

def load_config():
    global GEMINI_API_KEY, AI_ENABLED
    if PUBLIC_MODE:
        return          # public: key comes from the environment secret only, never a file
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            GEMINI_API_KEY = cfg.get('gemini_key', cfg.get('groq_key', GEMINI_API_KEY))  # migrate groq_key
            AI_ENABLED     = bool(cfg.get('ai_enabled', True))
            print("  ✓ Config loaded from config.json")
        except Exception as e:
            print(f"  ⚠  Could not load config.json: {e}")

def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump({'gemini_key': GEMINI_API_KEY, 'ai_enabled': AI_ENABLED}, f, indent=2)

def _ai_active():
    """AI is usable only if a key is set AND the user hasn't paused it."""
    return AI_ENABLED and bool(GEMINI_API_KEY) and GEMINI_API_KEY != 'YOUR_GEMINI_KEY'

load_config()
db.init()


# ─── Public-mode guards ───────────────────────────────────
import threading as _threading
from collections import deque as _deque

# Routes a visitor must never reach on the shared public server.
_PUBLIC_BLOCKED = {
    '/config', '/config/ai-toggle', '/config/clear-key',   # change / clear / pause the owner's key
    '/data/reset', '/cache/clear',                         # wipe shared data
    '/auto/start', '/auto/stop',                           # background collector (runs forever, costs money)
    '/analyse-library',                                    # bulk-analyses the whole library in one click
    '/favorite', '/unfavorite',                            # one shared DB — favorites would mix visitors
    '/searches',                                           # would reveal other visitors' searches
}
# Read-only routes that would otherwise leak shared/admin state → answer with harmless empties.
_PUBLIC_EMPTY = {
    '/favorites':     lambda: {'favorites': []},
    '/favorites/ids': lambda: {'ids': []},
    '/auto/status':   lambda: {'searches': [], 'interval_min': 30, 'daily_cap': 0, 'max_hours': 0},
}
# Per-visitor rate limits: path -> (max requests, window seconds).
_PUBLIC_LIMITS = {
    '/stream':          (12, 600),     # LinkedIn searches
    '/search':          (12, 600),
    '/job/':            (120, 600),    # job detail fetches (LinkedIn)
    '/jobs/bulk':       (60, 600),
    '/analyse-batch':   (20, 600),     # Gemini
    '/analyse':         (20, 600),
    '/summarize':       (20, 600),
    '/estimate-salary': (30, 600),
}
_rl_hits = {}
_rl_lock = _threading.Lock()


def _client_ip():
    """Best-effort visitor IP behind Cloudflare / Google Cloud Run proxies."""
    return (request.headers.get('CF-Connecting-IP')
            or (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
            or request.remote_addr or '?')


def _rate_limited(path):
    rule = next(((p, lim) for p, lim in _PUBLIC_LIMITS.items()
                 if path == p or (p.endswith('/') and path.startswith(p))), None)
    if not rule:
        return False
    prefix, (limit, window) = rule
    key, now = (_client_ip(), prefix), time.time()
    with _rl_lock:
        q = _rl_hits.setdefault(key, _deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        if len(_rl_hits) > 5000:                     # bound memory
            _rl_hits.pop(next(iter(_rl_hits)))
    return False


@app.before_request
def _public_guard():
    if not PUBLIC_MODE:
        return None
    path = request.path
    if path in _PUBLIC_BLOCKED:
        return jsonify({'error': 'Not available in the public demo.'}), 403
    if path in _PUBLIC_EMPTY:
        return jsonify(_PUBLIC_EMPTY[path]())
    if _rate_limited(path):
        msg = 'You are going a bit fast for the public demo — please wait a few minutes.'
        if path in ('/stream', '/analyse-library'):  # EventSource clients read SSE, not JSON
            from flask import Response
            return Response(f'data: {json.dumps({"__error__": msg})}\n\n', mimetype='text/event-stream')
        return jsonify({'error': msg}), 429
    return None


_gemini_day = {'day': None, 'n': 0}
_gemini_lock = _threading.Lock()


def _gemini_budget_ok():
    """Public mode: global daily cap on Gemini calls (all visitors together)."""
    if not PUBLIC_MODE:
        return True
    today = _dt.date.today().isoformat()
    with _gemini_lock:
        if _gemini_day['day'] != today:
            _gemini_day['day'], _gemini_day['n'] = today, 0
        if _gemini_day['n'] >= PUBLIC_GEMINI_DAY:
            return False
        _gemini_day['n'] += 1
        return True

# City → (lat, lng) for map markers
CITY_COORDS = {
    'lisbon': (38.716, -9.139), 'porto': (41.157, -8.629),
    'london': (51.507, -0.127), 'manchester': (53.483, -2.244),
    'birmingham': (52.480, -1.903), 'edinburgh': (55.953, -3.188),
    'berlin': (52.520, 13.405), 'munich': (48.137, 11.576),
    'hamburg': (53.551, 9.993), 'frankfurt': (50.110, 8.682),
    'paris': (48.856, 2.352), 'lyon': (45.764, 4.836),
    'amsterdam': (52.370, 4.895), 'rotterdam': (51.923, 4.478),
    'madrid': (40.416, -3.703), 'barcelona': (41.385, 2.173),
    'rome': (41.902, 12.496), 'milan': (45.464, 9.190),
    'dublin': (53.333, -6.248), 'zurich': (47.376, 8.548),
    'geneva': (46.204, 6.143), 'stockholm': (59.334, 18.063),
    'oslo': (59.913, 10.752), 'copenhagen': (55.676, 12.568),
    'helsinki': (60.169, 24.938), 'vienna': (48.208, 16.373),
    'warsaw': (52.237, 21.017), 'prague': (50.075, 14.437),
    'budapest': (47.497, 19.040), 'brussels': (50.850, 4.352),
    'toronto': (43.651, -79.347), 'montreal': (45.508, -73.587),
    'new york': (40.712, -74.005), 'san francisco': (37.774, -122.419),
    'seattle': (47.606, -122.332), 'austin': (30.267, -97.743),
    'sydney': (-33.868, 151.209), 'melbourne': (-37.813, 144.963),
    'singapore': (1.352, 103.820), 'dubai': (25.204, 55.270),
    'remote': (20.0, 0.0),
}

def get_coords(location_str):
    loc = location_str.lower()
    for city, coords in CITY_COORDS.items():
        if city in loc:
            return coords
    return (None, None)


# ─── LinkedIn guest endpoints (no login required) ─────────
GUEST_SEARCH   = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
GUEST_DETAIL   = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"
GUEST_PAGES    = 4    # 10 cards per page (first load / each "Load more" block)
DETAIL_WORKERS = 2    # the detail endpoint rate-limits hard — keep concurrency low
LOAD_ALL_MAX_PAGES = 30      # safety cap for "Load all (past week)" — 30 pages ≈ 300 cards
LOAD_ALL_CUTOFF_S  = 8 * 86400  # stop paging once a whole page is older than 8 days
SWEEP_MAX_PAGES    = 10      # per (title,location) safety cap when sweeping a window to its cutoff
AI_PROMPT_VERSION  = 3       # bump whenever the analyse-batch prompt/schema changes →
                             # cached analyses from older versions are re-run automatically
                             # v3: red flags now inferred from the posting (not just explicit)
SWEEP_MAX_PAIRS    = 11      # max (title, location) combinations per sweep
SWEEP_MAX_TIERS    = 14      # max time windows per sweep
if PUBLIC_MODE:              # lighter sweeps on the shared server so LinkedIn doesn't block it
    SWEEP_MAX_PAGES, SWEEP_MAX_PAIRS, SWEEP_MAX_TIERS = 3, 4, 4
    LOAD_ALL_MAX_PAGES = 8


def _ai_stale(ai):
    """True if this analysis was produced by an older prompt version (still shown, but
    the client re-runs it in the background to upgrade it)."""
    return bool(ai and ai.get('_v') != AI_PROMPT_VERSION)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.linkedin.com/jobs",
}
_session = requests.Session()
_session.headers.update(_HEADERS)


def _guest_get(url, tries=4, stat=None):
    """GET with exponential back-off + jitter for transient 429/5xx (IP rate-limits).
    If `stat` dict is passed, sets stat['throttled']=True when a 429/5xx was hit."""
    for attempt in range(tries):
        try:
            r = _session.get(url, timeout=30)
        except requests.RequestException:
            if attempt < tries - 1:
                time.sleep(1.5 * (2 ** attempt) + random.random()); continue
            return None
        if r.status_code == 200:
            return r.text
        if r.status_code in (429, 500, 502, 503) and attempt < tries - 1:
            if stat is not None:
                stat['throttled'] = True
            wait = 1.5 * (2 ** attempt) + random.random()   # ~1.5s, 3s, 6s (+jitter)
            print(f"  ⏳ guest {r.status_code}; retry {attempt + 1}/{tries - 1} in {wait:.1f}s")
            time.sleep(wait); continue
        return None
    return None


# ─── Timestamp parsing (date attr + relative 'X ago' text) ─
_AGE_UNITS = {'second': 1, 'minute': 60, 'hour': 3600, 'day': 86400,
              'week': 604800, 'month': 2592000, 'year': 31536000}

def parse_relative_age(text):
    """'3 minutes ago' -> seconds since posting; None if unknown."""
    if not text:
        return None
    t = text.lower()
    if 'just now' in t or 'just posted' in t:
        return 0
    m = re.search(r'(\d+)\s*(second|minute|hour|day|week|month|year)', t)
    if m:
        return int(m.group(1)) * _AGE_UNITS[m.group(2)]
    if 'today' in t:
        return 0
    return None

def card_timestamp(datetime_attr, rel_text):
    """Epoch seconds for sorting. Keep minute/hour precision for fresh posts
    (from the relative text); otherwise use the exact date attribute."""
    now = time.time()
    age = parse_relative_age(rel_text)
    if age is not None and age < 86400:        # fresh: precise enough to stay on top
        return now - age
    if datetime_attr:
        try:
            d = _dt.datetime.strptime(datetime_attr, '%Y-%m-%d')
            return d.replace(tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            pass
    return (now - age) if age is not None else None


# ─── Guest scraping ───────────────────────────────────────
def _card_text(card, tag, cls):
    el = card.find(tag, class_=cls)
    return el.get_text(strip=True) if el else None

def guest_search_page(keywords, location, start, tpr=''):
    """Return card dicts from one guest search page (~10 results).
    tpr = LinkedIn time filter, e.g. 'r86400' (24h), 'r604800' (week), 'r2592000' (month)."""
    from urllib.parse import quote
    url = (f"{GUEST_SEARCH}?keywords={quote(keywords)}"
           f"&location={quote(location)}&start={start}")
    if tpr:
        url += f"&f_TPR={tpr}"
    html = _guest_get(url)
    if not html:
        return []
    soup  = BeautifulSoup(html, 'html.parser')
    cards = soup.find_all('div', class_='base-card')
    out = []
    for c in cards:
        urn = c.get('data-entity-urn', '')
        jid = urn.split('jobPosting:')[-1].strip() if 'jobPosting:' in urn else None
        if not jid or not jid.isdigit():
            continue
        logo_el = c.find('img', class_='artdeco-entity-image')
        link_el = c.find('a', class_='base-card__full-link')
        time_el = c.find('time')
        out.append({
            'id':       jid,
            'title':    _card_text(c, 'h3', 'base-search-card__title')    or 'Unknown Title',
            'company':  _card_text(c, 'h4', 'base-search-card__subtitle') or 'Unknown Company',
            'location': _card_text(c, 'span', 'job-search-card__location') or location or 'Unknown',
            'logo':     (logo_el.get('data-delayed-url') or logo_el.get('src')) if logo_el else None,
            'url':      (link_el.get('href', '').split('?')[0]) if link_el else None,
            'rel_text': time_el.get_text(strip=True) if time_el else None,
            'datetime': time_el.get('datetime') if time_el else None,
        })
    return out

def guest_search(keywords, location, pages=GUEST_PAGES, tpr='', start_page=0):
    """Fetch `pages` pages in parallel beginning at page `start_page`, de-dupe by id,
    preserve order. start_page>0 powers 'Load more' (the next block of older jobs)."""
    starts = [p * 10 for p in range(start_page, start_page + pages)]
    with ThreadPoolExecutor(max_workers=min(len(starts), 4)) as ex:
        pages_out = list(ex.map(lambda s: guest_search_page(keywords, location, s, tpr), starts))
    seen, cards = set(), []
    for page in pages_out:
        for card in page:
            if card['id'] not in seen:
                seen.add(card['id']); cards.append(card)
    return cards

_CRIT_KEYS = {
    'seniority level': 'seniority_level',
    'employment type': 'employment_type',
    'job function':    'job_function',
    'industries':      'industries',
}

def guest_detail(jid, stat=None):
    """Return description + the 4 labelled criteria from a job's detail page."""
    out = {'description': '', 'seniority_level': None, 'employment_type': None,
           'job_function': None, 'industries': None}
    time.sleep(random.uniform(0.2, 0.8))   # stagger to ease the detail endpoint's rate limit
    html = _guest_get(GUEST_DETAIL.format(jid=jid), stat=stat)
    if not html:
        return out
    soup = BeautifulSoup(html, 'html.parser')
    desc = soup.find('div', class_='show-more-less-html__markup')
    if desc:
        # Mark each list item with a bullet so the structure survives the flatten
        # to plain text (the front-end re-renders these into proper <ul> lists).
        for li in desc.find_all('li'):
            li.string = '• ' + li.get_text(' ', strip=True)
        out['description'] = desc.get_text('\n', strip=True)[:6000]
    for item in soup.find_all('li', class_='description__job-criteria-item'):
        label = item.find('h3')
        value = item.find('span')
        if label and value:
            key = _CRIT_KEYS.get(label.get_text(strip=True).lower())
            if key:
                val = value.get_text(strip=True)
                # LinkedIn returns "Not Applicable" when the poster left a field blank —
                # treat that (and empty) as missing so it's simply omitted from the UI.
                if val and val.lower() != 'not applicable':
                    out[key] = val
    return out

def infer_work_type(title, location, description):
    """Guest data has no structured workplace field — infer from text."""
    hay  = f"{title} {location}".lower()
    desc = (description or '').lower()
    if 'hybrid' in hay or 'hybrid' in desc:
        return 'hybrid'
    if ('remote' in hay or 'fully remote' in desc or '100% remote' in desc
            or 'fully-remote' in desc or 'work from home' in desc):
        return 'remote'
    return 'onsite'


_ALIAS_MAP = {
    'lisbon':    ['lisbon', 'lisboa', 'portugal'],
    'porto':     ['porto', 'oporto', 'portugal'],
    'london':    ['london', 'united kingdom', 'england'],
    'paris':     ['paris', 'france'],
    'berlin':    ['berlin', 'germany'],
    'amsterdam': ['amsterdam', 'netherlands'],
    'madrid':    ['madrid', 'spain'],
    'barcelona': ['barcelona', 'spain'],
    'milan':     ['milan', 'milano', 'italy'],
    'rome':      ['rome', 'roma', 'italy'],
}

def _build_job(card, detail, loc_aliases):
    """Build a serialisable job dict from a guest card + detail page.
    Returns None if the job is filtered out by location."""
    jid     = card['id']
    title   = card.get('title')    or 'Unknown Title'
    company = card.get('company')  or 'Unknown Company'
    loc_str = card.get('location') or 'Unknown'
    desc    = detail.get('description', '')

    work_type = infer_work_type(title, loc_str, desc)

    if loc_aliases:
        loc_match = any(a in loc_str.lower() for a in loc_aliases)
        if not loc_match and work_type != 'remote':
            return None   # filtered out

    ts         = card_timestamp(card.get('datetime'), card.get('rel_text'))
    apply_link = card.get('url') or f"https://www.linkedin.com/jobs/view/{jid}/"
    lat, lng   = get_coords(loc_str)

    return {
        'job_id':                  jid,
        'job_title':               title,
        'employer_name':           company,
        'employer_logo':           card.get('logo'),
        'employer_website':        None,
        'job_publisher':           'LinkedIn',
        'job_employment_type':     detail.get('employment_type') or '',
        'job_apply_link':          apply_link,
        'job_description':         desc,
        'job_is_remote':           work_type == 'remote',
        'work_arrangement':        work_type,
        'work_type_inferred':      True,
        'job_posted_at_timestamp': ts,
        'job_posted_relative':     card.get('rel_text'),
        'job_location':            loc_str,
        'job_city':                loc_str.split(',')[0].strip(),
        'job_country':             loc_str.split(',')[-1].strip() if ',' in loc_str else '',
        'job_latitude':            lat,
        'job_longitude':           lng,
        'job_min_salary':          0,
        'job_max_salary':          0,
        'job_salary_currency':     'EUR',
        'employer_reviews':        [],
        'apply_options':           [{'publisher': 'LinkedIn', 'apply_link': apply_link, 'is_direct': True}],
        'benefits_extended':       [],
        'preferred_technologies':  [],
        'required_technologies':   [],
        'seniority_level':         detail.get('seniority_level'),
        'job_function':            detail.get('job_function'),
        'job_industries':          detail.get('industries'),
        'required_experience_years': None,
        'visa_sponsorship':        None,
        'soft_skills':             [],
        'methodologies':           [],
        'job_highlights':          {},
    }

# ─── Gemini helper ────────────────────────────────────────
def _call_gemini(prompt, max_tokens=500, json_mode=False):
    """Calls Gemini via REST API using requests (already a project dependency).
    Raises RuntimeError('no_key') if key is missing."""
    import json as _json, traceback, requests as _requests

    if not _ai_active():
        raise RuntimeError('ai_disabled' if (GEMINI_API_KEY and GEMINI_API_KEY != 'YOUR_GEMINI_KEY') else 'no_key')
    if not _gemini_budget_ok():
        raise RuntimeError('daily_limit — the public demo has used today\'s AI allowance, try again tomorrow')

    model = 'gemini-3.1-flash-lite'
    url   = (f'https://generativelanguage.googleapis.com/v1beta/models/'
             f'{model}:generateContent?key={GEMINI_API_KEY}')

    body = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'maxOutputTokens': max_tokens, 'temperature': 0.1},
    }
    if json_mode:
        body['generationConfig']['responseMimeType'] = 'application/json'

    print(f'\n  → Gemini call: model={model} tokens={max_tokens} json={json_mode}')
    # Retry transient errors (503 overloaded / 429 rate-limit / 500) with backoff.
    TRANSIENT = (429, 500, 503)
    MAX_TRIES = 4
    for attempt in range(MAX_TRIES):
        try:
            resp = _requests.post(url, json=body, timeout=90)
            print(f'  ← Gemini response: HTTP {resp.status_code}')
            if resp.status_code == 200:
                data = resp.json()
                return data['candidates'][0]['content']['parts'][0]['text']
            if resp.status_code in TRANSIENT and attempt < MAX_TRIES - 1:
                wait = 2 ** attempt   # 1s, 2s, 4s
                print(f'  ⏳ transient {resp.status_code}; retry {attempt + 1}/{MAX_TRIES - 1} in {wait}s')
                time.sleep(wait)
                continue
            print(f'  ✗ Gemini error body: {resp.text[:500]}')
            raise RuntimeError(f'Gemini API error {resp.status_code}: {resp.text[:300]}')
        except _requests.RequestException as e:
            if attempt < MAX_TRIES - 1:
                wait = 2 ** attempt
                print(f'  ⏳ network error ({e}); retry {attempt + 1}/{MAX_TRIES - 1} in {wait}s')
                time.sleep(wait)
                continue
            print(f'\n  ✗ Gemini network error: {e}')
            traceback.print_exc()
            raise

_detail_cache  = {}   # {job_id: detail_dict} — populated during search, served on click
_search_cache  = {}   # {(query, location): {'jobs': [...], 'ts': float}} — 10-min TTL
SEARCH_CACHE_TTL = 600   # seconds
MAX_DETAIL_CACHE = 2000  # cap to stop unbounded memory growth
MAX_SEARCH_CACHE = 100

def _cap_cache(cache, max_size):
    """Evict oldest entries (dicts keep insertion order) to bound memory."""
    while len(cache) > max_size:
        cache.pop(next(iter(cache)))


# ─── Routes ───────────────────────────────────────────────
@app.route('/')
def index():
    """Serve the app so it runs on http:// (needed for custom protocol support)."""
    resp = make_response(send_file(paths.resource_path('job-analyser.html')))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/health')
def health():
    has_key = bool(GEMINI_API_KEY) and GEMINI_API_KEY != 'YOUR_GEMINI_KEY'
    return jsonify({
        'status':               'ok',
        'source':               'linkedin-guest',
        'mode':                 'guest',
        'public':               PUBLIC_MODE,
        'anthropic_configured': _ai_active(),
        'has_key':              has_key,
        'ai_disabled':          has_key and not AI_ENABLED,
    })

@app.route('/config', methods=['POST'])
def update_config():
    """Guest mode: only the optional Gemini key is stored — no LinkedIn login."""
    global GEMINI_API_KEY
    body = request.get_json(force=True) or {}
    new_gemini = (body.get('gemini_key') or '').strip()
    if new_gemini:
        GEMINI_API_KEY = new_gemini
    save_config()
    print("\n  ✓ Config updated (Gemini key)")
    return jsonify({
        'status':               'ok',
        'mode':                 'guest',
        'anthropic_configured': _ai_active(),
    })

@app.route('/config/ai-toggle', methods=['POST'])
def ai_toggle():
    """Pause/resume AI without deleting the key — so testing doesn't spend Gemini quota."""
    global AI_ENABLED
    body = request.get_json(force=True) or {}
    AI_ENABLED = bool(body.get('enabled', not AI_ENABLED))
    save_config()
    print(f"\n  ✓ AI {'enabled' if AI_ENABLED else 'paused (key kept)'}")
    return jsonify({'status': 'ok', 'ai_enabled': AI_ENABLED, 'anthropic_configured': _ai_active()})

@app.route('/config/clear-key', methods=['POST'])
def clear_key():
    """Clear a single optional API key without requiring LinkedIn re-auth."""
    global GEMINI_API_KEY
    body = request.get_json(force=True) or {}
    key  = (body.get('key') or '').strip()
    if key in ('gemini_key', 'groq_key'):  # accept both during migration
        GEMINI_API_KEY = ''
    else:
        return jsonify({'error': 'unknown key — use gemini_key'}), 400
    save_config()
    print(f"  ✓ Cleared {key}")
    return jsonify({'status': 'ok'})


@app.route('/cache/clear', methods=['POST'])
def clear_search_cache():
    """Empty the server-side search/detail caches so the next fetch is fully live."""
    n = len(_search_cache) + len(_sweep_cache)
    _search_cache.clear()
    _sweep_cache.clear()
    _detail_cache.clear()
    print(f"  ✓ Cleared server cache ({n} searches)")
    return jsonify({'status': 'ok', 'cleared': n})


@app.route('/data/reset', methods=['POST'])
def data_reset():
    """Wipe the persistent database (cached jobs + favorites) and in-memory caches —
    a full clean slate, e.g. when handing the tool to a different user."""
    db.reset_all()
    _search_cache.clear()
    _sweep_cache.clear()
    _detail_cache.clear()
    return jsonify({'status': 'ok'})


# ─── Library / history ────────────────────────────────────
@app.route('/library')
def library():
    """Every job ever fetched (card + AI), newest posting first — the History page."""
    return jsonify({'jobs': db.library_list()})

@app.route('/analyse-library')
def analyse_library():
    """Analyse EVERY library job that doesn't yet have current-version AI — fetching any missing
    descriptions first. Streams progress (SSE). Lets you backfill jobs collected while AI was paused."""
    from flask import Response, stream_with_context

    def gen():
        if not _ai_active():
            yield 'data: {"__error__":"AI is off — add a key and resume AI first."}\n\n'
            return
        jobs = db.library_list(limit=5000)
        todo = [j for j in jobs if not (isinstance(j.get('ai'), dict) and j['ai'].get('_v') == AI_PROMPT_VERSION)]
        total = len(todo)
        print(f"\n  /analyse-library: {total} jobs need analysis")
        yield f'data: {{"__total__":{total}}}\n\n'
        done, batch = 0, []
        try:
            for j in todo:
                jid = j.get('job_id')
                d = _detail_cache.get(jid) or db.cache_get(jid)
                if not d or not d.get('description'):
                    d = guest_detail(jid)
                    if d.get('description'):
                        _detail_cache[jid] = d
                        db.cache_put(jid, d)
                desc = (d or {}).get('description') or ''
                if desc:
                    batch.append({'job_id': jid, 'title': j.get('job_title') or '',
                                  'company': j.get('employer_name') or '',
                                  'location': j.get('job_location') or '',
                                  'work_type': j.get('work_arrangement') or '',
                                  'description': desc})
                done += 1
                if len(batch) >= 15:
                    try: _analyse_and_persist(batch)
                    except Exception as e: print(f"  analyse-library batch error: {e}")
                    batch = []
                    yield f'data: {{"__progress__":{done},"total":{total}}}\n\n'
            if batch:
                try: _analyse_and_persist(batch)
                except Exception as e: print(f"  analyse-library batch error: {e}")
            print(f"  /analyse-library done: processed {done}")
            yield f'data: {{"__progress__":{done},"total":{total}}}\n\n'
            yield 'data: {"__done__":true}\n\n'
        except Exception as e:
            import traceback; traceback.print_exc()
            yield f'data: {json.dumps({"__error__": str(e)})}\n\n'

    return Response(stream_with_context(gen()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/searches')
def searches():
    """Recent searches, for the History page's strip."""
    return jsonify({'searches': db.recent_searches()})


# ─── Favorites ────────────────────────────────────────────
@app.route('/favorites')
def favorites_list():
    """All starred jobs (full snapshots)."""
    return jsonify({'favorites': db.fav_list()})

@app.route('/favorites/ids')
def favorites_ids():
    """Just the starred job ids — for marking search results."""
    return jsonify({'ids': db.fav_ids()})

@app.route('/favorite', methods=['POST'])
def favorite_add():
    body = request.get_json(force=True) or {}
    job  = body.get('job') or {}
    jid  = str(job.get('jobId') or job.get('job_id') or '').strip()
    if not jid:
        return jsonify({'error': 'job with a jobId is required'}), 400
    ok = db.fav_add(jid, job)
    print(f"  ★ Favorited {jid} ({job.get('title','')[:40]})")
    return jsonify({'status': 'ok' if ok else 'error', 'job_id': jid})

@app.route('/unfavorite', methods=['POST'])
def favorite_remove():
    body = request.get_json(force=True) or {}
    jid  = str(body.get('job_id') or '').strip()
    if not jid:
        return jsonify({'error': 'job_id is required'}), 400
    db.fav_remove(jid)
    print(f"  ☆ Unfavorited {jid}")
    return jsonify({'status': 'ok', 'job_id': jid})


@app.route('/search')
def search():
    """Blocking variant of /stream — returns all results in one JSON batch."""
    query    = request.args.get('q', '').strip()
    location = request.args.get('location', '').strip()
    tpr      = request.args.get('tpr', '').strip()
    if not query:
        return jsonify({'error': 'q param is required'}), 400

    cache_key = (query.lower(), location.lower(), tpr)
    cached    = _search_cache.get(cache_key)
    if cached and time.time() - cached['ts'] < SEARCH_CACHE_TTL:
        print(f"  /search cache hit for '{query}'")
        return jsonify({'data': cached['jobs'], 'total': len(cached['jobs']), 'source': 'linkedin-guest'})

    try:
        loc_aliases = set(_ALIAS_MAP.get(location.lower(), [location.lower()])) if location else set()
        print(f"\n  /search live: '{query}' in '{location or 'anywhere'}' (tpr={tpr or 'any'})")
        cards = guest_search(query, location or '', tpr=tpr)

        jobs = [j for j in (_build_job(c, {}, loc_aliases) for c in cards) if j]

        _search_cache[cache_key] = {'jobs': jobs, 'ts': time.time()}
        _cap_cache(_search_cache, MAX_SEARCH_CACHE)
        print(f"  /search: {len(jobs)} jobs returned")
        return jsonify({'data': jobs, 'total': len(jobs), 'source': 'linkedin-guest'})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ─── Query expansion (related-title search) ───────────────
_synonym_cache = {}   # query.lower() -> [synonym titles]  (one Gemini call, then cached)
_sweep_cache    = {}  # (query|loc|tpr|expand) -> {ts, jobs, titles, locs}: replay a live sweep
SWEEP_CACHE_TTL = 600 # seconds. ABSOLUTE — a cache hit does NOT reset the timer; after this the
                      # next search re-hammers LinkedIn to pick up new postings.


def _seniority_variants(q):
    """Generic, domain-free: add Senior/Junior unless the query already has a level."""
    ql = q.lower()
    out = []
    if not any(w in ql for w in ('senior', 'junior', 'lead', 'principal', 'head ', 'intern', 'staff', 'mid ')):
        for pre in ('Senior', 'Junior'):
            out.append(f"{pre} {q}")
    return out


def _ai_synonyms(q):
    """Keyword-anchored title variants. The AI finds the query's CORE keyword (e.g. "Product
    Owner"→"Product", "SAP Consultant"→"SAP") and proposes real titles built by adding qualifier
    words around it. We then KEEP ONLY titles that actually contain that core keyword as a whole
    word — so a "Product" search can surface "Data Product Manager" (related) but never an
    "SAP Consultant" (off-topic). The keyword itself is the noise filter."""
    import re as _re
    key = q.lower().strip()
    if key in _synonym_cache:
        return _synonym_cache[key]
    out = []
    if _ai_active():
        try:
            import json as _json
            prompt = (
                f'For the job title "{q}" return ONLY JSON: {{"core": "...", "titles": ["...", ...]}}.\n'
                f'"core" = the central domain keyword(s) of the title — drop the trailing role word '
                f'(Owner/Manager/Consultant/Engineer/Analyst/Specialist/Lead/Developer). Examples: '
                f'"Product Owner"→"Product"; "Data Product Owner"→"Data Product"; "SAP Consultant"→"SAP"; '
                f'"SAP Onboarding Consultant"→"SAP Onboarding"; "Frontend Developer"→"Frontend".\n'
                f'"titles" = up to 6 real job titles that ALL contain "core" verbatim, formed by adding '
                f'qualifier words before/after it (e.g. Senior, Lead, Principal, Staff, Technical, Digital, '
                f'Data, plus role words like Manager/Owner/Specialist). EVERY title must contain core. '
                f'Never include a title from a different domain.')
            text = _call_gemini(prompt, max_tokens=200, json_mode=True)
            data = _json.loads(text)
            core = (data.get('core') or '').strip() if isinstance(data, dict) else ''
            raw  = (data.get('titles') if isinstance(data, dict) else data) or []
            if not core:                                   # fallback: query minus its last (role) word
                parts = q.split()
                core = ' '.join(parts[:-1]) if len(parts) > 1 else q
            pat = _re.compile(r'\b' + _re.escape(core.lower()) + r'\b')
            for t in raw:
                t = str(t).strip()
                if t and t.lower() != key and pat.search(t.lower()):
                    out.append(t)
            out = out[:5]
        except Exception as e:
            print(f"  synonym gen error: {e}")
        _synonym_cache[key] = out      # cache only when AI actually ran — don't poison with [] while paused
    return out


def _expand_query(q):
    """Base query + AI synonyms + Senior/Junior of the base AND of each synonym.
    The senior forms matter: a specific title like "Senior Product Manager" surfaces a
    "Senior Data Product Manager" that a broad "Product Manager" buries in LinkedIn's ranking —
    so "Product Owner" should also fire "Senior Product Manager". De-duped, capped."""
    variants, seen = [], set()
    def add(t):
        t = (t or '').strip()
        if t and t.lower() not in seen:
            seen.add(t.lower()); variants.append(t)
    add(q)
    syns = _ai_synonyms(q)
    for s in syns:
        add(s)
    sen = []
    for base in [q] + syns:                       # seniority of base AND each synonym
        sen.extend(_seniority_variants(base))
    for v in sen:                                 # Senior forms first — the high-value catchers
        if v.lower().startswith('senior'):
            add(v)
    for v in sen:
        if v.lower().startswith('junior'):
            add(v)
    return variants[:7]


# A major city's guest search misses surrounding municipalities (e.g. "Lisbon" excludes
# Amadora/Oeiras). For known metros we also search the neighbouring towns and merge.
_METRO_MAP = {
    'lisbon': ['Lisbon', 'Amadora', 'Oeiras', 'Cascais', 'Sintra', 'Almada', 'Loures', 'Odivelas'],
    'lisboa': ['Lisbon', 'Amadora', 'Oeiras', 'Cascais', 'Sintra', 'Almada', 'Loures', 'Odivelas'],
    'porto':  ['Porto', 'Matosinhos', 'Vila Nova de Gaia', 'Maia', 'Gondomar', 'Valongo'],
}


def _expand_location(loc):
    """A metro's neighbouring municipalities (incl. the city itself), else just the typed value."""
    key = (loc or '').strip().lower()
    for k, towns in _METRO_MAP.items():
        if k in key:
            return towns[:4]
    return [loc] if loc else ['']


def _sweep_tiers(sel_days):
    """Nested windows to sweep so nothing is missed: DAILY for the first week (1..7), then WEEKLY
    out to the selected horizon. Each day/week gets a window where its jobs rank near the top.
    Bounded to ~14 passes so a deep horizon can't explode forever."""
    tiers = list(range(1, min(sel_days, 7) + 1))     # 1,2,…,7 — every day
    d = 14
    while d < sel_days:                              # then 14, 21, 28, … — every week
        tiers.append(d); d += 7
    if not tiers or tiers[-1] != sel_days:
        tiers.append(sel_days)
    tiers = tiers[:SWEEP_MAX_TIERS]
    if tiers[-1] != sel_days:                        # always finish exactly at the selected horizon
        tiers[-1] = sel_days
    return tiers


@app.route('/stream')
def stream_search():
    """SSE endpoint — streams jobs one-by-one as detail fetches complete.
    First job appears in ~1s; full set in 2-5s.  Results are cached."""
    from flask import Response, stream_with_context

    query    = request.args.get('q', '').strip()
    location = request.args.get('location', '').strip()
    tpr      = request.args.get('tpr', '').strip()
    force    = request.args.get('force', '0') == '1'
    mode     = request.args.get('mode', '').strip()        # 'all' = sweep the window back to the cutoff
    expand   = request.args.get('expand', '0') == '1'      # also search related/synonym + seniority titles
    try:
        start = max(0, int(request.args.get('start', '0')))  # card offset for "Load more"
    except ValueError:
        start = 0
    try:
        cutoff_s = max(1, int(request.args.get('cutoff', '8'))) * 86400  # how far back 'all' pages (days→sec)
    except ValueError:
        cutoff_s = LOAD_ALL_CUTOFF_S
    if not query:
        return jsonify({'error': 'q param is required'}), 400

    loc_aliases = set(_ALIAS_MAP.get(location.lower(), [location.lower()])) if location else set()
    cache_key   = (query.lower(), location.lower(), tpr)

    def generate():
        # ══ Window sweep — EVERY (title, location) variant is paged until jobs cross the window
        #    cutoff (so "Past month" really pulls the whole month). Titles expand (AI synonyms +
        #    Senior/Junior) unless exact-match; locations expand to nearby municipalities. The
        #    dropdown window sets the cutoff; "+1 month" re-runs this with a larger one. ══
        locs = _expand_location(location)
        if start == 0 and mode != 'all':
            try:
                # Replay the last live sweep if it's still fresh (absolute 10-min TTL — a cache hit
                # does NOT reset the clock). Skips LinkedIn entirely AND the synonym Gemini call.
                ck  = (query.lower(), location.lower(), tpr, '1' if expand else '0')
                hit = _sweep_cache.get(ck)
                if hit and time.time() - hit['ts'] < SWEEP_CACHE_TTL:
                    print(f"  /stream sweep CACHE hit ({int(time.time() - hit['ts'])}s old): {len(hit['jobs'])} jobs")
                    yield f'data: {json.dumps({"__variants__": hit["titles"], "__locations__": hit["locs"]})}\n\n'
                    for job in hit['jobs']:
                        db.card_put(job['job_id'], job)       # refresh last-seen
                        yield f"data: {json.dumps(job)}\n\n"
                    yield 'data: {"__done__":true,"cached":true}\n\n'
                    return

                # Overlap the (slow) synonym Gemini call with the deterministic sweep: kick it off in a
                # thread, sweep the base + Senior/Junior titles right away, then sweep the synonyms once
                # they're ready (by then they're cached, so _expand_query is instant).
                syn_thread = None
                if expand:
                    import threading
                    syn_thread = threading.Thread(target=lambda: _ai_synonyms(query), daemon=True)
                    syn_thread.start()

                sel_days = max(1, cutoff_s // 86400)
                tiers    = _sweep_tiers(sel_days)     # daily for the week, weekly for the month
                fetch_ts = time.time()                # cache timer anchors to when we queried LinkedIn
                seen, total, now, swept = set(), 0, fetch_ts, []

                # Nested windows + (primary city × all titles, towns × base title), for one title set.
                def _do_sweep(title_list):
                    nonlocal total
                    pairs, paired = [], set()
                    for li, loc in enumerate(locs):
                        for ti, t in enumerate(title_list):
                            if li == 0 or ti == 0:
                                kk = (t.lower(), (loc or '').lower())
                                if kk not in paired:
                                    paired.add(kk); pairs.append((t, loc))
                    pairs = pairs[:SWEEP_MAX_PAIRS]
                    primary_pairs = [(t, locs[0]) for t in title_list]
                    for d in tiers:
                        win_tpr   = f"r{d * 86400}"
                        win_cut   = d * 86400 + 86400         # 1-day grace at the boundary
                        win_pairs = pairs if d == sel_days else primary_pairs
                        for (t, loc) in win_pairs:
                            for p in range(SWEEP_MAX_PAGES):
                                page = guest_search_page(t, loc or '', p * 10, tpr=win_tpr)
                                if not page:
                                    break
                                known = db.known_ids([c['id'] for c in page])
                                any_in_window = False
                                for card in page:
                                    if card['id'] in seen:
                                        continue
                                    seen.add(card['id'])
                                    ts = card_timestamp(card.get('datetime'), card.get('rel_text'))
                                    if ts is not None and ts < now - win_cut:
                                        continue
                                    any_in_window = True
                                    job = _build_job(card, {}, loc_aliases)
                                    if job:
                                        job['_new'] = job['job_id'] not in known
                                        db.card_put(job['job_id'], job)
                                        swept.append(job)
                                        total += 1
                                        yield f"data: {json.dumps(job)}\n\n"
                                if not any_in_window:
                                    break
                                time.sleep(0.25)
                            yield f'data: {{"__progress__":{total}}}\n\n'

                # Phase 1 — deterministic titles (base + Senior/Junior of base): start immediately.
                det_titles = [query] + (_seniority_variants(query) if expand else [])
                print(f"\n  /stream sweep: det={det_titles} locs={locs} cutoff {sel_days}d")
                yield f'data: {json.dumps({"__variants__": det_titles, "__locations__": locs})}\n\n'
                for chunk in _do_sweep(det_titles):
                    yield chunk

                # Phase 2 — synonyms (now cached) + their seniority forms.
                if syn_thread:
                    syn_thread.join()
                full_titles = _expand_query(query) if expand else [query]
                det_lower   = {t.lower() for t in det_titles}
                syn_titles  = [t for t in full_titles if t.lower() not in det_lower]
                if syn_titles:
                    print(f"  /stream sweep: syn={syn_titles}")
                    yield f'data: {json.dumps({"__variants__": full_titles, "__locations__": locs})}\n\n'
                    for chunk in _do_sweep(syn_titles):
                        yield chunk

                _sweep_cache[ck] = {'ts': fetch_ts, 'jobs': swept, 'titles': full_titles, 'locs': locs}
                _cap_cache(_sweep_cache, MAX_SEARCH_CACHE)
                db.search_log(query, location, tpr, total)
                print(f"  /stream sweep done: {total} jobs, windows={tiers}d")
                yield 'data: {"__done__":true,"expanded":true}\n\n'
            except Exception as e:
                import traceback; traceback.print_exc()
                yield f'data: {json.dumps({"__error__": str(e)})}\n\n'
            return

        # ══ "Load all (past week)" — page until a whole page is older than 8 days ══
        if mode == 'all':
            try:
                print(f"\n  /stream load-all: '{query}' in '{location or 'anywhere'}' (tpr={tpr or 'any'})")
                seen, total, now = set(), 0, time.time()
                for p in range(LOAD_ALL_MAX_PAGES):
                    page = guest_search_page(query, location or '', p * 10, tpr=tpr)
                    if not page:
                        break
                    known = db.known_ids([c['id'] for c in page])
                    any_in_window = False
                    for card in page:
                        if card['id'] in seen:
                            continue
                        seen.add(card['id'])
                        ts = card_timestamp(card.get('datetime'), card.get('rel_text'))
                        if ts is not None and ts < now - cutoff_s:
                            continue                       # older than the requested horizon — drop it
                        any_in_window = True
                        job = _build_job(card, {}, loc_aliases)
                        if job:
                            job['_new'] = job['job_id'] not in known
                            db.card_put(job['job_id'], job)
                            total += 1
                            yield f"data: {json.dumps(job)}\n\n"
                    yield f'data: {{"__progress__":{total}}}\n\n'
                    if not any_in_window:
                        break                              # whole page past the 8-day line — stop
                    time.sleep(0.35)                       # be polite between pages
                db.search_log(query, location, tpr, total)
                print(f"  /stream load-all done: {total} jobs within {cutoff_s // 86400} days")
                yield 'data: {"__done__":true,"all":true}\n\n'
            except Exception as e:
                import traceback; traceback.print_exc()
                yield f'data: {json.dumps({"__error__": str(e)})}\n\n'
            return

        # ══ "Load more" — the next block of (older) pages at an offset; always live ══
        if start > 0:
            try:
                print(f"  /stream load-more from card {start} for '{query}'")
                cards = guest_search(query, location or '', tpr=tpr, start_page=start // 10)
                known = db.known_ids([c['id'] for c in cards])
                n = 0
                for card in cards:
                    job = _build_job(card, {}, loc_aliases)
                    if job:
                        job['_new'] = job['job_id'] not in known
                        db.card_put(job['job_id'], job)
                        n += 1
                        yield f"data: {json.dumps(job)}\n\n"
                print(f"  /stream load-more: {n} more cards")
                yield 'data: {"__done__":true,"more":true}\n\n'
            except Exception as e:
                import traceback; traceback.print_exc()
                yield f'data: {json.dumps({"__error__": str(e)})}\n\n'
            return

        # ══ Normal first load — serve from cache (unless forced) ══
        if not force:
            cached = _search_cache.get(cache_key)
            if cached and time.time() - cached['ts'] < SEARCH_CACHE_TTL:
                print(f"  /stream cache hit for '{query}'")
                for job in cached['jobs']:
                    db.card_put(job['job_id'], job)
                    yield f"data: {json.dumps(job)}\n\n"
                db.search_log(query, location, tpr, len(cached['jobs']))
                yield 'data: {"__done__":true,"cached":true}\n\n'
                return

        # ── Live fetch from the guest endpoints ───────────────
        try:
            print(f"\n  /stream live: '{query}' in '{location or 'anywhere'}' (tpr={tpr or 'any'}, force={force})")
            cards = guest_search(query, location or '', tpr=tpr)
            print(f"  Found {len(cards)} cards — details load on click (avoids rate limits)")
            known = db.known_ids([c['id'] for c in cards])   # which did we already have? (for "new" detection)

            jobs_out = []
            for card in cards:                       # build from card data only — no detail fetch
                job = _build_job(card, {}, loc_aliases)
                if job:
                    job['_new'] = job['job_id'] not in known
                    jobs_out.append(job)
                    db.card_put(job['job_id'], job)  # add to the persistent library
                    yield f"data: {json.dumps(job)}\n\n"

            _search_cache[cache_key] = {'jobs': jobs_out, 'ts': time.time()}
            _cap_cache(_search_cache, MAX_SEARCH_CACHE)
            db.search_log(query, location, tpr, len(jobs_out))
            print(f"  /stream done: {len(jobs_out)} jobs sent")
            yield 'data: {"__done__":true}\n\n'

        except Exception as e:
            import traceback; traceback.print_exc()
            yield f'data: {json.dumps({"__error__": str(e)})}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype  = 'text/event-stream',
        headers   = {
            'Cache-Control':       'no-cache',
            'X-Accel-Buffering':   'no',
            # Served from the same origin (localhost:5000); flask-cors already
            # whitelists localhost/127.0.0.1. No wildcard needed.
            'Access-Control-Allow-Origin': 'http://localhost:5000',
        }
    )


@app.route('/job/<jid>')
def job_detail(jid):
    """Return description + criteria for a job — served from cache when available."""
    try:
        d = _detail_cache.get(jid)          # in-memory cache (this session)
        throttled = False
        if not d:
            d = db.cache_get(jid)           # persistent disk cache (instant, no LinkedIn hit)
            if d:
                _detail_cache[jid] = d
        if not d:
            stat = {'throttled': False}
            d = guest_detail(jid, stat)     # live fetch from LinkedIn (rate-limited)
            throttled = stat['throttled']
            if d.get('description'):
                _detail_cache[jid] = d
                db.cache_put(jid, d)        # persist so it's instant next time
        if not d or not d.get('description'):
            return jsonify({'error': 'Job not found', 'throttled': throttled}), 404

        _cached_ai = db.ai_get(jid)
        return jsonify({
            'job_id':                  jid,
            'job_description':         (d.get('description') or '')[:5000],
            'job_min_salary':          0,
            'job_max_salary':          0,
            'job_salary_currency':     'EUR',
            'job_apply_link':          f"https://www.linkedin.com/jobs/view/{jid}/",
            'seniority_level':         d.get('seniority_level'),
            'job_function':            d.get('job_function'),
            'job_industries':          d.get('industries'),
            'job_employment_type':     d.get('employment_type'),
            'visa_sponsorship':        None,
            'benefits_extended':       [],
            'preferred_technologies':  [],
            'throttled':               throttled,
            'ai':                      _cached_ai,       # cached AI — shown even if from an older prompt
            'ai_stale':                _ai_stale(_cached_ai),  # …but flag it so the client re-runs it
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/jobs/bulk', methods=['POST'])
def jobs_bulk():
    """Return cached detail + AI for many job_ids at once (pure DB reads, no LinkedIn hits).
    Lets the client fill in every already-fetched job instantly, instead of one paced fetch
    each. Un-cached ids are simply omitted — the client fetches those live."""
    body = request.get_json(force=True) or {}
    ids  = [str(i) for i in (body.get('ids') or [])][:400]
    out  = {}
    for jid in ids:
        d = _detail_cache.get(jid) or db.cache_get(jid)
        if not d or not d.get('description'):
            continue                      # not cached — leave it for the live wave
        _detail_cache.setdefault(jid, d)
        ai = db.ai_get(jid)
        out[jid] = {
            'job_description':     (d.get('description') or '')[:5000],
            'seniority_level':     d.get('seniority_level'),
            'job_function':        d.get('job_function'),
            'job_industries':      d.get('industries'),
            'job_employment_type': d.get('employment_type'),
            'ai':                  ai,
            'ai_stale':            _ai_stale(ai),
        }
    return jsonify({'jobs': out})


@app.route('/analyse', methods=['POST'])
def analyse():
    """Single Gemini call returning 3-bullet summary + salary estimate."""
    import json as _json
    body      = request.get_json(force=True) or {}
    desc      = (body.get('description') or '').strip()[:4000]
    title     = (body.get('title')       or '').strip()
    company   = (body.get('company')     or '').strip()
    location  = (body.get('location')    or 'Lisbon, Portugal').strip()
    work_type = (body.get('work_type')   or '').strip()

    if not desc:
        return jsonify({'error': 'empty description'}), 400

    prompt = f"""You are an experienced recruiter and compensation analyst for the European technology job market.

Analyse this job posting and return ONLY valid JSON with these exact fields:

{{
  "do":           "Primary business outcome this role exists to achieve, then main day-to-day activity (max 15 words)",
  "need":         "The 1-2 genuine must-have requirements (max 15 words)",
  "flag":         "One notable perk, genuine concern, or unique detail (max 15 words)",
  "must_have":    ["Requirement that would eliminate a candidate if missing (max 10 words)"],
  "nice_to_have": ["Preferred qualification that is not blocking (max 10 words)"],
  "salary_min":   45000,
  "salary_max":   60000,
  "salary_note":  "brief reason for estimate (max 12 words)",
  "skills":       ["Skill1", "Skill2", "Skill3"],
  "seniority":    "mid",
  "red_flags":    ["Concern directly evidenced in the posting (max 10 words)"],
  "green_flags":  ["Positive signal from the posting (max 10 words)"],
  "culture":      "startup"
}}

Rules:
- do: start with the business outcome (e.g. "Drive growth of X" not "Attend meetings about X")
- must_have: 1-3 items. Only requirements whose absence would likely eliminate the candidate.
- nice_to_have: 1-3 items. Explicitly preferred but not required qualifications.
- salary: gross annual EUR for the role location. Multinationals 20-40% more than local SMEs. Remote +10-20%. Integers only.
- skills: up to 6 specific tools/languages/frameworks explicitly mentioned or strongly implied. No soft skills.
- seniority: junior / mid / senior / lead / executive
- red_flags: 0-3 concerns a candidate would note, INFERRED from the posting even if framed positively (long must-have list, "fast-paced/high-pressure", on-call/outside-hours, contract/short-term, no salary, vague scope, heavy travel, on-site only, senior demands under a junior title). [] only if genuinely clean.
- green_flags: 0-3 items. Only genuine positives from the posting. Do not invent.
- culture: startup / scale-up / corporate / enterprise

Job: {title} at {company}
Location: {location}
Work arrangement: {work_type or 'not specified'}

<JOB_POSTING>
{desc}
</JOB_POSTING>"""

    try:
        text   = _call_gemini(prompt, max_tokens=600, json_mode=True)
        result = _json.loads(text)
        mn = int(result.get('salary_min', 0))
        mx = int(result.get('salary_max', 0))
        if mn <= 0 or mx <= 0 or mn > mx:
            result['salary_min'] = result['salary_max'] = 0
        else:
            result['salary_min'] = mn
            result['salary_max'] = mx
        for key in ('skills', 'red_flags', 'green_flags', 'must_have', 'nice_to_have'):
            if not isinstance(result.get(key), list):
                result[key] = []
        valid_cultures  = {'startup', 'scale-up', 'corporate', 'enterprise'}
        valid_seniority = {'junior', 'mid', 'senior', 'lead', 'executive'}
        if result.get('culture')   not in valid_cultures:   result['culture']   = None
        if result.get('seniority') not in valid_seniority:  result['seniority'] = None
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _analyse_and_persist(jobs):
    """Run the batch Gemini analysis over job dicts (title/company/location/work_type/
    description), persist each result by job_id (stamped with the prompt version), and
    return the results list. Shared by the /analyse-batch route AND the auto-collector.
    Raises on Gemini failure."""
    import json as _json
    job_lines = ''
    for i, j in enumerate(jobs):
        desc = (j.get('description') or '')[:4500]   # reach the benefits/perks section, usually at the END of the JD
        job_lines += f"\n<JOB_{i}>\nTitle: {j.get('title','')} at {j.get('company','')} | {j.get('location','')} | {j.get('work_type','')}\n{desc}\n</JOB_{i}>\n"

    prompt = f"""You are an experienced recruiter and compensation analyst for the European technology job market.

Analyse these {len(jobs)} job postings. Return a JSON object with a "results" array containing exactly {len(jobs)} objects in input order.

Schema for each object:
{{
  "i":                    0,
  "do":                   "Primary business outcome this role exists to achieve, then main activity (max 15 words)",
  "need":                 "The 1-2 genuine must-have requirements (max 15 words)",
  "flag":                 "One notable perk, genuine concern, or unique detail (max 15 words)",
  "work_arrangement":     "remote | hybrid | onsite",
  "perks":                ["Concrete perk/benefit from the posting (max 6 words each)"],
  "requirements": {{
    "technical": {{ "must": ["Eliminating technical/domain requirement"], "nice": ["Preferred technical"] }},
    "soft":      {{ "must": ["Eliminating soft/process skill"],          "nice": ["Preferred soft/process"] }}
  }},
  "skills": {{ "technical": ["Tool/lang/framework"], "process": ["Methodology e.g. Scrum"], "soft": ["e.g. Facilitation"] }},
  "seniority":            "mid",
  "red_flags":            ["Concern a candidate should note, inferred from the posting (max 10 words)"],
  "green_flags":          ["Positive signal from posting (max 10 words)"],
  "culture":              "startup",
  "estimated_salary_min": 45000,
  "estimated_salary_max": 60000,
  "salary_currency":      "EUR",
  "salary_confidence":    "medium"
}}

Rules:
- i: REQUIRED. Echo the JOB_n number this object describes, as an integer (the object for <JOB_3> must have "i": 3). This is how results are matched back to jobs.
- READ THE WHOLE DESCRIPTION — it may be in Portuguese or another language. Base every field on the full text, not just the title.
- do: state the business outcome first (e.g. "Scale data platform to support 10x growth" not "Write code daily")
- need: the 1-2 things that would eliminate a candidate if missing
- flag: one concrete perk, red flag, or unique detail — not generic praise
- work_arrangement: "remote" (fully remote), "hybrid" (mix of office + home — e.g. text/title says hybrid / híbrido / "Smart Working"), or "onsite" (office required). Infer from the whole posting.
- perks: the concrete things the EMPLOYER OFFERS THE EMPLOYEE. Scan the WHOLE posting, and ESPECIALLY any section headed "Benefits", "Perks", "What we offer", "We offer", "What you get", "What's in it for you", "Why join (us)", "Our offer", "Vantagens", "Oferecemos", or similar — extract every offered benefit there as a short phrase, plus any offered elsewhere in the text. Capture what is actually written even when phrased softly: e.g. "Flexible working hours", "Trust & autonomy from day one", "High-growth environment", "Career acceleration", "Health insurance", "Learning / training budget", "Equity / stock options", "Extra paid leave", "Relocation support", "Equipped home office", "Structured onboarding & coaching", "Meal allowance". Do NOT invent benefits that aren't stated, and do NOT list the work arrangement itself (remote/hybrid/onsite) as a perk — that's reported separately. Up to 8 items, in the posting's own words; [] only if the posting genuinely offers nothing.
- requirements.technical: tools / tech / domain knowledge. must = absence eliminates the candidate; nice = preferred. 0-3 each.
- requirements.soft: soft / process / methodology skills (facilitation, conflict mediation, Scrum ceremonies, backlog management…). must / nice. 0-3 each.
- skills: group NAMED skills — technical (tools/languages/frameworks), process (methodologies/frameworks: Scrum, SAFe, backlog mgmt), soft (communication, facilitation…). Up to 5 each, [] if none.
- seniority: junior / mid / senior / lead / executive
- red_flags: 0-3 concerns a savvy candidate would note, INFERRED from the posting even when it's framed positively. Look for: a very long must-have list, "fast-paced / high-pressure / dynamic" language, "wear many hats" / sprawling or vague scope, on-call / outside-hours / shift / weekend expectations, short-term / contract / fixed-term, no salary disclosed, commission-heavy pay, senior demands under a junior title, lots of required years, heavy travel, strictly on-site / relocation required, hard-to-fill signals (reposted, "urgent"). Phrase each neutrally and tie it to the posting. [] only if the posting is genuinely clean — most have at least one.
- green_flags: 0-3 items. Genuine positives from the posting (growth, learning budget, strong benefits, clear mission, good culture signals). Do not invent.
- culture: startup / scale-up / corporate / enterprise
- Preserve input order exactly.

SALARY ESTIMATION (be conservative):
- gross annual integers in local currency.
- salary_currency: ISO 4217 (EUR for Portugal/Spain/Germany, GBP for UK, PLN for Poland, USD for US, etc.)
- Location matters: Warsaw/Lisbon/Bucharest are 40-60% lower than London/Amsterdam/Zurich.
- Local SME pays 30-40% less than a multinational for the same role. Remote +10-15%.
- salary_confidence: "high" if explicit signals, "medium" if reasonable inference, "low" if very little info.
- Set salary fields to 0 and confidence to "low" if truly unable to estimate.

Jobs:
{job_lines}"""

    try:
        text    = _call_gemini(prompt, max_tokens=8000, json_mode=True)
        data    = _json.loads(text)
        results = data.get('results', data) if isinstance(data, dict) else data

        if not isinstance(results, list):
            return []

        valid_cultures   = {'startup', 'scale-up', 'corporate', 'enterprise'}
        valid_seniority  = {'junior', 'mid', 'senior', 'lead', 'executive'}
        valid_confidence = {'high', 'medium', 'low'}

        def _mn(d):   # normalise a {must, nice} block
            d = d if isinstance(d, dict) else {}
            return {'must': d['must'] if isinstance(d.get('must'), list) else [],
                    'nice': d['nice'] if isinstance(d.get('nice'), list) else []}

        out = []
        for pos, r in enumerate(results):
            if not isinstance(r, dict):
                out.append({}); continue
            # Normalise the echoed index used to map results back to jobs.
            try:
                r['i'] = int(r['i'])
            except (KeyError, TypeError, ValueError):
                r['i'] = pos
            # work arrangement
            if r.get('work_arrangement') not in ('remote', 'hybrid', 'onsite'):
                r['work_arrangement'] = None
            # perks
            r['perks'] = [str(p) for p in r['perks']][:8] if isinstance(r.get('perks'), list) else []
            # requirements {technical:{must,nice}, soft:{must,nice}}
            req = r.get('requirements') if isinstance(r.get('requirements'), dict) else {}
            r['requirements'] = {'technical': _mn(req.get('technical')), 'soft': _mn(req.get('soft'))}
            # skills grouped {technical, process, soft}
            sk = r.get('skills')
            if isinstance(sk, list):
                sk = {'technical': sk}
            sk = sk if isinstance(sk, dict) else {}
            r['skills'] = {k: (sk[k] if isinstance(sk.get(k), list) else []) for k in ('technical', 'process', 'soft')}
            # signals
            for key in ('red_flags', 'green_flags'):
                if not isinstance(r.get(key), list):
                    r[key] = []
            if r.get('culture')   not in valid_cultures:   r['culture']   = None
            if r.get('seniority') not in valid_seniority:  r['seniority'] = None
            sal_min = int(r.get('estimated_salary_min') or 0)
            sal_max = int(r.get('estimated_salary_max') or 0)
            if sal_min <= 0 or sal_max <= 0 or sal_min > sal_max:
                r['estimated_salary_min'] = 0
                r['estimated_salary_max'] = 0
                r['salary_confidence']    = 'low'
            else:
                r['estimated_salary_min'] = sal_min
                r['estimated_salary_max'] = sal_max
            if r.get('salary_confidence') not in valid_confidence:
                r['salary_confidence'] = 'low'
            if not r.get('salary_currency'):
                r['salary_currency'] = 'EUR'
            out.append(r)

        while len(out) < len(jobs):
            out.append({})

        # Persist each AI result by job_id so we never re-analyse the same posting.
        for r in out:
            if not r:
                continue
            idx = r.get('i')
            if isinstance(idx, int) and 0 <= idx < len(jobs):
                jid = str(jobs[idx].get('job_id') or '').strip()
                if jid:
                    r['_v'] = AI_PROMPT_VERSION   # stamp the prompt version for cache invalidation
                    db.ai_put(jid, r)

        return out
    except Exception:
        raise


@app.route('/analyse-batch', methods=['POST'])
def analyse_batch():
    """Batch AI analysis for the jobs the client sends."""
    body = request.get_json(force=True) or {}
    jobs = body.get('jobs', [])
    if not jobs:
        return jsonify({'error': 'no jobs'}), 400
    if PUBLIC_MODE:
        jobs = jobs[:20]          # one Gemini call per request — keep each one bounded
    try:
        return jsonify({'results': _analyse_and_persist(jobs)})
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/summarize', methods=['POST'])
def summarize():
    """Use Gemini to produce a 3-bullet summary of a job description."""
    import json as _json
    body    = request.get_json(force=True) or {}
    desc    = (body.get('description') or '').strip()[:5000]
    title   = (body.get('title')       or '').strip()
    company = (body.get('company')     or '').strip()

    if not desc:
        return jsonify({'error': 'empty description'}), 400

    prompt = f"""You are an experienced recruiter for the European technology job market.

Summarise this job posting in exactly 3 bullets. Return ONLY valid JSON:

{{"do":"Primary business outcome this role exists to achieve, then main activity (max 15 words)","need":"The 1-2 genuine must-have requirements (max 15 words)","flag":"One notable fact — perk, genuine concern, or unique detail (max 15 words)"}}

Rule: for 'do', state what the role exists to achieve first (e.g. "Scale data platform to support 10x growth" not "Write code daily").

Job: {title} at {company}

<JOB_POSTING>
{desc}
</JOB_POSTING>"""

    try:
        text   = _call_gemini(prompt, max_tokens=250, json_mode=True)
        result = _json.loads(text)
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/estimate-salary', methods=['POST'])
def estimate_salary():
    """Use Gemini to estimate gross annual salary range for the Portuguese market."""
    import json as _json
    body      = request.get_json(force=True) or {}
    title     = (body.get('title')     or '').strip()
    company   = (body.get('company')   or '').strip()
    location  = (body.get('location')  or 'Lisbon, Portugal').strip()
    work_type = (body.get('work_type') or '').strip()
    desc      = (body.get('description') or '').strip()[:1500]

    prompt = f"""You are a compensation expert. Estimate the realistic gross annual salary range for this role.

Rules:
- Base on local market rates (Portuguese/Spanish/Polish etc. salaries are significantly lower than UK/Germany/US)
- Multinationals pay 20-40% more than local SMEs
- Factor in seniority signals (years exp, leadership, stack complexity)
- Remote roles typically pay 10-20% more
- Be conservative — use lower-quartile rates
- ALWAYS give a realistic range — never 0. For an internship/trainee, annualise the typical stipend
  (e.g. a paid Portugal internship ≈ €9,000–14,000/yr); for junior roles use entry-level rates. Set
  confidence to "low" if you're unsure, but still provide your best numeric estimate.

Return a JSON object: {{"min": 40000, "max": 55000, "confidence": "medium", "note": "mid-level at multinational"}}

Role: {title}
Company: {company}
Location: {location}
Work arrangement: {work_type or 'not specified'}
Description: {desc or 'not provided'}"""

    try:
        text   = _call_gemini(prompt, max_tokens=150, json_mode=True)
        result = _json.loads(text)
        mn = int(result.get('min', 0))
        mx = int(result.get('max', 0))
        if mn <= 0 or mx <= 0 or mn > mx:
            return jsonify({'error': 'invalid estimate'}), 500
        return jsonify({'min': mn, 'max': mx,
                        'confidence': result.get('confidence', 'medium'),
                        'note':       result.get('note', '')})
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── Auto-collector (background "active search") ──────────
AUTO_INTERVAL_S = 1800   # 30 min between runs of an active search
AUTO_CHECK_S    = 60     # how often the worker wakes to look for due searches
AUTO_DAILY_CAP  = 200    # max new jobs analysed per active search per day, then auto-pause
AUTO_MAX_HOURS  = 12     # auto-pause an active search this long after it was started


def _collect_once(s):
    """One cycle for an active search: fetch the window, add new jobs to the library, then
    enrich + analyse the new ones server-side so they arrive fully ready. Returns new count."""
    query    = s.get('query') or ''
    location = s.get('location') or ''
    tpr      = s.get('tpr') or ''
    loc_aliases = set(_ALIAS_MAP.get(location.lower(), [location.lower()])) if location else set()
    cards = guest_search(query, location, tpr=tpr)
    if not cards:
        return 0
    known = db.known_ids([c['id'] for c in cards])
    new_jobs = []
    for card in cards:
        job = _build_job(card, {}, loc_aliases)
        if not job:
            continue
        db.card_put(job['job_id'], job)               # keep the whole window fresh in the library
        if job['job_id'] not in known:
            new_jobs.append(job)
    if not new_jobs:
        return 0
    # Pull each new job's description (cache first), then analyse in chunks.
    batch = []
    for job in new_jobs:
        jid = job['job_id']
        d = db.cache_get(jid)
        if not d or not d.get('description'):
            d = guest_detail(jid)
            if d.get('description'):
                db.cache_put(jid, d)
        desc = (d or {}).get('description') or ''
        if not desc:
            continue
        batch.append({'job_id': jid, 'title': job.get('job_title') or '',
                      'company': job.get('employer_name') or '',
                      'location': job.get('job_location') or '',
                      'work_type': job.get('work_arrangement') or '',
                      'description': desc})
    for k in range(0, len(batch), 15):
        try:
            _analyse_and_persist(batch[k:k + 15])
        except Exception as e:
            print(f"  auto analyse error: {e}")
    return len(new_jobs)


def _auto_worker():
    import datetime as _dt2
    print(f"  ✓ Auto-collector ready (runs every {AUTO_INTERVAL_S // 60} min while a search is active)")
    while True:
        try:
            for s in db.active_list(only_active=True):
                now     = time.time()
                created = s.get('created_at') or now
                if now - created > AUTO_MAX_HOURS * 3600:
                    db.active_pause(s['key'], f'auto-paused after {AUTO_MAX_HOURS}h')
                    print(f"  auto: paused '{s.get('query')}' (ran {AUTO_MAX_HOURS}h)")
                    continue
                if now - (s.get('last_run_at') or 0) < AUTO_INTERVAL_S:
                    continue
                today     = _dt2.date.today().isoformat()
                new_today = s.get('new_today') or 0
                if s.get('day') != today:
                    new_today = 0
                if new_today >= AUTO_DAILY_CAP:
                    db.active_pause(s['key'], f'daily cap {AUTO_DAILY_CAP} reached')
                    continue
                try:
                    n = _collect_once(s)
                except Exception as e:
                    print(f"  auto collect error: {e}")
                    n = 0
                new_today += n
                db.active_record_run(s['key'], n, new_today, today)
                print(f"  auto: '{s.get('query')}' +{n} new (today {new_today})")
                if new_today >= AUTO_DAILY_CAP:
                    db.active_pause(s['key'], f'daily cap {AUTO_DAILY_CAP} reached')
                    print(f"  auto: paused '{s.get('query')}' (daily cap)")
        except Exception as e:
            print(f"  auto worker loop error: {e}")
        time.sleep(AUTO_CHECK_S)


def _start_auto_worker():
    import threading
    threading.Thread(target=_auto_worker, daemon=True).start()


@app.route('/auto/start', methods=['POST'])
def auto_start():
    body     = request.get_json(force=True) or {}
    query    = (body.get('query') or '').strip()
    location = (body.get('location') or '').strip()
    tpr      = (body.get('tpr') or '').strip()
    if not query:
        return jsonify({'error': 'query required'}), 400
    key = db.active_add(query, location, tpr)
    print(f"  ▶ Auto-search started: '{query}' in '{location or 'anywhere'}'")
    return jsonify({'status': 'ok', 'key': key})


@app.route('/auto/stop', methods=['POST'])
def auto_stop():
    body = request.get_json(force=True) or {}
    key  = (body.get('key') or '').strip()
    if not key:
        query    = (body.get('query') or '').strip().lower()
        location = (body.get('location') or '').strip().lower()
        tpr      = (body.get('tpr') or '').strip()
        key = f"{query}|{location}|{tpr}"
    db.active_remove(key)
    return jsonify({'status': 'ok'})


@app.route('/auto/status')
def auto_status():
    return jsonify({'searches': db.active_list(only_active=False),
                    'interval_min': AUTO_INTERVAL_S // 60,
                    'daily_cap': AUTO_DAILY_CAP,
                    'max_hours': AUTO_MAX_HOURS})


# ─── Start ────────────────────────────────────────────────
if __name__ == '__main__':
    print('\n' + '=' * 50)
    print('  Job Analyser — LinkedIn Server (guest mode)')
    print('=' * 50)
    print('  Source  : public guest endpoints (no login)')
    print('  Address : http://localhost:5000')
    print('=' * 50)
    print()
    ai_on = bool(GEMINI_API_KEY and GEMINI_API_KEY != 'YOUR_GEMINI_KEY')
    print(f'  AI summaries: {"on" if ai_on else "off (add a Gemini key in Settings)"}')
    print()
    # When launched as the packaged .exe, pop the browser open automatically so the
    # user never has to type a URL. (In dev, start.bat already opens it.)
    if paths.FROZEN:
        import threading, webbrowser
        threading.Timer(1.2, lambda: webbrowser.open('http://localhost:5000/')).start()
    if PUBLIC_MODE:
        # Hosted: listen on all interfaces at the platform's port (Hugging Face uses 7860).
        # No background collector — visitors can't start one and it would run up costs.
        print('  PUBLIC MODE: admin routes off, rate limits on')
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '7860')), debug=False, threaded=True)
    else:
        _start_auto_worker()   # background "active search" collector
        app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
