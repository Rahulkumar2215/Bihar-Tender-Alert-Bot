"""Tap-only Telegram menus, so contractors never have to type a command.

Setup after START (3 steps, all tick-boxes):
  1. Departments
  2. Districts
  3. Tender size: Under 50 L, 50 L-2 Cr, 2-10 Cr, 10 Cr+
Type of work (Roads, Buildings, Water...) is an optional extra filter in settings. Sand-ghat / mining auctions are hidden for now.

Every screen is built here as (text, buttons); telegram.py sends it or edits the tapped message.
Callback data stays under Telegram's 64-byte limit.
"""
import html
import re

from .. import config, db, matcher
from ..formatter import detail_message, digest_parts
from ..normalize import DISTRICTS, now_ist, parse_iso
from ..sectors import HIDE_MINING, SECTORS, SIZES, SIZE_LABELS, LABELS as SECTOR_LABELS

PAGE = 8          # departments per page
SHOW = 10         # tenders per "show" page

# Bottom keyboard (always visible).
KB_TENDERS = "📋 My tenders"
KB_DEPTS = "🏛 Departments"
KB_WORK = "🔧 Type of work"   # keyboards sent before 3 Oct 16:40 still show this
KB_DIST = "📍 Districts"
KB_BROWSE = "🔎 Browse all"
KB_SETTINGS = "⚙️ My settings"
REPLY_KEYBOARD = {"keyboard": [[{"text": KB_TENDERS}], [{"text": KB_DEPTS}, {"text": KB_DIST}],
                               [{"text": KB_BROWSE}, {"text": KB_SETTINGS}]],
                  "resize_keyboard": True, "is_persistent": True}
# labels used by the first version of the menus, so old keyboards keep working
OLD_LABELS = {"📋 My tenders / मेरे टेंडर": KB_TENDERS, "🏛 Departments / विभाग": KB_DEPTS,
              "📍 Districts / जिला": KB_DIST, "🔎 Browse all / सभी देखें": KB_BROWSE,
              "⚙️ My settings / सेटिंग": KB_SETTINGS}


def btn(text, data):
    return {"text": text, "callback_data": data}


def ikb(rows):
    return {"inline_keyboard": rows}


def esc(s):
    return html.escape(str(s), quote=False)


def short_name(name, limit=34):
    n = name.strip()
    if n.isupper() and len(n) > 8:   # SHOUTED names, but keep short codes like BCD
        n = n.title()
    for a, b in [(r"\bDepartment\b", "Dept"), (r"\bCorporation\b", "Corp"), (r"\bLimited\b|\bLtd\.?", ""),
                 (r"\bDevelopment\b", "Dev"), (r"\bBihar State\b", "Bihar"), (r"\bCompany\b", "Co"),
                 (r"\bInfrastructure\b", "Infra"), (r"\bConstruction\b", "Constr."), (r"\bDistribution\b", "Distrib."),
                 (r"\s+", " ")]:
        n = re.sub(a, b, n)
    n = n.strip(" ,.")
    return n if len(n) <= limit else n[: limit - 1] + "…"


def _sub(con, chat_id):
    return con.execute("SELECT * FROM subscribers WHERE channel='telegram' AND address=?", (str(chat_id),)).fetchone()


def _visible(con):
    """Open, not yet closed, and not hidden (mining)."""
    now = now_ist()
    return [t for t in db.open_tenders(con)
            if (not t["close_at"] or parse_iso(t["close_at"]) > now)
            and not (HIDE_MINING and "mining" in t["sectors"])]


def _depts(con):
    counts, names = {}, {}
    for t in _visible(con):
        counts[t["dept_code"]] = counts.get(t["dept_code"], 0) + 1
        names[t["dept_code"]] = t["dept_name"]
    return [{"dept_code": c, "dept_name": names[c], "n": n}
            for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], names[kv[0]]))]


def _district_counts(con):
    counts = {d: 0 for d in DISTRICTS}
    for t in _visible(con):
        for d in t["districts"]:
            counts[d] = counts.get(d, 0) + 1
    return counts


def _sector_counts(con):
    counts = {k: 0 for k, _ in SECTORS}
    for t in _visible(con):
        for s in t["sectors"]:
            if s in counts:
                counts[s] += 1
    return counts


def _rules(con, sub):
    return db.subscriber_rules(con, sub["id"])


def _done_button(wizard, next_label, next_data):
    return [btn(next_label if wizard else "✔️ Done", next_data if wizard else "set")]


# ------------------------------------------------------------------ setup screens

def welcome(con, sub, created):
    head = ("🙏 <b>Welcome to Bihar Tender Alerts</b>\n"
            "Get new government tenders from eproc2.bihar.gov.in here on Telegram, only for the departments, "
            "districts and tender size you choose.\n\nJust tap the buttons. No typing needed.\n"
            "Or simply type a district (Patna), a department (BCD), civil / electrical, or a Tender ID.\n\n")
    if not created and _rules(con, sub):
        return head + "You are already set up. Use the buttons below.", None
    text, kb = dept_picker(con, sub, 0, wizard=True)
    return head + text, kb


def sector_picker(con, sub, wizard=False):
    chosen = set(_rules(con, sub).get("sector", []))
    counts = _sector_counts(con)
    w = "w" if wizard else "s"
    kb = [[btn(("✅ " if k in chosen else "▫️ ") + f"{label} ({counts.get(k, 0)})", f"st:{w}:{k}")]
          for k, label in SECTORS]
    kb.append([btn("🌐 All types of work", f"sa:{w}")])
    kb.append([btn("✔️ Done", "set")])
    step = "<b>Type of work (optional)</b>\n"
    picked = ", ".join(SECTOR_LABELS[k] for k, _ in SECTORS if k in chosen) or "none yet = ALL work"
    return (step + "Narrow your departments further to the kind of work you do. Tap to tick ✅ "
            "(tap again to remove). Number = open tenders now.\n\nSelected: " + esc(picked)), ikb(kb)


def district_picker(con, sub, wizard=False):
    rules = _rules(con, sub)
    chosen = set(rules.get("district", []))
    counts = _district_counts(con)
    w = "w" if wizard else "s"
    kb, row = [], []
    for d in sorted(DISTRICTS):
        row.append(btn(("✅" if d in chosen else "") + f"{d} ({counts.get(d, 0)})", f"rt:{w}:{d}"))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    if chosen:
        nod = bool(rules.get("nodistrict"))
        kb.append([btn(("✅" if nod else "▫️") + " Also tenders with no district named", f"rn:{w}")])
    kb.append([btn("🌐 All of Bihar", f"ra:{w}")])
    kb.append(_done_button(wizard, "Next ➡ Tender size", "zp:w"))
    step = "<b>Step 2 of 3 — Districts</b>\n" if wizard else "<b>Districts</b>\n"
    picked = ", ".join(sorted(chosen)) if chosen else "none = ALL of Bihar"
    return (step + "Tap your districts ✅ (number = open tenders in that district).\n\nSelected: " + esc(picked)), ikb(kb)


def size_picker(con, sub, wizard=False):
    chosen = set(_rules(con, sub).get("size", []))
    w = "w" if wizard else "s"
    kb = [[btn(("✅ " if k in chosen else "▫️ ") + label, f"zt:{w}:{k}")] for k, label, _, _ in SIZES]
    kb.append([btn("🌐 Any size", f"za:{w}")])
    kb.append(_done_button(wizard, "✔️ Finish", "fin"))
    step = "<b>Step 3 of 3 — Tender size</b>\n" if wizard else "<b>Tender size</b>\n"
    picked = ", ".join(SIZE_LABELS[k] for k, *_ in SIZES if k in chosen) or "none = ANY size"
    return (step + "Tap the estimated values you can bid for ✅. "
            "Tenders that don't publish a value are always included.\n\nSelected: " + esc(picked)), ikb(kb)


def dept_picker(con, sub, page, wizard=False):
    chosen = set(_rules(con, sub).get("dept", []))
    rows = _depts(con)
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    w = "w" if wizard else "s"
    kb = [[btn(("✅ " if r["dept_code"] in chosen else "▫️ ") + f"{short_name(r['dept_name'])} ({r['n']})",
               f"dt:{w}:{page}:{r['dept_code']}")] for r in rows[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page > 0:
        nav.append(btn("◀ Prev", f"dp:{w}:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("More ▶", f"dp:{w}:{page + 1}"))
    kb.append(nav)
    kb.append([btn("🌐 All departments", f"da:{w}")])
    kb.append(_done_button(wizard, "Next ➡ Districts", "rp:w"))
    step = "<b>Step 1 of 3 — Departments</b>\n" if wizard else "<b>Departments</b>\n"
    names = {r["dept_code"]: short_name(r["dept_name"]) for r in rows}
    picked = ", ".join(sorted(names.get(c, c) for c in chosen)) if chosen else "none yet = ALL departments"
    return (step + "Tap to tick ✅ the departments you want (tap again to remove). Busiest first; "
            "tap More ▶ for the rest. Number = open tenders now.\n\nSelected: " + esc(picked)), ikb(kb)


def settings(con, sub, prefix=""):
    rules = _rules(con, sub)
    names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM departments")}
    work = ", ".join(SECTOR_LABELS[k] for k, _ in SECTORS if k in rules.get("sector", [])) or "ALL types"
    dists = ", ".join(rules.get("district", [])) or "ALL of Bihar"
    if rules.get("district") and rules.get("nodistrict"):
        dists += " (+ tenders with no district)"
    size = ", ".join(SIZE_LABELS[k] for k, *_ in SIZES if k in rules.get("size", [])) or "ANY size"
    depts = ", ".join(short_name(names.get(c, c), 40) for c in rules.get("dept", [])) or "ALL departments"
    _, total = matcher.open_matching(con, sub, limit=1)
    mode = sub["mode"]
    text = (prefix + "<b>Your alerts</b>\n\n"
            f"🏛 Departments: {esc(depts)}\n📍 Districts: {esc(dists)}\n💰 Size: {esc(size)}\n🔧 Type of work: {esc(work)}\n"
            f"⏰ Alerts: {'every 3 hours' if mode == 'instant' else 'once a day at 7:45 AM'}\n\n"
            f"<b>{total}</b> open tenders match right now.")
    kb = [[btn("📋 Show my tenders", "o:0")],
          [btn("🏛 Departments", "dp:s:0"), btn("📍 Districts", "rp:s")],
          [btn("💰 Tender size", "zp:s"), btn("🔧 Type of work", "sp:s")],
          [btn("⏰ Every 3 hours" + (" ✅" if mode == "instant" else ""), "mi"),
           btn("🌅 Once a day" + (" ✅" if mode == "digest" else ""), "md")],
          [btn("🔕 Stop alerts", "stop")]]
    return text, ikb(kb)


# ------------------------------------------------------------------ browsing

def browse_menu():
    kb = [[btn("🔧 By type of work", "bw")], [btn("📍 By district", "bq")], [btn("🏛 By department", "bp:0")]]
    return ("<b>Browse open tenders</b>\nLook at any type of work, district or department "
            "(this does not change your alerts)."), ikb(kb)


def browse_work(con):
    counts = _sector_counts(con)
    return "<b>Pick a type of work</b>:", ikb([[btn(f"{label} ({counts.get(k, 0)})", f"bs:{k}:0")] for k, label in SECTORS])


def browse_depts(con, page):
    rows = _depts(con)
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    kb = [[btn(f"{short_name(r['dept_name'])} ({r['n']})", f"bd:{r['dept_code']}:0")]
          for r in rows[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page > 0:
        nav.append(btn("◀ Prev", f"bp:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("More ▶", f"bp:{page + 1}"))
    kb.append(nav)
    return "<b>Pick a department</b> to see its open tenders:", ikb(kb)


def browse_districts(con):
    counts = _district_counts(con)
    names = [d for d in sorted(DISTRICTS) if counts.get(d)]
    kb = [[btn(f"{d} ({counts[d]})", f"br:{d}:0") for d in names[i:i + 3]] for i in range(0, len(names), 3)]
    return "<b>Pick a district</b> to see its open tenders:", ikb(kb)


def _bot(con):
    """The bot's @username for the per-tender detail links (cached after the first lookup)."""
    from . import telegram
    try:
        return telegram.bot_username(con)
    except Exception:
        return None


def tender_page(rows, offset, title, more_data, bot=None, extra_rows=None):
    """List of (text, buttons) messages for rows[offset:offset+SHOW]."""
    if not rows:
        return [("No open tenders here right now.", ikb([[btn("🏠 Menu", "menu")]]))]
    chunk = rows[offset:offset + SHOW]
    shown_to = offset + len(chunk)
    parts = digest_parts([(t, []) for t in chunk], "telegram",
                         title=f"{title} — {offset + 1}-{shown_to} of {len(rows)}",
                         footer="Tap 👉 under a tender for full details and who can bid." if bot else None, bot=bot)
    out = [(m, None) for m, _ids in parts]
    kb = []
    if shown_to < len(rows):
        kb.append([btn(f"Show next {min(SHOW, len(rows) - shown_to)} ▶", more_data(shown_to))])
    kb += extra_rows or []
    kb.append([btn("🏠 Menu", "menu")])
    out[-1] = (out[-1][0], ikb(kb))
    return out


NIT_NOTES = {
    "started": "🤖 The rules for this tender are only inside its NIT file. I am reading it now – "
               "the full summary will follow here in about a minute.",
    "waiting": "🤖 I am already reading this NIT – the summary will follow here shortly.",
    "daily": "(Today's limit for reading NIT files is used up. Try again tomorrow, or open the NIT on the portal.)",
    "user": "(You have used today's NIT readings. Try again tomorrow, or open the NIT on the portal.)",
}


def repeat_tap(con, chat_id, what, secs=60):
    """True if this person asked for the same thing a moment ago (double tap): answer only once."""
    key = f"tap:{chat_id}:{what}"
    last = db.get_kv(con, key)
    now = db.now()
    if last and (parse_iso(now) - parse_iso(last)).total_seconds() < secs:
        return True
    db.set_kv(con, key, now)
    con.commit()
    return False


FILE_NOTES = {"started": "📥 Fetching {what} from the portal – it will arrive here in about a minute.",
              "waiting": "📥 Already fetching {what} – it will arrive here shortly.",
              "none": "This tender has no {what} on the portal.",
              "limit": "You have reached today's download limit. Please use the portal, or try tomorrow."}


def tender_detail(con, tid, chat_id=None):
    """Full detail of one tender. With chat_id, an NIT-only tender also gets its NIT read for them."""
    from .. import nit_reader
    row = con.execute("SELECT * FROM tenders WHERE org_tender_id=? OR tender_id=? "
                      "ORDER BY org_tender_id=? DESC LIMIT 1", (tid, tid, tid)).fetchone()
    if not row:
        return (f"No tender with ID {tid} in our records.", ikb([[btn("🏠 Menu", "menu")]]))
    t = db.tender_dict(row)
    text = detail_message(t, "telegram")
    if chat_id is not None:
        from .. import stats
        stats.bump(con, "detail")
    if chat_id is not None and nit_reader.enabled() and nit_reader.needs_reading(t.get("elig")):
        text += "\n\n" + NIT_NOTES[nit_reader.request(con, tid, chat_id)]
    from .. import files
    rows = [[btn(label, data) for label, data in files.buttons(t)]] if files.buttons(t) else []
    return (text, ikb(rows + [[btn("📋 My tenders", "o:0"), btn("🏠 Menu", "menu")]]))


# ------------------------------------------------------------------ dispatch

def handle_text(con, chat_id, text, name):
    """Bottom-keyboard taps and greetings. Returns list of (text, markup) or None if not a menu text."""
    t = OLD_LABELS.get((text or "").strip(), (text or "").strip())
    key = (t.lstrip("/").split() or [""])[0].split("@")[0].upper()   # "/start channel" -> START
    sub = _sub(con, chat_id)
    if key in ("START", "MENU", "HI", "HELLO", "JOIN", "SUBSCRIBE") or sub is None:
        sub, created = db.get_or_create_subscriber(con, "telegram", chat_id, name)
        if created:  # contractors want tenders quickly: alerts every 3 hours by default
            con.execute("UPDATE subscribers SET mode='instant' WHERE id=?", (sub["id"],))
            sub = _sub(con, chat_id)
        con.commit()
        payload = t.split()[1] if len(t.split()) > 1 else ""
        out = []
        if re.fullmatch(r"t\d{3,9}", payload):  # tapped 👉 Full details under a tender
            if repeat_tap(con, chat_id, payload):
                return []                       # double tap: the detail is already on its way
            out.append(tender_detail(con, int(payload[1:]), chat_id))
            if not created:
                return out
        msg, kb = welcome(con, sub, created)
        out.append((msg, kb or REPLY_KEYBOARD))
        if kb:  # setup shown: also attach the bottom keyboard with a short line
            out.append(("👇 These buttons stay at the bottom of the chat.", REPLY_KEYBOARD))
        return out
    if not sub["active"]:
        return None
    if t == KB_TENDERS:
        return handle_callback(con, chat_id, "o:0")
    if t == KB_DEPTS:
        return [dept_picker(con, sub, 0)]
    if t == KB_WORK:
        return [sector_picker(con, sub)]
    if t == KB_DIST:
        return [district_picker(con, sub)]
    if t == KB_BROWSE:
        return [browse_menu()]
    if t == KB_SETTINGS:
        return [settings(con, sub)]
    if key in ("STATS", "ADMIN") and str(chat_id) in config.ADMIN_CHAT_IDS:
        from .. import stats
        return [(stats.report(con), None)]
    return understand(con, chat_id, sub, text)


def understand(con, chat_id, sub, text):
    """Plain words instead of commands: a Tender ID, a district, a department, civil / electrical ..."""
    from . import smart
    q = smart.parse(con, text)
    k = q["kind"]
    if k == "command":
        return None                                    # old typed commands still work (DISTRICT Patna ...)
    if k == "id":
        if repeat_tap(con, chat_id, f"t{q['id']}"):
            return []
        return [tender_detail(con, q["id"], chat_id)]
    if k == "depts":
        text_, kb = browse_depts(con, 0)
        return [(text_, kb)]
    if k == "districts":
        return [browse_districts(con)]
    if k == "unknown":
        return [(smart.HINT.format(text=esc(q["text"][:60])), REPLY_KEYBOARD)]
    smart.remember(con, chat_id, q)
    from .. import stats
    stats.bump(con, "search")
    return search_results(con, chat_id, q, 0)


def search_results(con, chat_id, q, offset):
    from . import smart
    if len(q.get("depts") or []) > 3 and not q.get("districts") and not q.get("cats"):
        names = {r["dept_code"]: r for r in _depts(con)}
        kb = [[btn(f"{short_name(names[c]['dept_name'])} ({names[c]['n']})", f"bd:{c}:0")]
              for c in q["depts"] if c in names][:10]
        if kb:
            return [("Several departments match. <b>Pick one</b>:", ikb(kb + [[btn("🏠 Menu", "menu")]]))]
    rows = [t for t in _visible(con) if smart.matches(t, q)]
    title = smart.title(con, q)
    alert = [[btn("🔔 Alert me for these", "qa")]]
    if not rows:
        return [(f"No open tenders for <b>{esc(title)}</b> right now.\n"
                 "Tap below to get an alert as soon as one is published.", ikb(alert + [[btn("🏠 Menu", "menu")]]))]
    return tender_page(rows, offset, title, lambda o: f"q:{o}", bot=_bot(con), extra_rows=alert)


def handle_callback(con, chat_id, data):
    """Button taps. Returns list of (text, markup)."""
    sub = _sub(con, chat_id)
    if sub is None:
        sub, _ = db.get_or_create_subscriber(con, "telegram", chat_id, None)
    parts = data.split(":")
    kind = parts[0]
    wiz = len(parts) > 1 and parts[1] == "w"

    def toggle(kind_, value):
        rules = _rules(con, sub).get(kind_, [])
        (db.remove_rule if value in rules else db.add_rule)(con, sub["id"], kind_, value)
        con.commit()

    def clear(kind_):
        db.remove_rule(con, sub["id"], kind_)
        con.commit()

    if kind == "noop":
        return []
    if kind == "ti" and len(parts) > 1 and parts[1].isdigit():
        if repeat_tap(con, chat_id, f"t{parts[1]}"):
            return []
        return [tender_detail(con, int(parts[1]), chat_id)]
    if kind == "q" and len(parts) == 2 and parts[1].isdigit():
        from . import smart
        q = smart.recall(con, chat_id)
        return search_results(con, chat_id, q, int(parts[1])) if q else [("That search has expired – just type it again.", None)]
    if kind == "qa":
        from . import smart
        q = smart.recall(con, chat_id)
        if not q:
            return [("That search has expired – just type it again.", None)]
        if repeat_tap(con, chat_id, "qa"):
            return []
        smart.add_to_alerts(con, sub["id"], q)
        return [(f"🔔 Done. You will now get alerts for <b>{esc(smart.title(con, q))}</b>.\n"
                 "See or change everything in ⚙️ My settings.", ikb([[btn("⚙️ My settings", "set")]]))]
    if kind == "dl" and len(parts) == 3 and parts[1].isdigit():
        if repeat_tap(con, chat_id, data):
            return []
        from .. import files
        what = "the BOQ" if parts[2] == "boq" else "the NIT"
        r = files.request(con, int(parts[1]), parts[2], chat_id)
        return [] if r == "sent" else [(FILE_NOTES[r].format(what=what), None)]
    if kind in ("menu", "set"):
        return [settings(con, sub)]
    if kind == "sp":
        return [sector_picker(con, sub, wiz)]
    if kind == "st":
        toggle("sector", parts[2])
        return [sector_picker(con, sub, wiz)]
    if kind == "sa":
        clear("sector")
        return [sector_picker(con, sub, wiz)]
    if kind == "rp":
        return [district_picker(con, sub, wiz)]
    if kind == "rt":
        toggle("district", parts[2])
        return [district_picker(con, sub, wiz)]
    if kind == "rn":
        toggle("nodistrict", "1")
        return [district_picker(con, sub, wiz)]
    if kind == "ra":
        clear("district")
        clear("nodistrict")
        return [district_picker(con, sub, wiz)]
    if kind == "zp":
        return [size_picker(con, sub, wiz)]
    if kind == "zt":
        toggle("size", parts[2])
        return [size_picker(con, sub, wiz)]
    if kind == "za":
        clear("size")
        return [size_picker(con, sub, wiz)]
    if kind == "dp":
        return [dept_picker(con, sub, int(parts[2]), wiz)]
    if kind == "dt":
        toggle("dept", parts[3])
        return [dept_picker(con, sub, int(parts[2]), wiz)]
    if kind == "da":
        clear("dept")
        return [dept_picker(con, sub, 0, wiz)]
    if kind == "fin":
        return [settings(con, sub, "✅ <b>Setup complete!</b>\nYou'll get new tenders and deadline reminders here.\n\n")]
    if kind in ("mi", "md"):
        con.execute("UPDATE subscribers SET mode=? WHERE id=?", ("instant" if kind == "mi" else "digest", sub["id"]))
        con.commit()
        return [settings(con, _sub(con, chat_id))]
    if kind == "stop":
        con.execute("UPDATE subscribers SET active=0 WHERE id=?", (sub["id"],))
        con.commit()
        return [("🔕 Alerts stopped. Tap below to start again.", ikb([[btn("🔔 Start again", "resume")]]))]
    if kind == "resume":
        con.execute("UPDATE subscribers SET active=1 WHERE id=?", (sub["id"],))
        con.commit()
        return [settings(con, _sub(con, chat_id))]
    if kind == "o":
        rows, _ = matcher.open_matching(con, sub, limit=10_000)
        return tender_page(rows, int(parts[1]), "Your open tenders", lambda o: f"o:{o}", bot=_bot(con))
    if kind == "bw":
        return [browse_work(con)]
    if kind == "bp":
        return [browse_depts(con, int(parts[1]))]
    if kind == "bq":
        return [browse_districts(con)]
    if kind == "bs":
        key, off = parts[1], int(parts[2])
        rows = [t for t in _visible(con) if key in t["sectors"]]
        return tender_page(rows, off, SECTOR_LABELS.get(key, key), lambda o: f"bs:{key}:{o}", bot=_bot(con))
    if kind == "bd":
        code, off = parts[1], int(parts[2])
        rows = [t for t in _visible(con) if t["dept_code"] == code]
        name = rows[0]["dept_name"] if rows else code
        return tender_page(rows, off, short_name(name, 40), lambda o: f"bd:{code}:{o}", bot=_bot(con))
    if kind == "br":
        dist, off = parts[1], int(parts[2])
        rows = [t for t in _visible(con) if dist in t["districts"]]
        return tender_page(rows, off, f"{dist} district", lambda o: f"br:{dist}:{o}", bot=_bot(con))
    return [("That button has expired. Tap 🏠 Menu.", ikb([[btn("🏠 Menu", "menu")]]))]


# picker screens replace the tapped message in place; tender lists are sent as new messages
EDIT_PREFIXES = ("sp", "st", "sa", "rp", "rt", "rn", "ra", "zp", "zt", "za", "dp", "dt", "da",
                 "set", "fin", "mi", "md", "stop", "resume", "menu", "bw", "bp", "bq")


def edits_in_place(data):
    return data.split(":")[0] in EDIT_PREFIXES
