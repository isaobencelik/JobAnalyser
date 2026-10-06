"""
SQLite persistence for the Job Analyser.

Two roles, one file (jobs.db, git-ignored):
  • jobs       — a persistent cache of fetched detail (description + criteria) by
                 job_id, so re-fetching a job we've seen is instant (no LinkedIn hit).
  • favorites  — a full snapshot of jobs the user starred, kept forever even after
                 the LinkedIn posting expires.

A fresh connection is opened per call (thread-safe for Flask's threaded server);
WAL mode lets readers and a writer coexist without locking each other out.
"""

import sqlite3, os, json, time
import paths

# Stored in DATA_DIR so favorites/history/cache survive swapping in a new .exe
# (see paths.py — it's the project folder in dev, %LOCALAPPDATA%\JobAnalyser when frozen).
DB_PATH = os.path.join(paths.DATA_DIR, 'jobs.db')


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_id       TEXT PRIMARY KEY,
            detail       TEXT NOT NULL,     -- JSON: description + criteria
            ai           TEXT,              -- JSON: AI analysis (summary, skills, salary…)
            card         TEXT,              -- JSON: card-level fields (title, company, location, logo, date…)
            posted_at    REAL,              -- posting timestamp, for ordering the library
            fetched_at   REAL,
            last_seen_at REAL
        )""")
        # Migrate older DBs that predate newer columns.
        for col, decl in (('ai', 'TEXT'), ('card', 'TEXT'), ('posted_at', 'REAL')):
            try:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        c.execute("""CREATE TABLE IF NOT EXISTS favorites (
            job_id   TEXT PRIMARY KEY,
            job      TEXT NOT NULL,          -- JSON snapshot of the full client job
            status   TEXT DEFAULT 'Saved',
            notes    TEXT DEFAULT '',
            saved_at REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS searches (
            key      TEXT PRIMARY KEY,       -- query|location|tpr
            query    TEXT,
            location TEXT,
            tpr      TEXT,
            count    INTEGER,
            run_at   REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS active_searches (
            key           TEXT PRIMARY KEY,  -- query|location|tpr
            query         TEXT,
            location      TEXT,
            tpr           TEXT,
            active        INTEGER DEFAULT 1,
            created_at    REAL,
            last_run_at   REAL DEFAULT 0,
            last_new      INTEGER DEFAULT 0,
            new_today     INTEGER DEFAULT 0,
            day           TEXT DEFAULT '',
            runs          INTEGER DEFAULT 0,
            paused_reason TEXT DEFAULT ''
        )""")
    print(f"  ✓ Database ready ({DB_PATH})")


# ── Detail cache ──────────────────────────────────────────
def cache_get(job_id):
    """Return the cached detail dict for job_id, or None."""
    try:
        with _conn() as c:
            row = c.execute("SELECT detail FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row['detail']) if row else None
    except Exception as e:
        print(f"  ⚠  db.cache_get: {e}")
        return None


def cache_put(job_id, detail):
    """Store/refresh the cached detail for job_id."""
    now = time.time()
    try:
        with _conn() as c:
            c.execute("""INSERT INTO jobs (job_id, detail, fetched_at, last_seen_at)
                         VALUES (?,?,?,?)
                         ON CONFLICT(job_id) DO UPDATE SET
                           detail=excluded.detail, last_seen_at=excluded.last_seen_at""",
                      (job_id, json.dumps(detail), now, now))
    except Exception as e:
        print(f"  ⚠  db.cache_put: {e}")


def ai_get(job_id):
    """Return the cached AI analysis dict for job_id, or None."""
    try:
        with _conn() as c:
            row = c.execute("SELECT ai FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(row['ai']) if row and row['ai'] else None
    except Exception as e:
        print(f"  ⚠  db.ai_get: {e}")
        return None


def ai_put(job_id, ai):
    """Store/refresh the AI analysis for job_id (creates the row if needed)."""
    now = time.time()
    try:
        with _conn() as c:
            c.execute("""INSERT INTO jobs (job_id, detail, ai, fetched_at, last_seen_at)
                         VALUES (?, '{}', ?, ?, ?)
                         ON CONFLICT(job_id) DO UPDATE SET
                           ai=excluded.ai, last_seen_at=excluded.last_seen_at""",
                      (job_id, json.dumps(ai), now, now))
    except Exception as e:
        print(f"  ⚠  db.ai_put: {e}")


# ── Library (every card-level job we've ever fetched) ─────
def card_put(job_id, card):
    """Store/refresh the card-level fields for job_id (builds the browsable library)."""
    now = time.time()
    posted = card.get('job_posted_at_timestamp')
    try:
        with _conn() as c:
            c.execute("""INSERT INTO jobs (job_id, detail, card, posted_at, fetched_at, last_seen_at)
                         VALUES (?, '{}', ?, ?, ?, ?)
                         ON CONFLICT(job_id) DO UPDATE SET
                           card=excluded.card, posted_at=excluded.posted_at, last_seen_at=excluded.last_seen_at""",
                      (job_id, json.dumps(card), posted, now, now))
    except Exception as e:
        print(f"  ⚠  db.card_put: {e}")


def library_list(limit=5000):
    """Every job that's ever appeared in a search, newest posting first, with its AI."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT card, ai FROM jobs WHERE card IS NOT NULL "
                "ORDER BY COALESCE(posted_at, 0) DESC LIMIT ?", (limit,)).fetchall()
    except Exception as e:
        print(f"  ⚠  db.library_list: {e}")
        return []
    out = []
    for r in rows:
        try:
            job = json.loads(r['card'])
        except Exception:
            continue
        if r['ai']:
            try: job['ai'] = json.loads(r['ai'])
            except Exception: pass
        out.append(job)
    return out


def known_ids(ids):
    """Of the given job_ids, return the set already in the library (seen before)."""
    ids = [i for i in ids if i]
    if not ids:
        return set()
    try:
        with _conn() as c:
            q = "SELECT job_id FROM jobs WHERE job_id IN (%s)" % ",".join("?" * len(ids))
            rows = c.execute(q, ids).fetchall()
        return {r['job_id'] for r in rows}
    except Exception as e:
        print(f"  ⚠  db.known_ids: {e}")
        return set()


def search_log(query, location, tpr, count):
    """Record a search so it can show in the recent-searches strip."""
    key = f"{(query or '').lower()}|{(location or '').lower()}|{tpr or ''}"
    try:
        with _conn() as c:
            c.execute("""INSERT INTO searches (key, query, location, tpr, count, run_at)
                         VALUES (?,?,?,?,?,?)
                         ON CONFLICT(key) DO UPDATE SET count=excluded.count, run_at=excluded.run_at""",
                      (key, query, location, tpr, count, time.time()))
    except Exception as e:
        print(f"  ⚠  db.search_log: {e}")


def recent_searches(limit=8):
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT query, location, tpr, count, run_at FROM searches "
                "ORDER BY run_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"  ⚠  db.recent_searches: {e}")
        return []


# ── Active (auto-refreshing) searches ─────────────────────
def active_add(query, location, tpr):
    """Register/activate a background search. Returns its key."""
    key = f"{(query or '').lower()}|{(location or '').lower()}|{tpr or ''}"
    now = time.time()
    try:
        with _conn() as c:
            c.execute("""INSERT INTO active_searches (key, query, location, tpr, active, created_at, last_run_at)
                         VALUES (?,?,?,?,1,?,0)
                         ON CONFLICT(key) DO UPDATE SET
                           active=1, created_at=?, last_run_at=0, paused_reason='', new_today=0""",
                      (key, query, location, tpr, now, now))
    except Exception as e:
        print(f"  ⚠  db.active_add: {e}")
    return key


def active_remove(key):
    try:
        with _conn() as c:
            c.execute("UPDATE active_searches SET active=0 WHERE key=?", (key,))
    except Exception as e:
        print(f"  ⚠  db.active_remove: {e}")


def active_pause(key, reason):
    try:
        with _conn() as c:
            c.execute("UPDATE active_searches SET active=0, paused_reason=? WHERE key=?", (reason, key))
    except Exception as e:
        print(f"  ⚠  db.active_pause: {e}")


def active_list(only_active=True):
    try:
        with _conn() as c:
            q = "SELECT * FROM active_searches"
            if only_active:
                q += " WHERE active=1"
            rows = c.execute(q).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"  ⚠  db.active_list: {e}")
        return []


def active_record_run(key, last_new, new_today, day):
    try:
        with _conn() as c:
            c.execute("""UPDATE active_searches
                         SET last_run_at=?, last_new=?, new_today=?, day=?, runs=runs+1 WHERE key=?""",
                      (time.time(), last_new, new_today, day, key))
    except Exception as e:
        print(f"  ⚠  db.active_record_run: {e}")


def reset_all():
    """Wipe every fetched job, favorite and search — a clean slate."""
    try:
        with _conn() as c:
            c.execute("DELETE FROM jobs")
            c.execute("DELETE FROM favorites")
            c.execute("DELETE FROM searches")
            c.execute("DELETE FROM active_searches")
        print("  ✓ Database reset (jobs + favorites + searches cleared)")
        return True
    except Exception as e:
        print(f"  ⚠  db.reset_all: {e}")
        return False


# ── Favorites ─────────────────────────────────────────────
def fav_add(job_id, job):
    """Add (or update) a favorite — stores the full job snapshot."""
    try:
        with _conn() as c:
            c.execute("""INSERT INTO favorites (job_id, job, saved_at) VALUES (?,?,?)
                         ON CONFLICT(job_id) DO UPDATE SET job=excluded.job""",
                      (job_id, json.dumps(job), time.time()))
        return True
    except Exception as e:
        print(f"  ⚠  db.fav_add: {e}")
        return False


def fav_remove(job_id):
    try:
        with _conn() as c:
            c.execute("DELETE FROM favorites WHERE job_id=?", (job_id,))
        return True
    except Exception as e:
        print(f"  ⚠  db.fav_remove: {e}")
        return False


def fav_list():
    """All favorited jobs (full snapshots), newest first."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT job, status, notes, saved_at FROM favorites ORDER BY saved_at DESC"
            ).fetchall()
    except Exception as e:
        print(f"  ⚠  db.fav_list: {e}")
        return []
    out = []
    for r in rows:
        try:
            j = json.loads(r['job'])
        except Exception:
            continue
        j['_favorite'] = True
        j['_status']   = r['status']
        j['_notes']    = r['notes']
        j['_savedAt']  = r['saved_at']
        out.append(j)
    return out


def fav_ids():
    """List of favorited job_ids (for marking search results)."""
    try:
        with _conn() as c:
            rows = c.execute("SELECT job_id FROM favorites").fetchall()
        return [r['job_id'] for r in rows]
    except Exception as e:
        print(f"  ⚠  db.fav_ids: {e}")
        return []
