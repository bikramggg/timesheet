"""Calendar collector via ICS URL. Reads recurring + non-recurring events in date range."""
import os, sys, httpx
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import conn

IST = timezone(timedelta(hours=5, minutes=30))
ICS_URL = os.environ.get("GCAL_ICS_URL","")

def collect(start_date, end_date):
    if not ICS_URL:
        print("GCAL_ICS_URL not set, skipping", file=sys.stderr)
        return []
    import icalendar, recurring_ical_events
    r = httpx.get(ICS_URL, timeout=60, follow_redirects=True)
    r.raise_for_status()
    cal = icalendar.Calendar.from_ical(r.content)
    s = datetime.fromisoformat(start_date).replace(tzinfo=IST)
    e = datetime.fromisoformat(end_date).replace(tzinfo=IST) + timedelta(days=1)
    events = recurring_ical_events.of(cal).between(s, e)
    rows = []
    for ev in events:
        summary = str(ev.get("SUMMARY",""))
        start = ev.get("DTSTART").dt
        end = ev.get("DTEND").dt if ev.get("DTEND") else start
        # Convert to IST date
        if isinstance(start, datetime):
            d = start.astimezone(IST).date().isoformat()
            dur_min = int((end - start).total_seconds() / 60) if isinstance(end, datetime) else None
            start_s = start.astimezone(IST).isoformat()
            end_s = end.astimezone(IST).isoformat() if isinstance(end, datetime) else None
        else:
            d = start.isoformat()
            dur_min = None
            start_s = start.isoformat()
            end_s = end.isoformat() if end else None
        if not (start_date <= d <= end_date): continue
        desc = str(ev.get("DESCRIPTION",""))
        loc = str(ev.get("LOCATION",""))
        is_meet = 1 if ("meet.google.com" in desc or "meet.google.com" in loc) else 0
        # PARTSTAT
        response = ""
        for att in ev.get("ATTENDEE", []):
            try:
                if att.params.get("CN","").lower().endswith("ghosh") or "bikram" in att.lower():
                    response = att.params.get("PARTSTAT","")
            except Exception: pass
        if response.upper() == "DECLINED": continue
        # cancelled
        if str(ev.get("STATUS","")).upper() == "CANCELLED": continue
        uid = str(ev.get("UID",""))
        rows.append((d, summary, start_s, end_s, dur_min, is_meet, response, uid))
    return rows

def save(rows):
    n = 0
    with conn() as c:
        for r in rows:
            cur = c.execute(
                "INSERT OR IGNORE INTO calendar_events(date, summary, start_time, end_time, duration_minutes, is_meet, response, uid) VALUES (?,?,?,?,?,?,?,?)", r
            )
            n += cur.rowcount
    return n

def main(start, end):
    rows = collect(start, end)
    n = save(rows)
    print(f"calendar: collected {len(rows)} rows, inserted {n}")
    return n

if __name__ == "__main__":
    if len(sys.argv) == 3:
        main(sys.argv[1], sys.argv[2])
    else:
        y = (datetime.now(IST) - timedelta(days=1)).date().isoformat()
        main(y, y)
