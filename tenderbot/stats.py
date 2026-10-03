"""Usage numbers for the owner: how many people use the bot and what they do with it."""
from datetime import timedelta

from . import config, db
from .normalize import parse_iso


def bump(con, name, n=1):
    """Count one event today (detail views, searches, downloads, NIT readings ...)."""
    key = f"stat:{db.now()[:10]}:{name}"
    db.set_kv(con, key, int(db.get_kv(con, key, 0)) + n)


def _day(con, name, day):
    return int(db.get_kv(con, f"stat:{day}:{name}", 0))


def report(con, channel_members=None):
    now = parse_iso(db.now())
    today = now.date().isoformat()
    yday = (now - timedelta(days=1)).date().isoformat()
    week = [(now - timedelta(days=i)).date().isoformat() for i in range(7)]
    admins = set(config.ADMIN_CHAT_IDS)
    users = [dict(r) for r in con.execute(
        "SELECT * FROM subscribers WHERE channel='telegram' AND address NOT LIKE '@%' AND address NOT LIKE '-100%'")]
    real = [u for u in users if u["address"] not in admins]
    active = [u for u in real if u["active"]]
    new_today = [u for u in real if (u["created_at"] or "")[:10] == today]
    new_week = [u for u in real if (u["created_at"] or "")[:10] in week]
    with_filters = {r[0] for r in con.execute("SELECT DISTINCT subscriber_id FROM subscriptions")}
    real_ids = {u["id"] for u in real}
    sent = lambda since: sum(1 for r in con.execute(
        "SELECT subscriber_id FROM deliveries WHERE status='sent' AND sent_at>=?", (since,)) if r[0] in real_ids)
    if channel_members is None:
        channel_members = _channel_members()
    lines = ["📊 <b>Bihar Tender Alerts – numbers</b>", ""]
    if channel_members:
        lines += [f"📢 Channel members: <b>{channel_members}</b>"]
    lines += [
        f"🤖 Bot users: <b>{len(active)}</b> active ({len(real) - len(active)} stopped) – not counting you",
        f"   New today: {len(new_today)} · last 7 days: {len(new_week)}",
        f"   Chose their own filters: {sum(1 for u in active if u['id'] in with_filters)} of {len(active)}",
        "",
        "<b>Today</b> (yesterday)",
    ]
    for name, label in (("detail", "Tender details opened"), ("search", "Typed searches"),
                        ("nit", "NITs read by AI"), ("file", "BOQ/NIT downloads")):
        lines.append(f"   {label}: {_day(con, name, today)} ({_day(con, name, yday)})")
    lines.append(f"   Alerts sent to users: {sent(today)} ({sent(yday) - sent(today)})")
    if new_week:
        lines += ["", "<b>Newest users</b>"]
        for u in sorted(new_week, key=lambda u: u["created_at"], reverse=True)[:5]:
            lines.append(f"   {(u['name'] or 'no name')[:30]} – joined {parse_iso(u['created_at']).strftime('%d %b %H:%M')}")
    return "\n".join(lines)


def _channel_members():
    from .channels import telegram
    total = 0
    for ch in config.TELEGRAM_CHANNELS:
        try:
            total += int(telegram._call("getChatMemberCount", chat_id=ch))
        except Exception:
            return None
    return total or None
