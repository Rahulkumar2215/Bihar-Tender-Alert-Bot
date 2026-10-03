"""Command line: python -m tenderbot <command>

  run [--digest]         scrape the portal, then send alerts (use this from cron / Task Scheduler)
  scrape [--no-details]  scrape only
  notify [--digest]      send alerts only
  bot                    Telegram bot: answers ADD / DISTRICT / OPEN ... (keep running)
  ensure-bot [--kill-old] start the bot hidden in the background if it is not running
  read-nit TENDER_ID     read a tender's NIT with AI and send it to whoever asked
  send-file TENDER_ID boq|nit   fetch a tender's BOQ / NIT from the portal and send it to whoever asked
  webhook [--port 8000]  WhatsApp webhook server (keep running, needs public HTTPS)
  subscribe              add a subscriber yourself (e.g. your own Telegram chat)
  subscribers            list subscribers and their filters
  depts                  departments with open tenders
  stats                  database summary
  export FILE.csv        open tenders to CSV (for Excel / Power BI)
"""
import argparse
import csv
import logging
import sys

from . import commands, config, db, notifier, pipeline


def main(argv=None):
    p = argparse.ArgumentParser(prog="tenderbot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "notify"):
        s = sub.add_parser(name)
        s.add_argument("--digest", action="store_true", help="include daily-digest subscribers")
    sub.add_parser("scrape").add_argument("--no-details", action="store_true")
    sub.add_parser("bot")
    sub.add_parser("ensure-bot").add_argument("--kill-old", action="store_true")
    sub.add_parser("read-nit").add_argument("tender_id", type=int)
    s = sub.add_parser("send-file")
    s.add_argument("tender_id", type=int)
    s.add_argument("kind", choices=["boq", "nit"])
    sub.add_parser("webhook").add_argument("--port", type=int, default=8000)
    s = sub.add_parser("subscribe")
    s.add_argument("--channel", choices=["telegram", "whatsapp"], required=True)
    s.add_argument("--address", required=True, help="Telegram chat id, or WhatsApp number like 9198XXXXXXXX")
    s.add_argument("--name")
    s.add_argument("--mode", choices=["instant", "digest"], default="digest")
    s.add_argument("--rules", nargs="*", default=[], help='commands, e.g. "ADD NBPDCL BCD" "DISTRICT Patna"')
    sub.add_parser("subscribers")
    sub.add_parser("depts")
    sub.add_parser("stats")
    sub.add_parser("export").add_argument("file")
    a = p.parse_args(argv)

    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if a.cmd in ("bot", "read-nit", "send-file"):  # run hidden (pythonw): log straight to a file
        from pathlib import Path
        log_path = Path(config.DB_PATH).parent / {"bot": "bot.log", "read-nit": "nit.log"}.get(a.cmd, "files.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format=fmt, filename=str(log_path), encoding="utf-8")
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)

    if a.cmd == "ensure-bot":
        from . import supervisor
        result = supervisor.ensure_bot(kill_old=a.kill_old)
        if result != "already running":
            print(db.now(), "bot", result)
        return 0

    con = db.connect()

    if a.cmd in ("run", "scrape"):
        print("scrape:", pipeline.run_scrape(con, fetch_details=not getattr(a, "no_details", False)))
    if a.cmd in ("run", "notify"):
        print("notify:", notifier.notify_all(con, digest=a.digest))

    elif a.cmd == "bot":
        from . import supervisor
        from .channels import telegram
        lock = supervisor.acquire_lock()
        if lock is None:
            logging.info("another bot is already running – exiting")
            return 0
        telegram.poll_forever(con, stop_when=supervisor.code_changed_checker())

    elif a.cmd == "read-nit":
        from . import nit_reader
        return 0 if nit_reader.run_reader(con, a.tender_id) else 1

    elif a.cmd == "send-file":
        from . import files
        return 0 if files.run_sender(con, a.tender_id, a.kind) else 1

    elif a.cmd == "webhook":
        from .channels import whatsapp
        app = whatsapp.create_app(db.connect)
        app.run(host="0.0.0.0", port=a.port)

    elif a.cmd == "subscribe":
        if a.channel == "whatsapp":
            print("Note: on WhatsApp, Meta requires that the person opted in to receive messages from you.")
        row, created = db.get_or_create_subscriber(con, a.channel, a.address, a.name)
        con.execute("UPDATE subscribers SET mode=? WHERE id=?", (a.mode, row["id"]))
        con.commit()
        for r in a.rules:
            for reply in commands.handle(con, a.channel, a.address, r):
                print(reply)
        print(("Created" if created else "Updated"), "subscriber", row["id"])

    elif a.cmd == "subscribers":
        for s in con.execute("SELECT * FROM subscribers ORDER BY id"):
            rules = db.subscriber_rules(con, s["id"])
            print(f"#{s['id']} {s['channel']}:{s['address']} {s['name'] or ''} mode={s['mode']} "
                  f"active={s['active']} rules={rules or 'ALL'}")

    elif a.cmd == "depts":
        for r in con.execute("SELECT dept_code, dept_name, COUNT(*) n FROM tenders WHERE status='open' "
                             "GROUP BY dept_code, dept_name ORDER BY n DESC"):
            print(f"{r['n']:4}  {r['dept_code']:<12} {r['dept_name']}")

    elif a.cmd == "stats":
        q = lambda s: con.execute(s).fetchone()[0]
        print("open tenders:", q("SELECT COUNT(*) FROM tenders WHERE status='open'"))
        print("with detail (value/EMD):", q("SELECT COUNT(*) FROM tenders WHERE status='open' AND detail_fetched=1"))
        print("all tenders ever seen:", q("SELECT COUNT(*) FROM tenders"))
        print("active subscribers:", q("SELECT COUNT(*) FROM subscribers WHERE active=1"))
        print("alerts sent:", q("SELECT COUNT(*) FROM deliveries WHERE status='sent'"))
        last = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            print("last run:", dict(last))

    elif a.cmd == "export":
        rows = [db.tender_dict(r) for r in con.execute("SELECT * FROM tenders WHERE status='open' ORDER BY close_at")]
        with open(a.file, "w", newline="", encoding="utf-8-sig") as f:
            if rows:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                for r in rows:
                    r["districts"] = ", ".join(r["districts"])
                    w.writerow(r)
        print(f"wrote {len(rows)} tenders to {a.file}")


if __name__ == "__main__":
    sys.exit(main())
