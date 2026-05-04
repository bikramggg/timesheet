"""GitHub collector. Fetches PRs authored by user + PRs commented on, both within date range."""
import os, sys, httpx
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import conn

IST = timezone(timedelta(hours=5, minutes=30))
USER = os.environ["GITHUB_USERNAME"]
TOKEN = os.environ["GITHUB_TOKEN"]
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

def to_ist_date(s):
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(IST).date().isoformat()
    except Exception:
        return None

def search_prs(query):
    url = "https://api.github.com/search/issues"
    out, page = [], 1
    with httpx.Client(timeout=60, headers=HEADERS) as c:
        while True:
            r = c.get(url, params={"q": query, "per_page": 100, "page": page})
            r.raise_for_status()
            items = r.json().get("items", [])
            out.extend(items)
            if len(items) < 100: break
            page += 1
            if page > 10: break
    return out

def collect(start_date, end_date):
    rows = []
    # PRs authored
    for p in search_prs(f"author:{USER} is:pr created:{start_date}..{end_date}"):
        repo = p.get("repository_url","").replace("https://api.github.com/repos/","")
        # opened
        d = to_ist_date(p.get("created_at"))
        if d and start_date <= d <= end_date:
            rows.append((d, repo, p["number"], p["title"], "opened", p.get("html_url","")))
        # merged
        merged = (p.get("pull_request") or {}).get("merged_at")
        d = to_ist_date(merged)
        if d and start_date <= d <= end_date:
            rows.append((d, repo, p["number"], p["title"], "merged", p.get("html_url","")))
        # closed (only if not merged on same day)
        d = to_ist_date(p.get("closed_at"))
        if d and start_date <= d <= end_date and p.get("state") == "closed" and not merged:
            rows.append((d, repo, p["number"], p["title"], "closed", p.get("html_url","")))

    # PRs commented on (by me) — search returns updated, not perfectly accurate but close
    for p in search_prs(f"commenter:{USER} updated:{start_date}..{end_date} is:pr"):
        if (p.get("user") or {}).get("login") == USER: continue  # my own PRs already covered
        repo = p.get("repository_url","").replace("https://api.github.com/repos/","")
        d = to_ist_date(p.get("updated_at"))
        if d and start_date <= d <= end_date:
            rows.append((d, repo, p["number"], p["title"], "commented", p.get("html_url","")))
    return rows

def save(rows):
    n = 0
    with conn() as c:
        for r in rows:
            cur = c.execute(
                "INSERT OR IGNORE INTO github_events(date, repo, pr_number, title, event, url) VALUES (?,?,?,?,?,?)", r
            )
            n += cur.rowcount
    return n

def main(start, end):
    rows = collect(start, end)
    n = save(rows)
    print(f"github: collected {len(rows)} rows, inserted {n}")
    return n

if __name__ == "__main__":
    if len(sys.argv) == 3:
        main(sys.argv[1], sys.argv[2])
    else:
        y = (datetime.now(IST) - timedelta(days=1)).date().isoformat()
        main(y, y)
