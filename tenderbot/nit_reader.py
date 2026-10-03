"""Read a tender's NIT file with AI (Gemini free tier, or Claude) when the portal itself does not list the eligibility rules.

Many small works (municipal, BUIDCo, RWD ...) only say "as per NIT", and their NITs are scanned
pages or old Hindi fonts that ordinary text extraction cannot read. Claude reads the PDF pages
(Hindi or English, scanned or typed) and returns the bidder rules as short English facts.

Runs only when a subscriber taps ✅ on such a tender; the result is saved, so each NIT is read
once and every later tap is free. Daily limits keep the API bill predictable.
"""
import base64
import io
import json
import logging
import os
import re
import subprocess
import time

import requests

from . import config, db
from .normalize import parse_iso

log = logging.getLogger(__name__)
API_URL = "https://api.anthropic.com/v1/messages"

_DOWNLOAD_JS = """async ([tenderId, orgId, attach]) => {
  const path = attach.split('|')[0];
  const body = new URLSearchParams({Authorization: (window.jQuery && jQuery('#Authorization').val()) || ''});
  body.append('tempGrpId', parseInt(path.split('/')[0]));
  body.append('tenderId', tenderId);
  body.append('fileName', attach);
  body.append('orgId', orgId);
  const r = await fetch(contextPath + '/rest/openarea/downloadattachment.action',
                        {method: 'POST', body, credentials: 'same-origin'});
  if (!r.ok) return {error: r.status};
  const buf = new Uint8Array(await r.arrayBuffer());
  let bin = '';
  for (let i = 0; i < buf.length; i += 32768) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 32768));
  return {b64: btoa(bin)};
}"""

PROMPT = """This is the tender notice (NIT) of a Bihar government tender: "{title}" (estimated cost {value}).
It may be in Hindi or English, typed or scanned. Read it and give a contractor the rules for who can bid.

Reply with ONLY a JSON object, no other text. Use short plain English (max ~15 words per value).
Write amounts in rupees with L (lakh) / Cr (crore), e.g. "₹1.33 Cr". If a percentage of the estimated
cost is given, also give the rupee amount. Give the actual figures and conditions, e.g. "average annual turnover ≥ ₹50 L in last 3 years" –
never answer only with a reference like "see Annexure-5"; look through the pages for the real numbers.
Use null when the NIT does not say. Do not guess.
{{
  "open_to": "who may bid, e.g. contractor class/category and registering department",
  "turnover": "turnover requirement",
  "experience": "similar work experience requirement",
  "bid_capacity": "bid capacity formula or requirement",
  "net_worth": null,
  "bank_credit": "solvency / bank credit requirement",
  "machinery": "machinery the bidder must own or hire",
  "staff": "engineers / technical staff required",
  "completion": "time allowed to complete the work",
  "bid_validity": "bid validity",
  "other": ["up to 4 other important conditions, e.g. site visit, labour licence, GST, affidavit"],
  "documents": ["documents to upload, up to 10"]
}}"""

FIELDS = [("open_to", "Open to"), ("turnover", "Turnover"), ("experience", "Experience"),
          ("bid_capacity", "Bid capacity"), ("net_worth", "Net worth"), ("bank_credit", "Bank credit"),
          ("machinery", "Machinery"), ("staff", "Staff"), ("completion", "Completion"),
          ("bid_validity", "Bid validity")]

_NIT_NAME = re.compile(r"\b(?:nit|niq|nib|ifb|e-?nit|notice|ntt|tender notice|advertisement|vigyapan)", re.I)


def enabled():
    return bool(config.GEMINI_API_KEY or config.ANTHROPIC_API_KEY)


def needs_reading(elig):
    """True when the portal gave us nothing useful and there is an NIT we have not read yet."""
    if not elig or elig.get("nit") or not pick_file(elig):
        return False
    rules = elig.get("rules") or {}
    useful = any(v.get("need") or v.get("formula") or v.get("class") or v.get("open") for v in rules.values())
    return elig.get("nit_only") or not useful


def pick_file(elig):
    """The NIT among the tender's files: its name says NIT/NIQ/notice, else the first PDF."""
    paths = [p for p in (elig or {}).get("file_paths") or [] if p.split("|")[0].lower().endswith(".pdf")]
    named = [p for p in paths if _NIT_NAME.search(p.split("|")[-1].replace("_", " "))]
    return (named or paths or [None])[0]


# ------------------------------------------------------------------ limits

def _today_key(kind, who=""):
    return f"nit_{kind}:{db.now()[:10]}{':' + str(who) if who else ''}"


def allowed(con, chat_id):
    """(ok, reason). A daily cap overall and per person keeps the bill predictable."""
    if int(db.get_kv(con, _today_key("count"), 0)) >= config.NIT_DAILY_LIMIT:
        return False, "daily"
    if int(db.get_kv(con, _today_key("count", chat_id), 0)) >= config.NIT_PER_USER_DAILY:
        return False, "user"
    return True, ""


def _count(con, chat_id):
    for k in (_today_key("count"), _today_key("count", chat_id)):
        db.set_kv(con, k, int(db.get_kv(con, k, 0)) + 1)


# ------------------------------------------------------------------ queue (bot side)

def request(con, tender_id, chat_id):
    """Called when someone taps ✅ on an NIT-only tender. Returns 'started', 'waiting', or a refusal reason."""
    key = f"nit_wait:{tender_id}"
    w = json.loads(db.get_kv(con, key, "{}") or "{}")
    fresh = w.get("at") and (parse_iso(db.now()) - parse_iso(w["at"])).total_seconds() < 600
    if fresh and w.get("chats"):  # someone already asked; they all get the answer
        if chat_id not in w["chats"]:
            w["chats"].append(chat_id)
            db.set_kv(con, key, json.dumps(w))
            con.commit()
        return "waiting"
    ok, why = allowed(con, chat_id)
    if not ok:
        return why
    _count(con, chat_id)
    from . import stats
    stats.bump(con, "nit")
    db.set_kv(con, key, json.dumps({"at": db.now(), "chats": [chat_id]}))
    con.commit()
    start_reader(tender_id)
    return "started"


def start_reader(tender_id):
    """Separate hidden process: opening the portal takes ~30 s and must not block the bot."""
    from .supervisor import ROOT, _pythonw
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    if config.DRY_RUN:
        log.info("DRY_RUN: would read NIT of %s", tender_id)
        return
    subprocess.Popen([_pythonw(), "-m", "tenderbot", "read-nit", str(tender_id)], cwd=str(ROOT),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


# ------------------------------------------------------------------ reader (separate process)

def download(page, tender_id, org_id, attach, pdf_only=True):
    """Download one of the tender's files from the portal (it only serves them inside its own page)."""
    r = page.evaluate(_DOWNLOAD_JS, [tender_id, org_id or 538, attach])
    if not r or r.get("error") or not r.get("b64"):
        raise RuntimeError(f"file download failed: {r and r.get('error')}")
    data = base64.b64decode(r["b64"])
    if pdf_only and not data.startswith(b"%PDF"):
        raise RuntimeError("downloaded file is not a PDF")
    return data


def first_pages(pdf, max_pages):
    """Keep only the first pages: the rules are near the start, and pages are what cost money."""
    try:
        from pypdf import PdfReader, PdfWriter
        rd = PdfReader(io.BytesIO(pdf), strict=False)
        n = min(len(rd.pages), max_pages)
        # always re-save: scanner-made PDFs often have a broken structure the AI rejects ("no pages")
        w = PdfWriter()
        for p in rd.pages[:n]:
            w.add_page(p)
        out = io.BytesIO()
        w.write(out)
        return out.getvalue(), n
    except Exception as e:  # pypdf missing or odd PDF: send as is if it is small enough
        log.warning("could not trim PDF (%s); sending whole file", e)
        return pdf, None


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def ask_ai(pdf, title, value_text):
    """Gemini (free tier) when GEMINI_API_KEY is set, otherwise Claude."""
    if config.GEMINI_API_KEY:
        return ask_gemini(pdf, title, value_text)
    return ask_claude(pdf, title, value_text)


def ask_gemini(pdf, title, value_text):
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "application/pdf", "data": base64.standard_b64encode(pdf).decode()}},
            {"text": PROMPT.format(title=title[:200], value=value_text)},
        ]}],
        # 2.5+ models "think" first and that counts against the output limit: keep thinking short, room to answer
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": 8192,
                             "thinkingConfig": {"thinkingBudget": 1024}},
    }
    last = "no Gemini model configured"
    for model in dict.fromkeys(config.GEMINI_MODELS):
        for attempt in range(2):           # one retry when Google is busy (503)
            r = requests.post(GEMINI_URL.format(model=model), json=body, timeout=180,
                              headers={"x-goog-api-key": config.GEMINI_API_KEY, "content-type": "application/json"})
            data = r.json()
            if r.status_code == 200:
                cand = (data.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                try:
                    return parse_reply(text), {"model": model, **(data.get("usageMetadata") or {})}
                except ValueError:
                    last = (f"Gemini {model}: unreadable reply (finish={cand.get('finishReason')}, "
                            f"feedback={data.get('promptFeedback')}): {text[:300]!r}")
                    log.warning(last)
                    break
            last = f"Gemini {model} {r.status_code}: {(data.get('error') or {}).get('message', '')[:200]}"
            log.warning(last)
            if r.status_code == 503 and attempt == 0:
                time.sleep(8)
                continue
            break
        if r.status_code not in (200, 429, 404, 503):  # bad request etc.: another model will not help
            break
    raise RuntimeError(last)


def ask_claude(pdf, title, value_text):
    body = {
        "model": config.NIT_MODEL,
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.standard_b64encode(pdf).decode()}},
            {"type": "text", "text": PROMPT.format(title=title[:200], value=value_text)},
        ]}],
    }
    r = requests.post(API_URL, json=body, timeout=180, headers={
        "x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"Claude API {r.status_code}: {(data.get('error') or {}).get('message')}")
    text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
    return parse_reply(text), data.get("usage", {})


def parse_reply(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in reply")
    raw = json.loads(m.group(0))
    out = {}
    for k, _ in FIELDS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip() and v.strip().lower() not in ("null", "none", "not mentioned", "n/a"):
            out[k] = " ".join(v.split())[:200]
    for k, n in (("other", 4), ("documents", 10)):
        vals = [" ".join(str(x).split())[:120] for x in raw.get(k) or [] if str(x).strip()]
        if vals:
            out[k] = vals[:n]
    return out


def read_and_store(con, tender_id, scraper=None):
    """Download the NIT, have Claude read it, save the result into the tender's elig JSON."""
    from .normalize import fmt_inr
    row = con.execute("SELECT * FROM tenders WHERE tender_id=? OR org_tender_id=? LIMIT 1",
                      (tender_id, tender_id)).fetchone()
    if not row:
        raise ValueError(f"unknown tender {tender_id}")
    t = db.tender_dict(row)
    elig = t.get("elig") or {}
    attach = pick_file(elig)
    if not attach:
        raise ValueError("no NIT file listed for this tender")
    if scraper is None:
        from .scraper import PortalScraper
        with PortalScraper() as ps:
            pdf = download(ps.page, t["tender_id"], elig.get("org_id"), attach)
    else:
        pdf = download(scraper.page, t["tender_id"], elig.get("org_id"), attach)
    max_pages = config.NIT_MAX_PAGES or (15 if config.GEMINI_API_KEY else 6)  # Gemini pages cost very little
    pdf, pages = first_pages(pdf, max_pages)
    result, usage = ask_ai(pdf, t["title"], fmt_inr(t.get("value_inr")) or "not given")
    elig["nit"] = result
    elig["nit_file"] = attach.split("|")[-1]
    elig["nit_pages"] = pages
    elig["nit_read_at"] = db.now()
    con.execute("UPDATE tenders SET elig=? WHERE tender_id=?", (json.dumps(elig), t["tender_id"]))
    con.commit()
    log.info("NIT of %s read: %s pages, usage %s", t["tender_id"], pages, usage)
    return t["tender_id"], result


def run_reader(con, tender_id):
    """Entry point of the hidden process: read, then send the full detail to everyone who asked."""
    from .channels import telegram, tg_menu
    row = con.execute("SELECT tender_id FROM tenders WHERE tender_id=? OR org_tender_id=? LIMIT 1",
                      (tender_id, tender_id)).fetchone()
    key = f"nit_wait:{tender_id}"
    try:
        read_and_store(con, row[0] if row else tender_id)
        ok = True
    except Exception as e:
        log.exception("reading NIT of %s failed", tender_id)
        ok = False
        err = str(e)
    waiters = (json.loads(db.get_kv(con, key, "{}") or "{}")).get("chats") or []
    db.set_kv(con, key, "{}")
    con.commit()
    for chat in waiters:
        try:
            if ok:
                text, markup = tg_menu.tender_detail(con, int(tender_id))
                telegram.send(chat, "🤖 I read the NIT for you:\n\n" + text, markup)
            else:
                telegram.send(chat, f"Sorry, I could not read the NIT of tender {tender_id} just now "
                                    f"({'download failed' if 'download' in err else 'reading failed'}). "
                                    "Please open it on the portal.")
        except Exception:
            log.exception("could not send NIT result to %s", chat)
    return ok
