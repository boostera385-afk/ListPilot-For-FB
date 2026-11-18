"""Text of the in-app "Getting Started" guide (no GUI imports, so it is unit tested).

The same steps are written out in docs/TESTING_GUIDE.md; a test keeps the titles in sync.
"""
from core.automation import listing_visibility as lv

# (section title, [step, ...]); a step is a dict:
#   id      - stable key used to remember "done" ticks
#   title   - heading (also used in docs/TESTING_GUIDE.md)
#   body    - what to do, plain text
#   action  - optional (button label, sidebar key) that jumps to the right page
#   auto    - optional key understood by ``auto_done`` (a step the app can verify itself)
SECTIONS = (
    ("Part 1 - One-time setup", [
        {
            "id": "start", "title": "Start the app",
            "body": "Always start ListPilot with Start_ListPilot.bat.\n"
                    "The first run installs Python (if missing), the environment and the libraries. "
                    "This takes a few minutes; later runs skip it.",
        },
        {
            "id": "browser", "title": "Choose your browser",
            "body": "Open Settings and set Browser to Incogniton or AdsPower.\n"
                    "Set the executable path only if the app cannot find the browser.\n"
                    "For AdsPower, also paste the API Key (AdsPower > Settings > API).",
            "action": ("Open Settings", "settings"),
        },
        {
            "id": "newsheet", "title": "Create a new Google Sheet for testing",
            "body": "Use a brand-new spreadsheet, not your listing-tracker sheet: go to sheets.new and name it "
                    "'ListPilot Test'. Do not paste any listing data (titles, cities, zipcodes) into it.\n"
                    "The Apps Script belongs to one spreadsheet, so this sheet gets its own script and its own URL.\n"
                    "The next step creates the four ListPilot tabs for you.",
        },
        {
            "id": "script", "title": "Install the Google Sheets script",
            "body": "1. Open Script Builder and click Copy Code.\n"
                    "2. In the NEW sheet: Extensions > Apps Script. Delete the old code and paste the new one.\n"
                    "3. Pick setupSheets in the function list and click Run (authorise it the first time).\n"
                    "4. Deploy > Manage deployments > Edit > New version > Deploy.\n"
                    "    Execute as: Me.  Who has access: Anyone.\n"
                    "5. Copy the Web app URL.\n"
                    "After setupSheets, the sheet has the tabs: FB Listings, Dominancy Checker, Response Checker, "
                    "Live Checker.",
            "action": ("Open Script Builder", "script"),
        },
        {
            "id": "url", "title": "Connect the Web App URL",
            "body": "In Settings, replace the 'Google Apps Script Web App URL' with the NEW sheet's URL and save. "
                    "The app reads only the sheet behind this URL, so an old URL keeps reading the old sheet.",
            "action": ("Open Settings", "settings"), "auto": "script_url",
        },
        {
            "id": "checkers", "title": "Set the PK and US checker accounts (optional)",
            "body": "In Settings > Live Checker - Checker Profiles choose one Pakistani profile and one US profile "
                    "with a confirmed location.\nLeave them empty to skip the buyer visibility step. "
                    "For the very first test, leave them empty.",
            "action": ("Open Settings", "settings"), "auto": "checkers",
        },
    ]),
    ("Part 2 - Prepare the sheet", [
        {
            "id": "rows", "title": "Add two test rows",
            "body": "Open the 'Live Checker' tab of the new sheet. Row 1 already has the headers; fill rows 2 and 3 only:\n"
                    "  ID - the profile ID from Incogniton / AdsPower (or leave empty and use Profile Name).\n"
                    "  Profile Name - the exact profile name in your browser.\n"
                    "  Proxy - leave EMPTY. A filled Proxy overwrites the profile's own proxy before launch.\n"
                    "  Status - leave EMPTY (empty means 'to do').\n"
                    "  Row 2 (Row A): a profile you know is logged in and has listings.\n"
                    "  Row 3 (Row B): the profile that previously showed nothing.\n"
                    "Nothing else is needed: the other columns are filled in by the app. "
                    "Both profiles must be logged in to Facebook and not already open.",
        },
    ]),
    ("Part 3 - Tests", [
        {
            "id": "t1", "title": "Test 1: basic run",
            "body": "Open Live Checker, mode 'New rows (empty Status)', concurrency 1 or 2, click Start. "
                    "Do not use the profile's browser window while it runs.\n"
                    "The log should show: Connecting to browser, Opening Marketplace 'Your listings', "
                    "Responses: N, then 'Full scan: ... (N listings; tabs: ...)'.\n"
                    "In the sheet check: Status = Success, Check Result = OK, Total Listings = Live + Pending + "
                    "Rejected + Other, and the total matches Facebook's own Your listings page. "
                    "The Live History tab gets a row with Change = First check.",
            "action": ("Open Live Checker", "live"),
        },
        {
            "id": "t2", "title": "Test 2: failure handling",
            "body": "Check Result tells you why a profile failed:\n"
                    "  LOGGED_OUT - log in, then re-check.\n"
                    "  CHECKPOINT - fix by hand; it is never retried.\n"
                    "  LANG_NOT_EN - switch the Facebook language to English.\n"
                    "  NO_LISTINGS - really no listings; counts show 0.\n"
                    "  LAYOUT_UNKNOWN / TIMEOUT - retried once automatically, then see the screenshot in debug/.\n"
                    "  PARTIAL: ... - counts saved but incomplete; read the reason after the colon.",
        },
        {
            "id": "t3", "title": "Test 3: re-check modes",
            "body": "Run 'Re-check every profile': Live History Change should say No change, or show what moved "
                    "(e.g. Live 5->3).\n"
                    "Try 'Re-check failed profiles' (should pick up Row B) and 'Re-check profiles with pending "
                    "listings' (rows with Pending Count above 0).",
            "action": ("Open Live Checker", "live"),
        },
        {
            "id": "t4", "title": "Test 4: scale and stop",
            "body": "Add 5 to 10 profiles, concurrency 3. Click Stop while it runs: current rows end as "
                    "'Failed (Stopped)' and none stays CLAIMED.\n"
                    "Rows left CLAIMED for over an hour (a crashed run) are freed automatically at the next start.",
        },
        {
            "id": "t5", "title": "Test 5: buyer visibility (optional)",
            "body": "With the PK and US checker profiles set, run a check on 1-2 profiles with live listings. "
                    "Step 2 opens the PK account, then the US account.\n"
                    "Read PK Check, US Check, Visibility and Visibility Details. LINK_ONLY from the PK account is "
                    "normal for US-city listings; HIDDEN is the real warning.",
        },
    ]),
    ("Part 4 - Report back", [
        {
            "id": "send", "title": "Send the results",
            "body": "After each test send: the log text from the Live Checker tab, the sheet row, the newest "
                    "screenshot from the debug/ folder (if it failed), and what Facebook shows for the same "
                    "account (counts).\n"
                    "Safety: start with 1-2 profiles; do not re-run checkpointed profiles over and over; "
                    "do not share debug/ or config.json.",
        },
    ]),
)


def all_steps():
    return [step for _, steps in SECTIONS for step in steps]


def auto_done(key, get_setting):
    """True when the app can see a step is finished. ``get_setting`` is core.config.get_setting."""
    if key == "script_url":
        return bool(str(get_setting("apps_script_url", "") or "").strip())
    if key == "checkers":
        return any(str(get_setting(setting, "") or "").strip() for _, setting in lv.CHECKERS)
    return False


def progress(done_ids, get_setting):
    """(finished, total) counting manual ticks and auto-detected steps."""
    steps = all_steps()
    done = sum(
        1 for step in steps
        if step["id"] in (done_ids or ()) or (step.get("auto") and auto_done(step["auto"], get_setting))
    )
    return done, len(steps)
