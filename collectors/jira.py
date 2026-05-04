"""Jira collector. Fetches issues touched by user in date range, extracts comments, worklogs,
   transitions, description edits, and other field edits attributable to the user."""
import os, sys, httpx, json, base64, sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import conn

IST = timezone(timedelta(hours=5, minutes=30))
SITE = os.environ["JIRA_SITE"]
EMAIL = os.environ["JIRA_EMAIL"]
TOKEN = os.environ["JIRA_API_TOKEN"]
ME = os.environ["JIRA_ACCOUNT_ID"]
BASE = f"https://{SITE}/rest/api/3"

def auth_header():
    creds = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}

def to_ist_date(s):
    if not s: return None
    s = s.replace(' ', 'T') if 'T' in s and ' ' in s else s
    try:
        t = datetime.fromisoformat(s.replace('Z','+00:00'))
        return t.astimezone(IST).date().isoformat()
    except Exception:
        return None

def search(jql, fields, expand=None, max_results=100):
    """Atlassian deprecated GET /search (410). Uses POST /search/jql with nextPageToken pagination."""
    url = f"{BASE}/search/jql"
    out = []
    next_token = None
    headers = {**auth_header(), "Content-Type": "application/json"}
    with httpx.Client(timeout=60) as c:
        while True:
            body = {"jql": jql, "maxResults": max_results, "fields": fields}
            if expand: body["expand"] = expand
            if next_token: body["nextPageToken"] = next_token
            r = c.post(url, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            issues = data.get("issues", [])
            out.extend(issues)
            next_token = data.get("nextPageToken")
            if data.get("isLast", True) or not next_token or not issues: break
    return out

def collect(start_date, end_date):
    """start_date, end_date as YYYY-MM-DD strings (inclusive, IST). Returns list of activity rows."""
    jql = (
        f'(assignee = currentUser() OR worklogAuthor = currentUser() OR reporter = currentUser()) '
        f'AND updated >= "{start_date}" AND updated <= "{end_date} 23:59" ORDER BY updated DESC'
    )
    issues = search(jql, ["summary","status","comment","worklog","updated","project"], expand="changelog")
    rows = []
    for n in issues:
        key = n["key"]
        f = n.get("fields", {}) or {}
        summary = f.get("summary","")
        status = (f.get("status") or {}).get("name","")
        project = (f.get("project") or {}).get("key","")

        # Comments by me
        for c in (f.get("comment") or {}).get("comments", []):
            if (c.get("author") or {}).get("accountId") != ME: continue
            d = to_ist_date(c.get("created"))
            if d and start_date <= d <= end_date:
                rows.append((d, key, summary, status, project, "comment", c.get("id","")))

        # Worklogs by me
        for w in (f.get("worklog") or {}).get("worklogs", []):
            aid = (w.get("author") or {}).get("accountId") or (w.get("updateAuthor") or {}).get("accountId")
            if aid != ME: continue
            d = to_ist_date(w.get("started"))
            if d and start_date <= d <= end_date:
                rows.append((d, key, summary, status, project, "worklog", str(w.get("timeSpentSeconds",0))))

        # Changelog by me — skip volatile fields that are side-effects of worklog/comment ops
        VOLATILE_FIELDS = {"timeestimate", "timespent", "WorklogId", "Worklog Id",
                           "WorklogTimeSpent", "Comment Id", "Workflow",
                           "RemoteIssueLink", "Link"}
        for h in (n.get("changelog") or {}).get("histories", []):
            if (h.get("author") or {}).get("accountId") != ME: continue
            d = to_ist_date(h.get("created"))
            if not d or not (start_date <= d <= end_date): continue
            for it in h.get("items", []):
                field = it.get("field")
                if field in VOLATILE_FIELDS: continue
                if field == "status":
                    detail = f"{it.get('fromString','')}->{it.get('toString','')}"
                    rows.append((d, key, summary, status, project, "transition", detail))
                elif field == "description":
                    rows.append((d, key, summary, status, project, "edit_description", ""))
                else:
                    rows.append((d, key, summary, status, project, f"edit_field:{field}", ""))
    return rows

def save(rows):
    added = 0
    with conn() as c:
        for r in rows:
            try:
                c.execute(
                    "INSERT OR IGNORE INTO jira_activity(date, issue_key, summary, status, project, action, detail) VALUES (?,?,?,?,?,?,?)",
                    r,
                )
                if c.total_changes > added: added = c.total_changes
            except Exception as e:
                print(f"row insert err: {e}", file=sys.stderr)
    return added

def main(start, end):
    rows = collect(start, end)
    n = save(rows)
    print(f"jira: collected {len(rows)} rows, inserted {n}")
    return n

if __name__ == "__main__":
    if len(sys.argv) == 3:
        main(sys.argv[1], sys.argv[2])
    else:
        # default: yesterday
        y = (datetime.now(IST) - timedelta(days=1)).date().isoformat()
        main(y, y)
