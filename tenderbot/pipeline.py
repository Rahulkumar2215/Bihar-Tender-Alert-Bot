"""Scrape -> store. Kept separate from sending so each step can be run and tested on its own."""
import logging

from . import config, db

log = logging.getLogger(__name__)


def store_listing(con, tenders, orgs, seen_at=None):
    seen_at = seen_at or db.now()
    if db.get_kv(con, "baseline_at") is None:
        # First ever scrape: everything already on the portal is the baseline, not "new".
        # (Subscribers still get closing-soon reminders for these.)
        db.set_kv(con, "baseline_at", seen_at)
    db.upsert_departments(con, orgs)
    new_ids, extended = db.upsert_tenders(con, tenders, seen_at)
    db.mark_missing(con, [t["tender_id"] for t in tenders], seen_at)
    con.commit()
    return new_ids, extended


def run_scrape(con, fetch_details=True):
    from .scraper import PortalScraper

    started = db.now()
    with PortalScraper() as s:
        tenders, orgs = s.listing()
        new_ids, extended = store_listing(con, tenders, orgs)
        log.info("listed %d open tenders: %d new, %d deadline extensions", len(tenders), len(new_ids), len(extended))

        fetched = 0
        if fetch_details:
            todo = db.ids_needing_detail(con, config.MAX_DETAILS_PER_RUN)
            # older tenders stored before "who can bid" existed: catch up a batch per run
            todo += [i for i in db.ids_needing_elig(con, config.ELIG_BACKFILL_PER_RUN) if i not in todo]
            if todo:
                log.info("fetching detail for %d tenders (≈%ds)", len(todo), len(todo) * config.DETAIL_DELAY_MS // 1000)
                for tid, summary in s.details(todo).items():
                    db.save_detail(con, tid, summary)
                    fetched += 1
                con.commit()

    con.execute("INSERT INTO runs(started_at, finished_at, listed, new_count, extended, details) VALUES (?,?,?,?,?,?)",
                (started, db.now(), len(tenders), len(new_ids), len(extended), fetched))
    con.commit()
    return {"listed": len(tenders), "new": len(new_ids), "extended": len(extended), "details": fetched}
