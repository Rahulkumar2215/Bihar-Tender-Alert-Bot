"""Send each subscriber what is new / closing soon / extended for them, department-wise."""
import logging
import os
from datetime import timedelta

from . import config, db, matcher
from .channels import telegram, whatsapp
from .formatter import digest_messages, digest_parts, short_summary
from .normalize import fmt_dt, now_ist, parse_iso

log = logging.getLogger(__name__)
WA_TEMPLATE_MIN_GAP_H = int(os.getenv("WA_TEMPLATE_MIN_GAP_H", "12"))  # cap paid template sends


def notify_all(con, digest=False, only_subscriber=None):
    """digest=False: instant-mode subscribers only (run every few hours).
    digest=True: everyone (run once a day, e.g. 8 AM)."""
    _ensure_channels(con)
    if digest:
        _send_admin_report(con)
    q = "SELECT * FROM subscribers WHERE active=1"
    params = []
    if not digest:
        q += " AND mode='instant'"
    if only_subscriber:
        q += " AND id=?"
        params.append(only_subscriber)
    stats = {"subscribers": 0, "sent": 0, "queued": 0, "skipped": 0, "errors": 0}
    for sub in con.execute(q, params).fetchall():
        items = matcher.pending_items(con, sub)
        if _is_channel(sub) and not config.TELEGRAM_CHANNEL_REMINDERS:
            items = [(t, [r for r in rs if not r.startswith("closing")]) for t, rs in items]
            items = [(t, rs) for t, rs in items if rs]
        if not items:
            if digest and sub["channel"] == "telegram" and not _is_channel(sub):
                _send_all_quiet(con, sub)  # daily proof that the system is alive
            stats["skipped"] += 1
            continue
        stats["subscribers"] += 1
        try:
            if sub["channel"] == "telegram":
                for m, markup in telegram_digest(con, items, channel=_is_channel(sub)):
                    telegram.send(sub["address"], m, markup)
                matcher.record(con, sub["id"], items, "sent")
                stats["sent"] += len(items)
            elif sub["channel"] == "whatsapp":
                stats[_notify_whatsapp(con, sub, items)] += len(items)
        except PermissionError:
            log.info("subscriber %s blocked the bot – deactivating", sub["id"])
            con.execute("UPDATE subscribers SET active=0 WHERE id=?", (sub["id"],))
            con.commit()
        except Exception:
            stats["errors"] += 1
            log.exception("notify failed for subscriber %s", sub["id"])
    return stats


def telegram_digest(con, items, channel=False, title="Bihar tender alerts"):
    """Digest messages. Private chats: under each tender a link that opens its full details in the bot.
    The channel stays a clean list: one line at the end tells readers to send the Tender ID to the bot."""
    try:
        bot = telegram.bot_username(con)
    except Exception as e:  # no links is better than no alert
        log.warning("bot username unknown, sending without detail links: %s", e)
        bot = None
    if channel:
        footer = (f"For full details, who can bid, BOQ and NIT: open @{bot} and send the Tender ID."
                  if bot else f"Search the Tender ID on {config.PORTAL_LINK}")
        return [(text, None) for text in digest_messages(items, "telegram", title=title, footer=footer)]
    footer = (f"Tap 👉 under a tender for full details and who can bid.\nOr search the ID on {config.PORTAL_LINK}"
              if bot else None)
    return [(text, None) for text in digest_messages(items, "telegram", title=title, bot=bot, footer=footer)]


def _notify_whatsapp(con, sub, items):
    if whatsapp.in_service_window(sub):
        for m in digest_messages(items, "whatsapp"):
            whatsapp.send_text(sub["address"], m)
        matcher.record(con, sub["id"], items, "sent")
        return "sent"

    matcher.record(con, sub["id"], items, "queued")
    key = f"wa_template_at:{sub['id']}"
    last = db.get_kv(con, key)
    if last and now_ist() - parse_iso(last) < timedelta(hours=WA_TEMPLATE_MIN_GAP_H):
        return "queued"  # they already have a nudge waiting; it'll include these when they reply
    waiting = matcher.queued_items(con, sub["id"])
    whatsapp.send_template(sub["address"], [
        (sub["name"] or "there").split()[0],
        str(len(waiting)),
        short_summary(waiting),
    ])
    db.set_kv(con, key, db.now())
    con.commit()
    return "queued"


def _is_channel(sub):
    a = str(sub["address"])
    return sub["channel"] == "telegram" and (a.startswith("@") or a.startswith("-100"))


def _ensure_channels(con):
    """Channels listed in TELEGRAM_CHANNELS are subscribers in instant mode (all departments by default)."""
    for ch in config.TELEGRAM_CHANNELS:
        row, created = db.get_or_create_subscriber(con, "telegram", ch, ch)
        if created:
            con.execute("UPDATE subscribers SET mode='instant' WHERE id=?", (row["id"],))
            log.info("registered channel %s", ch)
    con.commit()
    for ch in config.TELEGRAM_CHANNELS:
        try:
            telegram.ensure_channel_promo(con, ch)
        except Exception as e:
            log.warning("channel promo for %s not posted yet: %s", ch, e)


def _send_all_quiet(con, sub):
    rows, total = matcher.open_matching(con, sub, limit=1)
    msg = f"Good morning. No new tenders for your choices since yesterday. {total} open tenders match"
    if rows:
        msg += f"; the next one closes {fmt_dt(rows[0]['close_at'])}"
    msg += ".\nTap 📋 My tenders to see them."
    try:
        telegram.send(sub["address"], msg)
    except Exception:
        log.exception("all-quiet message failed for subscriber %s", sub["id"])


def _send_admin_report(con):
    """Morning numbers for the owner (ADMIN_CHAT_IDS in .env)."""
    from . import stats
    for admin in config.ADMIN_CHAT_IDS:
        try:
            telegram.send(admin, stats.report(con))
        except Exception:
            log.exception("admin report to %s failed", admin)
