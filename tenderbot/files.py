"""Send a tender's BOQ / NIT file into the Telegram chat when someone taps 📥.

The portal only hands out files from inside its own page, so a hidden helper process opens the
portal, downloads the file and uploads it to Telegram. Telegram then gives us a file_id, which we
keep: every later request for the same file is sent instantly without touching the portal again.
"""
import json
import logging
import os
import re
import subprocess

from . import config, db
from .normalize import parse_iso

log = logging.getLogger(__name__)

KINDS = {
    "boq": ("📥 BOQ", re.compile(r"\bboq|bill of quantit|price ?bid|financial (?:bid|template)|\bsor\b", re.I)),
    "nit": ("📄 NIT", None),   # same file the AI reads
}
MAX_BYTES = 45 * 1024 * 1024   # Telegram bots can upload up to 50 MB


def _name(path):
    return path.split("|")[-1]


def pick(elig, kind):
    """The tender's BOQ or NIT file (portal path string), or None."""
    paths = (elig or {}).get("file_paths") or []
    if kind == "nit":
        from .nit_reader import pick_file
        return pick_file(elig)
    rx = KINDS[kind][1]
    hits = [p for p in paths if rx.search(_name(p).replace("_", " "))]
    # a spreadsheet BOQ is the useful one for pricing; PDF if that is all there is
    hits.sort(key=lambda p: not _name(p).lower().endswith((".xls", ".xlsx")))
    return hits[0] if hits else None


def buttons(t):
    """Download buttons for the detail screen: [(label, callback_data)]."""
    out = []
    for kind, (label, _) in KINDS.items():
        p = pick(t.get("elig"), kind)
        if p:
            ext = _name(p).rsplit(".", 1)[-1].lower() if "." in _name(p) else ""
            out.append((f"{label}{' (' + ext + ')' if ext else ''}", f"dl:{t.get('org_tender_id') or t['tender_id']}:{kind}"))
    return out


def request(con, org_id, kind, chat_id):
    """Returns 'sent' (from cache), 'started', 'waiting', 'none' or 'limit'."""
    from .channels import telegram
    row = con.execute("SELECT * FROM tenders WHERE org_tender_id=? OR tender_id=? LIMIT 1", (org_id, org_id)).fetchone()
    if not row:
        return "none"
    t = db.tender_dict(row)
    path = pick(t.get("elig"), kind)
    if not path:
        return "none"
    cached = db.get_kv(con, f"tgfile:{path}")
    from . import stats
    if cached:
        stats.bump(con, "file")
        con.commit()
        telegram.send_document(chat_id, file_id=cached, caption=_caption(t, path))
        return "sent"
    key = f"file_wait:{org_id}:{kind}"
    w = json.loads(db.get_kv(con, key, "{}") or "{}")
    if w.get("at") and (parse_iso(db.now()) - parse_iso(w["at"])).total_seconds() < 600 and w.get("chats"):
        if chat_id not in w["chats"]:
            w["chats"].append(chat_id)
            db.set_kv(con, key, json.dumps(w))
            con.commit()
        return "waiting"
    day = f"file_count:{db.now()[:10]}:{chat_id}"
    if int(db.get_kv(con, day, 0)) >= config.FILES_PER_USER_DAILY:
        return "limit"
    db.set_kv(con, day, int(db.get_kv(con, day, 0)) + 1)
    stats.bump(con, "file")
    db.set_kv(con, key, json.dumps({"at": db.now(), "chats": [chat_id]}))
    con.commit()
    start_sender(org_id, kind)
    return "started"


def start_sender(org_id, kind):
    from .supervisor import ROOT, _pythonw
    if config.DRY_RUN:
        log.info("DRY_RUN: would send %s of %s", kind, org_id)
        return
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([_pythonw(), "-m", "tenderbot", "send-file", str(org_id), kind], cwd=str(ROOT),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def _caption(t, path):
    return f"{_name(path)}\nTender ID {t.get('org_tender_id')} · {t['title'][:150]}"


def run_sender(con, org_id, kind, scraper=None):
    """Hidden helper process: download from the portal, upload to everyone who asked, remember file_id."""
    from .channels import telegram
    from .nit_reader import download
    key = f"file_wait:{org_id}:{kind}"
    row = con.execute("SELECT * FROM tenders WHERE org_tender_id=? OR tender_id=? LIMIT 1", (org_id, org_id)).fetchone()
    t = db.tender_dict(row) if row else None
    path = pick(t.get("elig"), kind) if t else None
    chats = (json.loads(db.get_kv(con, key, "{}") or "{}")).get("chats") or []
    ok = False
    try:
        if not path:
            raise ValueError("no such file")
        if scraper is None:
            from .scraper import PortalScraper
            with PortalScraper() as ps:
                data = download(ps.page, t["tender_id"], t["elig"].get("org_id"), path, pdf_only=False)
        else:
            data = download(scraper.page, t["tender_id"], t["elig"].get("org_id"), path, pdf_only=False)
        if len(data) > MAX_BYTES:
            raise ValueError("file too large for Telegram")
        file_id = None
        for chat in chats:
            if file_id:
                telegram.send_document(chat, file_id=file_id, caption=_caption(t, path))
            else:
                file_id = telegram.send_document(chat, data=data, filename=_name(path), caption=_caption(t, path))
        if file_id:
            db.set_kv(con, f"tgfile:{path}", file_id)
        ok = True
    except Exception as e:
        log.exception("sending %s of %s failed", kind, org_id)
        for chat in chats:
            try:
                telegram.send(chat, f"Sorry, I could not fetch that file just now ({'too large' if 'large' in str(e) else 'portal busy'}). "
                                    f"Please download it from the portal – search Tender ID {org_id}.")
            except Exception:
                pass
    db.set_kv(con, key, "{}")
    con.commit()
    return ok
