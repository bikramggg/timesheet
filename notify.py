"""ntfy publisher with daily-summary helper."""
import os, httpx
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

def send(title, message, priority="default", tags=None):
    base = os.environ.get("NTFY_URL", "").rstrip("/")
    topic = os.environ.get("NTFY_TOPIC", "timelog")
    if not base or not topic:
        return False
    headers = {
        "Title": title,
        "Priority": priority,
    }
    if tags: headers["Tags"] = ",".join(tags)
    token = os.environ.get("NTFY_TOKEN", "")
    if token: headers["Authorization"] = f"Bearer {token}"
    url = f"{base}/{topic}"
    try:
        r = httpx.post(url, content=message.encode("utf-8"), headers=headers, timeout=15, verify=True)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"ntfy error: {e}")
        return False

def daily_summary(date_str, db_conn):
    """Build a daily summary message and send."""
    c = db_conn
    vs_min = c.execute("SELECT COALESCE(SUM(minutes),0) FROM vscode_entries WHERE date=?", (date_str,)).fetchone()[0]
    aw = c.execute("SELECT active_seconds FROM activitywatch_daily WHERE date=?", (date_str,)).fetchone()
    aw_h = round((aw[0] or 0)/3600, 2) if aw else 0
    jira_actions = c.execute("SELECT COUNT(*) FROM jira_activity WHERE date=?", (date_str,)).fetchone()[0]
    jira_issues = c.execute("SELECT COUNT(DISTINCT issue_key) FROM jira_activity WHERE date=?", (date_str,)).fetchone()[0]
    pr_open = c.execute("SELECT COUNT(*) FROM github_events WHERE date=? AND event='opened'", (date_str,)).fetchone()[0]
    pr_merge = c.execute("SELECT COUNT(*) FROM github_events WHERE date=? AND event='merged'", (date_str,)).fetchone()[0]
    meet = c.execute("SELECT COALESCE(SUM(duration_minutes),0), COUNT(*) FROM calendar_events WHERE date=?", (date_str,)).fetchone()
    meet_h = round((meet[0] or 0)/60, 2)

    # top jira
    top_issue_row = c.execute(
        "SELECT issue_key, COUNT(*) n FROM jira_activity WHERE date=? GROUP BY issue_key ORDER BY n DESC LIMIT 1",
        (date_str,)
    ).fetchone()
    top_issue = f" · top: {top_issue_row[0]}" if top_issue_row else ""

    msg = (
        f"VSCode: {round(vs_min/60,2)}h · AW: {aw_h}h · Meet: {meet_h}h ({meet[1]})\n"
        f"Jira: {jira_actions} actions, {jira_issues} issues{top_issue}\n"
        f"PRs: {pr_open} opened, {pr_merge} merged"
    )
    title = f"Timesheet {date_str}"
    return send(title, msg, tags=["chart_with_upwards_trend"])
