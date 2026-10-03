"""SQLite storage. Every tender is stored once (tender_id is the key), so nothing is sent twice."""
import json
import sqlite3
from pathlib import Path

from . import config
from .normalize import dept_code, now_ist, parse_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    dept_id     INTEGER PRIMARY KEY,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dept_code ON departments(code);

CREATE TABLE IF NOT EXISTS tenders (
    tender_id       INTEGER PRIMARY KEY,
    org_tender_id   INTEGER,
    ref_no          TEXT,
    title           TEXT,
    dept_id         INTEGER,
    dept_code       TEXT,
    dept_name       TEXT,
    category        TEXT,
    tender_type     TEXT,
    districts       TEXT,            -- JSON list
    published_at    TEXT,
    bid_start_at    TEXT,
    close_at        TEXT,
    open_at         TEXT,
    value_inr       REAL,            -- estimated value (from detail page)
    emd_inr         REAL,
    tender_fee_inr  REAL,
    processing_fee_inr REAL,
    prebid_at       TEXT,
    prebid_venue    TEXT,
    attachments     INTEGER,
    detail_fetched  INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'open',   -- open | closed | removed
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tender_close ON tenders(status, close_at);
CREATE INDEX IF NOT EXISTS ix_tender_dept ON tenders(dept_code);

CREATE TABLE IF NOT EXISTS tender_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL,       -- extended
    old_value   TEXT,
    new_value   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscribers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel         TEXT NOT NULL,   -- telegram | whatsapp
    address         TEXT NOT NULL,   -- telegram chat_id or WhatsApp number (91XXXXXXXXXX)
    name            TEXT,
    mode            TEXT DEFAULT 'digest',   -- digest (once a day) | instant (every run)
    active          INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL,
    opted_in_at     TEXT,
    last_inbound_at TEXT,            -- WhatsApp 24h service window
    UNIQUE(channel, address)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    subscriber_id   INTEGER NOT NULL,
    kind            TEXT NOT NULL,   -- dept | district | category | keyword | min_value | max_value
    value           TEXT NOT NULL,
    PRIMARY KEY (subscriber_id, kind, value)
);

CREATE TABLE IF NOT EXISTS deliveries (
    subscriber_id   INTEGER NOT NULL,
    tender_id       INTEGER NOT NULL,
    reason          TEXT NOT NULL,   -- new | closing | extended:<close_at>
    status          TEXT NOT NULL,   -- sent | queued
    created_at      TEXT NOT NULL,
    sent_at         TEXT,
    PRIMARY KEY (subscriber_id, tender_id, reason)
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
    listed INTEGER, new_count INTEGER, extended INTEGER, details INTEGER, note TEXT
);
"""

TENDER_COLS = ["org_tender_id", "ref_no", "title", "dept_id", "dept_code", "dept_name", "category",
               "tender_type", "published_at", "bid_start_at", "close_at", "open_at"]


def connect(path=None):
    path = path or config.DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    if path != ":memory:":
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(tenders)")}
    if "sectors" not in cols:  # added Oct 2026
        con.execute("ALTER TABLE tenders ADD COLUMN sectors TEXT")
    if "elig" not in cols:  # who can bid, documents, files (JSON) – added Oct 2026
        con.execute("ALTER TABLE tenders ADD COLUMN elig TEXT")
    return con


def now():
    return now_ist().isoformat(timespec="seconds")


# ---------------------------------------------------------------- tenders

def upsert_departments(con, orgs):
    con.executemany(
        "INSERT INTO departments(dept_id, code, name) VALUES (?,?,?) "
        "ON CONFLICT(dept_id) DO UPDATE SET code=excluded.code, name=excluded.name",
        [(o["organizationId"], dept_code(o.get("organizationCode"), o["organizationId"]),
          o["organizationName"].strip()) for o in orgs])


def upsert_tenders(con, tenders, seen_at=None):
    """Insert new tenders, refresh existing ones, record deadline extensions.

    Returns (new_ids, extended_ids)."""
    seen_at = seen_at or now()
    new_ids, extended = [], []
    for t in tenders:
        row = con.execute("SELECT close_at FROM tenders WHERE tender_id=?", (t["tender_id"],)).fetchone()
        if row is None:
            con.execute(
                f"INSERT INTO tenders(tender_id, {', '.join(TENDER_COLS)}, districts, sectors, status, first_seen, last_seen) "
                f"VALUES (?, {', '.join('?' * len(TENDER_COLS))}, ?, ?, 'open', ?, ?)",
                [t["tender_id"]] + [t.get(c) for c in TENDER_COLS]
                + [json.dumps(t["districts"]), json.dumps(t.get("sectors") or []), seen_at, seen_at])
            new_ids.append(t["tender_id"])
            continue
        old_close = row["close_at"]
        if t.get("close_at") and old_close and parse_iso(t["close_at"]) > parse_iso(old_close):
            con.execute("INSERT INTO tender_events(tender_id, kind, old_value, new_value, created_at) "
                        "VALUES (?, 'extended', ?, ?, ?)", (t["tender_id"], old_close, t["close_at"], seen_at))
            extended.append(t["tender_id"])
        con.execute(
            f"UPDATE tenders SET {', '.join(c + '=?' for c in TENDER_COLS)}, districts=?, sectors=?, status='open', "
            f"last_seen=? WHERE tender_id=?",
            [t.get(c) for c in TENDER_COLS] + [json.dumps(t["districts"]), json.dumps(t.get("sectors") or []),
                                               seen_at, t["tender_id"]])
    return new_ids, extended


def mark_missing(con, seen_ids, seen_at=None):
    """Tenders no longer listed: closed if past deadline, otherwise removed (cancelled/withdrawn)."""
    seen_at = seen_at or now()
    open_rows = con.execute("SELECT tender_id, close_at FROM tenders WHERE status='open'").fetchall()
    seen = set(seen_ids)
    nowdt = parse_iso(seen_at)
    for r in open_rows:
        if r["tender_id"] in seen:
            continue
        closed = r["close_at"] and parse_iso(r["close_at"]) <= nowdt
        con.execute("UPDATE tenders SET status=? WHERE tender_id=?", ("closed" if closed else "removed", r["tender_id"]))


def ids_needing_detail(con, limit):
    return [r[0] for r in con.execute(
        "SELECT tender_id FROM tenders WHERE status='open' AND detail_fetched=0 ORDER BY first_seen DESC LIMIT ?",
        (limit,))]


def save_detail(con, tender_id, s):
    con.execute(
        "UPDATE tenders SET value_inr=?, emd_inr=?, tender_fee_inr=?, processing_fee_inr=?, prebid_at=?, "
        "prebid_venue=?, attachments=?, elig=?, detail_fetched=1 WHERE tender_id=?",
        (s.get("value"), s.get("emd"), s.get("tender_fee"), s.get("processing_fee"), s.get("prebid"),
         s.get("prebid_venue"), s.get("attachments", 0), json.dumps(s["elig"]) if s.get("elig") else None,
         tender_id))


def ids_needing_elig(con, limit):
    """Tenders fetched before eligibility was collected: re-fetch them, newest first."""
    return [r[0] for r in con.execute(
        "SELECT tender_id FROM tenders WHERE status='open' AND detail_fetched=1 AND elig IS NULL "
        "ORDER BY first_seen DESC LIMIT ?", (limit,))]


def tender_dict(row):
    from .sectors import classify
    d = dict(row)
    d["districts"] = json.loads(d.get("districts") or "[]")
    d["elig"] = json.loads(d["elig"]) if d.get("elig") else None
    # always classify fresh, so improved rules apply to tenders already in the database
    d["sectors"] = classify(d.get("dept_code"), d.get("category"), d.get("title"))
    return d


def open_tenders(con):
    return [tender_dict(r) for r in con.execute("SELECT * FROM tenders WHERE status='open' ORDER BY close_at")]


# ---------------------------------------------------------------- subscribers

def get_or_create_subscriber(con, channel, address, name=None, opted_in=True):
    row = con.execute("SELECT * FROM subscribers WHERE channel=? AND address=?", (channel, str(address))).fetchone()
    if row:
        if not row["active"] and opted_in:
            con.execute("UPDATE subscribers SET active=1, opted_in_at=? WHERE id=?", (now(), row["id"]))
            row = con.execute("SELECT * FROM subscribers WHERE id=?", (row["id"],)).fetchone()
        return row, False
    ts = now()
    con.execute("INSERT INTO subscribers(channel, address, name, created_at, opted_in_at) VALUES (?,?,?,?,?)",
                (channel, str(address), name, ts, ts if opted_in else None))
    return con.execute("SELECT * FROM subscribers WHERE channel=? AND address=?", (channel, str(address))).fetchone(), True


def subscriber_rules(con, sub_id):
    rules = {}
    for r in con.execute("SELECT kind, value FROM subscriptions WHERE subscriber_id=?", (sub_id,)):
        rules.setdefault(r["kind"], []).append(r["value"])
    return rules


def add_rule(con, sub_id, kind, value):
    if kind in ("min_value", "max_value"):
        con.execute("DELETE FROM subscriptions WHERE subscriber_id=? AND kind=?", (sub_id, kind))
    con.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?,?)", (sub_id, kind, str(value)))


def remove_rule(con, sub_id, kind, value=None):
    if value is None:
        con.execute("DELETE FROM subscriptions WHERE subscriber_id=? AND kind=?", (sub_id, kind))
    else:
        con.execute("DELETE FROM subscriptions WHERE subscriber_id=? AND kind=? AND value=?", (sub_id, kind, str(value)))


def get_kv(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_kv(con, k, v):
    con.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
