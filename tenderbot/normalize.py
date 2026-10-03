"""Turn the portal's raw JSON into clean tender records."""
import re
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# Bihar's 38 districts, plus common town / old-name aliases that show up in tender titles.
DISTRICTS = {
    "Araria": ["Forbesganj", "Jogbani"], "Arwal": ["Kurtha"],
    "Aurangabad": ["Daudnagar", "Navinagar", "Rafiganj"], "Banka": ["Bounsi"],
    "Begusarai": ["Teghra", "Barauni", "Bakhri"], "Bhagalpur": ["Naugachia", "Sultanganj", "Kahalgaon", "Sabour"],
    "Bhojpur": ["Arrah", "Jagdishpur", "Piro"], "Buxar": ["Dumraon"],
    "Darbhanga": ["Hayaghat", "Baheri", "Benipur"],
    "East Champaran": ["Motihari", "Purbi Champaran", "Raxaul", "Dhaka", "Chakia", "Sugauli", "Mehsi"],
    "Gaya": ["Bodh Gaya", "Bodhgaya", "Gayaji", "Gayajee", "Sherghati", "Dobhi", "Tekari", "Wazirganj", "Chandauti"],
    "Gopalganj": ["Mirganj"], "Jamui": ["Jhajha"], "Jehanabad": ["Makhdumpur"],
    "Kaimur": ["Bhabua", "Mohania", "Kudra"], "Katihar": ["Barsoi", "Manihari"], "Khagaria": ["Parbatta", "Beldaur", "Gogri"],
    "Kishanganj": ["Thakurganj"], "Lakhisarai": [], "Madhepura": ["Murliganj"],
    "Madhubani": ["Jaynagar", "Jhanjharpur", "Benipatti"], "Munger": ["Monghyr", "Jamalpur"],
    "Muzaffarpur": ["Baruraj", "Sakra", "Kurhani", "Minapur", "Paroo", "Saraiya", "Musahri", "Kanti", "Motipur"],
    "Nalanda": ["Bihar Sharif", "Biharsharif", "Rajgir", "Asthawan", "Sarmera", "Hilsa", "Islampur"],
    "Nawada": ["Rajauli", "Warisaliganj"],
    "Patna": ["Danapur", "Barh", "Masaurhi", "Phulwari", "Phulwarisharif", "Kankarbagh", "Khagaul", "Bihta",
              "Fatuha", "Mokama", "Bakhtiyarpur", "Patliputra"],
    "Purnia": ["Purnea", "Banmankhi", "Kasba", "Dhamdaha"],
    "Rohtas": ["Sasaram", "Dehri", "Dinara", "Kochas", "Nasriganj", "Bikramganj"], "Saharsa": ["Simri Bakhtiyarpur"],
    "Samastipur": ["Rosera", "Dalsinghsarai"], "Saran": ["Chapra", "Chhapra", "Sonpur", "Marhaura"],
    "Sheikhpura": ["Barbigha"], "Sheohar": [],
    "Sitamarhi": ["Dumra", "Bairgania", "Pupri"], "Siwan": ["Mairwa", "Maharajganj"], "Supaul": ["Simrahi", "Birpur", "Nirmali"],
    "Vaishali": ["Hajipur", "Mahua", "Lalganj"],
    "West Champaran": ["Bettiah", "Bagaha", "Pashchim Champaran", "Narkatiaganj", "Ramnagar"],
}

_DISTRICT_PATTERNS = []
for _canon, _name in sorted(((c, n) for c, al in DISTRICTS.items() for n in [c] + al), key=lambda x: -len(x[1])):
    if True:
        _DISTRICT_PATTERNS.append((re.compile(r"\b" + re.escape(_name).replace(r"\ ", r"[\s-]*") + r"\b", re.I), _canon))
# "Champaran" alone is ambiguous, so it is not matched without East/West.


def canonical_district(text):
    """Map a user-typed district or town name to the canonical district, or None."""
    text = (text or "").strip()
    for pat, canon in _DISTRICT_PATTERNS:
        if pat.fullmatch(text):
            return canon
    return None


def detect_districts(*texts):
    found = []
    blob = " ".join(t for t in texts if t)
    for pat, canon in _DISTRICT_PATTERNS:
        if canon not in found and pat.search(blob):
            found.append(canon)
            blob = pat.sub(" ", blob)   # "Simri Bakhtiyarpur" must not also count as Bakhtiyarpur (Patna)
    return found


def epoch_ms_to_iso(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, IST).isoformat(timespec="minutes")
    except (ValueError, OSError, OverflowError):
        return None


def parse_iso(s):
    return datetime.fromisoformat(s) if s else None


def now_ist():
    return datetime.now(IST)


def fmt_dt(iso):
    dt = parse_iso(iso)
    return dt.strftime("%d %b %Y, %I:%M %p") if dt else "-"


def fmt_inr(amount):
    """1170260000 -> '₹117.03 Cr', 2171000 -> '₹21.71 L', 10000 -> '₹10,000'."""
    if amount is None:
        return None
    a = float(amount)
    if a <= 0:
        return None
    if a >= 1e7:
        return f"₹{a / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"₹{a / 1e5:.2f} L"
    s = f"{int(round(a)):,}"
    return f"₹{s}"


def parse_amount(text):
    """'50L', '2cr', '5 lakh', '150000' -> rupees (int). None if unreadable."""
    t = (text or "").lower().replace(",", "").replace("₹", "").replace("rs", "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(cr|crore|crores|l|lac|lakh|lakhs|k)?", t)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"cr": 1e7, "crore": 1e7, "crores": 1e7, "l": 1e5, "lac": 1e5, "lakh": 1e5,
            "lakhs": 1e5, "k": 1e3}.get(m.group(2) or "", 1)
    return int(n * mult)


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def dept_code(code, dept_id):
    """Short code users type (NBPDCL, BCD...). Codes with spaces/commas become D<id>."""
    c = clean(code).upper()
    return c if re.fullmatch(r"[A-Z0-9_\-]{2,20}", c) else f"D{dept_id}"


def normalize_listing(t, orgs, proc_cats, tender_types):
    """One row of the portal's allTenderList -> our tender dict (listing-level fields)."""
    dept_id = t.get("currentdeptid")
    org = orgs.get(dept_id) or orgs.get(str(dept_id)) or {}
    title = clean(t.get("currentdescription"))
    from .sectors import classify
    category = proc_cats.get(t.get("currentproccatid"), "")
    code = dept_code(org.get("organizationCode"), dept_id)
    return {
        "sectors": classify(code, category, title),
        "tender_id": int(t["currenttenderid"]),
        "org_tender_id": t.get("currentOrgTenderId"),
        "ref_no": clean(t.get("currenttenderrefno")),
        "title": title,
        "dept_id": dept_id,
        "dept_name": clean(org.get("organizationName")) or f"Dept {dept_id}",
        "dept_code": dept_code(org.get("organizationCode"), dept_id),
        "category": proc_cats.get(t.get("currentproccatid"), ""),
        "tender_type": tender_types.get(t.get("currenttendertypeid"), ""),
        "published_at": epoch_ms_to_iso(t.get("currentTenderPublishDate")),
        "bid_start_at": epoch_ms_to_iso(t.get("currentbidStartDate")),
        "close_at": epoch_ms_to_iso(t.get("currentbidEndDate")),
        "open_at": epoch_ms_to_iso(t.get("currentbidOpenDate")),
        # place from the title or ref. no.; failing that, from the body itself (Patna Municipal Corporation)
        "districts": (detect_districts(title, clean(t.get("currenttenderrefno")))
                      or detect_districts(clean(org.get("organizationName")))),
    }


def summarize_detail(d):
    """Pull the useful bits out of previewTenderByTenderId's large response."""
    if not d:
        return {}
    out = {"value": None, "emd": None, "tender_fee": None, "processing_fee": None,
           "prebid": None, "prebid_venue": None, "attachments": 0, "query_string": d.get("queryString")}
    criteria, docs, files = [], [], []
    pac = d.get("pacamt")
    if pac and float(pac) > 0 and d.get("pacVisibilityFlag", "Y") != "N":
        out["value"] = float(pac)
    for tp in d.get("templates") or []:
        sec = (tp.get("subProcessName") or "").lower()
        fields = {f.get("code"): f.get("value") for f in tp.get("templateFieldList") or [] if f.get("value") not in (None, "")}
        if sec == "payment":
            kind = (fields.get("payment_type") or "").lower()
            amt = _num(fields.get("amount"))
            if "emd" in kind or "earnest" in kind or "bid security" in kind:
                out["emd"] = amt
            elif "processing" in kind:
                out["processing_fee"] = amt
            elif "fee" in kind or "cost" in kind:
                out["tender_fee"] = amt
        elif sec.startswith("pre-bid"):
            if fields.get("discussion_type") and fields.get("discussion_type") != "Not Required":
                out["prebid"] = epoch_ms_to_iso(fields.get("meeting_start_date"))
                out["prebid_venue"] = clean(fields.get("venue"))[:120] or None
        elif sec == "attachments" and fields.get("attach_file"):
            out["attachments"] += 1
            files.append(fields["attach_file"])
            if not out.get("files_group"):
                out["files_group"] = tp.get("templategroupId")
        elif sec == "general particulars":
            criteria += [v for k, v in fields.items() if k.startswith("unf") and isinstance(v, str)]
        elif sec.startswith("required attachment") and fields.get("supporting_doc"):
            docs.append(clean(fields["supporting_doc"]))
    from .eligibility import extract
    out["elig"] = extract(criteria, docs, out["value"], d.get("offerValidity"), clean(d.get("office") or "") or None,
                          clean(d.get("officer") or "") or None, [f.split("|")[-1] for f in files])
    out["elig"]["file_paths"] = files[:12]
    out["elig"]["files_group"] = out.pop("files_group", None)
    out["elig"]["org_id"] = d.get("orgid")
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
