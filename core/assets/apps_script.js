/**
 * Google Apps Script for FB Listings Automation
 *
 * INSTRUCTIONS:
 * 1. Open your Google Sheet.
 * 2. Click on "Extensions" > "Apps Script".
 * 3. Delete any code in the editor and paste this entire script.
 * 4. Select setupSheets and click Run once. This adds missing columns without deleting data.
 * 5. Click the "Deploy" button (top right) > "New deployment" (or "Manage deployments" -> Edit -> New version).
 * 6. Select type "Web app".
 * 7. Set "Execute as" to "Me".
 * 8. Set "Who has access" to "Anyone".
 * 9. Click "Deploy" (you may need to authorize permissions).
 * 10. Copy the generated "Web app URL" and save it in the Desktop App Settings.
 *
 * Tab names and column headers are matched forgivingly: case, spacing,
 * punctuation, plurals and small typos are accepted ("fb listing",
 * "profile_name", "Responses", "Pictres").
 */

const SHEET_FB_LISTING = "FB Listings";
const SHEET_DOMINANCY_CHECKER = "Dominancy Checker";
const SHEET_RESPONSE_CHECKER = "Response Checker";
const SHEET_LIVE_CHECKER = "Live Checker";
const SHEET_LIVE_HISTORY = "Live History";  // created automatically by appendLiveHistory

// Writes a value without letting Sheets run it as a formula. Facebook listing
// titles are untrusted text: a title such as =IMPORTXML(...) must stay text.
function setCellSafely_(range, value) {
  if (typeof value === "string") {
    if (/^[=+\-@\t\r]/.test(value)) range.setNumberFormat("@");
    // Sheets cells hold at most 50,000 characters.
    value = value.length > 49000 ? value.slice(0, 48997) + "..." : value;
  }
  range.setValue(value);
}

// Columns the app writes to; created automatically when missing.
// "Response" = unread Messenger count when the profile opens;
// "Responses" = messages on the just-published listing (from the live check).
// "All Listings" / "Needs Attention" = the first listing of each tab.
const AUTO_COLUMNS = {};
const LIVE_CHECK_COLUMNS = ["Total Listings", "Live Count", "Pending Count", "Rejected Count", "Other Count", "Status Summary", "Listing Details", "Check Result", "Last Checked", "Scan Time"];
AUTO_COLUMNS[SHEET_FB_LISTING] = ["Response", "Responses", "Live", "All Listings", "Needs Attention"].concat(LIVE_CHECK_COLUMNS);
// Buyer-side check from the Pakistani and USA checker accounts.
const VISIBILITY_COLUMNS = ["PK Check", "US Check", "Visibility", "Visibility Details", "Visibility Checked"];
AUTO_COLUMNS[SHEET_LIVE_CHECKER] = ["Responses", "All Listings", "Needs Attention"].concat(LIVE_CHECK_COLUMNS, VISIBILITY_COLUMNS);
AUTO_COLUMNS[SHEET_RESPONSE_CHECKER] = ["Responses"];

function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// "Profile_Names " -> "profilename" (letters/digits only, no trailing plural s).
function headerKey_(value) {
  let key = String(value == null ? "" : value).toLowerCase().replace(/[^0-9a-z]+/g, "");
  if (key.length > 3 && key.charAt(key.length - 1) === "s") key = key.slice(0, -1);
  return key;
}

function similarity_(a, b) {
  if (a === b) return 1;
  if (!a || !b) return 0;
  const prev = [];
  for (let j = 0; j <= b.length; j++) prev[j] = j;
  for (let i = 1; i <= a.length; i++) {
    let diag = prev[0];
    prev[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const temp = prev[j];
      prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, diag + (a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1));
      diag = temp;
    }
  }
  return 1 - prev[b.length] / Math.max(a.length, b.length);
}

function digits_(value) {
  return (String(value).match(/\d+/g) || []).join(",");
}

// Index of the best match for any of `names` in `values`, or -1.
// Tiers: exact (case/space-insensitive) -> same letters -> one clear typo match.
function findBest_(values, names) {
  const list = values.map(v => String(v == null ? "" : v));
  const wanted = [].concat(names);
  for (const name of wanted) {
    const target = String(name).trim().toLowerCase().replace(/\s+/g, " ");
    const idx = list.findIndex(v => v.trim().toLowerCase().replace(/\s+/g, " ") === target);
    if (idx !== -1) return idx;
  }
  for (const name of wanted) {
    const target = headerKey_(name);
    const idx = list.findIndex(v => target && headerKey_(v) === target);
    if (idx !== -1) return idx;
  }
  for (const name of wanted) {
    const target = headerKey_(name);
    if (!target) continue;
    const scored = list
      .map((v, i) => ({i: i, score: digits_(v) === digits_(name) ? similarity_(target, headerKey_(v)) : 0}))
      .sort((x, y) => y.score - x.score);
    if (scored.length && scored[0].score >= 0.8 && (scored.length === 1 || scored[0].score - scored[1].score >= 0.05)) {
      return scored[0].i;
    }
  }
  return -1;
}

function getSheet_(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const exact = ss.getSheetByName(name);
  if (exact) return exact;
  const sheets = ss.getSheets();
  const idx = findBest_(sheets.map(s => s.getName()), name);
  return idx === -1 ? null : sheets[idx];
}

function canonicalSheetName_(sheet) {
  const idx = findBest_([SHEET_FB_LISTING, SHEET_DOMINANCY_CHECKER, SHEET_RESPONSE_CHECKER, SHEET_LIVE_CHECKER], sheet.getName());
  return idx === -1 ? sheet.getName() : [SHEET_FB_LISTING, SHEET_DOMINANCY_CHECKER, SHEET_RESPONSE_CHECKER, SHEET_LIVE_CHECKER][idx];
}

function getHeaders_(sheet) {
  return sheet.getRange(1, 1, 1, Math.max(sheet.getLastColumn(), 1)).getValues()[0];
}

// Index of a header equal to `name` ignoring only case and spacing, or -1.
// Used for app-owned columns so "Responses" never lands in "Response".
function findExact_(values, name) {
  const target = String(name).trim().toLowerCase().replace(/\s+/g, " ");
  return values.findIndex(v => String(v == null ? "" : v).trim().toLowerCase().replace(/\s+/g, " ") === target);
}

// 1-based column for app-owned `name`, creating it at the end when missing.
function ensureColumn_(sheet, name) {
  let idx = findExact_(getHeaders_(sheet), name);
  if (idx !== -1) return idx + 1;

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    idx = findExact_(getHeaders_(sheet), name);
    if (idx !== -1) return idx + 1;
    const column = sheet.getLastColumn() + 1;
    sheet.getRange(1, column).setValue(name).setFontWeight("bold");
    // New columns match the existing header look instead of appearing plain.
    if (column > 1) sheet.getRange(1, column - 1).copyFormatToRange(sheet, column, column, 1, 1);
    return column;
  } finally {
    lock.releaseLock();
  }
}

function ensureAutoColumns_(sheet) {
  (AUTO_COLUMNS[canonicalSheetName_(sheet)] || []).forEach(name => ensureColumn_(sheet, name));
}

function isPendingStatus_(value) {
  const status = String(value == null ? "" : value).trim().toLowerCase();
  return status === "" || status === "pending";
}

function setupSheets() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const requiredSheets = [SHEET_FB_LISTING, SHEET_DOMINANCY_CHECKER, SHEET_RESPONSE_CHECKER, SHEET_LIVE_CHECKER];

  requiredSheets.forEach(sheetName => {
    let sheet = getSheet_(sheetName);
    if (!sheet) {
      sheet = ss.insertSheet(sheetName);
      if (sheetName === SHEET_FB_LISTING) {
        sheet.appendRow(["ID", "Profile Name", "Title", "Price", "Description", "Category", "Condition", "Location", "Pictures", "Proxy", "Response", "Responses", "Live", "Status"]);
        sheet.getRange("A1:N1").setFontWeight("bold");
        sheet.setFrozenRows(1);
      } else if (sheetName === SHEET_DOMINANCY_CHECKER) {
        sheet.appendRow(["ID", "Profile Name", "Query", "Location", "Radius", "Target Number", "Proxy", "Status"]);
        sheet.getRange("A1:H1").setFontWeight("bold");
        sheet.setFrozenRows(1);
      } else if (sheetName === SHEET_RESPONSE_CHECKER) {
        sheet.appendRow(["ID", "Profile Name", "Proxy", "Status"]);
        sheet.getRange("A1:D1").setFontWeight("bold");
        sheet.setFrozenRows(1);
      } else if (sheetName === SHEET_LIVE_CHECKER) {
        sheet.appendRow(["ID", "Profile Name", "Proxy", "Status", "All Listings", "Needs Attention"]);
        sheet.getRange("A1:F1").setFontWeight("bold");
        sheet.setFrozenRows(1);
      }
    }
    ensureAutoColumns_(sheet);
  });
  formatListPilotSheets();
}

/**
 * Run once after pasting this script, or use the ListPilot menu in the sheet.
 * Formatting only: never clears cells, rewrites values/formulas, or touches Sheet1.
 */
function formatListPilotSheets() {
  const names = [SHEET_FB_LISTING, SHEET_DOMINANCY_CHECKER, SHEET_RESPONSE_CHECKER, SHEET_LIVE_CHECKER, SHEET_LIVE_HISTORY];
  const formatted = [];
  names.forEach(name => {
    const sheet = getSheet_(name);
    if (!sheet || sheet.getLastColumn() < 1) return;
    formatListPilotSheet_(sheet);
    formatted.push(sheet.getName());
  });
  console.log("ListPilot formatting applied: " + formatted.join(", "));
  return formatted;
}

function onOpen() {
  SpreadsheetApp.getUi().createMenu("ListPilot")
    .addItem("Format ListPilot tabs", "formatListPilotSheets")
    .addItem("Set up missing tabs and columns", "setupSheets")
    .addToUi();
}

function formatListPilotSheet_(sheet) {
  const headers = getHeaders_(sheet);
  const columns = headers.length;
  const rows = sheet.getMaxRows();
  const header = sheet.getRange(1, 1, 1, columns);
  const all = sheet.getRange(1, 1, rows, columns);
  all.setFontFamily("Arial").setFontSize(11).setFontColor("#172033").setVerticalAlignment("middle");
  header.setBackground("#274690").setFontColor("#FFFFFF").setFontWeight("bold")
    .setWrap(true).setHorizontalAlignment("left");
  sheet.setRowHeight(1, 44);
  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(Math.min(2, columns));
  sheet.setTabColor("#3D5AFE");
  sheet.setColumnWidths(1, columns, 150);
  if (!sheet.getFilter()) all.createFilter();
  if (rows < 2) return;

  const body = sheet.getRange(2, 1, rows - 1, columns);
  // Plain text only for typed-in identifiers; counts, dates and any formulas keep their own format.
  body.setWrap(true).setHorizontalAlignment("left");
  // Keep logs/details readable without letting a 40-line cell fill the screen.
  sheet.setRowHeightsForced(2, rows - 1, 42);

  const notes = {
    id: "Input: exact Incogniton/AdsPower profile ID. Stored as plain text.",
    profilename: "Input: exact seller profile name. Buyer checker accounts belong in ListPilot Settings, not the seller queue.",
    proxy: "Optional input. Leave blank to keep the browser profile's current proxy. Do not share proxy credentials.",
    status: "Blank or Pending queues this row. Success means the owner task completed; also review Check Result and Visibility.",
    checkresult: "Owner scan quality. OK means the scan was read; it does not mean every listing is live.",
    othercount: "Listings needing identity action, sold, expired, draft, removed, or unknown. Total = Live + Pending + Rejected + Other.",
    visibility: "Buyer sample result, not a global visibility verdict. NOT_IN_SEARCH means the link opened but the sampled search did not find it.",
    visibilitydetail: "Sample scope and per-listing results. Exact Facebook listing IDs are required for verified search matches. Expand this row to read the full details.",
    pkcheck: "PK buyer sample. Off means disabled. Search results depend on the account and its saved Marketplace location.",
    uscheck: "US buyer sample. Off means disabled. Search results depend on the account and its saved Marketplace location."
  };
  const numeric = new Set(["responses", "response", "totallisting", "livecount", "pendingcount", "rejectedcount", "othercount", "total", "live", "pending", "rejected"]);
  const statusColumns = new Set(["status", "checkresult", "visibility", "pkcheck", "uscheck"]);
  // Replace only this helper's own rules on repeat runs; preserve user rules.
  const rules = sheet.getConditionalFormatRules().filter(rule => {
    const condition = rule.getBooleanCondition();
    return !condition || !condition.getCriteriaValues().some(v => String(v).indexOf("ListPilotFormat:") !== -1);
  });
  const ownRules = [];
  function rule(range, marker, formula, bg, fg) {
    ownRules.push(SpreadsheetApp.newConditionalFormatRule()
      .whenFormulaSatisfied('=AND(N("ListPilotFormat:' + marker + '")=0,' + formula + ')')
      .setBackground(bg).setFontColor(fg).setRanges([range]).build());
  }
  headers.forEach((name, index) => {
    const normalized = headerKey_(name);
    const key = normalized === "statu" ? "status" : normalized;
    const column = index + 1;
    const range = sheet.getRange(2, column, rows - 1, 1);
    const letter = sheet.getRange(1, column).getA1Notation().replace(/\d+$/, "");
    const cell = "$" + letter + "2";
    if (notes[key] && !sheet.getRange(1, column).getNote()) sheet.getRange(1, column).setNote(notes[key]);
    if (numeric.has(key)) {
      range.setNumberFormat("0").setHorizontalAlignment("center");
      sheet.setColumnWidth(column, 100);
      rule(range, key + ":positive", "IFERROR(VALUE(" + cell + ")>0,FALSE)",
        key === "rejectedcount" || key === "rejected" ? "#FDEBEC" :
        key === "pendingcount" || key === "othercount" || key === "pending" ? "#FFF3D6" : "#E7F5ED",
        key === "rejectedcount" || key === "rejected" ? "#A32632" : "#175A36");
    } else if (key === "id") {
      sheet.setColumnWidth(column, 290);
      range.setNumberFormat("@");  // long profile/listing IDs must never become 1.2E+15
    } else if (key === "profilename" || key === "profile") {
      sheet.setColumnWidth(column, 210);
    } else if (key.includes("detail") || key === "description") {
      sheet.setColumnWidth(column, 520);
    } else if (key === "alllisting" || key === "needsattention" || key === "statussummary") {
      sheet.setColumnWidth(column, 380);
    } else if (key === "status" || key === "checkresult") {
      sheet.setColumnWidth(column, 300);
    } else if (key === "visibility" || key === "pkcheck" || key === "uscheck") {
      sheet.setColumnWidth(column, 240);
    } else if (key === "scantime") {
      sheet.setColumnWidth(column, 110);
      range.setHorizontalAlignment("center");
    } else if (key === "lastchecked" || key === "visibilitychecked" || key === "time") {
      sheet.setColumnWidth(column, 185);
      if (key === "time") range.setNumberFormat("yyyy-mm-dd hh:mm");
    }
    if (statusColumns.has(key)) {
      rule(range, key + ":error", 'REGEXMATCH(TO_TEXT(' + cell + '),"(?i)^failed|logged_out|checkpoint|layout_unknown|no_marketplace|timeout|hidden|error|unavailable")', "#FDEBEC", "#A32632");
      rule(range, key + ":warning", 'REGEXMATCH(TO_TEXT(' + cell + '),"(?i)partial|not_in_search|action_required|link only|unverified|waiting|claimed|pending")', "#FFF3D6", "#825500");
      rule(range, key + ":off", 'REGEXMATCH(TO_TEXT(' + cell + '),"(?i)^(off|skipped|not_checked|no_listings|stopped)")', "#EDF0F5", "#596579");
      rule(range, key + ":ok", 'REGEXMATCH(TO_TEXT(' + cell + '),"(?i)^(success|ok)|^[0-9]+/[0-9]+ in search$")', "#E7F5ED", "#175A36");
    }
  });
  rule(body, "band", "MOD(ROW(),2)=0", "#F3F6FC", "#172033");
  // User rules retain priority. Alternating row colors come after status rules.
  sheet.setConditionalFormatRules(rules.concat(ownRules));
}

function doGet(e) {
  const action = e.parameter.action;

  // Read-only health check: no row reads, claims, column creation or writes.
  if (action === "health") {
    try {
      const ss = SpreadsheetApp.getActiveSpreadsheet();
      if (!ss) return jsonOut_({success: false, service: "ListPilot"});
      ss.getSheets();  // Verify that the deployment can access spreadsheet metadata.
      return jsonOut_({success: true, service: "ListPilot", spreadsheetReady: true});
    } catch (err) {
      return jsonOut_({success: false, service: "ListPilot"});
    }
  }

  if (action === "getPendingRows") {
    const sheetName = e.parameter.sheetName || SHEET_FB_LISTING;
    const sheet = getSheet_(sheetName);
    if (!sheet) return jsonOut_({error: "Sheet not found: " + sheetName});
    ensureAutoColumns_(sheet);

    const dataRange = sheet.getDataRange();
    const data = dataRange.getValues();
    const richTextData = dataRange.getRichTextValues();
    if (data.length < 2) return jsonOut_({success: true, data: []});

    const headers = data[0].map(h => h.toString().trim());
    const pendingRows = [];

    const statusIdx = findBest_(headers, "status");
    if (statusIdx === -1) return jsonOut_({error: "Status column not found"});
    const idIdx = findBest_(headers, ["id", "profile id", "user id", "browser id"]);
    const nameIdx = findBest_(headers, ["profile name", "name", "profile"]);

    for (let i = 1; i < data.length; i++) {
      const row = data[i];
      if (!row.some(cell => String(cell).trim() !== "")) continue;
      if (isPendingStatus_(row[statusIdx])) {

        const rowObj = { rowNumber: i + 1 };
        for (let c = 0; c < headers.length; c++) {
            if (headers[c]) {
                let cellVal = row[c];
                if (typeof cellVal === "string") cellVal = cellVal.trim();
                const lowerHeader = headers[c].toLowerCase();
                if (lowerHeader.includes("pic") || lowerHeader.includes("image") || lowerHeader.includes("photo") || lowerHeader.includes("img")) {
                    const richText = richTextData[i][c];
                    if (richText) {
                        const runs = richText.getRuns();
                        const links = [];
                        for(let r=0; r<runs.length; r++) {
                            const l = runs[r].getLinkUrl();
                            if(l) links.push(l);
                        }
                        if(links.length > 0) {
                            cellVal = links.join(",");
                        }
                    }
                }
                rowObj[headers[c]] = cellVal;
            }
        }

        // Auto-mappings for convenience
        const idVal = idIdx !== -1 ? String(row[idIdx]).trim() : "";
        const nameVal = nameIdx !== -1 ? String(row[nameIdx]).trim() : "";
        rowObj.id = idVal || nameVal;
        rowObj.profileName = nameVal;

        pendingRows.push(rowObj);
      }
    }

    return jsonOut_({success: true, data: pendingRows});
  }

  return jsonOut_({error: "Invalid action"});
}

function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    const action = payload.action;

    if (action === "updateRowStatus" || action === "updateStatus") {
      const targetSheetName = payload.sheetName || SHEET_FB_LISTING;
      const sheet = getSheet_(targetSheetName);
      if(!sheet) return jsonOut_({success: false, error: "Sheet not found: " + targetSheetName});

      const statusIdx = findBest_(getHeaders_(sheet), "status");
      if (statusIdx === -1) return jsonOut_({success: false, error: "Status column not found"});

      sheet.getRange(payload.rowNumber, statusIdx + 1).setValue(payload.status);
      return jsonOut_({success: true});
    }

    if (action === "updateColumnValue") {
      const targetSheetName = payload.sheetName || SHEET_FB_LISTING;
      const columnName = String(payload.columnName || "");
      const sheet = getSheet_(targetSheetName);
      if(!sheet) return jsonOut_({success: false, error: "Sheet not found: " + targetSheetName});

      // App-owned columns (Response, Responses, Live) match exactly and are
      // created when missing; any other column is matched forgivingly.
      const autoColumns = AUTO_COLUMNS[canonicalSheetName_(sheet)] || [];
      const autoIdx = findExact_(autoColumns, columnName);
      let colIdx = autoIdx !== -1
          ? ensureColumn_(sheet, autoColumns[autoIdx]) - 1
          : findBest_(getHeaders_(sheet), columnName);

      if (colIdx !== -1) {
          setCellSafely_(sheet.getRange(payload.rowNumber, colIdx + 1), payload.value);
          return jsonOut_({success: true});
      }
      return jsonOut_({success: false, error: "Column not found: " + columnName});
    }

    // Writes several columns of one row in a single request (Live Checker v2).
    if (action === "updateColumnValues") {
      const targetSheetName = payload.sheetName || SHEET_FB_LISTING;
      const sheet = getSheet_(targetSheetName);
      if(!sheet) return jsonOut_({success: false, error: "Sheet not found: " + targetSheetName});

      const values = payload.values || {};
      const autoColumns = AUTO_COLUMNS[canonicalSheetName_(sheet)] || [];
      const missing = [];
      Object.keys(values).forEach(columnName => {
        const autoIdx = findExact_(autoColumns, columnName);
        const colIdx = autoIdx !== -1
            ? ensureColumn_(sheet, autoColumns[autoIdx]) - 1
            : findBest_(getHeaders_(sheet), columnName);
        if (colIdx === -1) { missing.push(columnName); return; }
        setCellSafely_(sheet.getRange(payload.rowNumber, colIdx + 1), values[columnName]);
      });
      if (missing.length) return jsonOut_({success: false, error: "Column not found: " + missing.join(", ")});
      return jsonOut_({success: true});
    }

    // Appends one scan to the "Live History" sheet and notes what changed since
    // that profile's previous scan (e.g. "Live 5->3, Rejected 0->2").
    if (action === "appendLiveHistory") {
      const lock = LockService.getScriptLock();
      lock.waitLock(15000);
      try {
        const ss = SpreadsheetApp.getActiveSpreadsheet();
        let history = ss.getSheetByName(SHEET_LIVE_HISTORY);  // exact name: getSheet_ matches loosely
        if (!history) {
          history = ss.insertSheet(SHEET_LIVE_HISTORY);
          history.appendRow(["Time", "Profile", "Total", "Live", "Pending", "Rejected", "Check Result", "Change"]);
          history.getRange("A1:H1").setFontWeight("bold");
          history.setFrozenRows(1);
          formatListPilotSheet_(history);
        }
        const toNum = v => (v === null || v === undefined || v === "" ? null : Number(v));
        const now = {live: toNum(payload.live), pending: toNum(payload.pending), rejected: toNum(payload.rejected)};
        const profile = String(payload.profile || "");

        let change = "First check";
        if (now.live === null) {
          change = "Check failed";
        } else {
          const lastRow = history.getLastRow();
          const from = Math.max(2, lastRow - 999);
          if (lastRow >= 2) {
            const rows = history.getRange(from, 1, lastRow - from + 1, 8).getValues();
            for (let i = rows.length - 1; i >= 0; i--) {
              if (String(rows[i][1]) !== profile || rows[i][3] === "" || rows[i][3] === null) continue;
              const before = {live: Number(rows[i][3]), pending: Number(rows[i][4]), rejected: Number(rows[i][5])};
              const parts = [["Live", "live"], ["Pending", "pending"], ["Rejected", "rejected"]]
                .filter(p => before[p[1]] !== now[p[1]])
                .map(p => p[0] + " " + before[p[1]] + "->" + now[p[1]]);
              change = parts.length ? parts.join(", ") : "No change";
              break;
            }
          }
        }

        history.appendRow([
          new Date(), profile,
          payload.total === null || payload.total === undefined ? "" : payload.total,
          now.live === null ? "" : now.live,
          now.pending === null ? "" : now.pending,
          now.rejected === null ? "" : now.rejected,
          String(payload.check || ""), change
        ]);

        // Keep the sheet bounded: drop the oldest 5000 rows once past 50000.
        if (history.getLastRow() > 50000) history.deleteRows(2, 5000);
        return jsonOut_({success: true, change: change});
      } finally {
        lock.releaseLock();
      }
    }

    // Clears Status on finished rows so getPendingRows queues them again.
    // mode: "pending" (Pending Count > 0), "failed" (Failed status or Check
    // Result not OK), "all", or "stuck" (CLAIMED for over an hour = crashed run).
    // Other modes never touch rows being processed (CLAIMED).
    if (action === "resetForRecheck") {
      const targetSheetName = payload.sheetName || SHEET_LIVE_CHECKER;
      const sheet = getSheet_(targetSheetName);
      if(!sheet) return jsonOut_({success: false, error: "Sheet not found: " + targetSheetName});
      const mode = String(payload.mode || "");
      if (["pending", "failed", "all", "stuck"].indexOf(mode) === -1) return jsonOut_({success: false, error: "Unknown mode: " + mode});
      ensureAutoColumns_(sheet);

      const lock = LockService.getScriptLock();
      lock.waitLock(15000);
      try {
        const lastRow = sheet.getLastRow();
        if (lastRow < 2) return jsonOut_({success: true, reset: 0});
        const headers = getHeaders_(sheet);
        const statusIdx = findBest_(headers, "status");
        if (statusIdx === -1) return jsonOut_({success: false, error: "Status column not found"});
        const pendingIdx = findExact_(headers, "Pending Count");
        const checkIdx = findExact_(headers, "Check Result");

        const data = sheet.getRange(2, 1, lastRow - 1, headers.length).getValues();
        const statuses = data.map(row => [row[statusIdx]]);
        let reset = 0;
        // "stuck": rows left as CLAIMED for over an hour (the run crashed). Rows
        // claimed by an older script version have no timestamp and are treated as stuck.
        const notes = mode === "stuck" ? sheet.getRange(2, statusIdx + 1, lastRow - 1, 1).getNotes() : null;
        const STUCK_MS = 60 * 60 * 1000;
        data.forEach((row, i) => {
          if (!row.some(cell => String(cell).trim() !== "")) return;
          const status = String(row[statusIdx] == null ? "" : row[statusIdx]).trim().toLowerCase();
          if (mode === "stuck") {
            if (status !== "claimed") return;
            const m = String(notes[i][0] || "").match(/Claimed (\S+)/);
            const claimedAt = m ? Date.parse(m[1]) : NaN;
            if (isNaN(claimedAt) || Date.now() - claimedAt > STUCK_MS) { statuses[i][0] = ""; reset++; }
            return;
          }
          if (status === "claimed" || isPendingStatus_(status)) return;  // running, or already queued
          const check = checkIdx === -1 ? "" : String(row[checkIdx] || "").trim();
          const pending = pendingIdx === -1 ? 0 : Number(row[pendingIdx]) || 0;
          const match = mode === "all"
              || (mode === "pending" && pending > 0)
              || (mode === "failed" && (status.indexOf("failed") === 0 || (check !== "" && check !== "OK")));
          if (match) { statuses[i][0] = ""; reset++; }
        });
        if (reset) sheet.getRange(2, statusIdx + 1, statuses.length, 1).setValues(statuses);
        return jsonOut_({success: true, reset: reset});
      } finally {
        lock.releaseLock();
      }
    }

    if (action === "claimRow") {
      const targetSheetName = payload.sheetName || SHEET_FB_LISTING;
      const sheet = getSheet_(targetSheetName);
      if(!sheet) return jsonOut_({success: false, error: "Sheet not found: " + targetSheetName});

      const statusIdx = findBest_(getHeaders_(sheet), "status");
      if (statusIdx === -1) return jsonOut_({success: false, error: "Status column not found"});

      // Lock so two workers can never claim the same row at the same time.
      const lock = LockService.getScriptLock();
      lock.waitLock(15000);
      try {
        const cell = sheet.getRange(payload.rowNumber, statusIdx + 1);
        const currentStatus = cell.getValue().toString().trim().toLowerCase();
        if (isPendingStatus_(currentStatus)) {
            cell.setValue("CLAIMED");
            cell.setNote("Claimed " + new Date().toISOString());  // lets "stuck" recheck spot crashed runs
            SpreadsheetApp.flush();
            return jsonOut_({success: true, claimed: true});
        }
        return jsonOut_({success: true, claimed: false, currentStatus: currentStatus});
      } finally {
        lock.releaseLock();
      }
    }

    return jsonOut_({success: false, error: "Invalid action"});
  } catch(err) {
    return jsonOut_({success: false, error: err.message});
  }
}
