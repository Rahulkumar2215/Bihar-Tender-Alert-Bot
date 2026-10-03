"""End-to-end tests on real portal rows (tests/fixtures/listing.json). No network needed.

Run:  python -m pytest -q
"""
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tenderbot import commands, config, db, formatter, matcher, notifier, pipeline
from tenderbot import normalize
from tenderbot.channels import telegram, whatsapp
from tenderbot.normalize import IST
from tenderbot.scraper import parse_listing

FIX = json.loads((Path(__file__).parent / "fixtures" / "listing.json").read_text())
T0 = datetime(2026, 10, 3, 9, 0, tzinfo=IST)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": T0}
    f = lambda: state["now"]
    for mod in (normalize, db, matcher, formatter, notifier, whatsapp):
        monkeypatch.setattr(mod, "now_ist", f, raising=False)
    return state


@pytest.fixture
def con(clock):
    return db.connect(":memory:")


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "send", lambda chat, text, markup=None: sent.append(("tg", str(chat), text, markup)) or True)
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: sent.append(("wa", str(to), body)))
    monkeypatch.setattr(whatsapp, "send_template", lambda to, params: sent.append(("wa-template", str(to), params)))
    return sent


def scrape(con, data=FIX):
    tenders, orgs = parse_listing(data)
    return pipeline.store_listing(con, tenders, orgs)


def ids(items):
    return {t["tender_id"]: r for t, r in items}


# ------------------------------------------------------------------ parsing

def test_listing_parsed_with_departments_and_districts():
    tenders, _ = parse_listing(FIX)
    by = {t["tender_id"]: t for t in tenders}
    assert len(tenders) == 8
    assert by[139849]["dept_code"] == "NBPDCL" and by[139849]["category"] == "ELECTRICAL"
    assert by[139560]["districts"] == ["Nalanda"]          # "Asthawan, Nalanda"
    assert by[140471]["districts"] == ["West Champaran"]
    assert by[140667]["districts"] == ["Aurangabad"]       # Daudnagar
    assert by[140361]["districts"] == ["West Champaran"]   # Bettiah
    assert by[140959]["districts"] == ["Khagaria"]
    assert by[140471]["close_at"] == "2026-10-03T12:39+05:30"


def test_odd_department_codes_become_typeable():
    assert normalize.dept_code("BPSC, Patna", 2560) == "D2560"
    assert normalize.dept_code("nbpdcl", 1025) == "NBPDCL"


def test_detail_summary_extracts_value_emd_fees():
    s = normalize.summarize_detail(FIX["detail_140471"])
    assert s["value"] == 117026000.0
    assert s["emd"] == 2171000 and s["tender_fee"] == 10000 and s["processing_fee"] == 11800
    assert s["attachments"] == 1 and s["prebid_venue"].startswith("Office")
    assert normalize.fmt_inr(s["value"]) == "₹11.70 Cr" and normalize.fmt_inr(s["emd"]) == "₹21.71 L"


def test_amount_parsing():
    assert normalize.parse_amount("50L") == 5_000_000
    assert normalize.parse_amount("2 cr") == 20_000_000
    assert normalize.parse_amount("1,50,000") == 150_000
    assert normalize.parse_amount("lots") is None


# ------------------------------------------------------------------ alert logic

def test_first_scrape_is_baseline_but_closing_soon_is_sent(con, clock):
    scrape(con)
    sub, _ = db.get_or_create_subscriber(con, "telegram", "1", "Rahul")
    items = ids(matcher.pending_items(con, sub))
    # nothing is "new" (it was all on the portal already) ...
    assert not any("new" in r for r in items.values())
    # ... but the three tenders closing before 6 Oct 09:00 get a reminder
    assert set(items) == {139560, 140471, 140667}


def test_new_tender_sent_once_never_repeated(con, clock, outbox):
    scrape(con)
    sub, _ = db.get_or_create_subscriber(con, "telegram", "1", "Rahul")
    con.execute("UPDATE subscribers SET mode='instant' WHERE id=?", (sub["id"],))
    notifier.notify_all(con)                              # sends the closing reminders
    outbox.clear()

    clock["now"] = T0 + timedelta(hours=3)
    data = copy.deepcopy(FIX)
    fresh = copy.deepcopy(data["tenders"][6])
    fresh.update(currenttenderid=999001, currenttenderrefno="NEW/1",
                 currentdescription="Construction of PCC road in Purnia", currentbidEndDate=1792661441000)
    data["tenders"].append(fresh)
    new_ids, _ = scrape(con, data)
    assert new_ids == [999001]

    notifier.notify_all(con)
    assert len(outbox) == 1 and "PCC road in Purnia" in outbox[0][2] and "🆕" in outbox[0][2]
    assert "Purnia" in outbox[0][2]

    outbox.clear()
    scrape(con, data)                                     # same data again
    notifier.notify_all(con)
    assert outbox == []                                   # nothing repeated


def test_deadline_extension_is_reported_and_reminder_rearmed(con, clock, outbox):
    scrape(con)
    sub, _ = db.get_or_create_subscriber(con, "telegram", "1")
    con.execute("UPDATE subscribers SET mode='instant' WHERE id=?", (sub["id"],))
    notifier.notify_all(con)
    outbox.clear()

    data = copy.deepcopy(FIX)
    t = next(x for x in data["tenders"] if x["currenttenderid"] == 140667)
    t["currentbidEndDate"] += 2 * 86400 * 1000             # pushed by two days
    clock["now"] = T0 + timedelta(hours=1)
    _, extended = scrape(con, data)
    assert extended == [140667]
    notifier.notify_all(con)
    assert len(outbox) == 1 and "Deadline extended" in outbox[0][2]


def test_department_district_and_value_filters(con, clock):
    scrape(con)
    for tid, s in {140471: normalize.summarize_detail(FIX["detail_140471"])}.items():
        db.save_detail(con, tid, s)
    sub, _ = db.get_or_create_subscriber(con, "telegram", "7")

    commands.handle(con, "telegram", "7", "ADD NBPDCL")
    rows, _ = matcher.open_matching(con, sub)
    assert {t["dept_code"] for t in rows} == {"NBPDCL"}

    commands.handle(con, "telegram", "7", "CLEAR")
    commands.handle(con, "telegram", "7", "DISTRICT West Champaran")
    rows, _ = matcher.open_matching(con, sub)
    assert {t["tender_id"] for t in rows} == {140471, 140361}           # strictly West Champaran
    db.add_rule(con, sub["id"], "nodistrict", "1")                      # opt in to no-district tenders
    rows, _ = matcher.open_matching(con, sub)
    assert {t["tender_id"] for t in rows} == {140471, 140361, 139849}

    commands.handle(con, "telegram", "7", "MINVALUE 50cr")
    rows, _ = matcher.open_matching(con, sub)
    # 140471 is ₹11.7 Cr (dropped); the others have no published value (kept)
    assert {t["tender_id"] for t in rows} == {140361, 139849}


# ------------------------------------------------------------------ chat commands

def test_commands_flow(con, clock):
    scrape(con)
    r = commands.handle(con, "telegram", "42", "/start", "Rahul")
    assert "subscribed" in r[0]
    r = commands.handle(con, "telegram", "42", "DEPTS")
    assert "NBPDCL" in r[0] and "UDHD_HQ" in r[0]
    r = commands.handle(con, "telegram", "42", "ADD nbpdcl XYZ")
    assert "NBPDCL" in r[0] and "Unknown code(s): XYZ" in r[0]
    r = commands.handle(con, "telegram", "42", "MY")
    assert "NBPDCL" in r[0]
    r = commands.handle(con, "telegram", "42", "OPEN")
    assert "11KV XLPE" in r[0] and "<b>" in r[0]
    r = commands.handle(con, "telegram", "42", "STOP")
    assert "Unsubscribed" in r[0]


def test_unknown_user_must_opt_in_first(con):
    r = commands.handle(con, "whatsapp", "919800000000", "ADD BCD")
    assert "Send START" in r[0]
    assert con.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0] == 0


# ------------------------------------------------------------------ WhatsApp window

def test_whatsapp_outside_window_queues_then_reply_shows_full_list(con, clock, outbox):
    scrape(con)
    commands.handle(con, "whatsapp", "919811111111", "START", "Rahul Kumar")
    con.execute("UPDATE subscribers SET mode='instant', last_inbound_at=? WHERE address='919811111111'",
                ((T0 - timedelta(days=2)).isoformat(),))       # window closed
    stats = notifier.notify_all(con)
    assert stats["queued"] == 3
    kind, to, params = outbox[-1]
    assert kind == "wa-template" and params[0] == "Rahul" and params[1] == "3"
    assert "BRPNNL: 1" in params[2] and "\n" not in params[2]

    outbox.clear()
    notifier.notify_all(con)                                    # no new template spam
    assert outbox == []

    replies = commands.handle(con, "whatsapp", "919811111111", "1")
    assert "Toll Plaza" in "".join(replies) and "*" in replies[0]
    assert matcher.queued_items(con, 1) == []


def test_whatsapp_inside_window_sends_free_text(con, clock, outbox):
    scrape(con)
    commands.handle(con, "whatsapp", "919822222222", "START")
    con.execute("UPDATE subscribers SET mode='instant'")
    notifier.notify_all(con)
    assert outbox and outbox[0][0] == "wa"


# ------------------------------------------------------------------ formatting

def test_long_digest_is_split_under_limits(con, clock):
    scrape(con)
    items = [(t, ["new"]) for t in db.open_tenders(con)] * 40
    for style, limit in (("telegram", formatter.TELEGRAM_LIMIT), ("whatsapp", formatter.WHATSAPP_LIMIT)):
        msgs = formatter.digest_messages(items, style)
        assert len(msgs) > 1 and all(len(m) <= limit for m in msgs)


def test_live_detail_from_patna_municipal_corporation():
    s = normalize.summarize_detail(FIX["detail_140043_live"])
    assert (s["value"], s["emd"], s["tender_fee"], s["processing_fee"]) == (13439050, 268800, 10000, 5900)
    assert s["attachments"] == 2 and "Patna" in s["prebid_venue"]


def test_channel_gets_only_new_tenders(con, clock, outbox, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHANNELS", ["@bihar_tender_alerts"])
    scrape(con)
    notifier.notify_all(con)                      # registers the channel; nothing new yet,
    assert outbox == []                           # and closing reminders are skipped for channels
    clock["now"] = T0 + timedelta(hours=3)
    data = copy.deepcopy(FIX)
    fresh = copy.deepcopy(data["tenders"][0])
    fresh.update(currenttenderid=999002, currentdescription="New bridge work in Siwan", currentbidEndDate=1792661441000)
    data["tenders"].append(fresh)
    scrape(con, data)
    notifier.notify_all(con)
    assert len(outbox) == 1 and outbox[0][1] == "@bihar_tender_alerts" and "Siwan" in outbox[0][2]



def test_alerts_show_portal_id_and_detail_command(con, clock):
    scrape(con)
    db.save_detail(con, 140471, normalize.summarize_detail(FIX["detail_140471"]))
    commands.handle(con, "telegram", "9", "START")
    r = commands.handle(con, "telegram", "9", "OPEN")
    assert "ID 140276" in "".join(r)                 # portal's visible Tender/RFQ ID
    r = commands.handle(con, "telegram", "9", "DETAIL 140276")[0]
    assert "Toll Plaza" in r and "₹11.70 Cr" in r and "₹21.71 L" in r and "Pre-bid venue" in r
    assert "No tender" in commands.handle(con, "telegram", "9", "DETAIL 1")[0]



def test_telegram_html_only_uses_bold_tags(con, clock):
    """Telegram rejects the whole message on any unknown tag (this broke the channel on 3 Oct)."""
    import re as _re
    scrape(con)
    db.save_detail(con, 140471, normalize.summarize_detail(FIX["detail_140471"]))
    items = [(t, ["new"]) for t in db.open_tenders(con)]
    msgs = formatter.digest_messages(items, "telegram")
    commands.handle(con, "telegram", "5", "START")
    msgs += commands.handle(con, "telegram", "5", "OPEN") + commands.handle(con, "telegram", "5", "HELP")
    msgs += commands.handle(con, "telegram", "5", "DETAIL 140276")
    for m in msgs:
        assert set(_re.findall(r"</?([a-zA-Z]+)", m)) <= {"b", "a"}, m[:200]
        assert all(h.startswith("https://t.me/") for h in _re.findall(r'<a href="([^"]+)"', m))


def test_plain_text_fallback_when_html_rejected(monkeypatch):
    calls = []
    def fake(method, **p):
        calls.append(p)
        if p.get("parse_mode"):
            raise RuntimeError("Telegram sendMessage failed: Bad Request: can't parse entities")
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)
    assert telegram.send("1", "<b>Road</b> &amp; drain")
    assert calls[-1]["text"] == "Road & drain" and "parse_mode" not in calls[-1]



def test_daily_digest_sends_all_quiet_when_nothing_new(con, clock, outbox):
    scrape(con)
    commands.handle(con, "telegram", "8", "START")
    commands.handle(con, "telegram", "8", "ADD BMSICL")
    clock["now"] = T0 - timedelta(days=2)      # BMSICL tender closes 12 Oct: not new, not closing soon
    notifier.notify_all(con, digest=True)
    assert len(outbox) == 1 and "No new tenders" in outbox[0][2] and "1 open" in outbox[0][2]


def test_single_bot_lock_and_code_reload(tmp_path, monkeypatch):
    from tenderbot import supervisor
    lock = supervisor.acquire_lock()
    assert lock is not None and supervisor.bot_running() and supervisor.acquire_lock() is None
    lock.close()
    assert not supervisor.bot_running()
    (tmp_path / "a.py").write_text("x=1")
    monkeypatch.setattr(supervisor, "CODE_DIR", tmp_path)
    changed = supervisor.code_changed_checker()
    assert not changed()
    import os, time
    os.utime(tmp_path / "a.py", (time.time() + 10, time.time() + 10))
    assert changed()


def test_bot_loop_exits_when_code_changes(con, monkeypatch):
    monkeypatch.setattr(telegram, "_call", lambda *a, **k: [])
    telegram.poll_forever(con, stop_when=lambda: True)   # returns instead of looping forever


# ------------------------------------------------------------------ tap-only menus

class FakeTG:
    def __init__(self):
        self.calls = []

    def __call__(self, method, **p):
        self.calls.append((method, p))
        return {"message_id": len(self.calls)}

    def last(self, method=None):
        for m, p in reversed(self.calls):
            if method in (None, m):
                return p

    def buttons(self, method=None):
        kb = (self.last(method) or {}).get("reply_markup", {}).get("inline_keyboard", [])
        return [b for row in kb for b in row]


def tap(con, fake, label_part):
    b = next(b for b in fake.buttons() if label_part in b["text"])
    telegram.handle_update(con, {"callback_query": {"id": "1", "data": b["callback_data"],
                                                    "message": {"message_id": 5, "chat": {"id": 77}}}})
    return b


def say(con, text):
    telegram.handle_update(con, {"message": {"text": text, "chat": {"id": 77, "first_name": "Ramesh"}}})


def test_contractor_sets_up_everything_by_tapping(con, clock, monkeypatch):
    import re as _re
    fake = FakeTG()
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)
    scrape(con)
    db.save_detail(con, 140471, normalize.summarize_detail(FIX["detail_140471"]))   # toll plaza: ₹11.70 Cr

    say(con, "/start")                                        # step 1: departments
    assert any("Step 1 of 3 — Departments" in p.get("text", "") for _, p in fake.calls)
    fake.calls = [c for c in fake.calls if "inline_keyboard" in c[1].get("reply_markup", {})]
    tap(con, fake, "Pul Nirman")                              # BRPNNL (toll plaza)
    tap(con, fake, "Housing")                                 # UDHD_HQ (Bettiah road repair)
    assert "✅ Urban Dev and Housing" in "".join(b["text"] for b in fake.buttons("editMessageText"))
    tap(con, fake, "Next ➡ Districts")                        # step 2
    assert "Step 2 of 3" in fake.last()["text"]
    tap(con, fake, "West Champaran")
    tap(con, fake, "Next ➡ Tender size")                      # step 3
    assert "Step 3 of 3 — Tender size" in fake.last()["text"]
    tap(con, fake, "Under ₹50 L")
    tap(con, fake, "Finish")
    done = fake.last()["text"]
    assert "Setup complete" in done and "Pul Nirman" in done and "West Champaran" in done and "Under ₹50 L" in done
    assert "Type of work: ALL types" in done

    sub = con.execute("SELECT * FROM subscribers WHERE address='77'").fetchone()
    rules = db.subscriber_rules(con, sub["id"])
    assert sorted(rules["dept"]) == ["BRPNNL", "UDHD_HQ"] and rules["district"] == ["West Champaran"]
    assert rules["size"] == ["s"] and "sector" not in rules

    tap(con, fake, "Show my tenders")
    shown = fake.last("sendMessage")["text"]
    assert "Bettiah" in shown            # road repair, value not published -> kept
    assert "Toll Plaza" not in shown     # ₹11.70 Cr is above "Under ₹50 L"

    for m, p in fake.calls:              # valid Telegram HTML and short callback data everywhere
        if "text" in p:
            assert set(_re.findall(r"</?([a-zA-Z]+)", p["text"])) <= {"b"}, p["text"][:120]
        for b in [b for row in p.get("reply_markup", {}).get("inline_keyboard", []) for b in row]:
            assert len(b["callback_data"].encode()) <= 64


def test_sand_ghat_auctions_hidden_everywhere(con, clock, monkeypatch):
    fake = FakeTG()
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)
    scrape(con)
    sub, _ = db.get_or_create_subscriber(con, "telegram", "55")
    rows, _ = matcher.open_matching(con, sub, limit=100)           # no filters = everything
    assert rows and not any("Sand ghat" in t["title"] for t in rows)
    say(con, "/start")
    say(con, "🔎 Browse all")
    tap(con, fake, "By department")
    assert not any("MINES" in b["text"] or "Mines" in b["text"] for b in fake.buttons())


def test_browse_by_work_department_and_district_without_changing_alerts(con, clock, monkeypatch):
    fake = FakeTG()
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)
    scrape(con)
    say(con, "/start")
    say(con, "🔎 Browse all")
    tap(con, fake, "By type of work")
    tap(con, fake, "Electrical")
    assert "11KV XLPE" in fake.last("sendMessage")["text"]
    say(con, "🔎 Browse all")
    tap(con, fake, "By department")
    tap(con, fake, "Medical Services")
    assert "Wellness" in fake.last("sendMessage")["text"]
    say(con, "🔎 Browse all")
    tap(con, fake, "By district")
    tap(con, fake, "Nalanda")
    assert "Asthawan" in fake.last("sendMessage")["text"]
    sub = con.execute("SELECT * FROM subscribers WHERE address='77'").fetchone()
    assert db.subscriber_rules(con, sub["id"]) == {}          # browsing changed nothing


def test_bottom_keyboard_settings_and_stop_resume(con, clock, monkeypatch):
    fake = FakeTG()
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)
    scrape(con)
    say(con, "/start")
    kbd = next(p["reply_markup"]["keyboard"] for _, p in fake.calls if p.get("reply_markup", {}).get("keyboard"))
    assert [b["text"] for row in kbd for b in row] == ["📋 My tenders", "🏛 Departments", "📍 Districts",
                                                        "🔎 Browse all", "⚙️ My settings"]
    say(con, "⚙️ My settings")
    assert "ALL departments" in fake.last()["text"] and "ANY size" in fake.last()["text"]
    tap(con, fake, "Type of work")                            # optional extra filter
    assert "Type of work (optional)" in fake.last()["text"]
    tap(con, fake, "Electrical")
    assert "✅ ⚡ Electrical" in "".join(b["text"] for b in fake.buttons("editMessageText"))
    tap(con, fake, "Done")
    say(con, "🏛 Departments")
    tap(con, fake, "North Bihar Power")
    assert "✅ North Bihar Power" in "".join(b["text"] for b in fake.buttons("editMessageText"))
    tap(con, fake, "Done")
    say(con, "🔧 Type of work")                               # old bottom keyboards still work
    assert "Type of work (optional)" in fake.last("sendMessage")["text"]
    tap(con, fake, "Done")                                    # back to settings
    tap(con, fake, "Stop alerts")
    assert con.execute("SELECT active FROM subscribers WHERE address='77'").fetchone()[0] == 0
    tap(con, fake, "Start again")
    assert con.execute("SELECT active FROM subscribers WHERE address='77'").fetchone()[0] == 1
    say(con, "📋 My tenders / मेरे टेंडर")                      # keyboards from the first version still work
    assert "Your open tenders" in fake.last("sendMessage")["text"]
    say(con, "ADD BCD")                                       # typing still works for those who want it
    assert "Following: BCD" in fake.last("sendMessage")["text"]


def test_sector_classification():
    from tenderbot.sectors import classify, size_ok
    assert classify("BRPNNL", "CIVIL", "Construction of 6 Lane Toll Plaza") == ["roads"]
    assert set(classify("UDHD_HQ", "CIVIL", "PCC road and RCC drain in Ward No. 12")) == {"roads", "water", "municipal"}
    assert classify("NBPDCL", "ELECTRICAL", "Procurement of 11KV cable") [0] == "electrical"
    assert classify("MINES", "eAuction-Mining", "E-auction of sand ghat") == ["mining"]
    assert classify("BMSICL", "CIVIL", "Construction of Health & Wellness Centre building") == ["buildings"]
    assert size_ok(None, ["s"]) and size_ok(4e6, ["s"]) and not size_ok(5e6, ["s"]) and size_ok(5e6, ["m"])


def test_channel_promo_posted_and_pinned_once(con, monkeypatch):
    fake = FakeTG()
    real = fake.__call__
    def tg(method, **p):
        if method == "getMe":
            return {"username": "biar_tender_bot"}
        return real(method, **p)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", tg)
    assert telegram.ensure_channel_promo(con, "@bihar_tender_alerts")
    sent = fake.last("sendMessage")
    assert sent["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/biar_tender_bot?start=channel"
    assert fake.last("pinChatMessage")["disable_notification"] is True
    n = len(fake.calls)
    assert not telegram.ensure_channel_promo(con, "@bihar_tender_alerts")   # never reposted
    assert len(fake.calls) == n


def test_deep_link_start_from_channel_opens_setup(con, clock, monkeypatch):
    fake = FakeTG()
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)
    scrape(con)
    say(con, "/start channel")
    assert any("Step 1 of 3" in p.get("text", "") for _, p in fake.calls)



def test_minutes_left_when_under_an_hour():
    from datetime import timedelta as _td
    assert formatter._left((T0 + _td(minutes=36)).isoformat(), T0) == "36 min left"


# ------------------------------------------------------------------ who can bid (eligibility)

DET = json.loads((Path(__file__).parent / "fixtures" / "details_elig.json").read_text())


def test_eligibility_read_from_real_tenders():
    from tenderbot import eligibility
    bsbccl = normalize.summarize_detail(DET["140804"])["elig"]
    r = bsbccl["rules"]
    assert r["registration"]["open"] and "Building Construction" in r["registration"]["later"]
    assert r["turnover"]["need"].startswith("≥ ₹1.33 Cr (50%")          # 50% of ₹2.67 Cr
    assert r["experience"]["need"].startswith("≥ ₹1.33 Cr")
    assert r["capacity"]["formula"] == "A×N×3 − B"
    assert r["credit"]["need"].startswith("≥ ₹26.69 L")
    assert "Concrete Mixer 1" in r["equipment"]["note"] and r["personnel"]["note"] == "Site Engineer, Site Supervisor"
    assert bsbccl["validity"] == 120 and bsbccl["office"] == "DGM BSBCCL PATNA"
    assert "NIT-25.PDF" in bsbccl["files"]

    rcd = normalize.summarize_detail(DET["139915"])["elig"]["rules"]
    assert rcd["turnover"]["need"].startswith("≥ ₹44.43 Cr (40%") and rcd["turnover"]["any_year"]
    assert rcd["experience"]["combined"] and rcd["experience"]["count"] == 2
    assert rcd["equipment"]["note"].startswith("own hot-mix plant")

    cable = normalize.summarize_detail(DET["139849"])["elig"]
    assert cable["rules"]["manufacturer"]["note"] == "original manufacturers only"
    assert cable["rules"]["turnover"]["need"] == "≥ ₹50.00 Cr" and cable["rules"]["turnover"]["average"]
    assert eligibility.headline(cable) == ["Original manufacturers only · Turnover ≥ ₹50.00 Cr"]

    nit_only = normalize.summarize_detail(DET["140727"])["elig"]   # small road+drain: rules only in the NIT
    assert nit_only["nit_only"] and eligibility.headline(nit_only) == []
    assert ("Eligibility", "given only in the NIT file on the portal") in eligibility.detail_lines(nit_only)


def _with_elig(con):
    scrape(con)
    tid = FIX["tenders"][0]["currenttenderid"]
    db.save_detail(con, tid, normalize.summarize_detail(DET["140804"]))
    con.commit()
    return db.tender_dict(con.execute("SELECT * FROM tenders WHERE tender_id=?", (tid,)).fetchone())


def test_each_tender_has_a_detail_link_instead_of_a_summary(con, clock, monkeypatch):
    t = _with_elig(con)
    db.set_kv(con, "bot_username", "biar_tender_bot")
    link = f'<a href="https://t.me/biar_tender_bot?start=t{t['org_tender_id']}">'
    msgs = notifier.telegram_digest(con, [(t, ["new"])])          # private alert: link under each tender
    text = msgs[-1][0]
    assert link in text and "Full details &amp; who can bid" in text
    assert "Turnover" not in text and msgs[-1][1] is None   # no summary line, no button grid
    ch = notifier.telegram_digest(con, [(t, ["new"])], channel=True)[-1]   # channel: clean list, no links
    assert "<a " not in ch[0] and ch[1] is None
    assert "open @biar_tender_bot and send the Tender ID" in ch[0]
    detail = formatter.detail_message(t, "telegram")
    assert "Who can bid" in detail and "Documents to upload" in detail and "A×N×3 − B" in detail

    # the "My tenders" list in the bot: link under each tender, only menu buttons below
    sub, _ = db.get_or_create_subscriber(con, "telegram", 77, "R")
    from tenderbot.channels import tg_menu
    page = tg_menu.tender_page([t], 0, "Your open tenders", lambda o: f"o:{o}", bot="biar_tender_bot")
    assert link in page[-1][0] and [b["text"] for row in page[-1][1]["inline_keyboard"] for b in row] == ["🏠 Menu"]


def test_detail_link_opens_the_bot_on_that_tender(con, clock, monkeypatch):
    t = _with_elig(con)
    fake = FakeTG()
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)
    say(con, f"/start t{t['org_tender_id']}")
    texts = [p.get("text", "") for m, p in fake.calls if m == "sendMessage"]
    assert "Who can bid" in texts[0]                 # the tender they tapped comes first
    assert any("Step 1 of 3" in x for x in texts)    # then setup, because they are new
    fake.calls.clear()
    say(con, f"/start t{t['org_tender_id']}")        # accidental double tap: nothing sent twice
    assert not [p for m, p in fake.calls if m == "sendMessage"]
    clock["now"] += timedelta(minutes=2)
    say(con, f"/start t{t['org_tender_id']}")        # asking again later: just the detail
    texts = [p.get("text", "") for m, p in fake.calls if m == "sendMessage"]
    assert len(texts) == 1 and "Who can bid" in texts[0]


def test_old_tenders_get_eligibility_backfilled(con, clock):
    scrape(con)
    ids_ = [t["currenttenderid"] for t in FIX["tenders"]][:3]
    for tid in ids_:   # fetched before this feature existed: no elig column value
        db.save_detail(con, tid, {"value": 1e6, "attachments": 0})
    con.commit()
    assert set(ids_) <= set(db.ids_needing_elig(con, 50))
    db.save_detail(con, ids_[0], normalize.summarize_detail(DET["140727"]))
    assert ids_[0] not in db.ids_needing_elig(con, 50)


# ------------------------------------------------------------------ reading NIT files with Claude

def _nit_only(con):
    scrape(con)
    tid = FIX["tenders"][0]["currenttenderid"]
    db.save_detail(con, tid, normalize.summarize_detail(DET["140727"]))   # Jehanabad road+drain: rules only in NIT
    con.commit()
    return db.tender_dict(con.execute("SELECT * FROM tenders WHERE tender_id=?", (tid,)).fetchone())


def test_nit_file_is_picked_and_reply_parsed():
    from tenderbot import nit_reader
    e = normalize.summarize_detail(DET["140727"])["elig"]
    assert nit_reader.needs_reading(e)
    assert nit_reader.pick_file(e).split("|")[-1] == "NIT 02 26-27 Jehanabad.pdf"
    assert not nit_reader.needs_reading(normalize.summarize_detail(DET["140804"])["elig"])  # portal had the rules
    r = nit_reader.parse_reply('Here: {"open_to": "Contractors registered in appropriate class with any works dept",'
                               ' "turnover": null, "completion": "4 months", "other": ["Visit site before bidding"],'
                               ' "documents": ["PAN", "GST"]}')
    assert r == {"open_to": "Contractors registered in appropriate class with any works dept",
                 "completion": "4 months", "other": ["Visit site before bidding"], "documents": ["PAN", "GST"]}


def test_tapping_nit_only_tender_reads_nit_once_for_everyone(con, clock, monkeypatch):
    from tenderbot import nit_reader
    t = _nit_only(con)
    tid = t["org_tender_id"]
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    started = []
    monkeypatch.setattr(nit_reader, "start_reader", lambda x: started.append(x))
    fake = FakeTG()
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)

    def tap_as(chat):
        telegram.handle_update(con, {"callback_query": {"id": "1", "data": f"ti:{tid}",
                                                        "message": {"message_id": 5, "chat": {"id": chat}}}})
        return fake.last("sendMessage")["text"]

    assert "I am reading it now" in tap_as(77)
    assert "already reading" in tap_as(88)          # second person: no second reading
    assert started == [tid]

    # the hidden reader process: download + Claude are faked here
    monkeypatch.setattr(nit_reader, "download", lambda page, *a: b"%PDF-1.4 fake")
    monkeypatch.setattr(nit_reader, "ask_ai", lambda pdf, title, value: (
        {"open_to": "Registered contractors of appropriate class", "completion": "4 months",
         "other": ["Bid valid 120 days from upload"]}, {}))

    class P:
        page = None
    monkeypatch.setattr(nit_reader, "first_pages", lambda pdf, n: (pdf, 1))
    import tenderbot.scraper as sc
    monkeypatch.setattr(sc, "PortalScraper", lambda: type("S", (), {"__enter__": lambda s: P(), "__exit__": lambda s, *a: None})())
    fake.calls.clear()
    assert nit_reader.run_reader(con, tid)
    sent = [(p["chat_id"], p["text"]) for m, p in fake.calls if m == "sendMessage"]
    assert {c for c, _ in sent} == {77, 88}
    assert all("I read the NIT" in x and "Finish in 4 months" not in x and "Completion: 4 months" in x for _, x in sent)

    t2 = db.tender_dict(con.execute("SELECT * FROM tenders WHERE org_tender_id=?", (tid,)).fetchone())
    assert "Completion: 4 months" in formatter.detail_message(t2, "telegram")
    fake.calls.clear()
    assert "reading it now" not in tap_as(99)          # saved: later taps cost nothing
    assert started == [tid]


def test_nit_reading_has_daily_limits(con, clock, monkeypatch):
    from tenderbot import nit_reader
    _nit_only(con)
    monkeypatch.setattr(nit_reader, "start_reader", lambda x: None)
    monkeypatch.setattr(config, "NIT_PER_USER_DAILY", 1)
    monkeypatch.setattr(config, "NIT_DAILY_LIMIT", 2)
    assert nit_reader.request(con, 1, 77) == "started"
    assert nit_reader.request(con, 2, 77) == "user"
    assert nit_reader.request(con, 3, 88) == "started"
    assert nit_reader.request(con, 4, 99) == "daily"


def test_gemini_free_tier_used_first_and_falls_back_when_quota_ends(monkeypatch):
    from tenderbot import nit_reader
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert nit_reader.enabled()
    calls = []

    class R:
        def __init__(self, code, data):
            self.status_code, self._d = code, data
        def json(self):
            return self._d

    def post(url, json=None, timeout=None, headers=None):
        calls.append(url.split("/models/")[1].split(":")[0])
        assert headers["x-goog-api-key"] == "g-key" and json["contents"][0]["parts"][0]["inline_data"]["mime_type"] == "application/pdf"
        if len(calls) == 1:
            return R(429, {"error": {"message": "quota exceeded"}})
        return R(200, {"candidates": [{"content": {"parts": [{"text": '{"open_to": "Any registered contractor", "completion": "4 months"}'}]}}]})

    monkeypatch.setattr(nit_reader.requests, "post", post)
    result, usage = nit_reader.ask_ai(b"%PDF-1.4", "Road and drain", "₹44.55 L")
    assert calls == config.GEMINI_MODELS[:2]
    assert result == {"open_to": "Any registered contractor", "completion": "4 months"}


def test_gemini_busy_is_retried_and_retired_model_skipped(monkeypatch):
    from tenderbot import nit_reader
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(config, "GEMINI_MODELS", ["old-model", "busy-model"])
    monkeypatch.setattr(nit_reader.time, "sleep", lambda s: None)
    calls = []

    class R:
        def __init__(self, code, data):
            self.status_code, self._d = code, data
        def json(self):
            return self._d

    def post(url, json=None, timeout=None, headers=None):
        m = url.split("/models/")[1].split(":")[0]
        calls.append(m)
        if m == "old-model":
            return R(404, {"error": {"message": "no longer available to new users"}})
        if calls.count("busy-model") == 1:
            return R(503, {"error": {"message": "high demand"}})
        return R(200, {"candidates": [{"content": {"parts": [{"text": '{"completion": "6 months"}'}]}}]})

    monkeypatch.setattr(nit_reader.requests, "post", post)
    assert nit_reader.ask_ai(b"%PDF", "x", "y")[0] == {"completion": "6 months"}
    assert calls == ["old-model", "busy-model", "busy-model"]


def test_boq_and_nit_download_buttons_send_file_once_then_from_cache(con, clock, monkeypatch):
    from tenderbot import files
    from tenderbot.channels import tg_menu
    t = _with_elig(con)       # BSBCCL tender: files include NIT-25.PDF and BOQ_NALANDA BIND.xlsx
    text, kb = tg_menu.tender_detail(con, t["org_tender_id"])
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert "📥 BOQ (xlsx)" in labels and "📄 NIT (pdf)" in labels
    assert files.pick(t["elig"], "boq").endswith("BOQ_NALANDA BIND.xlsx")

    started, docs = [], []
    monkeypatch.setattr(files, "start_sender", lambda oid, kind: started.append((oid, kind)))
    monkeypatch.setattr(telegram, "send_document",
                        lambda chat, data=None, filename=None, file_id=None, caption=None:
                        docs.append((chat, filename, file_id)) or "FILE123")
    fake = FakeTG()
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)

    def tap(chat):
        telegram.handle_update(con, {"callback_query": {"id": "1", "data": f"dl:{t['org_tender_id']}:boq",
                                                        "message": {"message_id": 5, "chat": {"id": chat}}}})
    tap(77)
    assert "Fetching the BOQ" in fake.last("sendMessage")["text"]
    n = len(fake.calls)
    tap(77)                                   # double tap: ignored
    assert len(fake.calls) == n + 1 and fake.calls[-1][0] == "answerCallbackQuery"
    tap(88)                                   # someone else while it downloads: joins the same download
    assert "Already fetching" in fake.last("sendMessage")["text"] and len(started) == 1

    import tenderbot.nit_reader as nr
    monkeypatch.setattr(nr, "download", lambda page, *a, **k: b"PK fake xlsx")
    assert files.run_sender(con, t["org_tender_id"], "boq", scraper=type("S", (), {"page": None})())
    assert docs == [(77, "BOQ_NALANDA BIND.xlsx", None), (88, None, "FILE123")]   # uploaded once, then re-sent by id

    clock["now"] += timedelta(minutes=5)
    tap(99)                                   # later: straight from Telegram's copy, portal not touched
    assert docs[-1] == (99, None, "FILE123") and len(started) == 1


def test_plain_words_instead_of_commands(con, clock, monkeypatch):
    from tenderbot.channels import tg_menu
    scrape(con)
    fake = FakeTG()
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(telegram, "_call", fake)
    say(con, "/start")
    vis = tg_menu._visible(con)
    t = next(x for x in vis if x["districts"])
    dist = t["districts"][0]

    def reply(text):
        fake.calls.clear()
        say(con, text)
        return [p for m, p in fake.calls if m == "sendMessage"]

    r = reply(dist)                                       # just a district name
    assert dist in r[0]["text"]
    kb = [b["text"] for row in r[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert "🔔 Alert me for these" in kb
    n = sum(1 for x in vis if dist in x["districts"])
    assert f"of {n}" in r[0]["text"]

    tap(con, fake, "Alert me for these")                  # one tap adds it to their alerts
    sub = db.get_or_create_subscriber(con, "telegram", 77)[0]
    assert dist in db.subscriber_rules(con, sub["id"])["district"]

    assert "Pick a department" in reply("departments")[0]["text"]
    assert "Pick a district" in reply("districts")[0]["text"]
    code = t["dept_code"]
    rd = reply(code)                                      # a department code
    assert rd and "No open tenders" not in rd[0]["text"]
    civil = reply("civil")[0]["text"]
    assert "Civil" in civil
    clock["now"] += timedelta(minutes=2)
    d = reply(str(t["org_tender_id"]))                     # a Tender ID: full details
    assert d and f"<b>Tender ID</b>: {t['org_tender_id']}" in d[0]["text"]
    assert "I could not find" in reply("blah blah xyz")[0]["text"]
    assert "District filter" in reply(f"DISTRICT {dist}")[0]["text"]   # old commands still work


def test_owner_sees_stats_and_others_do_not(con, clock, monkeypatch):
    from tenderbot import stats
    scrape(con)
    fake = FakeTG()
    real = fake.__call__
    monkeypatch.setattr(telegram, "_call", lambda m, **p: 57 if m == "getChatMemberCount" else real(m, **p))
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "TELEGRAM_CHANNELS", ["@bihar_tender_alerts"])
    monkeypatch.setattr(config, "ADMIN_CHAT_IDS", ["77"])
    say(con, "/start")                                            # the owner
    telegram.handle_update(con, {"message": {"text": "/start", "chat": {"id": 501, "first_name": "Suresh"}}})
    telegram.handle_update(con, {"message": {"text": "Patna", "chat": {"id": 501, "first_name": "Suresh"}}})
    t = db.open_tenders(con)[0]
    telegram.handle_update(con, {"message": {"text": str(t["org_tender_id"]), "chat": {"id": 501, "first_name": "Suresh"}}})
    fake.calls.clear()
    say(con, "STATS")
    r = fake.last("sendMessage")["text"]
    assert "Channel members: <b>57</b>" in r and "Bot users: <b>1</b> active" in r
    assert "Typed searches: 1" in r and "Tender details opened: 1" in r and "Suresh" in r
    fake.calls.clear()
    telegram.handle_update(con, {"message": {"text": "STATS", "chat": {"id": 501, "first_name": "Suresh"}}})
    assert "Bot users" not in fake.last("sendMessage")["text"]     # strangers cannot see the numbers
