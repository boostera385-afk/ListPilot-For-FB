# ListPilot - Getting started and first live test

The same steps are shown inside the app under **Getting Started** (sidebar), with tick boxes and
buttons that jump to the right page.

## Part 1 - One-time setup

### Start the app
- Always start ListPilot with `Start_ListPilot.bat`.
- The first run installs Python (if missing), the virtual environment and the libraries. This takes a few minutes; later runs skip it.

### Choose your browser
- Open **Settings** and set **Browser** to Incogniton or AdsPower.
- Set the **executable path** only if the app cannot find the browser.
- For AdsPower, also paste the **API Key** (AdsPower > Settings > API).

### Install the Google Sheets script
1. Open **Script Builder** and click **Copy Code** (same file as `core/assets/apps_script.js`).
2. In your Google Sheet: **Extensions > Apps Script**. Delete the old code and paste the new one.
3. Pick `setupSheets` in the function list and click **Run** (authorise it the first time). This adds missing columns and never deletes data.
4. **Deploy > Manage deployments > Edit > New version > Deploy**. Execute as **Me**, who has access **Anyone**.
5. Copy the **Web app URL**.

### Connect the Web App URL
- In **Settings**, paste the URL into **Google Apps Script Web App URL** and save.

### Set the PK and US checker accounts (optional)
- In **Settings > Live Checker - Checker Profiles** choose one Pakistani profile and one US profile with a confirmed location.
- Leave them empty to skip the buyer visibility step. For the very first test, leave them empty.

## Part 2 - Prepare the sheet

### Add two test rows
Open the **Live Checker** tab of your Sheet. It should have `ID`, `Profile Name`, `Proxy`, `Status`, `Responses`, `All Listings`, `Needs Attention`, `Total Listings`, `Live Count`, `Pending Count`, `Rejected Count`, `Other Count`, `Status Summary`, `Listing Details`, `Check Result`, `Last Checked` and the PK/US columns.

Add only two rows and leave `Status` empty (empty means "to do"):
- **Row A** - a profile you know is logged in and has listings.
- **Row B** - the profile that previously showed nothing.

Make sure both profiles are logged in to Facebook and not already open.

## Part 3 - Tests

### Test 1: basic run
Open **Live Checker**, mode "New rows (empty Status)", concurrency 1 or 2, click **Start**. Do not use the profile's browser window while it runs.

The log should show: `Connecting to browser`, `Opening Marketplace 'Your listings'`, `Responses: N`, then `Full scan: ... (N listings; tabs: ...)`.

| Check in the sheet | Expected |
|---|---|
| `Status` | `Success` (or `Failed: ...`) |
| `Total Listings` | Live + Pending + Rejected + Other |
| `Total Listings` vs Facebook | Matches Facebook's own Your listings page |
| `Check Result` | `OK` |
| `Status Summary` | e.g. `12 LIVE, 2 PENDING` |
| `Listing Details` | One line per listing with its ID |
| `Last Checked` | Today's time |
| **Live History** tab | New row, `Change` = `First check` |

Compare two or three numbers with Facebook by hand: Active, Needs attention, and one listing's clicks.

### Test 2: failure handling
| `Check Result` | Meaning | What to do |
|---|---|---|
| `LOGGED_OUT` | Profile not logged in | Log in, then re-check |
| `CHECKPOINT` | Facebook identity/lock screen | Fix by hand; never retried |
| `LANG_NOT_EN` | Account language is not English | Switch Facebook to English |
| `NO_LISTINGS` | Really no listings | Counts show 0 |
| `LAYOUT_UNKNOWN` / `TIMEOUT` | Page loaded oddly | Retried once automatically; then see the screenshot in `debug/` |
| `PARTIAL: ...` | Counts saved but incomplete | Read the reason after the colon |

### Test 3: re-check modes
- Run "Re-check every profile": Live History `Change` should say `No change`, or show what moved (e.g. `Live 5->3`).
- Try "Re-check failed profiles" (should pick up Row B) and "Re-check profiles with pending listings" (rows with `Pending Count` above 0).

### Test 4: scale and stop
- Add 5 to 10 profiles, concurrency 3.
- Click **Stop** while it runs: current rows end as `Failed (Stopped)` and none stays `CLAIMED`.
- Rows left `CLAIMED` for over an hour (a crashed run) are freed automatically at the next start.

### Test 5: buyer visibility (optional)
- With the PK and US checker profiles set, run a check on 1-2 profiles with live listings. Step 2 opens the PK account, then the US account.
- Read `PK Check`, `US Check`, `Visibility` and `Visibility Details`. `LINK_ONLY` from the PK account is normal for US-city listings; `HIDDEN` is the real warning.

## Part 4 - Report back

### Send the results
After each test send:
- the **log text** from the Live Checker tab
- the **sheet row**
- the newest **screenshot** from `debug/` (if it failed)
- what Facebook shows for the same account (counts)

Safety:
- Start with 1-2 profiles, not your whole list.
- Do not re-run checkpointed profiles over and over.
- `debug/` screenshots can contain account content, and `config.json` holds your Apps Script URL. Do not share either.
