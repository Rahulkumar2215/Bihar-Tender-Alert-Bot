"""Chat commands, shared by Telegram and WhatsApp. Users manage their own alerts by message."""
import html
import re

from . import db, matcher
from .formatter import detail_message, digest_messages, pack, TELEGRAM_LIMIT, WHATSAPP_LIMIT
from .normalize import DISTRICTS, canonical_district, fmt_inr, parse_amount

CATEGORIES = ["CIVIL", "ELECTRICAL", "MECHANICAL", "GENERAL", "IT-RELATED-WORKS", "EAUCTION-MINING"]

HELP = """Easiest: use the buttons at the bottom of the chat (send MENU if you don't see them).

Or type a command:

DEPTS – list departments with open tenders and their codes
ADD NBPDCL BCD – follow departments
REMOVE BCD – stop following a department
DISTRICT Patna Purnia – only tenders mentioning these districts
CATEGORY CIVIL – Civil / Electrical / Mechanical / General
KEYWORD road bridge – titles containing any of these words
MINVALUE 50L / MAXVALUE 5CR – value range
MY – show your filters
OPEN – open tenders that match you, closing soonest first
DETAIL 140276 – full details of one tender (value, EMD, fees, pre-bid)
MODE INSTANT – alerts every few hours (MODE DIGEST = once a day)
CLEAR – remove all filters (you get everything)
STOP – unsubscribe"""


def _style(channel):
    return "telegram" if channel == "telegram" else "whatsapp"


class Html(str):
    """Marks a reply that is already formatted for Telegram HTML."""


def handle(con, channel, address, text, name=None):
    """Process one inbound message. Returns a list of reply strings ready for the channel."""
    replies = _handle(con, channel, address, text, name)
    if channel != "telegram":
        return [str(r) for r in replies]
    return [str(r) if isinstance(r, Html) else html.escape(r, quote=False) for r in replies]


def _handle(con, channel, address, text, name=None):
    text = (text or "").strip()
    raw = text.lstrip("/")
    cmd, _, arg = raw.partition(" ")
    cmd = cmd.split("@")[0].upper()   # Telegram sends /cmd@BotName in groups
    args = [a for a in re.split(r"[\s,]+", arg.strip()) if a]
    style = _style(channel)

    row = con.execute("SELECT * FROM subscribers WHERE channel=? AND address=?", (channel, str(address))).fetchone()
    if channel == "whatsapp" and row:
        con.execute("UPDATE subscribers SET last_inbound_at=? WHERE id=?", (db.now(), row["id"]))
        con.commit()

    if cmd in ("START", "JOIN", "SUBSCRIBE", "HI", "HELLO") or row is None:
        if row is None and cmd not in ("START", "JOIN", "SUBSCRIBE", "HI", "HELLO"):
            return ["Welcome to Bihar Tender Alerts. Send START to subscribe."]
        sub, created = db.get_or_create_subscriber(con, channel, address, name)
        if channel == "whatsapp":
            con.execute("UPDATE subscribers SET last_inbound_at=? WHERE id=?", (db.now(), sub["id"]))
        con.commit()
        intro = ("You're subscribed to Bihar eProcurement tender alerts. 🎉\n"
                 "You'll get new tenders and reminders before deadlines, grouped by department.\n"
                 "Right now you'll get ALL departments. Pick yours with DEPTS, then ADD <code>.\n\n")
        return [intro + HELP] if created else ["You're already subscribed.\n\n" + HELP]

    sub = row
    if not sub["active"] and cmd not in ("START",):
        return ["You're unsubscribed. Send START to subscribe again."]

    if cmd in ("HELP", "MENU", "?"):
        return [HELP]

    if cmd in ("DEPTS", "DEPARTMENTS"):
        rows = con.execute(
            "SELECT dept_code, dept_name, COUNT(*) n FROM tenders WHERE status='open' "
            "GROUP BY dept_code, dept_name ORDER BY n DESC").fetchall()
        lines = ["Departments with open tenders (code – name – open count):"]
        lines += [f"{r['dept_code']} – {r['dept_name']} – {r['n']}" for r in rows]
        lines.append("\nFollow with e.g.: ADD " + " ".join(r["dept_code"] for r in rows[:2]))
        return pack(lines, TELEGRAM_LIMIT if style == "telegram" else WHATSAPP_LIMIT)

    if cmd in ("ADD", "FOLLOW", "REMOVE", "UNFOLLOW"):
        if not args:
            return [f"Usage: {cmd} NBPDCL BCD (send DEPTS for codes)"]
        known = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM departments")}
        ok, bad = [], []
        for a in (x.upper() for x in args):
            if a in known:
                (db.add_rule if cmd in ("ADD", "FOLLOW") else db.remove_rule)(con, sub["id"], "dept", a)
                ok.append(f"{a} ({known[a]})")
            else:
                bad.append(a)
        con.commit()
        msg = ("Following: " if cmd in ("ADD", "FOLLOW") else "Removed: ") + (", ".join(ok) or "nothing")
        if bad:
            msg += f"\nUnknown code(s): {', '.join(bad)} – send DEPTS to see codes."
        return [msg]

    if cmd == "DISTRICT":
        if not args:
            return ["Usage: DISTRICT Patna Purnia (or DISTRICT CLEAR)"]
        if args[0].upper() == "CLEAR":
            db.remove_rule(con, sub["id"], "district")
            con.commit()
            return ["District filter removed."]
        # allow two-word names like "West Champaran"
        names, i, ok, bad = args, 0, [], []
        while i < len(names):
            two = " ".join(names[i:i + 2])
            d = canonical_district(two) if i + 1 < len(names) else None
            if d:
                i += 2
            else:
                d = canonical_district(names[i])
                i += 1
            if d:
                db.add_rule(con, sub["id"], "district", d)
                ok.append(d)
            else:
                bad.append(names[i - 1])
        con.commit()
        msg = "District filter: " + ", ".join(sorted(set(ok))) if ok else "No district recognised."
        if bad:
            msg += f"\nNot recognised: {', '.join(bad)}. Districts: {', '.join(DISTRICTS)}"
        return [msg]

    if cmd == "CATEGORY":
        if not args or args[0].upper() == "CLEAR":
            db.remove_rule(con, sub["id"], "category")
            con.commit()
            return ["Category filter removed."]
        ok = []
        for a in args:
            m = next((c for c in CATEGORIES if c.startswith(a.upper())), None)
            if m:
                db.add_rule(con, sub["id"], "category", m)
                ok.append(m)
        con.commit()
        return ["Category filter: " + ", ".join(ok) if ok else "Use CIVIL, ELECTRICAL, MECHANICAL or GENERAL."]

    if cmd == "KEYWORD":
        if not args or args[0].upper() == "CLEAR":
            db.remove_rule(con, sub["id"], "keyword")
            con.commit()
            return ["Keyword filter removed."]
        for a in args:
            db.add_rule(con, sub["id"], "keyword", a.lower())
        con.commit()
        return ["Keywords: " + ", ".join(a.lower() for a in args)]

    if cmd in ("MINVALUE", "MAXVALUE"):
        kind = "min_value" if cmd == "MINVALUE" else "max_value"
        if not args or args[0].upper() == "CLEAR":
            db.remove_rule(con, sub["id"], kind)
            con.commit()
            return [f"{cmd} removed."]
        amt = parse_amount(" ".join(args))
        if amt is None:
            return [f"Couldn't read that amount. Try {cmd} 50L or {cmd} 2CR."]
        db.add_rule(con, sub["id"], kind, amt)
        con.commit()
        return [f"{cmd} set to {fmt_inr(amt)}. (Tenders whose value isn't published are still sent.)"]

    if cmd in ("MY", "LIST", "FILTERS", "STATUS"):
        rules = db.subscriber_rules(con, sub["id"])
        if not rules:
            body = "No filters – you get every department."
        else:
            names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM departments")}
            parts = []
            if rules.get("dept"):
                parts.append("Departments: " + ", ".join(f"{c} ({names.get(c, '?')})" for c in rules["dept"]))
            for k, label in (("district", "Districts"), ("category", "Categories"), ("keyword", "Keywords")):
                if rules.get(k):
                    parts.append(f"{label}: " + ", ".join(rules[k]))
            if rules.get("min_value"):
                parts.append("Min value: " + fmt_inr(float(rules["min_value"][0])))
            if rules.get("max_value"):
                parts.append("Max value: " + fmt_inr(float(rules["max_value"][0])))
            body = "\n".join(parts)
        return [f"{body}\nMode: {sub['mode']}"]

    if cmd in ("OPEN", "TENDERS", "LATEST"):
        rows, total = matcher.open_matching(con, sub)
        if not rows:
            return ["No open tenders match your filters right now."]
        msgs = digest_messages([(t, []) for t in rows], style, title=f"Open tenders for you ({len(rows)} of {total})")
        return [Html(m) for m in msgs]

    if cmd in ("DETAIL", "DETAILS", "INFO"):
        if not args or not args[0].isdigit():
            return ["Usage: DETAIL 140276 (the Tender ID shown in alerts)"]
        row = con.execute("SELECT * FROM tenders WHERE org_tender_id=? OR tender_id=? "
                          "ORDER BY org_tender_id=? DESC LIMIT 1", (int(args[0]), int(args[0]), int(args[0]))).fetchone()
        if not row:
            return [f"No tender with ID {args[0]} in our records."]
        return [Html(detail_message(db.tender_dict(row), style))]

    if cmd == "MODE":
        m = (args[0].lower() if args else "")
        if m not in ("instant", "digest"):
            return ["Use MODE INSTANT (every few hours) or MODE DIGEST (once a day)."]
        con.execute("UPDATE subscribers SET mode=? WHERE id=?", (m, sub["id"]))
        con.commit()
        return [f"Mode set to {m}."]

    if cmd == "CLEAR":
        con.execute("DELETE FROM subscriptions WHERE subscriber_id=?", (sub["id"],))
        con.commit()
        return ["All filters removed – you'll get every department."]

    if cmd in ("STOP", "UNSUBSCRIBE"):
        con.execute("UPDATE subscribers SET active=0 WHERE id=?", (sub["id"],))
        con.commit()
        return ["Unsubscribed. Send START any time to come back."]

    if cmd in ("1", "SHOW", "YES", "SEE"):
        items = matcher.queued_items(con, sub["id"])
        if not items:
            return ["Nothing pending. Send OPEN to see open tenders that match you."]
        msgs = digest_messages(items, style)
        matcher.mark_queued_sent(con, sub["id"])
        return [Html(m) for m in msgs]

    return ["Sorry, I didn't get that.\n\n" + HELP]
