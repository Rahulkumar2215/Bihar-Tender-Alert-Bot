"""Build department-wise digest messages for Telegram (HTML) and WhatsApp (*bold* markdown)."""
import html
from collections import OrderedDict

from . import config, eligibility
from .normalize import fmt_dt, fmt_inr, now_ist, parse_iso

TELEGRAM_LIMIT = 3900   # API max is 4096
WHATSAPP_LIMIT = 3800   # API max is 4096


def _left(close_iso, now):
    close = parse_iso(close_iso)
    if not close:
        return ""
    secs = (close - now).total_seconds()
    if secs <= 0:
        return "closed"
    days, hours = int(secs // 86400), int(secs % 86400 // 3600)
    if days == 0:
        return f"{hours}h left" if hours else f"{max(1, int(secs // 60))} min left"
    return f"{days} day{'s' if days > 1 else ''} left"


def _b(text, style):
    return f"<b>{html.escape(text)}</b>" if style == "telegram" else f"*{text}*"


def _e(text, style):
    return html.escape(text) if style == "telegram" else text


def tender_block(t, reasons, style, now=None, bot=None):
    now = now or now_ist()
    tags = []
    if "new" in reasons:
        tags.append("🆕")
    if any(r.startswith("closing") for r in reasons):
        tags.append("⏰")
    if any(r.startswith("extended") for r in reasons):
        tags.append("📅")
    title = t["title"] if len(t["title"]) <= 170 else t["title"][:167] + "..."
    lines = [f"{''.join(tags)} {_b(title, style)}".strip()]

    meta = [f"ID {t['org_tender_id']}"] if t.get("org_tender_id") else []
    if t.get("ref_no"):
        meta.append(f"Ref: {t['ref_no']}")
    if t.get("category"):
        meta.append(t["category"].title() if t["category"].isupper() else t["category"])
    if t.get("districts"):
        meta.append(", ".join(t["districts"]))
    if meta:
        lines.append(_e(" · ".join(meta), style))

    money = []
    for label, key in (("Value", "value_inr"), ("EMD", "emd_inr"), ("Fee", "tender_fee_inr")):
        v = fmt_inr(t.get(key))
        if v:
            money.append(f"{label} {v}")
    if money:
        lines.append("💰 " + _e(" · ".join(money), style))

    lines.append(f"⏳ Closes {_e(fmt_dt(t['close_at']), style)} ({_left(t['close_at'], now)})")
    ext = next((r for r in reasons if r.startswith("extended:")), None)
    if ext:
        lines.append(f"📅 Deadline extended — now {_e(fmt_dt(ext.split(':', 1)[1]), style)}")
    if t.get("prebid_at") and parse_iso(t["prebid_at"]) and parse_iso(t["prebid_at"]) > now:
        lines.append(f"🤝 Pre-bid {_e(fmt_dt(t['prebid_at']), style)}")
    tid = t.get("org_tender_id") or t.get("tender_id")
    if bot and style == "telegram" and tid:  # opens the bot on this tender: who can bid, documents, fees
        lines.append(f'👉 <a href="https://t.me/{bot}?start=t{tid}">Full details &amp; who can bid</a>')
    return "\n".join(lines)


def group_by_dept(items):
    groups = OrderedDict()
    for t, reasons in sorted(items, key=lambda x: (x[0]["dept_name"], x[0]["close_at"] or "")):
        groups.setdefault((t["dept_code"], t["dept_name"]), []).append((t, reasons))
    # biggest departments first
    return OrderedDict(sorted(groups.items(), key=lambda kv: -len(kv[1])))


def digest_messages(items, style, title="Bihar tender alerts", now=None, bot=None, footer=None):
    """items: [(tender, reasons)] -> list of message strings, each under the channel's limit."""
    return [text for text, _ids in digest_parts(items, style, title, now, footer, bot)]


def digest_parts(items, style, title="Bihar tender alerts", now=None, footer=None, bot=None):
    """Like digest_messages, but each message comes with the Tender IDs it contains (for buttons)."""
    now = now or now_ist()
    limit = TELEGRAM_LIMIT if style == "telegram" else WHATSAPP_LIMIT
    n_new = sum(1 for _, r in items if "new" in r)
    n_close = sum(1 for _, r in items if any(x.startswith("closing") for x in r))
    n_ext = sum(1 for _, r in items if any(x.startswith("extended") for x in r))
    head = [_b(f"{title} — {now.strftime('%d %b %Y')}", style)]
    counts = []
    if n_new:
        counts.append(f"🆕 {n_new} new")
    if n_close:
        counts.append(f"⏰ {n_close} closing within {config.CLOSING_SOON_DAYS} days")
    if n_ext:
        counts.append(f"📅 {n_ext} extended")
    if counts:
        head.append(" · ".join(counts))

    blocks = [("\n".join(head), None)]
    for (code, name), rows in group_by_dept(items).items():
        dept_head = f"🏛 {_b(f'{name} ({code})', style)} — {len(rows)}"
        blocks.append((dept_head, None))
        for t, reasons in rows:
            blocks.append((tender_block(t, reasons, style, now, bot), t.get("org_tender_id") or t.get("tender_id")))
    if footer is None:
        footer = (f"Search the ID on {config.PORTAL_LINK}\n"
                  "Send DETAIL followed by the ID for who can bid, documents, EMD and fees.")
    if footer:
        blocks.append((_e(footer, style), None))
    return pack_ids(blocks, limit)


def pack_ids(blocks, limit):
    """[(text, id or None)] -> [(message, [ids])], each message under the limit."""
    out, cur, ids = [], "", []
    for b, tid in blocks:
        if len(b) > limit:
            b = b[: limit - 3] + "..."
        if cur and len(cur) + 2 + len(b) > limit:
            out.append((cur, ids))
            cur, ids = b, []
        else:
            cur = f"{cur}\n\n{b}" if cur else b
        if tid:
            ids.append(tid)
    if cur:
        out.append((cur, ids))
    return out


def pack(blocks, limit):
    msgs, cur = [], ""
    for b in blocks:
        if len(b) > limit:
            b = b[: limit - 3] + "..."
        if cur and len(cur) + 2 + len(b) > limit:
            msgs.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        msgs.append(cur)
    return msgs


def short_summary(items, max_len=900):
    """Single-line summary for a WhatsApp template parameter (no newlines allowed there)."""
    parts = []
    for (code, _name), rows in group_by_dept(items).items():
        parts.append(f"{code}: {len(rows)}")
    s = " | ".join(parts)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def detail_message(t, style, now=None):
    """Everything we know about one tender (the portal blocks direct tender links)."""
    now = now or now_ist()
    lines = [_b(t["title"], style), ""]
    rows = [("Tender ID", t.get("org_tender_id")), ("Ref no.", t.get("ref_no")),
            ("Department", f"{t['dept_name']} ({t['dept_code']})"), ("Category", t.get("category")),
            ("Type", t.get("tender_type")), ("District", ", ".join(t.get("districts") or []) or None),
            ("Estimated value", fmt_inr(t.get("value_inr"))), ("EMD", fmt_inr(t.get("emd_inr"))),
            ("Tender fee", fmt_inr(t.get("tender_fee_inr"))), ("Processing fee", fmt_inr(t.get("processing_fee_inr"))),
            ("Published", fmt_dt(t.get("published_at")) if t.get("published_at") else None),
            ("Bid closes", f"{fmt_dt(t['close_at'])} ({_left(t['close_at'], now)})" if t.get("close_at") else None),
            ("Bid opens", fmt_dt(t.get("open_at")) if t.get("open_at") else None),
            ("Pre-bid meeting", fmt_dt(t.get("prebid_at")) if t.get("prebid_at") else None),
            ("Pre-bid venue", t.get("prebid_venue")),
            ("Documents", f"{t['attachments']} file(s) on the portal" if t.get("attachments") else None)]
    for k, v in rows:
        if v not in (None, ""):
            lines.append(f"{_b(k, style)}: {_e(str(v), style)}")
    if not t.get("detail_fetched"):
        lines.append(_e("(Value/EMD not fetched yet – check again after the next update.)", style))
    e = t.get("elig")
    if e:
        office = e.get("office")
        if office:
            lines.append(f"{_b('Office', style)}: {_e(office, style)}")
        if e.get("officer"):
            lines.append(f"{_b('Dealing officer', style)}: {_e(e['officer'], style)}")
        nit = e.get("nit")
        if nit:
            from .nit_reader import FIELDS
            lines += ["", _b(f"✅ Who can bid (read from {e.get('nit_file') or 'the NIT'})", style)]
            lines += [f"• {_e(label, style)}: {_e(nit[k], style)}" for k, label in FIELDS if nit.get(k)]
            lines += [f"• {_e(x, style)}" for x in nit.get("other") or []]
            rows_e = [(k, v) for k, v in eligibility.detail_lines(e) if k == "Bid validity" and not nit.get("bid_validity")]
            lines += [f"• {_e(k, style)}: {_e(v, style)}" for k, v in rows_e]
        else:
            rows_e = eligibility.detail_lines(e)
            if rows_e:
                lines += ["", _b("✅ Who can bid", style)]
                lines += [f"• {_e(k, style)}: {_e(v, style)}" for k, v in rows_e]
        if nit and nit.get("documents") and not e.get("docs"):
            lines += ["", _b("📎 Documents to upload", style)]
            lines += [f"• {_e(d, style)}" for d in nit["documents"]]
        if e.get("docs"):
            lines += ["", _b("📎 Documents to upload", style)]
            lines += [f"• {_e(d.capitalize() if d.isupper() else d, style)}" for d in e["docs"]]
        if e.get("files"):
            lines += ["", _b("📄 Files on the portal", style), _e(", ".join(e["files"]), style)]
        lines += ["", _e(("Summary read from the NIT by AI" if nit else "Summary made by reading the tender automatically")
                         + " – always confirm in the NIT before bidding.",
                         style)]
    elif t.get("detail_fetched"):
        lines.append(_e("(Who-can-bid summary will appear after the next update.)", style))
    lines.append("")
    lines.append(_e(f"Portal: {config.PORTAL_LINK} – search Tender ID {t.get('org_tender_id')}", style))
    text = "\n".join(lines)
    return text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 3] + "..."
