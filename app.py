"""FastAPI dashboard."""
import os, sys
from datetime import datetime, timedelta, timezone, date
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import conn, init

IST = timezone(timedelta(hours=5, minutes=30))
JIRA_BASE = os.environ.get("JIRA_BASE_URL") or (f"https://{os.environ['JIRA_SITE']}" if os.environ.get("JIRA_SITE") else "")
app = FastAPI(title="Timesheet")
HERE = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

def fetch_day_summary(start, end):
    with conn() as c:
        days = {r["date"]: dict(r) for r in c.execute(
            "SELECT date, weekday, is_weekend, holiday FROM days WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
        ).fetchall()}
        # ensure all dates present
        s = date.fromisoformat(start); e = date.fromisoformat(end)
        d = s
        while d <= e:
            ds = d.isoformat()
            if ds not in days:
                wd = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][d.weekday()]
                days[ds] = {"date": ds, "weekday": wd, "is_weekend": 1 if d.weekday()>=5 else 0, "holiday": None}
            d += timedelta(days=1)

        for r in c.execute(
            "SELECT date, SUM(minutes) m FROM vscode_entries WHERE date BETWEEN ? AND ? GROUP BY date", (start, end)
        ).fetchall():
            days[r["date"]]["vscode_minutes"] = round(r["m"] or 0, 1)

        for r in c.execute(
            "SELECT date, active_seconds FROM activitywatch_daily WHERE date BETWEEN ? AND ?", (start, end)
        ).fetchall():
            days[r["date"]]["aw_active_seconds"] = r["active_seconds"]
            days[r["date"]]["aw_active_hours"] = round((r["active_seconds"] or 0)/3600, 2)

        for r in c.execute(
            "SELECT date, COUNT(DISTINCT issue_key) issues, COUNT(*) actions FROM jira_activity "
            "WHERE date BETWEEN ? AND ? AND action IN ('comment','worklog','transition','edit_description') "
            "GROUP BY date", (start, end)
        ).fetchall():
            days[r["date"]]["jira_issues"] = r["issues"]
            days[r["date"]]["jira_actions"] = r["actions"]

        for r in c.execute(
            "SELECT date, COUNT(*) n FROM github_events WHERE date BETWEEN ? AND ? AND event='opened' GROUP BY date",
            (start, end)
        ).fetchall():
            days[r["date"]]["gh_prs_opened"] = r["n"]
        for r in c.execute(
            "SELECT date, COUNT(*) n FROM github_events WHERE date BETWEEN ? AND ? AND event='merged' GROUP BY date",
            (start, end)
        ).fetchall():
            days[r["date"]]["gh_prs_merged"] = r["n"]

        for r in c.execute(
            "SELECT date, COUNT(*) ev, COALESCE(SUM(duration_minutes),0) mins FROM calendar_events "
            "WHERE date BETWEEN ? AND ? GROUP BY date", (start, end)
        ).fetchall():
            days[r["date"]]["meeting_count"] = r["ev"]
            days[r["date"]]["meeting_minutes"] = r["mins"]
            days[r["date"]]["meeting_hours"] = round((r["mins"] or 0)/60, 2)
    return [days[k] for k in sorted(days)]

def fetch_day_detail(d):
    with conn() as c:
        out = {"date": d}
        out["vscode"] = [dict(r) for r in c.execute(
            "SELECT project, branch, language, ROUND(minutes,2) minutes FROM vscode_entries WHERE date=? ORDER BY minutes DESC", (d,)
        ).fetchall()]
        aw = c.execute(
            "SELECT active_seconds, window_seconds, afk_seconds FROM activitywatch_daily WHERE date=?", (d,)
        ).fetchone()
        out["activitywatch"] = dict(aw) if aw else None
        out["jira"] = [dict(r) for r in c.execute(
            "SELECT issue_key, summary, status, project, action, detail FROM jira_activity WHERE date=? ORDER BY issue_key", (d,)
        ).fetchall()]
        out["github"] = [dict(r) for r in c.execute(
            "SELECT repo, pr_number, title, event, url FROM github_events WHERE date=? ORDER BY repo, pr_number", (d,)
        ).fetchall()]
        out["calendar"] = [dict(r) for r in c.execute(
            "SELECT summary, start_time, end_time, duration_minutes, is_meet, response FROM calendar_events WHERE date=? ORDER BY start_time", (d,)
        ).fetchall()]
        out["meetings"] = [dict(r) for r in c.execute(
            "SELECT source, title, start_time, end_time, duration_seconds FROM meeting_sessions WHERE date=? ORDER BY start_time", (d,)
        ).fetchall()]
        out["summary"] = None
        s = c.execute("SELECT summary_md, generated_at FROM daily_summary WHERE date=?", (d,)).fetchone()
        if s: out["summary"] = dict(s)
    return out

@app.on_event("startup")
def _startup():
    init()

@app.get("/", response_class=HTMLResponse)
def index(request: Request, start: str = None, end: str = None):
    today = datetime.now(IST).date()
    if not start:
        first = today.replace(day=1)
        start = first.isoformat()
    if not end:
        end = today.isoformat()
    rows = fetch_day_summary(start, end)
    totals = {
        "vscode_hours": round(sum((r.get("vscode_minutes") or 0) for r in rows)/60, 2),
        "aw_active_hours": round(sum((r.get("aw_active_hours") or 0) for r in rows), 2),
        "meeting_hours": round(sum((r.get("meeting_hours") or 0) for r in rows), 2),
        "jira_actions": sum((r.get("jira_actions") or 0) for r in rows),
        "prs_opened": sum((r.get("gh_prs_opened") or 0) for r in rows),
        "prs_merged": sum((r.get("gh_prs_merged") or 0) for r in rows),
    }
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "rows": rows, "start": start, "end": end, "totals": totals,
        "jira_base": JIRA_BASE,
    })

@app.get("/day/{d}", response_class=HTMLResponse)
def day(request: Request, d: str):
    detail = fetch_day_detail(d)
    # Pivot vscode by branch; jira by issue_key
    by_branch = {}
    for r in detail.get("vscode", []):
        b = r["branch"] or "(none)"
        g = by_branch.setdefault(b, {"total": 0, "children": []})
        g["total"] += r["minutes"] or 0
        g["children"].append(r)
    vscode_tree = sorted(
        [{"branch": b, **v, "children": sorted(v["children"], key=lambda x: -(x["minutes"] or 0))}
         for b, v in by_branch.items()],
        key=lambda x: -x["total"],
    )

    by_key = {}
    for r in detail.get("jira", []):
        g = by_key.setdefault(r["issue_key"],
            {"key": r["issue_key"], "summary": r["summary"], "status": r["status"], "project": r["project"], "actions": []})
        g["actions"].append({"action": r["action"], "detail": r["detail"]})
    jira_tree = list(by_key.values())

    # Meetings summary
    meet_summary = {}
    for r in detail.get("meetings", []):
        s = meet_summary.setdefault(r["source"], {"count": 0, "seconds": 0})
        s["count"] += 1
        s["seconds"] += r["duration_seconds"] or 0

    return templates.TemplateResponse("day.html", {
        "request": request, "d": d, "detail": detail,
        "vscode_tree": vscode_tree, "jira_tree": jira_tree, "meet_summary": meet_summary,
        "jira_base": JIRA_BASE,
    })

@app.get("/api/range")
def api_range(start: str, end: str):
    return JSONResponse(fetch_day_summary(start, end))

@app.get("/api/day/{d}")
def api_day(d: str):
    return JSONResponse(fetch_day_detail(d))

@app.post("/api/run")
def api_run(start: str = None, end: str = None):
    """Trigger a collection run on demand."""
    from collectors.run_all import main as run_main
    run_main(start, end)
    return {"status": "ok"}

@app.get("/api/health")
def health():
    with conn() as c:
        runs = [dict(r) for r in c.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT 20").fetchall()]
    return {"status": "ok", "recent_runs": runs}


# ---------- Analytics ----------

@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, start: str = None, end: str = None, project: str = None, branch: str = None):
    today = datetime.now(IST).date()
    if not start:
        first = today.replace(day=1)
        start = first.isoformat()
    if not end:
        end = today.isoformat()
    return templates.TemplateResponse("analytics.html", {
        "request": request, "start": start, "end": end,
        "project": project or "", "branch": branch or "",
    })


def _filters(c, start, end, project=None, branch=None):
    where = "date BETWEEN ? AND ?"
    args = [start, end]
    if project:
        where += " AND project = ?"
        args.append(project)
    if branch:
        where += " AND branch = ?"
        args.append(branch)
    return where, args


@app.get("/api/charts/insights")
def chart_insights(start: str, end: str, project: str = None, branch: str = None):
    """Most productive weekday, languages, projects, streak."""
    with conn() as c:
        where, args = _filters(c, start, end, project, branch)
        # weekday averages
        rows = c.execute(
            f"SELECT date, SUM(minutes) m FROM vscode_entries WHERE {where} GROUP BY date", args
        ).fetchall()
        weekday_minutes = {i: [] for i in range(7)}
        weekday_names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        for r in rows:
            wd = date.fromisoformat(r["date"]).weekday()
            weekday_minutes[wd].append(r["m"] or 0)
        weekday_avg = [
            {"name": weekday_names[i], "avg_minutes": round(sum(v)/len(v),1) if v else 0,
             "total_minutes": round(sum(v),1)}
            for i, v in weekday_minutes.items()
        ]
        most_productive = max(weekday_avg, key=lambda x: x["avg_minutes"]) if any(d["avg_minutes"]>0 for d in weekday_avg) else None

        # languages
        langs = [dict(r) for r in c.execute(
            f"SELECT language, ROUND(SUM(minutes),1) minutes FROM vscode_entries WHERE {where} GROUP BY language ORDER BY minutes DESC",
            args
        ).fetchall()]
        total_lang = sum(l["minutes"] or 0 for l in langs) or 1
        for l in langs: l["pct"] = round(100 * (l["minutes"] or 0) / total_lang, 1)
        top_language = langs[0] if langs else None

        # projects
        projs = [dict(r) for r in c.execute(
            f"SELECT project, ROUND(SUM(minutes),1) minutes FROM vscode_entries WHERE {where} GROUP BY project ORDER BY minutes DESC",
            args
        ).fetchall()]
        total_proj = sum(p["minutes"] or 0 for p in projs) or 1
        for p in projs: p["pct"] = round(100 * (p["minutes"] or 0) / total_proj, 1)
        top_project = projs[0] if projs else None

        # streak: longest run of consecutive days with any minutes (in selected range)
        active_dates = sorted({r["date"] for r in c.execute(
            f"SELECT DISTINCT date FROM vscode_entries WHERE {where} AND minutes > 0", args
        ).fetchall()})
        longest = current = 0
        prev = None
        for ds in active_dates:
            d = date.fromisoformat(ds)
            if prev and (d - prev).days == 1:
                current += 1
            else:
                current = 1
            longest = max(longest, current)
            prev = d
        # current streak ending today
        today = datetime.now(IST).date()
        cs = 0
        d = today
        active_set = set(active_dates)
        while d.isoformat() in active_set:
            cs += 1
            d -= timedelta(days=1)

    return {
        "weekday_avg": weekday_avg,
        "most_productive": most_productive,
        "languages": langs[:20],
        "top_language": top_language,
        "projects": projs[:20],
        "top_project": top_project,
        "longest_streak": longest,
        "current_streak": cs,
        "language_count": len([l for l in langs if (l["minutes"] or 0) > 0]),
        "project_count": len([p for p in projs if (p["minutes"] or 0) > 0]),
    }


@app.get("/api/charts/time_summary")
def chart_time_summary(project: str = None, branch: str = None):
    today = datetime.now(IST).date()
    spans = {
        "today":     (today, today),
        "this_week": (today - timedelta(days=today.weekday()), today),
        "this_month":(today.replace(day=1), today),
        "this_year": (today.replace(month=1, day=1), today),
        "all_time":  (date(2000,1,1), today),
    }
    out = {}
    with conn() as c:
        for k, (s, e) in spans.items():
            where, args = _filters(c, s.isoformat(), e.isoformat(), project, branch)
            r = c.execute(f"SELECT COALESCE(SUM(minutes),0) m FROM vscode_entries WHERE {where}", args).fetchone()
            out[k] = round(r["m"] or 0, 1)
    return out


@app.get("/api/charts/daily")
def chart_daily(start: str, end: str, project: str = None, branch: str = None):
    """Minutes per day for daily summary line chart."""
    with conn() as c:
        where, args = _filters(c, start, end, project, branch)
        rows = [dict(r) for r in c.execute(
            f"SELECT date, ROUND(SUM(minutes),1) minutes FROM vscode_entries WHERE {where} GROUP BY date ORDER BY date",
            args
        ).fetchall()]
    # fill gaps
    s = date.fromisoformat(start); e = date.fromisoformat(end)
    by_d = {r["date"]: r["minutes"] for r in rows}
    out = []
    d = s
    while d <= e:
        ds = d.isoformat()
        out.append({"date": ds, "minutes": by_d.get(ds, 0)})
        d += timedelta(days=1)
    return out


@app.get("/api/charts/weekly")
def chart_weekly(start: str, end: str, project: str = None, branch: str = None):
    """Minutes per ISO week."""
    with conn() as c:
        where, args = _filters(c, start, end, project, branch)
        rows = c.execute(
            f"SELECT date, SUM(minutes) m FROM vscode_entries WHERE {where} GROUP BY date", args
        ).fetchall()
    weeks = {}
    for r in rows:
        d = date.fromisoformat(r["date"])
        y, w, _ = d.isocalendar()
        key = f"{y}-W{w:02d}"
        weeks[key] = weeks.get(key, 0) + (r["m"] or 0)
    return [{"week": k, "minutes": round(v, 1)} for k, v in sorted(weeks.items())]


@app.get("/api/charts/monthly")
def chart_monthly(start: str, end: str, project: str = None, branch: str = None):
    """Minutes per month."""
    with conn() as c:
        where, args = _filters(c, start, end, project, branch)
        rows = [dict(r) for r in c.execute(
            f"SELECT substr(date,1,7) ym, ROUND(SUM(minutes),1) minutes FROM vscode_entries WHERE {where} GROUP BY ym ORDER BY ym",
            args
        ).fetchall()]
    return rows


@app.get("/api/charts/heatmap")
def chart_heatmap(months: int = 3, project: str = None, branch: str = None):
    """Last N months of daily minutes for the heatmap."""
    today = datetime.now(IST).date()
    s = (today.replace(day=1) - timedelta(days=months*31)).replace(day=1)
    with conn() as c:
        where, args = _filters(c, s.isoformat(), today.isoformat(), project, branch)
        rows = [dict(r) for r in c.execute(
            f"SELECT date, ROUND(SUM(minutes),1) minutes FROM vscode_entries WHERE {where} GROUP BY date",
            args
        ).fetchall()]
    by = {r["date"]: r["minutes"] for r in rows}
    out = []
    d = s
    while d <= today:
        ds = d.isoformat()
        out.append({"date": ds, "minutes": by.get(ds, 0)})
        d += timedelta(days=1)
    return {"start": s.isoformat(), "end": today.isoformat(), "days": out}


@app.get("/api/charts/branches")
def chart_branches(start: str, end: str, project: str = None):
    """Branches used in the date range, optionally filtered by project."""
    with conn() as c:
        where, args = _filters(c, start, end, project, None)
        rows = [dict(r) for r in c.execute(
            f"SELECT branch, project, ROUND(SUM(minutes),1) minutes FROM vscode_entries WHERE {where} GROUP BY branch, project ORDER BY minutes DESC",
            args
        ).fetchall()]
    return rows


@app.get("/api/charts/projects_list")
def projects_list():
    with conn() as c:
        rows = [r["project"] for r in c.execute(
            "SELECT DISTINCT project FROM vscode_entries WHERE project!='' ORDER BY project"
        ).fetchall()]
    return rows


# ---------- Worklog plan (HnR Forge app payload shape) ----------

import re
ISSUE_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

def _add_minutes(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = h * 60 + m + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


@app.get("/api/worklog/plan")
def worklog_plan(date: str, target_hours: float = 8.0, day_start: str = "10:00",
                 jira_only_minutes: int = 10, skip_zero: bool = True):
    """
    Plan worklog entries for one day. Output shape matches HnR Forge app's createEntry payload:
        {taskKey, minutes, notes, entryDate, startTime, endTime}

    Allocation:
      1. VSCode branches matching an issue key (e.g. TF-1234) → minutes proportional to coded time
      2. Jira-touched issues with no VSCode time → flat `jira_only_minutes` each
      3. Total scaled so coding time fills (target_hours - meeting_minutes)
      4. Sequential time slots starting at `day_start`, no overlap
    """
    with conn() as c:
        # VSCode minutes per branch
        vs_rows = c.execute(
            "SELECT branch, project, ROUND(SUM(minutes),2) m FROM vscode_entries WHERE date=? AND branch != '' GROUP BY branch, project",
            (date,)
        ).fetchall()
        vs_by_issue = {}
        vs_unmatched = 0.0
        for r in vs_rows:
            mobj = ISSUE_RE.search(r["branch"] or "")
            if mobj:
                k = mobj.group(1)
                e = vs_by_issue.setdefault(k, {"minutes": 0.0, "branches": set(), "projects": set()})
                e["minutes"] += r["m"] or 0
                e["branches"].add(r["branch"])
                e["projects"].add(r["project"])
            else:
                vs_unmatched += r["m"] or 0

        # Jira activity today
        jira_rows = c.execute(
            "SELECT issue_key, summary, status, project, action, detail FROM jira_activity WHERE date=?",
            (date,)
        ).fetchall()
        jira_by_key = {}
        for r in jira_rows:
            j = jira_by_key.setdefault(r["issue_key"], {
                "summary": r["summary"], "status": r["status"], "project": r["project"],
                "actions": []
            })
            j["actions"].append({"action": r["action"], "detail": r["detail"]})

        # Already-logged worklogs (by me) for the day, to surface delta
        existing = {}
        for r in c.execute(
            "SELECT issue_key, SUM(CAST(detail AS INTEGER)) sec FROM jira_activity WHERE date=? AND action='worklog' GROUP BY issue_key",
            (date,)
        ).fetchall():
            existing[r["issue_key"]] = round((r["sec"] or 0) / 60)

        aw_row = c.execute("SELECT active_seconds FROM activitywatch_daily WHERE date=?", (date,)).fetchone()
        aw_min = round((aw_row[0] or 0) / 60) if aw_row else 0

        meet_min = round(c.execute(
            "SELECT COALESCE(SUM(duration_seconds),0)/60.0 FROM meeting_sessions WHERE date=?", (date,)
        ).fetchone()[0])

    # Allocation
    target_min = int(round(target_hours * 60))
    coding_pool = max(target_min - meet_min, 0)

    jira_only_keys = [k for k in jira_by_key if k not in vs_by_issue]
    jira_only_alloc = min(len(jira_only_keys) * jira_only_minutes, int(coding_pool * 0.25))
    coding_for_vs = max(coding_pool - jira_only_alloc, 0)

    total_vs = sum(v["minutes"] for v in vs_by_issue.values())

    raw_entries = []  # before scaling, for reference

    if total_vs > 0 and coding_for_vs > 0:
        scale = coding_for_vs / total_vs
        for k, v in vs_by_issue.items():
            mins = int(round(v["minutes"] * scale))
            if mins < 1: continue
            actions = jira_by_key.get(k, {}).get("actions", [])
            note_parts = [f"Coding on {', '.join(sorted(v['branches']))}"]
            if actions:
                act_strs = [f"{a['action']}{':'+a['detail'] if a['detail'] else ''}" for a in actions[:5]]
                note_parts.append("Activity: " + ", ".join(act_strs))
            raw_entries.append({
                "taskKey": k,
                "minutes": mins,
                "notes": ". ".join(note_parts),
                "entryDate": date,
                "_source": "vscode+jira" if k in jira_by_key else "vscode_only",
                "_summary": jira_by_key.get(k, {}).get("summary", ""),
                "_already_logged_minutes": existing.get(k, 0),
                "_vscode_minutes": round(v["minutes"], 1),
            })

    if jira_only_keys and jira_only_alloc > 0:
        per = max(jira_only_minutes, jira_only_alloc // len(jira_only_keys))
        for k in jira_only_keys:
            j = jira_by_key[k]
            act_strs = [f"{a['action']}{':'+a['detail'] if a['detail'] else ''}" for a in j["actions"][:5]]
            raw_entries.append({
                "taskKey": k,
                "minutes": per,
                "notes": "Activity: " + ", ".join(act_strs),
                "entryDate": date,
                "_source": "jira_only",
                "_summary": j["summary"],
                "_already_logged_minutes": existing.get(k, 0),
                "_vscode_minutes": 0,
            })

    # Sort by largest first so big slots come early in day
    raw_entries.sort(key=lambda x: -x["minutes"])

    # Sequential time slots starting at day_start
    cursor = day_start
    plan = []
    for e in raw_entries:
        if skip_zero and e["minutes"] < 1:
            continue
        start = cursor
        end = _add_minutes(start, e["minutes"])
        plan.append({
            "taskKey": e["taskKey"],
            "minutes": e["minutes"],
            "notes": e["notes"],
            "entryDate": e["entryDate"],
            "startTime": start,
            "endTime": end,
            # extra context for the UI/automation
            "summary": e["_summary"],
            "source": e["_source"],
            "already_logged_minutes": e["_already_logged_minutes"],
            "vscode_minutes": e["_vscode_minutes"],
        })
        cursor = end

    return {
        "date": date,
        "target_hours": target_hours,
        "active_aw_minutes": aw_min,
        "meeting_minutes": meet_min,
        "vscode_minutes_total": round(total_vs),
        "vscode_unmatched_minutes": round(vs_unmatched),
        "jira_only_issue_count": len(jira_only_keys),
        "plan": plan,
        "plan_total_minutes": sum(p["minutes"] for p in plan),
        "plan_total_hours": round(sum(p["minutes"] for p in plan) / 60, 2),
    }


@app.get("/api/worklog/plan_range")
def worklog_plan_range(start: str, end: str, target_hours: float = 8.0,
                       day_start: str = "10:00", skip_weekends: bool = True):
    """Plan a date range. Skips weekends by default."""
    s = date.fromisoformat(start); e = date.fromisoformat(end)
    out = []
    d = s
    while d <= e:
        if skip_weekends and d.weekday() >= 5:
            d += timedelta(days=1); continue
        out.append(worklog_plan(d.isoformat(), target_hours, day_start))
        d += timedelta(days=1)
    return out
