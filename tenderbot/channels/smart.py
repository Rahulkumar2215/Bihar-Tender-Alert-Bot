"""Understand plain words typed to the bot, so nobody has to remember commands.

  140276                 -> full details of that tender
  departments / dept     -> pick a department to see its tenders
  districts              -> pick a district
  Patna, Purnia          -> open tenders in those districts
  BCD / rural works      -> open tenders of that department
  civil / electrical     -> open tenders of that category
  civil patna            -> both together (different kinds AND, same kind OR)

Results come with a "🔔 Alert me for these" button that adds the same choices to the person's alerts.
"""
import json
import re

from .. import db
from ..normalize import DISTRICTS, canonical_district, detect_districts

CATEGORY_WORDS = {
    "civil": "CIVIL",
    "electrical": "ELECTRICAL", "electric": "ELECTRICAL", "electricity": "ELECTRICAL", "bijli": "ELECTRICAL",
    "mechanical": "MECHANICAL",
    "general": "GENERAL", "supply": "GENERAL", "goods": "GENERAL",
    "computer": "IT-RELATED-WORKS", "software": "IT-RELATED-WORKS",
}
CATEGORY_LABELS = {"CIVIL": "Civil", "ELECTRICAL": "Electrical", "MECHANICAL": "Mechanical", "GENERAL": "General",
                   "IT-RELATED-WORKS": "IT works"}
STOP = {"department", "departments", "dept", "depts", "deptt", "of", "and", "the", "tender", "tenders", "in", "for",
        "bihar", "ltd", "limited", "show", "me", "all", "wise", "district", "districts", "please",
        "pls", "list", "open", "new", "latest", "&", "-", "jila", "zila", "vibhag"}
LOOSE = {"work", "works"}            # used to pick "Rural Works", ignored when they match nothing
MUNICIPAL = {"nagar", "nigam", "parishad", "municipal", "municipality", "ulb"}
NAME_FILLER = STOP | {"state", "corporation", "company", "society", "co", "pvt"}
# commands the old typed interface understands: leave those to commands.py
COMMANDS = {"START", "STOP", "ADD", "FOLLOW", "REMOVE", "UNFOLLOW", "DISTRICT", "CATEGORY", "KEYWORD", "MINVALUE",
            "MAXVALUE", "CLEAR", "MODE", "MY", "LIST", "FILTERS", "STATUS", "OPEN", "TENDERS", "LATEST", "DETAIL",
            "DETAILS", "INFO", "HELP", "MENU", "?", "UNSUBSCRIBE", "1", "SHOW", "YES", "SEE", "DEPTS", "JOIN",
            "SUBSCRIBE", "HI", "HELLO"}


def _words(s):
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]


def _departments(con):
    return [dict(r) for r in con.execute("SELECT code, name FROM departments")]


def parse(con, text):
    """-> {"kind": "id"|"depts"|"districts"|"search"|"command"|"unknown", ...}"""
    raw = (text or "").strip()
    low = raw.lower().strip(" .?!")
    if not low:
        return {"kind": "unknown", "text": raw}
    m = re.fullmatch(r"(?:id|tender(?: id)?|no\.?)?\s*#?\s*(\d{5,7})", low)
    if m:
        return {"kind": "id", "id": int(m.group(1))}
    if re.fullmatch(r"(?:all\s+)?(?:departments?|depts?|deptt|vibhag|विभाग)", low):
        return {"kind": "depts"}
    if re.fullmatch(r"(?:all\s+)?(?:districts?|jila|zila|जिला)", low):
        return {"kind": "districts"}
    first = raw.lstrip("/").split()[0].split("@")[0].upper()
    if first in COMMANDS:
        return {"kind": "command"}

    words = _words(raw)
    districts = detect_districts(raw)
    district_words = set(w for d in districts for w in _words(d)) | {w for w in words if canonical_district(w)}
    cats = []
    for w in words:
        c = CATEGORY_WORDS.get(w)
        if c and c not in cats:
            cats.append(c)

    # departments: by short code (BCD, RWD, NBPDCL, RCD for RCD_HQ) or by words of the name
    depts, sectors = [], []
    rest = [w for w in words if w not in STOP and w not in district_words and w not in CATEGORY_WORDS]
    all_depts = _departments(con)
    for w in list(rest):
        hits = [d["code"] for d in all_depts if w.upper() in (d["code"], d["code"].split("_")[0])]
        if hits:
            depts += [h for h in hits if h not in depts]
            rest.remove(w)
    for attempt in (rest, [w for w in rest if w not in LOOSE], [w for w in rest if w not in LOOSE | MUNICIPAL]):
        hits = _name_hits(all_depts, attempt)
        if hits and len(hits) > 1 and {"department", "dept", "deptt"} & set(words):
            # "Building Construction Department": the department itself, not the corporation of the same name
            only = [c for c in hits if "department" in next(d["name"] for d in all_depts if d["code"] == c).lower()]
            hits = only or hits
        if hits:
            depts += [h for h in hits if h not in depts]
            rest = []
            break
    if rest and MUNICIPAL & set(rest):      # "nagar nigam", "nagar parishad": municipal works of any town
        sectors.append("municipal")
        rest = [w for w in rest if w not in MUNICIPAL | LOOSE]
    # a district typed in a department name ("Patna Municipal Corporation") is still a district filter – fine
    if not (districts or cats or depts or sectors):
        return {"kind": "unknown", "text": raw}
    return {"kind": "search", "depts": depts, "districts": districts, "cats": cats, "sectors": sectors,
            "unmatched": [w for w in rest if len(w) >= 3]}


def _name_hits(all_depts, rest):
    """Departments whose name contains every typed word (as a word start). With 2+ words typed, keep only the
    closest names, so "building construction" means BCD, not every building corporation."""
    rest = [w for w in rest if len(w) >= 3 and w not in NAME_FILLER]
    if not rest or all(w in LOOSE for w in rest):     # "civil work": no department meant
        return []
    scored = []
    for d in all_depts:
        name_words = [w for w in _words(d["name"]) if w not in NAME_FILLER]
        if all(any(nw.startswith(w) for nw in name_words) for w in rest):
            extra = sum(1 for nw in name_words if not any(nw.startswith(w) for w in rest))
            scored.append((extra, d["code"]))
    if len(rest) >= 2 and scored:
        best = min(e for e, _ in scored)
        scored = [x for x in scored if x[0] == best]
    return [c for _, c in sorted(scored)]


def matches(t, q):
    if q.get("depts") and t["dept_code"] not in q["depts"]:
        return False
    if q.get("districts") and not set(q["districts"]) & set(t["districts"]):
        return False
    if q.get("cats") and (t.get("category") or "").upper() not in q["cats"]:
        return False
    if q.get("sectors") and not set(q["sectors"]) & set(t.get("sectors") or []):
        return False
    return True


def title(con, q):
    names = {d["code"]: d["name"] for d in _departments(con)}
    parts = []
    if q.get("cats"):
        parts.append(" / ".join(CATEGORY_LABELS.get(c, c.title()) for c in q["cats"]))
    if q.get("districts"):
        parts.append(", ".join(q["districts"]))
    if q.get("sectors"):
        parts.append("Municipal / nagar works")
    if q.get("depts"):
        ds = q["depts"]
        parts.append(names.get(ds[0], ds[0]) if len(ds) == 1 else f"{len(ds)} departments")
    return " · ".join(parts) or "Search"


def remember(con, chat_id, q):
    db.set_kv(con, f"q:{chat_id}", json.dumps({k: q.get(k) for k in ("depts", "districts", "cats", "sectors")}))
    con.commit()


def recall(con, chat_id):
    v = db.get_kv(con, f"q:{chat_id}")
    return json.loads(v) if v else None


def add_to_alerts(con, sub_id, q):
    for code in q.get("depts") or []:
        db.add_rule(con, sub_id, "dept", code)
    for d in q.get("districts") or []:
        db.add_rule(con, sub_id, "district", d)
    for c in q.get("cats") or []:
        db.add_rule(con, sub_id, "category", c)
    for k in q.get("sectors") or []:
        db.add_rule(con, sub_id, "sector", k)
    con.commit()


HINT = ("I could not find “{text}”.\n\nYou can simply type:\n"
        "• a <b>Tender ID</b>, e.g. 140276\n"
        "• a <b>district</b>, e.g. Patna or Purnia\n"
        "• a <b>department</b>, e.g. BCD or Rural Works – or type <b>departments</b> to pick one\n"
        "• <b>civil</b> or <b>electrical</b>\n"
        "You can mix them, e.g. civil Patna. Or use the buttons below.")
