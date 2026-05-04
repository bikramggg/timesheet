"""Parse VSCode tracker JSON + ActivityWatch SQLite pushed from Mac.
   Expected files in LOCAL_DATA_DIR:
     - vscode_tracker.json   (raw timeData.timeEntries from globalState)
     - activitywatch.db      (peewee-sqlite.v2.db copy)
"""
import os, sys, sqlite3, json, collections
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import conn

IST = timezone(timedelta(hours=5, minutes=30))
LOCAL_DIR = os.environ.get("LOCAL_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "local"))

def parse_vscode(start_date, end_date):
    p = os.path.join(LOCAL_DIR, "vscode_tracker.json")
    if not os.path.exists(p):
        print(f"vscode: {p} missing", file=sys.stderr); return []
    data = json.load(open(p))
    rows = []
    agg = collections.defaultdict(float)  # (date,proj,branch,lang) -> minutes
    for e in data.get("timeEntries", []):
        d = e.get("date")
        if not d or not (start_date <= d <= end_date): continue
        agg[(d, e.get("project",""), e.get("branch",""), e.get("language",""))] += e.get("timeSpent",0)
    for (d,p,b,l), m in agg.items():
        rows.append((d, p, b, l, round(m, 3)))
    return rows

def save_vscode(rows):
    n = 0
    with conn() as c:
        for r in rows:
            # upsert: replace any existing row for that key
            c.execute(
                "INSERT INTO vscode_entries(date, project, branch, language, minutes) VALUES (?,?,?,?,?) "
                "ON CONFLICT(date, project, branch, language) DO UPDATE SET minutes=excluded.minutes",
                r,
            )
            n += 1
    return n

def parse_aw(start_date, end_date):
    p = os.path.join(LOCAL_DIR, "activitywatch.db")
    if not os.path.exists(p):
        print(f"aw: {p} missing", file=sys.stderr); return {}
    con = sqlite3.connect(p)
    buckets = dict(con.execute("SELECT id, key FROM bucketmodel").fetchall())
    win_keys = [k for i,k in buckets.items() if "window_" in i]
    afk_keys = [k for i,k in buckets.items() if "afk" in i]
    if not win_keys or not afk_keys:
        return {}
    win_key = win_keys[0]; afk_key = afk_keys[0]
    by_day = collections.defaultdict(lambda: {"window": 0.0, "active": 0.0, "afk": 0.0})
    s = start_date; e = end_date
    # window
    q = f"SELECT timestamp, duration FROM eventmodel WHERE bucket_id=? AND timestamp >= ? AND timestamp < ?"
    end_excl = (datetime.fromisoformat(e) + timedelta(days=1)).date().isoformat()
    for ts, dur in con.execute(q, (win_key, s, end_excl)):
        try:
            t = datetime.fromisoformat(ts.replace(' ','T'))
            if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
            d = t.astimezone(IST).date().isoformat()
            if s <= d <= e: by_day[d]["window"] += dur
        except: pass
    # afk
    q2 = f"SELECT timestamp, duration, datastr FROM eventmodel WHERE bucket_id=? AND timestamp >= ? AND timestamp < ?"
    for ts, dur, ds in con.execute(q2, (afk_key, s, end_excl)):
        try:
            t = datetime.fromisoformat(ts.replace(' ','T'))
            if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
            d = t.astimezone(IST).date().isoformat()
            if not (s <= d <= e): continue
            if ds and "not-afk" in ds: by_day[d]["active"] += dur
            else: by_day[d]["afk"] += dur
        except: pass
    return dict(by_day)

def save_aw(by_day):
    n = 0
    with conn() as c:
        for d, v in by_day.items():
            c.execute(
                "INSERT INTO activitywatch_daily(date, active_seconds, window_seconds, afk_seconds) VALUES (?,?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET active_seconds=excluded.active_seconds, window_seconds=excluded.window_seconds, afk_seconds=excluded.afk_seconds",
                (d, v["active"], v["window"], v["afk"]),
            )
            n += 1
    return n

MEET_GAP_SEC = 300  # merge events within 5 min into one session

def classify_meeting(app, title, url=""):
    a = (app or "").lower()
    t = (title or "")
    u = (url or "").lower()
    if "slack" in a and (t.startswith("Huddle:") or "Huddle Preview" in t or "Huddle - Slack" in t):
        return "slack_huddle"
    if "meet.google.com" in u or "Google Meet" in t:
        return "google_meet"
    if "zoom" in a or "zoom.us" in u or t.startswith("Zoom Meeting"):
        return "zoom"
    if "teams" in a or "teams.microsoft.com" in u or "Microsoft Teams" in t:
        return "teams"
    if "facetime" in a:
        return "facetime"
    return None

def parse_meetings(start_date, end_date):
    p = os.path.join(LOCAL_DIR, "activitywatch.db")
    if not os.path.exists(p): return []
    con = sqlite3.connect(p)
    buckets = dict(con.execute("SELECT id, key FROM bucketmodel").fetchall())
    win_id = next((k for i,k in buckets.items() if "window_" in i), None)
    web_id = next((k for i,k in buckets.items() if "web-brave_" in i), None)
    if not win_id: return []
    end_excl = (datetime.fromisoformat(end_date) + timedelta(days=1)).date().isoformat()

    events = []  # (start, end, source, title)
    for ts, dur, ds in con.execute(
        "SELECT timestamp, duration, datastr FROM eventmodel WHERE bucket_id=? AND timestamp >= ? AND timestamp < ?",
        (win_id, start_date, end_excl)
    ):
        try: d = json.loads(ds)
        except: continue
        src = classify_meeting(d.get("app",""), d.get("title",""))
        if not src: continue
        try:
            t = datetime.fromisoformat(ts.replace(' ','T'))
            if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
        except: continue
        events.append((t, t + timedelta(seconds=dur or 0), src, d.get("title","")))

    if web_id:
        for ts, dur, ds in con.execute(
            "SELECT timestamp, duration, datastr FROM eventmodel WHERE bucket_id=? AND timestamp >= ? AND timestamp < ?",
            (web_id, start_date, end_excl)
        ):
            try: d = json.loads(ds)
            except: continue
            src = classify_meeting("brave browser", d.get("title",""), d.get("url",""))
            if not src or src == "google_meet" and "Brave" not in d.get("title",""):
                pass
            if src:
                try:
                    t = datetime.fromisoformat(ts.replace(' ','T'))
                    if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
                except: continue
                events.append((t, t + timedelta(seconds=dur or 0), src, d.get("title","") or d.get("url","")))

    # Merge consecutive same-source events into sessions (gap < 5min)
    events.sort(key=lambda e: (e[2], e[0]))
    sessions = []
    cur = None
    for s, e, src, title in events:
        if cur and cur["source"] == src and (s - cur["end"]).total_seconds() <= MEET_GAP_SEC:
            cur["end"] = max(cur["end"], e)
            cur["title"] = cur["title"] or title
        else:
            if cur: sessions.append(cur)
            cur = {"start": s, "end": e, "source": src, "title": title}
    if cur: sessions.append(cur)

    rows = []
    for s in sessions:
        d = s["start"].astimezone(IST).date().isoformat()
        if not (start_date <= d <= end_date): continue
        dur = (s["end"] - s["start"]).total_seconds()
        if dur < 30: continue  # ignore < 30s blips
        rows.append((d, s["source"], s["title"], s["start"].astimezone(IST).isoformat(),
                     s["end"].astimezone(IST).isoformat(), dur))
    return rows

def save_meetings(rows):
    n = 0
    with conn() as c:
        for r in rows:
            cur = c.execute(
                "INSERT OR IGNORE INTO meeting_sessions(date, source, title, start_time, end_time, duration_seconds) VALUES (?,?,?,?,?,?)",
                r,
            )
            n += cur.rowcount
    return n

def main(start, end):
    vrows = parse_vscode(start, end)
    vn = save_vscode(vrows)
    aw = parse_aw(start, end)
    an = save_aw(aw)
    mrows = parse_meetings(start, end)
    mn = save_meetings(mrows)
    print(f"vscode: {vn} entries, aw: {an} days, meetings: {mn} sessions")

if __name__ == "__main__":
    if len(sys.argv) == 3:
        main(sys.argv[1], sys.argv[2])
    else:
        y = (datetime.now(IST) - timedelta(days=1)).date().isoformat()
        main(y, y)
