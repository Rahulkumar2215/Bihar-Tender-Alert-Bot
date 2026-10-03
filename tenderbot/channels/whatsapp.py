"""WhatsApp Cloud API (Meta's official API – no ban risk, unlike WhatsApp Web bots).

Meta's rules this module follows:
* A subscriber must message you first (START) – that is their opt-in.
* For 24h after their last message you may send free-form text (the full digest).
* Outside that window you may only send a pre-approved TEMPLATE. We send a short template
  ("You have N tender alerts: BCD: 3 | NBPDCL: 2 – reply 1 to see them"). Their reply
  re-opens the window and the full department-wise list is sent free of template charges.
"""
import hashlib
import hmac
import logging
from datetime import timedelta

import requests

from .. import commands, config, db
from ..normalize import now_ist, parse_iso

log = logging.getLogger(__name__)


def _url():
    return f"https://graph.facebook.com/{config.WA_API_VERSION}/{config.WA_PHONE_NUMBER_ID}/messages"


def _post(payload):
    if config.DRY_RUN:
        print(f"\n--- whatsapp -> {payload['to']} ---\n{payload.get('text', {}).get('body') or payload.get('template')}")
        return {}
    if not (config.WA_TOKEN and config.WA_PHONE_NUMBER_ID):
        raise RuntimeError("WA_TOKEN / WA_PHONE_NUMBER_ID are not set")
    r = requests.post(_url(), json=payload, timeout=30,
                      headers={"Authorization": f"Bearer {config.WA_TOKEN}"})
    if r.status_code >= 400:
        raise RuntimeError(f"WhatsApp API {r.status_code}: {r.text[:300]}")
    return r.json()


def send_text(to, body):
    return _post({"messaging_product": "whatsapp", "to": str(to), "type": "text",
                  "text": {"preview_url": False, "body": body}})


def send_template(to, params):
    """Body parameters must be single-line (Meta rejects newlines/tabs in template variables)."""
    clean = [" ".join(str(p).split())[:1000] for p in params]
    return _post({"messaging_product": "whatsapp", "to": str(to), "type": "template",
                  "template": {"name": config.WA_TEMPLATE_NAME, "language": {"code": config.WA_TEMPLATE_LANG},
                               "components": [{"type": "body",
                                               "parameters": [{"type": "text", "text": p} for p in clean]}]}})


def in_service_window(sub, now=None):
    last = parse_iso(sub["last_inbound_at"]) if sub["last_inbound_at"] else None
    return bool(last) and (now or now_ist()) - last < timedelta(hours=23, minutes=30)


# ------------------------------------------------------------------ webhook (Flask)

def create_app(con_factory):
    """Flask app for Meta's webhook. Must be reachable over public HTTPS (Render, Railway, a VPS...)."""
    from flask import Flask, abort, request

    app = Flask(__name__)

    @app.get("/webhook")
    def verify():
        if (request.args.get("hub.mode") == "subscribe"
                and request.args.get("hub.verify_token") == config.WA_VERIFY_TOKEN):
            return request.args.get("hub.challenge", ""), 200
        abort(403)

    @app.post("/webhook")
    def inbound():
        if config.WA_APP_SECRET:
            sig = request.headers.get("X-Hub-Signature-256", "")
            want = "sha256=" + hmac.new(config.WA_APP_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, want):
                abort(403)
        data = request.get_json(silent=True) or {}
        con = con_factory()
        try:
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
                    for m in value.get("messages", []):
                        frm = m.get("from")
                        if m.get("type") == "text":
                            text = m["text"]["body"]
                        elif m.get("type") == "button":          # quick-reply button on the template
                            text = m["button"].get("text", "1")
                        elif m.get("type") == "interactive":
                            ia = m["interactive"]
                            text = (ia.get("button_reply") or ia.get("list_reply") or {}).get("title", "")
                        else:
                            continue
                        for reply in commands.handle(con, "whatsapp", frm, text, names.get(frm)):
                            try:
                                send_text(frm, reply)
                            except Exception:
                                log.exception("reply to %s failed", frm)
        finally:
            con.close()
        return "ok", 200

    @app.get("/health")
    def health():
        return {"ok": True, "time": db.now()}

    return app
