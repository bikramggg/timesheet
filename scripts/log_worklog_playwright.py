"""
Log time to in-house Jira HnR Forge app via Playwright (deterministic, no LLM).

Two modes:
  --mode=ui      drive the form UI (default)
  --mode=replay  intercept fresh contextToken + persisted-query hash, replay GraphQL mutation directly (faster)

Usage:
    pip install playwright httpx python-dotenv
    playwright install chromium
    python scripts/log_worklog_playwright.py 2026-04-30

First run: a Chromium window opens, you log in to Atlassian once.
The session is stored at scripts/.playwright_state.json so subsequent runs are non-interactive.
"""
import os, sys, asyncio, httpx, json, re, time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

TIMESHEET_URL = os.environ.get("TIMESHEET_URL", "http://raspberrypi.local:8080")
JIRA_BASE = os.environ.get("JIRA_BASE_URL", "https://testlify.atlassian.net")
HNR_PATH = os.environ.get(
    "HNR_PATH",
    "/jira/apps/450c899a-1fd9-417d-8934-8898c123f3ab/1baa3c54-c998-4854-b9c6-b38d1007a73c",
)
STATE_FILE = Path(__file__).parent / ".playwright_state.json"


async def fetch_plan(date_str: str, target_hours: float = 8.0):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{TIMESHEET_URL}/api/worklog/plan",
                        params={"date": date_str, "target_hours": target_hours})
        r.raise_for_status()
        return r.json()


async def replay_mode(plan, headless=False):
    """Capture one real submit, extract the GraphQL endpoint + contextToken, replay for remaining entries."""
    from playwright.async_api import async_playwright
    captured = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(storage_state=str(STATE_FILE) if STATE_FILE.exists() else None)
        page = await ctx.new_page()

        async def on_req(req):
            url = req.url
            if "/gateway/api/graphql/pq/" in url and "useInvokeExtensionRelayMutation" in (url + (req.post_data or "")):
                if not req.post_data: return
                try:
                    body = json.loads(req.post_data)
                except Exception:
                    return
                payload = body.get("variables", {}).get("input", {}).get("payload", {})
                call = payload.get("call", {})
                if call.get("functionKey") == "createEntry":
                    captured["url"] = url
                    captured["body_template"] = body  # we'll mutate the inner taskKey/minutes/etc
                    captured["headers"] = await req.all_headers()
                    captured["cookies"] = await ctx.cookies()
                    print("[replay] captured live createEntry request")

        page.on("request", on_req)
        await page.goto(JIRA_BASE + HNR_PATH)
        print("[replay] log in if needed, then submit ONE worklog entry by hand. Waiting 5 minutes.")
        # Wait until we see the request
        for _ in range(60):
            if "url" in captured: break
            await asyncio.sleep(5)
        if "url" not in captured:
            print("Timed out waiting for a sample createEntry submission. Aborting.")
            await ctx.storage_state(path=str(STATE_FILE))
            await browser.close()
            return

        await ctx.storage_state(path=str(STATE_FILE))

        # Replay
        async with httpx.AsyncClient(
            timeout=30,
            cookies={c["name"]: c["value"] for c in captured["cookies"] if c["domain"].endswith("atlassian.net") or c["domain"].endswith("atlassian.com")},
            headers={
                "content-type": "application/json",
                "origin": JIRA_BASE,
                "referer": JIRA_BASE + HNR_PATH,
                "user-agent": captured["headers"].get("user-agent", "Mozilla/5.0"),
                "atl-attribution": captured["headers"].get("atl-attribution", ""),
                "atl-client-name": captured["headers"].get("atl-client-name", "atlassian-frontend-monorepo"),
                "x-experimentalapi": captured["headers"].get("x-experimentalapi", ""),
                "x-request-fallback-to-post-reason": "mutation",
            },
        ) as cli:
            for e in plan:
                body = json.loads(json.dumps(captured["body_template"]))
                body["variables"]["input"]["payload"]["call"]["payload"] = {
                    "taskKey": e["taskKey"],
                    "minutes": int(e["minutes"]),
                    "notes": e["notes"],
                    "entryDate": e["entryDate"],
                    "startTime": e["startTime"],
                    "endTime": e["endTime"],
                }
                r = await cli.post(captured["url"], json=body)
                ok = r.status_code == 200 and "errors" not in r.text
                print(f"  {e['taskKey']:<12} {e['minutes']}m  -> {r.status_code}  {'OK' if ok else r.text[:120]}")

        await browser.close()


async def ui_mode(plan, headless=False):
    """Drive the form UI with deterministic selectors. Adjust selectors after inspecting the HnR page."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(storage_state=str(STATE_FILE) if STATE_FILE.exists() else None)
        page = await ctx.new_page()
        await page.goto(JIRA_BASE + HNR_PATH)

        if not STATE_FILE.exists():
            print("First run: log in, then return to terminal.")
            input("Press Enter once logged in and the HnR page is loaded... ")
            await ctx.storage_state(path=str(STATE_FILE))

        # The HnR app is in an iframe (Forge bridge). Find it.
        await page.wait_for_load_state("networkidle")

        # Find Forge iframe
        iframes = page.frames
        forge_frame = None
        for f in iframes:
            if "extension/" in f.url or "hnr-global-page" in f.url:
                forge_frame = f; break
        if not forge_frame:
            print("Could not find Forge iframe. Frames:")
            for f in iframes: print(f"  {f.url}")
            await browser.close()
            return

        for e in plan:
            try:
                # !! ADJUST THESE SELECTORS — inspect the HnR app to find real ones !!
                await forge_frame.click("button:has-text('Add')")
                await forge_frame.fill("input[name='taskKey'], input[placeholder*='task' i]", e["taskKey"])
                await forge_frame.fill("input[type='date'], input[name='entryDate']", e["entryDate"])
                await forge_frame.fill("input[name='startTime'], input[placeholder*='start' i]", e["startTime"])
                await forge_frame.fill("input[name='endTime'], input[placeholder*='end' i]", e["endTime"])
                await forge_frame.fill("textarea[name='notes'], textarea[placeholder*='note' i]", e["notes"])
                await forge_frame.click("button:has-text('Save')")
                await forge_frame.wait_for_timeout(1500)
                print(f"  {e['taskKey']:<12} {e['minutes']}m  OK")
            except Exception as ex:
                print(f"  {e['taskKey']:<12} {e['minutes']}m  FAIL: {ex}")

        await ctx.storage_state(path=str(STATE_FILE))
        await browser.close()


async def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: log_worklog_playwright.py YYYY-MM-DD [--mode=ui|replay] [--target=8] [--headless] [--dry-run]")
        sys.exit(1)
    date_str = args[0]
    mode = next((a.split("=",1)[1] for a in args if a.startswith("--mode=")), "ui")
    target = float(next((a.split("=",1)[1] for a in args if a.startswith("--target=")), 8.0))
    headless = "--headless" in args
    dry = "--dry-run" in args

    plan_resp = await fetch_plan(date_str, target)
    plan = plan_resp["plan"]
    print(f"=== Plan for {date_str} ({plan_resp['plan_total_hours']}h, {len(plan)} entries) ===")
    for e in plan:
        print(f"  {e['taskKey']:<12} {e['minutes']:>4}m ({e['startTime']}-{e['endTime']})  {e['notes'][:80]}")
    if dry: return

    if mode == "replay":
        await replay_mode(plan, headless)
    else:
        await ui_mode(plan, headless)


if __name__ == "__main__":
    asyncio.run(main())
