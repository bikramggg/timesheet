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
TEMPLATE_FILE = Path(__file__).parent / ".hnr_template.json"


async def fetch_plan(date_str: str, target_hours: float = 8.0):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{TIMESHEET_URL}/api/worklog/plan",
                        params={"date": date_str, "target_hours": target_hours})
        r.raise_for_status()
        return r.json()


async def _capture_template(plan, headless=False):
    """Open browser, wait for user to submit ONE entry by hand to capture template.
    Saves snapshot synchronously when the request fires, so closing the browser doesn't lose data."""
    from playwright.async_api import async_playwright
    captured = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(storage_state=str(STATE_FILE) if STATE_FILE.exists() else None)
        page = await ctx.new_page()

        def on_req_sync(req):
            url = req.url
            if "/gateway/api/graphql/pq/" not in url: return
            pd = req.post_data
            if not pd or "useInvokeExtensionRelayMutation" not in pd: return
            try: body = json.loads(pd)
            except Exception: return
            payload = body.get("variables", {}).get("input", {}).get("payload", {})
            if payload.get("call", {}).get("functionKey") != "createEntry": return
            # Use SYNC headers dict — available immediately, no await needed
            try:
                hdrs = dict(req.headers)
            except Exception:
                hdrs = {}
            captured["url"] = url
            captured["body_template"] = body
            captured["headers"] = hdrs
            print("[capture] got live createEntry request")

        page.on("request", on_req_sync)
        await page.goto(JIRA_BASE + HNR_PATH)
        print("[capture] log in if needed, then submit ONE worklog entry by hand. Waiting 5 min...")
        for _ in range(60):
            if "url" in captured: break
            await asyncio.sleep(5)
        # Snapshot cookies + state while browser still alive
        if "url" in captured:
            try:
                captured["cookies"] = await ctx.cookies()
            except Exception:
                captured["cookies"] = []
        try:
            await ctx.storage_state(path=str(STATE_FILE))
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass

    if "url" not in captured:
        return None

    hdrs = captured.get("headers", {})
    template = {
        "url": captured["url"],
        "body_template": captured["body_template"],
        "atl_attribution": hdrs.get("atl-attribution", ""),
        "atl_client_name": hdrs.get("atl-client-name", "atlassian-frontend-monorepo"),
        "x_experimentalapi": hdrs.get("x-experimentalapi", ""),
        "user_agent": hdrs.get("user-agent", "Mozilla/5.0"),
        "cookies": captured.get("cookies", []),
    }
    TEMPLATE_FILE.write_text(json.dumps(template, indent=2))
    print(f"[capture] template saved to {TEMPLATE_FILE}")
    return template


async def _refresh_context_token(headless=True):
    """Open page (headless OK), wait for any Forge GraphQL request, return fresh contextToken + cookies."""
    from playwright.async_api import async_playwright
    fresh = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(storage_state=str(STATE_FILE) if STATE_FILE.exists() else None)
        page = await ctx.new_page()

        def on_req_sync(req):
            if "/gateway/api/graphql/pq/" not in req.url: return
            pd = req.post_data
            if not pd: return
            try: body = json.loads(pd)
            except: return
            payload = body.get("variables", {}).get("input", {}).get("payload", {})
            ctx_tok = payload.get("contextToken")
            if ctx_tok and "contextToken" not in fresh:
                fresh["contextToken"] = ctx_tok
                fresh["context"] = payload.get("context", {})
                fresh["contextIds"] = body.get("variables", {}).get("input", {}).get("contextIds", [])
                fresh["extensionId"] = body.get("variables", {}).get("input", {}).get("extensionId", "")

        page.on("request", on_req_sync)
        try:
            await page.goto(JIRA_BASE + HNR_PATH, timeout=60000)
        except Exception as e:
            print(f"[refresh] navigate warning: {e}")
        # Wait up to 30s
        for _ in range(30):
            if "contextToken" in fresh: break
            await asyncio.sleep(1)
        cookies = []
        try:
            cookies = await ctx.cookies()
        except Exception: pass
        try:
            await ctx.storage_state(path=str(STATE_FILE))
        except Exception: pass
        try:
            await browser.close()
        except Exception: pass

    fresh["cookies"] = cookies
    return fresh if "contextToken" in fresh else None


async def replay_mode(plan, headless=False):
    """
    First run (no template cached): user logs in + submits 1 entry by hand to capture template.
    Subsequent runs: headless, fetches fresh contextToken, replays all entries directly.
    """
    if not TEMPLATE_FILE.exists():
        print("[replay] no template cached. Capturing now (one-time setup).")
        template = await _capture_template(plan, headless=False)
        if not template:
            print("[replay] capture failed/timed out. Aborting.")
            return
        # Replay remaining entries (skip first, which was submitted manually)
        plan_to_send = plan[1:]
        print(f"[replay] replaying {len(plan_to_send)} remaining entries from captured template")
    else:
        template = json.loads(TEMPLATE_FILE.read_text())
        print("[replay] using cached template; refreshing contextToken headlessly")
        fresh = await _refresh_context_token(headless=True)
        if not fresh:
            print("[replay] could not get fresh contextToken (session expired?). Re-run with --recapture to redo login.")
            return
        # Inject fresh tokens into template
        body = json.loads(json.dumps(template["body_template"]))
        body["variables"]["input"]["contextIds"] = fresh["contextIds"] or body["variables"]["input"]["contextIds"]
        body["variables"]["input"]["extensionId"] = fresh["extensionId"] or body["variables"]["input"]["extensionId"]
        body["variables"]["input"]["payload"]["context"] = fresh["context"] or body["variables"]["input"]["payload"]["context"]
        body["variables"]["input"]["payload"]["contextToken"] = fresh["contextToken"]
        template["body_template"] = body
        template["cookies"] = fresh["cookies"]
        plan_to_send = plan

    cookies = template.get("cookies") or []
    cookie_dict = {c["name"]: c["value"] for c in cookies
                   if c["domain"].endswith("atlassian.net") or c["domain"].endswith("atlassian.com")}

    async with httpx.AsyncClient(
        timeout=30,
        cookies=cookie_dict,
        headers={
            "content-type": "application/json",
            "origin": JIRA_BASE,
            "referer": JIRA_BASE + HNR_PATH,
            "user-agent": template["user_agent"],
            "atl-attribution": template["atl_attribution"],
            "atl-client-name": template["atl_client_name"],
            "x-experimentalapi": template["x_experimentalapi"],
            "x-request-fallback-to-post-reason": "mutation",
            "accept": "application/graphql-response+json, application/json",
        },
    ) as cli:
        ok_count = fail_count = skip_count = 0
        for e in plan_to_send:
            already = e.get("already_logged_minutes") or 0
            net = max(0, int(e["minutes"]) - int(already))
            if net < 1:
                skip_count += 1
                print(f"  {e['taskKey']:<12} {e['minutes']:>4}m  SKIP (already logged {already}m)")
                continue
            # Adjust endTime if we shrank duration due to existing log
            new_end = e["endTime"]
            if net != int(e["minutes"]):
                from datetime import datetime as _dt, timedelta as _td
                st = _dt.strptime(e["startTime"], "%H:%M")
                new_end = (st + _td(minutes=net)).strftime("%H:%M")

            body = json.loads(json.dumps(template["body_template"]))
            body["variables"]["input"]["payload"]["call"]["payload"] = {
                "taskKey": e["taskKey"],
                "minutes": net,
                "notes": e["notes"] + (f" (delta from {e['minutes']}m, already {already}m)" if net != e["minutes"] else ""),
                "entryDate": e["entryDate"],
                "startTime": e["startTime"],
                "endTime": new_end,
            }
            r = await cli.post(template["url"], json=body)
            ok = False; reason = ""
            try:
                resp = r.json()
                errs = resp.get("errors")
                if r.status_code == 200 and not errs:
                    ok = True
                else:
                    reason = json.dumps(errs)[:140] if errs else f"HTTP {r.status_code}"
            except Exception:
                reason = r.text[:140]
            ok_count += int(ok); fail_count += int(not ok)
            print(f"  {e['taskKey']:<12} {e['minutes']:>4}m {e['startTime']}-{e['endTime']}  -> {r.status_code}  {'OK' if ok else 'FAIL: '+reason}")
        print(f"[replay] done: {ok_count} OK, {fail_count} FAIL, {skip_count} SKIP")


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
        print("Usage: log_worklog_playwright.py YYYY-MM-DD [--mode=ui|replay] [--target=8] [--headless] [--dry-run] [--recapture]")
        sys.exit(1)
    date_str = args[0]
    mode = next((a.split("=",1)[1] for a in args if a.startswith("--mode=")), "replay")
    target = float(next((a.split("=",1)[1] for a in args if a.startswith("--target=")), 8.0))
    headless = "--headless" in args
    dry = "--dry-run" in args
    recapture = "--recapture" in args

    if recapture and TEMPLATE_FILE.exists():
        TEMPLATE_FILE.unlink()
        print(f"[setup] removed cached template at {TEMPLATE_FILE}")

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
