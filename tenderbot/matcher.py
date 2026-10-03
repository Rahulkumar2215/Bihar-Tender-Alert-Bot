"""Decide which tenders each subscriber should hear about, and why.

Filter logic: values of the same kind are OR-ed (NBPDCL or BCD), different kinds are AND-ed
(dept AND district AND keyword). A subscriber with no filters gets everything.

Reasons a tender is sent (each at most once per subscriber):
  new       - first appeared on the portal after the subscriber joined
  closing:<date>   - bid deadline is within CLOSING_SOON_DAYS (again if the date moves)
  extended:<date>  - the department pushed the deadline later (sent once per new date)
"""
from datetime import timedelta

from . import config, db
from .sectors import HIDE_MINING, classify, size_ok
from .normalize import now_ist, parse_iso


def matches(t, rules):
    secs = t.get("sectors") or classify(t.get("dept_code"), t.get("category"), t.get("title"))
    if HIDE_MINING and "mining" in secs:
        return False                      # sand-ghat / mining auctions hidden for now
    if rules.get("sector") and not set(rules["sector"]) & set(secs):
        return False
    if rules.get("size") and not size_ok(t.get("value_inr"), rules["size"]):
        return False
    if rules.get("dept") and t["dept_code"] not in rules["dept"] and str(t["dept_id"]) not in rules["dept"]:
        return False
    if rules.get("category") and (t.get("category") or "").upper() not in rules["category"]:
        return False
    if rules.get("district"):
        if t.get("districts"):
            if not set(rules["district"]) & set(t["districts"]):
                return False
        elif not rules.get("nodistrict"):   # opt-in: also tenders that name no district
            return False
    if rules.get("keyword"):
        hay = f"{t.get('title', '')} {t.get('ref_no', '')}".lower()
        if not any(k in hay for k in rules["keyword"]):
            return False
    v = t.get("value_inr")
    if v:  # tenders whose value is hidden are never dropped by a value filter
        if rules.get("min_value") and v < float(rules["min_value"][0]):
            return False
        if rules.get("max_value") and v > float(rules["max_value"][0]):
            return False
    return True


def pending_items(con, sub, now=None):
    """Return [(tender, [reasons])] not yet sent/queued for this subscriber."""
    now = now or now_ist()
    rules = db.subscriber_rules(con, sub["id"])
    baseline = db.get_kv(con, "baseline_at")
    joined = parse_iso(sub["created_at"])
    new_after = max(joined, parse_iso(baseline)) if baseline else joined
    window_end = now + timedelta(days=config.CLOSING_SOON_DAYS)

    done = {(r["tender_id"], r["reason"]) for r in con.execute(
        "SELECT tender_id, reason FROM deliveries WHERE subscriber_id=?", (sub["id"],))}
    ext = {}
    for r in con.execute("SELECT tender_id, new_value FROM tender_events WHERE kind='extended' AND created_at > ?",
                         (sub["created_at"],)):
        ext[r["tender_id"]] = r["new_value"]  # latest extension wins

    out = []
    for t in db.open_tenders(con):
        close = parse_iso(t["close_at"])
        if close and close <= now:
            continue
        if not matches(t, rules):
            continue
        reasons = []
        if parse_iso(t["first_seen"]) > new_after and (t["tender_id"], "new") not in done:
            reasons.append("new")
        closing_key = f"closing:{t['close_at']}"  # keyed by date, so an extended tender is reminded again
        if close and close <= window_end and (t["tender_id"], closing_key) not in done:
            reasons.append(closing_key)
        if t["tender_id"] in ext:
            key = f"extended:{ext[t['tender_id']]}"
            if (t["tender_id"], key) not in done and "new" not in reasons:
                reasons.append(key)
        if reasons:
            out.append((t, reasons))
    return out


def open_matching(con, sub, limit=None):
    rules = db.subscriber_rules(con, sub["id"])
    now = now_ist()
    rows = [t for t in db.open_tenders(con)
            if matches(t, rules) and (not t["close_at"] or parse_iso(t["close_at"]) > now)]
    return rows[: limit or config.OPEN_LIST_LIMIT], len(rows)


def record(con, sub_id, items, status):
    ts = db.now()
    con.executemany(
        "INSERT OR REPLACE INTO deliveries(subscriber_id, tender_id, reason, status, created_at, sent_at) "
        "VALUES (?,?,?,?,?,?)",
        [(sub_id, t["tender_id"], r, status, ts, ts if status == "sent" else None) for t, rs in items for r in rs])
    con.commit()


def queued_items(con, sub_id):
    """Items held back for a WhatsApp user outside the 24h window (still open tenders only)."""
    now = now_ist()
    by_tender = {}
    for r in con.execute("SELECT tender_id, reason FROM deliveries WHERE subscriber_id=? AND status='queued'", (sub_id,)):
        by_tender.setdefault(r["tender_id"], []).append(r["reason"])
    out = []
    for t in db.open_tenders(con):
        if t["tender_id"] in by_tender and (not t["close_at"] or parse_iso(t["close_at"]) > now):
            out.append((t, by_tender[t["tender_id"]]))
    return out


def mark_queued_sent(con, sub_id):
    con.execute("UPDATE deliveries SET status='sent', sent_at=? WHERE subscriber_id=? AND status='queued'",
                (db.now(), sub_id))
    con.commit()
