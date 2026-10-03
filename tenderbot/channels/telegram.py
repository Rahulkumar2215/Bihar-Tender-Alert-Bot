"""Telegram Bot API: send messages and answer subscriber commands (long polling, no server needed)."""
import html
import logging
import re
import time

import requests

from .. import commands, config, db

log = logging.getLogger(__name__)
CHANNEL_COMMANDS = {"START", "STOP", "ADD", "REMOVE", "DISTRICT", "CATEGORY", "KEYWORD",
                    "MINVALUE", "MAXVALUE", "CLEAR", "MODE", "MY"}
API = "https://api.telegram.org/bot{token}/{method}"


def _call(method, **params):
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    r = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method), json=params, timeout=90)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data["result"]


def send(chat_id, text, markup=None):
    if config.DRY_RUN:
        print(f"\n--- telegram -> {chat_id} ---\n{text}")
        return True
    for attempt in range(3):
        try:
            extra = {"reply_markup": markup} if markup else {}
            _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True,
                  **extra)
            time.sleep(0.05)  # stay well under Telegram's 30 msg/s limit
            return True
        except RuntimeError as e:
            if "blocked by the user" in str(e) or "chat not found" in str(e):
                raise PermissionError(str(e))
            if "can't parse entities" in str(e):
                # never lose an alert over formatting: resend as plain text
                log.warning("HTML rejected, resending as plain text: %s", e)
                extra = {"reply_markup": markup} if markup else {}
                _call("sendMessage", chat_id=chat_id, text=html.unescape(re.sub(r"</?(?:b|a)\b[^>]*>", "", text)),
                      disable_web_page_preview=True, **extra)
                return True
            if "Too Many Requests" in str(e):
                time.sleep(3 * (attempt + 1))
                continue
            raise
    return False


def send_document(chat_id, data=None, filename=None, file_id=None, caption=None):
    """Send a file. Returns Telegram's file_id so the same file can be re-sent without uploading."""
    if config.DRY_RUN:
        print(f"\n--- telegram document -> {chat_id}: {filename or file_id}")
        return file_id or "dry-run-file-id"
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendDocument")
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1000]
    if file_id:
        r = requests.post(url, data={**fields, "document": file_id}, timeout=120)
    else:
        r = requests.post(url, data=fields, files={"document": (filename, data)}, timeout=300)
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed: {res.get('description')}")
    return (res["result"].get("document") or {}).get("file_id")


def edit(chat_id, message_id, text, markup=None):
    if config.DRY_RUN:
        print(f"\n--- telegram edit {chat_id}/{message_id} ---\n{text}")
        return
    try:
        _call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
              disable_web_page_preview=True, **({"reply_markup": markup} if markup else {}))
    except RuntimeError as e:
        if "message is not modified" in str(e):
            return
        send(chat_id, text, markup)  # e.g. message too old to edit


def handle_update(con, u):
    """One Telegram update -> replies. Kept separate from the polling loop so it can be tested."""
    from . import tg_menu
    cq = u.get("callback_query")
    if cq:
        chat_id = cq["message"]["chat"]["id"]
        data = cq.get("data") or ""
        try:
            _call("answerCallbackQuery", callback_query_id=cq["id"])
        except Exception:
            pass
        screens = tg_menu.handle_callback(con, chat_id, data)
        for i, (text, markup) in enumerate(screens):
            if i == 0 and tg_menu.edits_in_place(data):
                edit(chat_id, cq["message"]["message_id"], text, markup)
            else:
                send(chat_id, text, markup)
        return
    is_channel = "channel_post" in u
    msg = u.get("message") or u.get("channel_post") or {}
    text, chat = msg.get("text"), msg.get("chat", {})
    if not text or not chat:
        return
    if is_channel and text.strip().lstrip("/").split(" ")[0].split("@")[0].upper() not in CHANNEL_COMMANDS:
        return  # ordinary channel posts are ignored
    name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
    if not is_channel:
        screens = tg_menu.handle_text(con, chat["id"], text, name)
        if screens is not None:
            for text_, markup in screens:
                send(chat["id"], text_, markup)
            return
    replies = commands.handle(con, "telegram", chat["id"], text, name)
    if is_channel:  # keep the channel clean: one short confirmation, no help text
        replies = [replies[0].split("\n\n")[0]] if replies else []
    for reply in replies:
        send(chat["id"], reply)


def poll_forever(con, stop_when=None):
    """Answer commands like ADD NBPDCL, DISTRICT Patna, OPEN. Run this as a long-lived process."""
    offset = int(db.get_kv(con, "telegram_offset", 0))
    log.info("Telegram bot polling for commands...")
    while True:
        if stop_when and stop_when():
            log.info("code updated – exiting so the new version starts")
            return
        try:
            updates = _call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "channel_post", "callback_query"])
        except Exception as e:  # network hiccup: wait and retry
            log.warning("getUpdates failed: %s", e)
            time.sleep(30 if "Conflict" in str(e) else 5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle_update(con, u)
            except Exception:
                log.exception("failed to handle update %s", u.get("update_id"))
        db.set_kv(con, "telegram_offset", offset)
        con.commit()



PROMO_TEXT = (
    "🔔 <b>Get tenders for YOUR work and district</b>\n\n"
    "This channel shows every new tender from eproc2.bihar.gov.in.\n"
    "Want only your departments, your districts and your tender size, "
    "plus a reminder before the deadline?\n\n"
    "👇 Tap the button, press START and choose with a few taps. No typing needed. Free."
)


def tender_buttons(ids, bot=None):
    """One button per Tender ID. In private chats it opens the details right there;
    in the channel (bot given) it is a link that opens the bot on that tender."""
    keys = []
    for tid in ids:
        if bot:
            keys.append({"text": f"✅ {tid}", "url": f"https://t.me/{bot}?start=t{tid}"})
        else:
            keys.append({"text": f"✅ {tid}", "callback_data": f"ti:{tid}"})
    rows = [keys[i:i + 3] for i in range(0, len(keys), 3)]
    return {"inline_keyboard": rows} if rows else None


def bot_username(con):
    name = db.get_kv(con, "bot_username")
    if not name:
        name = _call("getMe")["username"]
        db.set_kv(con, "bot_username", name)
        con.commit()
    return name


def ensure_channel_promo(con, channel):
    """Post and pin, once per channel, a message with a button that opens the bot."""
    key = f"promo_pinned:{channel}"
    if db.get_kv(con, key) or config.DRY_RUN:
        return False
    url = f"https://t.me/{bot_username(con)}?start=channel"
    markup = {"inline_keyboard": [[{"text": "🔔 Get my alerts", "url": url}]]}
    msg = _call("sendMessage", chat_id=channel, text=PROMO_TEXT, parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=markup)
    try:
        _call("pinChatMessage", chat_id=channel, message_id=msg["message_id"], disable_notification=True)
    except RuntimeError as e:  # bot lacks "pin messages" right: the post is still there
        log.warning("could not pin promo in %s: %s", channel, e)
    db.set_kv(con, key, db.now())
    con.commit()
    log.info("promo posted in %s", channel)
    return True
