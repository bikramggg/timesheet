"""
Log time to in-house Jira HnR Forge app via browser-use (LLM-driven automation).

Usage:
    pip install browser-use python-dotenv httpx
    export OPENAI_API_KEY=sk-...   # or ANTHROPIC_API_KEY
    python scripts/log_worklog_browseruse.py 2026-04-30

Reads plan from /api/worklog/plan, then drives the HnR app UI to submit each entry.
"""
import os, sys, asyncio, httpx, json
from dotenv import load_dotenv
load_dotenv()

TIMESHEET_URL = os.environ.get("TIMESHEET_URL", "http://raspberrypi.local:8080")
JIRA_BASE = os.environ.get("JIRA_BASE_URL", "https://testlify.atlassian.net")
HNR_PATH = os.environ.get(
    "HNR_PATH",
    "/jira/apps/450c899a-1fd9-417d-8934-8898c123f3ab/1baa3c54-c998-4854-b9c6-b38d1007a73c",
)


async def fetch_plan(date_str: str, target_hours: float = 8.0):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{TIMESHEET_URL}/api/worklog/plan",
                        params={"date": date_str, "target_hours": target_hours})
        r.raise_for_status()
        return r.json()


async def main(date_str: str, target_hours: float = 8.0, dry_run: bool = False):
    plan_resp = await fetch_plan(date_str, target_hours)
    plan = plan_resp["plan"]
    print(f"=== Plan for {date_str} ({plan_resp['plan_total_hours']}h, {len(plan)} entries) ===")
    for e in plan:
        delta = e["minutes"] - (e.get("already_logged_minutes") or 0)
        print(f"  {e['taskKey']:<12} {e['minutes']:>4}m ({e['startTime']}-{e['endTime']})  Δ{delta}m  {e['notes'][:80]}")
    if dry_run:
        return

    from browser_use import Agent
    from browser_use.browser.browser import Browser, BrowserConfig
    try:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=os.environ.get("OPENAI_MODEL", "gpt-4o"))
    except ImportError:
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"))

    # Build a single task description for the LLM
    entries_text = "\n".join(
        f"{i+1}. taskKey={e['taskKey']}, minutes={e['minutes']}, "
        f"startTime={e['startTime']}, endTime={e['endTime']}, "
        f"entryDate={e['entryDate']}, notes={e['notes']!r}"
        for i, e in enumerate(plan)
    )

    task = f"""
You are logging time entries in the in-house "HnR" Jira Forge app.

1. Navigate to {JIRA_BASE}{HNR_PATH}
2. Wait for the page to load and the time-entry form to be visible.
3. For each of the entries below, open the "Add entry" / "+" button (or the form), fill in:
     Task Key:    the value of `taskKey`
     Date:        the value of `entryDate`
     Start time:  the value of `startTime` (24h, HH:MM)
     End time:    the value of `endTime`
     Minutes:     the value of `minutes` (if a separate field exists)
     Notes:       the value of `notes`
   Then click Save / Submit.
4. After each save, wait for confirmation that the entry was created before moving on.
5. If you encounter an error for an entry, log it and continue with the next one.

Entries to log:
{entries_text}

Return a JSON summary at the end: {{"logged": [...], "failed": [...], "errors": [...]}}.
"""
    # browser-use expects a Browser config; using default Chromium
    browser = Browser(config=BrowserConfig(headless=False))
    agent = Agent(task=task, llm=llm, browser=browser)
    result = await agent.run()
    print("\n=== Result ===")
    print(result)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Usage: log_worklog_browseruse.py YYYY-MM-DD [target_hours] [--dry-run]")
        sys.exit(1)
    date_str = args[0]
    target = float(args[1]) if len(args) > 1 and not args[1].startswith("--") else 8.0
    dry = "--dry-run" in args
    asyncio.run(main(date_str, target, dry))
