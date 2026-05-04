import sqlite3, os
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    date TEXT PRIMARY KEY,
    weekday TEXT,
    is_weekend INTEGER,
    holiday TEXT
);

CREATE TABLE IF NOT EXISTS vscode_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    project TEXT NOT NULL,
    branch TEXT,
    language TEXT,
    minutes REAL NOT NULL,
    UNIQUE(date, project, branch, language)
);
CREATE INDEX IF NOT EXISTS idx_vscode_date ON vscode_entries(date);

CREATE TABLE IF NOT EXISTS activitywatch_daily (
    date TEXT PRIMARY KEY,
    active_seconds REAL,
    window_seconds REAL,
    afk_seconds REAL
);

CREATE TABLE IF NOT EXISTS jira_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    issue_key TEXT NOT NULL,
    summary TEXT,
    status TEXT,
    project TEXT,
    action TEXT NOT NULL,  -- comment | worklog | transition | edit_description | edit_field:X | touched
    detail TEXT,           -- worklog seconds, transition from->to, etc.
    UNIQUE(date, issue_key, action, detail)
);
CREATE INDEX IF NOT EXISTS idx_jira_date ON jira_activity(date);
CREATE INDEX IF NOT EXISTS idx_jira_key ON jira_activity(issue_key);

CREATE TABLE IF NOT EXISTS github_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER,
    title TEXT,
    event TEXT,            -- opened | merged | closed | commented
    url TEXT,
    UNIQUE(date, repo, pr_number, event)
);
CREATE INDEX IF NOT EXISTS idx_gh_date ON github_events(date);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    summary TEXT,
    start_time TEXT,
    end_time TEXT,
    duration_minutes INTEGER,
    is_meet INTEGER DEFAULT 0,
    response TEXT,
    uid TEXT,
    UNIQUE(date, uid, start_time)
);
CREATE INDEX IF NOT EXISTS idx_cal_date ON calendar_events(date);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    source TEXT,
    status TEXT,
    rows_added INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    summary_md TEXT,
    generated_at TEXT
);

CREATE TABLE IF NOT EXISTS meeting_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    source TEXT NOT NULL,         -- slack_huddle | google_meet | zoom | teams | facetime | other
    title TEXT,
    start_time TEXT,
    end_time TEXT,
    duration_seconds REAL,
    UNIQUE(date, source, start_time)
);
CREATE INDEX IF NOT EXISTS idx_meet_date ON meeting_sessions(date);
"""

def get_db_path():
    return os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "timesheet.db"))

@contextmanager
def conn():
    p = get_db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()

def init():
    with conn() as c:
        c.executescript(SCHEMA)

if __name__ == "__main__":
    init()
    print(f"DB initialized at {get_db_path()}")
