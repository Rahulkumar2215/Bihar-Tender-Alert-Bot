"""Sort each tender into the kinds of work contractors think in, instead of department codes.

A tender can belong to more than one sector (a "road and drain" job is both Roads and Water & drainage).
Sand-ghat / mining e-auctions are their own sector and hidden from everyone for now (HIDE_MINING).
"""
import re

HIDE_MINING = True

# key, button label
SECTORS = [
    ("roads", "🛣 Roads & bridges"),
    ("buildings", "🏢 Buildings"),
    ("water", "💧 Water & drainage"),
    ("electrical", "⚡ Electrical & power"),
    ("municipal", "🏘 Municipal & ward works"),
    ("supply", "📦 Supply & services"),
]
LABELS = dict(SECTORS)

_DEPTS = {
    "roads": {"RCD_HQ", "BSRDCL", "BRPNNL", "RWD"},
    "buildings": {"BCD", "BSBCCL", "BSEIDCL", "BPBCC", "BSHB"},
    "water": {"PHED_HQ", "WRD", "MWRD"},
    "electrical": {"NBPDCL", "SBPDCL", "BSPTCL", "BSPGCL", "BSHPCL", "BREDA", "BSPHCL"},
    "municipal": {"UDHD_HQ", "PMC", "BUIDCL", "PSCL", "MSCL"},
}

_WORDS = {
    "roads": r"\broads?\b|\bbridges?\b|\bpul\b|\bculverts?\b|\bflyovers?\b|\bpcc\b|\bhighway|\bsadak|\btoll plaza"
             r"|\bbituminous|\bpavement|\bpath\b|\brob\b|\brub\b|\bcarriageway",
    "buildings": r"\bbuildings?\b|\bbulidings?\b|\bbhawan\b|\bschool\b|\bhostel\b|\bhospital\b|\baphc\b|\bphc\b|\bwellness cent"
                 r"|\bboundary wall|\bstadium\b|\bquarters?\b|\bcommunity hall|\bcommunity building"
                 r"|\bpanchayat sarkar bhawan|\bmaintenance of .*building|\boffice building|\bcollege\b",
    "water": r"\bdrains?\b|\bdrainage\b|\bnala\b|\bsewer|\bsewage\b|\bstp\b|\bwater supply|\btube ?wells?\b|\bboring\b"
             r"|\bpipe ?line|\bnal ?jal|\bcanal\b|\bembankment|\bponds?\b|\bwater tank|\bpiyau\b|\bflood\b|\bsluice",
    "electrical": r"\belectric|\bsolar\b|\btransformer|\bcables?\b|\bhigh mast|\bstreet ?lights?|\bsubstation|\bgss\b"
                  r"|\b\d+\s*kv\b|\bmeters?\b|\bpower\b|\blighting\b|\bdg set|\blift\b",
    "municipal": r"\bward\s*no|\bnagar\s*(panchayat|parishad|nigam)|\bmunicipal|\bsolid waste|\bmrf\b|\bcompost",
    "supply": r"\bsupply of\b|\bprocurement\b|\brate contract|\boutsourc|\bmanpower\b|\bconsultan|\bselection of (agency|agencies)"
              r"|\brequest for proposal|\brfp\b|\bhousekeeping|\bsecurity guard|\bhiring of|\bpurchase of|\bequipment\b"
              r"|\bdrugs?\b|\bsoftware\b|\bbooks?\b|\bfurniture",
}
_WORDS = {k: re.compile(v, re.I) for k, v in _WORDS.items()}
_LANDMARK = re.compile(r"\b(?:from|near|nearby|via|to|till|upto|up to|in front of|behind|towards)\b", re.I)


def is_mining(dept_code, category, title=""):
    return (dept_code == "MINES" or (category or "").upper() == "EAUCTION-MINING"
            or bool(re.search(r"\bsand\s*ghats?\b", title or "", re.I)))


def classify(dept_code, category, title):
    """Words in the title decide the type of work; the department is only a fallback.
    Municipal is the exception: any work by a city/town body counts as municipal."""
    if is_mining(dept_code, category, title):
        return ["mining"]
    title = title or ""
    cat = (category or "").upper()
    # the work is named before the landmarks: "RCC drain FROM Ram's house TO Kushwaha Bhawan"
    head = _LANDMARK.split(title, 1)[0]
    work = [k for k, _ in SECTORS if k != "municipal" and _WORDS[k].search(head)]
    if not work:
        work = [k for k, _ in SECTORS if k != "municipal" and _WORDS[k].search(title)]
    if not work:
        work = [k for k, _ in SECTORS if k != "municipal" and dept_code in _DEPTS.get(k, ())]
    if cat == "ELECTRICAL" and "electrical" not in work:
        work.append("electrical")
    if not work:
        work = ["supply" if cat in ("GENERAL", "IT-RELATED-WORKS") else "buildings"]
    if dept_code in _DEPTS["municipal"] or _WORDS["municipal"].search(title):
        work.append("municipal")
    return [k for k, _ in SECTORS if k in work]


# Tender size buttons (rupees). Tenders whose value is not published always pass a size filter.
SIZES = [
    ("s", "Under ₹50 L", 0, 50e5),
    ("m", "₹50 L – 2 Cr", 50e5, 2e7),
    ("l", "₹2 – 10 Cr", 2e7, 1e8),
    ("xl", "₹10 Cr +", 1e8, float("inf")),
]
SIZE_LABELS = {k: lab for k, lab, _, _ in SIZES}


def size_ok(value, chosen):
    if not chosen or not value:
        return True
    return any(lo <= value < hi for k, _, lo, hi in SIZES if k in chosen)
