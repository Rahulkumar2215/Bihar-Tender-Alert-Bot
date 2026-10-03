"""Who can bid? Turn the portal's qualification rows into a short, plain-English checklist.

The detail response (previewTenderByTenderId) carries, for many tenders:
* "GENERAL PARTICULARS" rows: the qualification criteria the bidder must meet
  (turnover, similar-work experience, bid capacity, equipment, staff, registration ...)
* "Required Attachment from Bidder" rows: the documents to upload
* offerValidity (days), the issuing office and the dealing officer
Smaller tenders often just say "as per NIT" – then the rules are only in the NIT file,
and we say so instead of guessing.
"""
import re

from .normalize import fmt_inr

_PCT_OF_COST = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:\(?[a-z ]{0,25}\)?\s*)?(?:of\s+)?(?:the\s+)?"
    r"(?:estimated|e\.?c\.?v|ecv|tender(?:ed)? (?:value|amount|cost)|contract value|put to tender|bid value|"
    r"cost of (?:the )?work|project cost|pac\b)", re.I)
_AMOUNT = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?|[a-z]+(?:[ -][a-z]+)?)\s*(crores?|cr\.?|lakhs?|lacs?)\b", re.I)
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25, "twenty-five": 25, "thirty": 30,
            "forty": 40, "fifty": 50, "sixty": 60, "seventy five": 75, "hundred": 100}
_CAPACITY = re.compile(r"\bA\s*[x×*]\s*N\s*[x×*]\s*(\d(?:\.\d)?)\s*[-–]\s*B\b", re.I)
_CLASS = re.compile(r"\bclass[\s-]*(I{1,3}V?|IV|V|[1-5])\b(?!\s*indiv)", re.I)
_YEARS = re.compile(r"last\s*(?:\[?\(?(\d|five|three|seven)\)?\]?\s*)(?:\(\w+\)\s*)?(?:financial\s*)?years?", re.I)
_COUNT_SIMILAR = re.compile(r"\b(one|two|three|1|2|3)\b[\s\[\]\(\)\d]*(?:similar|such)?\s*(?:completed\s*)?(?:similar )?(?:works?|contracts?)", re.I)

TOPICS = [  # order matters: first match wins
    ("capacity", r"bid(?:ding)?\s*capacity|A\s*[x×*]\s*N\s*[x×*]"),
    ("turnover", r"turn\s*over|turnover|\bMAAT\b|value of civil engineering construction work"),
    ("experience", r"similar|experience|past performance|completed works?|quantit(?:y|ies)|\bQTY\b"),
    ("networth", r"net\s*worth"),
    ("profit", r"net\s*profit"),
    ("credit", r"credit|financial resources|solvency|banker|liquid assets"),
    ("equipment", r"equipment|machin|plant\b|hot mix|testing facilit"),
    ("personnel", r"personnel|site engineer|supervisor|technical staff|key position|B\.?\s?E\.?\s*\(civil\)|diploma"),
    ("registration", r"contractors? registered|registered with|registration (?:with|certificate)|"
                     r"contractor registration|licen[cs]e|\bclass[\s-]*(?:I{1,3}|IV|V|[1-5])\b"),
    ("manufacturer", r"original manufacturer|production capacity|type test"),
]
TOPICS = [(k, re.compile(v, re.I)) for k, v in TOPICS]

_AS_PER_NIT = re.compile(r"^\W*(?:as per|accept(?:ed)?(?: all)? terms|all\b|nit\b|\d+\W*$)", re.I)


def _rupees(num, unit):
    num = num.strip().lower()
    val = _WORDNUM.get(num)
    if val is None:
        try:
            val = float(num.replace(",", ""))
        except ValueError:
            return None
    return val * (1e7 if unit.lower().startswith("cr") else 1e5)


def _years(text):
    m = _YEARS.search(text)
    if not m:
        return None
    g = m.group(1).lower()
    return {"five": 5, "three": 3, "seven": 7}.get(g) or int(g)


def _pct(text):
    m = _PCT_OF_COST.search(text)
    return float(m.group(1)) if m else None


def _abs_amount(text):
    m = _AMOUNT.search(text)
    return _rupees(m.group(1), m.group(2)) if m else None


def _money(pct, value, absolute=None):
    """'≥ ₹1.33 Cr (50% of cost)' – rupees when we know the estimated value."""
    if absolute:
        return f"≥ {fmt_inr(absolute)}"
    if pct is None:
        return None
    pct_s = f"{pct:g}%"
    if value:
        return f"≥ {fmt_inr(value * pct / 100)} ({pct_s} of cost)"
    return f"≥ {pct_s} of estimated cost"


def _count(text):
    m = _COUNT_SIMILAR.search(text)
    if not m:
        return None
    return {"one": 1, "two": 2, "three": 3}.get(m.group(1).lower()) or int(m.group(1))


def _apply(rules, topic, flat, value):
    r = rules.setdefault(topic, {})
    if topic == "turnover":
        pct, absolute = _pct(flat), _abs_amount(flat)
        if pct or absolute:
            r.setdefault("need", _money(pct, value, absolute))
        r.setdefault("years", _years(flat))
        if re.search(r"average|MAAT", flat, re.I):
            r["average"] = True
        elif re.search(r"any one\s*\[?\(?(?:1|one)?\)?\]?\s*(?:financial\s*)?year", flat, re.I):
            r["any_year"] = True
    elif topic == "experience":
        pct, absolute = _pct(flat), _abs_amount(flat)
        if (pct or absolute) and "need" not in r:
            r["need"] = _money(pct, value, absolute)
            r["count"] = _count(flat)
            if re.search(r"combined", flat, re.I):
                r["combined"] = True
        qty = re.search(r"supplied[^.]{0,30}?(\d{1,3})\s*%\s*of\s*(?:the\s*)?tender(?:ed)?\s*quantit", flat, re.I)
        if qty and "need" not in r:
            r["need"] = f"supplied ≥ {qty.group(1)}% of tendered quantity"
            r["supply"] = True
        if re.search(r"quantit|\bQTY\b", flat, re.I) and not r.get("supply"):
            r["quantities"] = True
        r.setdefault("years", _years(flat))
    elif topic == "capacity":
        m = _CAPACITY.search(flat)
        r["formula"] = f"A×N×{m.group(1)} − B" if m else r.get("formula") or "A×N×M − B"
    elif topic in ("networth", "credit"):
        pct, absolute = _pct(flat), _abs_amount(flat)
        if pct or absolute:
            r.setdefault("need", _money(pct, value, absolute))
        if topic == "networth" and re.search(r"positive", flat, re.I):
            r.setdefault("need", "positive")
    elif topic == "profit":
        m = re.search(r"profit in any\s*(\w+)\s*\[?\d?\]?\s*years?", flat, re.I)
        r["need"] = f"in {m.group(1).lower()} of last 5 years" if m else "required"
    elif topic == "equipment":
        items = [f"{' '.join(n.split())} {q.strip()}" for n, q in re.findall(
            r"\d\)\s*([A-Za-z][A-Za-z &/().-]{2,70}?)\s+(\d[\d,]*\s*(?:sft|sqft|nos?\.?|cum|kl)?)(?=\s|$)", flat, re.I)]
        if items:
            r["note"] = ", ".join(i.replace(" with Integral weigh Batching facility", "") for i in items[:6])
        if re.search(r"hot mix", flat, re.I):
            r["note"] = "own hot-mix plant" + (f"; {r['note']}" if items else "")
        r.setdefault("note", "own / hired machinery list")
    elif topic == "personnel":
        roles = [" ".join(x.split()) for x in re.findall(r"\d\)\s*([A-Za-z][A-Za-z .]{2,30}?)\s*/", flat)]
        if roles:
            r["note"] = ", ".join(dict.fromkeys(roles))
        r.setdefault("note", "site engineer / key staff with certificates")
    elif topic == "registration":
        m = _CLASS.search(flat)
        if m and not re.search(r"any contractor", flat, re.I):
            r["class"] = m.group(1).upper()
        if re.search(r"any contractor|any state government|central government", flat, re.I):
            r["open"] = True
        dept = re.search(r"registration with (?:the )?([A-Za-z ,]{3,45}?(?:Department|Dept\.?))", flat, re.I)
        if dept and re.search(r"after|before execution|letter of acceptance|LoA", flat, re.I):
            r["later"] = " ".join(dept.group(1).split())
    elif topic == "manufacturer":
        if re.search(r"original manufacturer", flat, re.I):
            r["note"] = "original manufacturers only"
        elif re.search(r"type test", flat, re.I):
            r.setdefault("note", "type-test report needed")


def _short_doc(d):
    d = " ".join(d.split()).strip(" .")
    if len(d) > 60:
        d = re.split(r"\s*[(:]\s*|\s+[-–]\s*|(?<=[a-z])-(?=[A-Z])", d)[0].strip(" .")
    if len(d) > 70:
        d = d[:70].rsplit(" ", 1)[0] + "…"
    return d


def extract(criteria, docs=None, value=None, validity=None, office=None, officer=None, files=None):
    """criteria: list of qualification texts; docs: documents to upload.
    Returns a dict that formatter.eligibility_lines() renders."""
    docs = [_short_doc(d) for d in (docs or []) if d and not re.search(r"any other doc", d, re.I)]
    docs = list(dict.fromkeys(d for d in docs if d))
    real = [c.strip() for c in criteria or []
            if c and len(c.strip()) > 3 and not (len(c.strip()) < 80 and _AS_PER_NIT.match(c.strip()))]
    out = {"rules": {}, "docs": docs[:15], "validity": validity, "office": office, "officer": officer,
           "files": (files or [])[:12], "nit_only": not real}
    rules = out["rules"]
    for text in real:
        flat = " ".join(text.split())
        topics = [k for k, rx in TOPICS if rx.search(flat)]
        if ("turnover" in topics or "capacity" in topics) and not re.search(r"similar", flat, re.I):
            topics = [k for k in topics if k != "experience"]  # "cost of completed works" is turnover talk
        for topic in topics:
            _apply(rules, topic, flat, value)
    # documents also tell us things the criteria rows did not
    joined = " | ".join(docs)
    if re.search(r"electrical licen", joined, re.I):
        rules.setdefault("registration", {})["licence"] = "electrical licence"
    if not rules.get("turnover") and re.search(r"turn\s*over|balance sheet", joined, re.I):
        rules["turnover"] = {"proof_only": True}
    if not rules.get("experience") and re.search(r"experience", joined, re.I):
        rules["experience"] = {"proof_only": True}
    for k in [k for k, v in rules.items() if not any(x for x in v.values())]:
        del rules[k]  # e.g. a bare "Registration No" row tells us nothing
    out["nit_only"] = not rules
    return out


def headline(e):
    """One or two short lines for the alert itself. Empty when we know nothing useful."""
    if not e:
        return []
    nit = e.get("nit")
    if nit:  # read from the NIT file by Claude
        bits = [f"{label} {nit[k]}" if label else nit[k] for k, label in
                (("open_to", ""), ("turnover", "Turnover"), ("experience", "Experience"), ("completion", "Finish in"))
                if nit.get(k)]
        bits = [b if len(b) <= 60 else b[:57].rsplit(" ", 1)[0] + "…" for b in bits]
        return [" · ".join(bits)] if bits else []
    r = e.get("rules") or {}
    parts = []
    reg = r.get("registration", {})
    if reg.get("class"):
        parts.append(f"Class {reg['class']} contractors")
    elif reg.get("open"):
        parts.append("Any govt-registered contractor")
    if r.get("manufacturer", {}).get("note"):
        parts.append(r["manufacturer"]["note"].capitalize())
    if reg.get("licence"):
        parts.append(reg["licence"].capitalize() + " needed")
    to = r.get("turnover", {})
    if to.get("need"):
        parts.append(f"Turnover {to['need'].split(' (')[0]}")
    ex = r.get("experience", {})
    if ex.get("need") and not ex.get("supply"):
        parts.append(f"{'Similar works' if ex.get('combined') else 'Similar work'} {ex['need'].split(' (')[0]}")
    if r.get("capacity"):
        parts.append("Bid capacity check")
    line1 = " · ".join(parts)
    if not line1:
        return []
    return [line1]


def detail_lines(e):
    """Full checklist for the DETAIL screen."""
    if not e:
        return []
    r = e.get("rules") or {}
    out = []
    reg = r.get("registration", {})
    if reg:
        who = (f"Class {reg['class']} contractors" if reg.get("class") else
               "Any contractor registered with Central/State Govt or a PSU" if reg.get("open") else "Registered contractors")
        if reg.get("later"):
            who += f" (register with {reg['later']} after award)"
        if reg.get("licence"):
            who += f"; {reg['licence']}"
        out.append(("Open to", who))
    if r.get("manufacturer", {}).get("note"):
        out.append(("Supplier", r["manufacturer"]["note"]))
    to = r.get("turnover")
    if to:
        if to.get("need"):
            yrs = f" in {to['years']} years" if to.get("years") else ""
            kind = ("average annual turnover" if to.get("average") else
                    "annual turnover in any one year" if to.get("any_year") else "annual turnover")
            out.append(("Turnover", f"{kind} {to['need']}{yrs.replace(' in', ', last') if yrs else ''}"))
        else:
            out.append(("Turnover", f"proof needed{' for last %d years' % to['years'] if to.get('years') else ''} (amount in NIT)"))
    ex = r.get("experience")
    if ex:
        if ex.get("need"):
            if ex.get("supply"):
                txt = ex["need"]
            elif ex.get("combined") and ex.get("count"):
                txt = f"up to {ex['count']} similar works, combined {ex['need']}"
            else:
                n = f"{ex['count']} " if ex.get("count") and ex["count"] > 1 else ""
                txt = f"{n}similar work{'s' if n else ''}, each {ex['need']}" if n else f"one similar work {ex['need']}"
            if ex.get("years"):
                txt += f", last {ex['years']} years"
            if ex.get("quantities") and not ex.get("supply"):
                txt += "; minimum quantities too"
            out.append(("Experience", txt))
        else:
            out.append(("Experience", "similar-work certificates needed" + ("; minimum quantities" if ex.get("quantities") else "")))
    if r.get("capacity"):
        out.append(("Bid capacity", f"{r['capacity']['formula']} must exceed the bid"))
    if r.get("networth"):
        out.append(("Net worth", r["networth"].get("need") or "required"))
    if r.get("profit"):
        out.append(("Net profit", r["profit"]["need"]))
    if r.get("credit"):
        out.append(("Bank credit", r["credit"].get("need") or "banker's certificate"))
    if r.get("equipment"):
        out.append(("Machinery", r["equipment"]["note"]))
    if r.get("personnel"):
        out.append(("Staff", r["personnel"]["note"]))
    if e.get("nit_only") and not out:
        out.append(("Eligibility", "given only in the NIT file on the portal"))
    if e.get("validity"):
        out.append(("Bid validity", f"{e['validity']} days"))
    return out
