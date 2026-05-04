"""Daily orchestrator. Runs all collectors for a date range and logs results."""
import os, sys, traceback
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import conn, init

IST = timezone(timedelta(hours=5, minutes=30))

def log_run(source, status, rows, error=""):
    with conn() as c:
        c.execute(
            "INSERT INTO run_log(started_at, finished_at, source, status, rows_added, error) VALUES (?,?,?,?,?,?)",
            (datetime.now(IST).isoformat(), datetime.now(IST).isoformat(), source, status, rows, error),
        )

def upsert_day(d):
    weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][date.fromisoformat(d).weekday()]
    is_we = 1 if date.fromisoformat(d).weekday() >= 5 else 0
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO days(date, weekday, is_weekend) VALUES (?,?,?)",
            (d, weekday, is_we),
        )

def run_one(name, fn, start, end):
    try:
        n = fn(start, end) or 0
        log_run(name, "ok", n)
        print(f"[{name}] ok")
    except Exception as e:
        traceback.print_exc()
        log_run(name, "error", 0, str(e))
        print(f"[{name}] FAILED: {e}", file=sys.stderr)

def main(start=None, end=None):
    init()
    if not start:
        # default = yesterday IST (run after midnight)
        d = (datetime.now(IST) - timedelta(days=1)).date().isoformat()
        start = end = d
    elif not end:
        end = start

    # populate days table
    s = date.fromisoformat(start); e = date.fromisoformat(end)
    while s <= e:
        upsert_day(s.isoformat())
        s += timedelta(days=1)

    # run collectors
    from collectors import jira as cj, github as cg, calendar as cc, local_data as cl
    run_one("jira", cj.main, start, end)
    run_one("github", cg.main, start, end)
    run_one("calendar", cc.main, start, end)
    run_one("local_data", cl.main, start, end)

    # ntfy: send a daily summary for the END date (covers most-recent if range)
    try:
        import notify
        with conn() as c:
            ok = notify.daily_summary(end, c)
        log_run("notify", "ok" if ok else "skip", 0)
    except Exception as e:
        traceback.print_exc()
        log_run("notify", "error", 0, str(e))

if __name__ == "__main__":
    if len(sys.argv) == 3: main(sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 2: main(sys.argv[1], sys.argv[1])
    else: main()
