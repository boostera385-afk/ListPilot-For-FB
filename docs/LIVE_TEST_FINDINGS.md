# Live testing findings, fixes and what is left

Written after the first real-account runs. "Fixed" means a code change plus a passing test. Nothing here claims more than was actually run.

## 1. Time counts

| What | Time | Source |
|---|---|---|
| Owner scan of the test run | about 2 min (about 14:34 to 14:36) | `logs/listpilot.log` |
| Buyer pass, before the speed-up: 15 listings x 2 buyer accounts = 30 checks | about 9 min (14:36 to 14:45), about 18 s per check | `logs/listpilot.log` |
| Buyer pass, after the speed-up (Quick sample = 3 listings x 2 buyers = 6 checks, fixed waits cut to 0.2 s, page waits capped at 5 s, repeated searches reused) | **not measured on a live account yet**; expect roughly 1-2 min | estimate |

Time is now counted by the app itself, so the next run gives real numbers:
- **Live Checker page:** `Phase - mm:ss - current listing` ticks every second.
- **Log:** `Full scan: ... in 1m 42s`, then `Owner scan time`, `Buyer sample finished in Ns` and `Total run time (owner scan, buyer sample)`.
- **Sheet:** the `Scan Time` column per profile, and `Buyer check took ...` in `Visibility Details`.

## 2. Problems found in real use, and the fix

| # | Problem found | Status |
|---|---|---|
| 1 | Windows showed the Python icon | Fixed: ListPilot airplane icon, taskbar identity and shortcut |
| 2 | Three tools had different, older dashboards | Fixed: FB Listings, Dominancy and Response now share the Live Checker dashboard |
| 3 | No way to tell if the browser or Sheets were connected | Fixed: verified connection indicators, background checks, manual refresh |
| 4 | Older Facebook cards misread: Active/date text joined, same title with a different price merged, pages with no sections, lazy-loaded cards missed, renewal tips counted as expired | Fixed (`tests/test_old_listing_cards.py`) |
| 5 | Identity notices counted as ordinary rejections; Other not visible; stale counts stayed after a failed read | Fixed: `ACTION_REQUIRED`, visible Other count, amber warnings, counts cleared on failure |
| 6 | Buyer pass slow (about 9 min for 30 checks) | Fixed: Quick / Standard / Extended sample (3 / 5 / 15), bounded waits, cached searches |
| 7 | **Misleading result:** 10 sampled listings without an ID were reported "in search" because a common title matched | Fixed: a search match now needs the exact Facebook listing ID. No ID means `UNCLEAR` / unverified |
| 8 | No way to turn buyer checks off | Fixed: master switch plus separate PK and US switches; saved profile IDs are kept |
| 9 | Buyer step turned green even for `NOT_IN_SEARCH`; summaries cut at the first comma; checker names showed as long IDs | Fixed: amber when anything needs a look, clearer text |
| 10 | A buyer account could also be in the seller queue (the "testing" row) | Fixed: blocked before any profile starts |
| 11 | Sheet looked plain and hard to scan | Fixed: see section 4 |
| 12 | Earlier test runs: a tab that never opened was read under the wrong label; the table scroll skipped rows | Fixed (see git history) |

## 3. Reading buyer results correctly
- A buyer result is a **sample**, not a full audit. The `Visibility Details` first line says how many of the live listings were checked.
- `LINK_ONLY` for the Pakistani account on a US-city listing is expected: search is location based. `HIDDEN` / "unavailable" is the real warning.
- The PK buyer's saved Marketplace location is Houston, Texas and the US buyer's is San Diego, California. A listing in another city may not appear in their search.

## 4. Sheet formatting (Apps Script)
Run `formatListPilotSheets` once (or use the **ListPilot** menu in the sheet). It never clears, moves or rewrites values or formulas, and never touches other tabs such as Sheet1.

- Dark-blue header row, frozen header and first two columns, filter, tab colour, wrapped text, alternating row shading.
- Column widths by purpose (ID wide, counts narrow, details very wide).
- Colour rules on Status, Check Result, Visibility, PK Check and US Check: **green** = success / OK, **amber** = partial, pending, claimed, unverified, link only, **red** = failed, logged out, checkpoint, hidden, error, **grey** = off, skipped, not checked.
- Count columns coloured when above 0 (Rejected red, Pending / Other amber, Live green).
- Hover notes on key headers explaining what each column means.
- Only the ID column is forced to plain text, so long IDs never turn into `1.2E+15`; counts and dates keep their own format.
- New columns the app adds copy the header style automatically.
- Re-running replaces only the helper's own colour rules and keeps yours.

## 5. Still open
1. **Redeploy the Apps Script** (new version), run `setupSheets` once, then `formatListPilotSheets`. Formatting could not be tested inside a real Sheet from here, only syntax-checked.
2. **Account `19 june id 6` has a Facebook checkpoint.** It needs the account owner to confirm identity by hand. ListPilot does not bypass it or retry it.
3. **US buyer location** is set in Marketplace, but its IP/country was not independently verified.
4. **The ten planned department rows are not ten verified published listings.** Buyer checks need real listing IDs from the owner scan.
5. **Buyer timing** after the speed-up is not measured live yet (section 1).
6. Row `testing` is an old failed seller row and is now the PK buyer. Do not use "Re-check every profile" on it.

## 6. Second real run (profiles `1 Oct Id 1 ali` and `1 Oct Id 2 ali`, buyers on, Quick sample)

Measured: owner scan 1m 45s, buyer sample 1m 32s (6 checks per buyer), total run 3m 18s. The buyer pass went from about 9 min (30 checks) to 1m 32s.

| # | Problem found | Fix |
|---|---|---|
| 1 | **The same profile was queued twice** (rows 7 and 8 were both named `1 Oct Id 1 ali`). It was scanned twice at the same time, so totals doubled (42 LIVE = 21 x 2) and two tabs worked on one Facebook account together. | Rows naming the same profile (same ID, same name ignoring case and spaces, or the same resolved profile) are now checked once. The later row is marked `Skipped: same profile as row N` and the log says so. |
| 2 | Two identical listings (same title and price, no ID) were both reported "in search" although one search card was seen. | Identical no-ID listings now each need their own result card: one card proves one listing, not two. |
| 3 | `NOT_FOUND` gave no clue why. | The log now shows what the search returned (`not in search (saw: $34 Furniture ... | ...)`), so a title or price mismatch is visible at once. |
| 4 | The results table showed only about one row on a normal 1080p screen. | Stat cards are one line, the explanatory lines hide on a short window, and the table height follows the window. The results area grew by about 24 percent on the test window. |

Still true after this run: a listing in a city far from the buyer's saved Marketplace location can show "Not in search" while being live. Set the buyer's Marketplace location near the listing to test search properly.

## 7. Third real run (3 profiles at once, buyers on, Quick sample)

Measured: 3 profiles read in 2m 32s (about 1 min each, in parallel), buyer pass 3m 02s for 9 listings x 2 buyers, total 5m 33s. No `Skipped` rows and no doubled totals, so the duplicate-row fix held. The `not in search (saw: ...)` lines worked: they showed the PK buyer was looking at Houston listings and the US buyer at San Diego, El Cajon and La Mesa listings, with other sellers' prices.

| # | Finding | Change |
|---|---|---|
| 1 | The Quick sample was the first three live listings in tab order, so it was mostly the same service (for example Furniture, Furniture, Carpet). | The sample now takes listing IDs first, then different title + price combinations before any repeat, so three listings cover three services where the account has them. |
| 2 | PK and US ran one after the other (about 3 min). They are different browsers. | They now run side by side, so the buyer pass is about half as long. One buyer failing to start keeps the other's results. |
| 3 | A repeated no-ID listing could trigger a second search page load. | When several listings share a search, it is scrolled once to the end and every repeat is answered from it. |

Reading the numbers: because the three `1 Oct` accounts list almost every service at the same price ($34), a title + price match only says that *a* listing with that title and price shows in the buyer's search. It does not prove it is this seller's. That is why those rows stay `PARTIAL` or `NOT_IN_SEARCH` until an ID confirms them.

## 8. Fourth real run (same 3 profiles, buyers on) - counts that changed between runs

Measured: owner scan 2m 03s, buyer pass 2m 03s (was 3m 02s), total 4m 06s (was 5m 33s). The log shows PK and US working at the same moment, and the sample covered different services (Furniture, Carpet, Spa, Christmas lights). Both changes worked.

**Problem found in the Live History tab:** the same profiles gave different totals within a few hours, every time with `Check Result = OK`:

| Profile | Totals read (LIVE) |
|---|---|
| `1 Oct Id 1 ali` | 21, 28, 21 |
| `6 aug id 2` | 25, 35, 15, 25 |
| `1 Oct Id 2 ali` | 14, 14, 14 (stable) |

The smaller reads were the top of the larger ones: the "Renew your listing?" cards at the bottom were missing. The older layout loads listings as you scroll, and the scan stopped after about 3 idle seconds. With three browsers sharing one connection a batch can take longer than that, so an account looked smaller than it is.

| # | Fix |
|---|---|
| 1 | The older-layout scan now waits up to 8 s for the first card, then keeps scrolling and waits 4 s after each scroll for more cards (longer while a loading spinner shows). It only stops after two quiet windows plus a last jump to the very bottom. |
| 2 | A fake lazy-loading page with 4.5 s batches reproduced both failures (0 cards read, then early stop). It now reads every card. It takes about 12 s longer per profile to be sure the feed really ended. |
| 3 | A buyer search page that was slow to load gets one more try before the listing is called "not verified". |

If the totals still move between runs after this, the account's listings really changed (new posts, expiries, deletions). Compare the `Listing Details` column of two runs.

## 9. Fifth real run (`19 june id 7` alone, buyers on)

Measured: owner scan 2m 09s (the full scan itself took 1m 14s), buyer pass 1m 05s, total 3m 14s.

**Good news:** `19 june id 7` now reads **54 live + 5 rejected**. Earlier runs read only about 25 live. The 54 entries in `Listing Details` are distinct listings, so the lazy-loading fix is showing the real size of the account.

| # | Finding | Change |
|---|---|---|
| 1 | The PK buyer was stopped with "account blocked mid-run" and no reason, no screenshot. The second and third listings were left as Error. | A suspected block is checked again after 3 s (a passing pop-up is not a block). A confirmed block logs the reason (`LOGGED_OUT` or `CHECKPOINT`) and saves a screenshot to `debug/`. |
| 2 | The Buyer checks tab showed "Not in search" for a listing whose PK check had failed, hiding that one buyer never looked. | Verdicts now say so: `Not in search (a buyer could not check)`, `Visible in search (a buyer could not check)`. The green colour is kept only for a fully checked result. |
| 3 | About 55 s of the 2m 09s was not the scan: for this older-layout account the app waited about 15 s for a table it does not have. | Each profile's layout is remembered in `layout_memory.json` (local, not committed). The next run goes straight to the right page and falls back to the other layout if it does not load. |
| 4 | The patient end-of-feed wait added about 12 s per profile. | After one quiet window the scan jumps to the bottom, where a loader triggers, and stops if that is quiet too (about 7 s). |

Seeing a PK error again: open the PK profile by hand and look at the screenshot in `debug/`. If the account really is logged out or at a checkpoint, fix that first.

## 10. November 20: false `LAYOUT_UNKNOWN` with visible listings

The linked Live Checker sheet showed two failed owner scans, and the app logged
`Listings found: No listings`. The saved screenshot visibly contained seller cards.
Direct inspection of the real older layout reproduced the cause: **Clear filters**
remains enabled in the default sidebar. After clicking it, the checker incorrectly
required the button to disappear.

- Reset filters once, then check remaining search/filter indicators; persistent Clear
  alone no longer blocks the scan. Hidden/unrelated Clear buttons are ignored.
- Failed owner scans now report an **unknown/incomplete** count, not a verified zero.
- The sidebar's `20+ active listings` is checked as a lower bound rather than ignored.
- Older cards often have no item links until opened. Only the selected buyer-sample
  cards are opened to recover title-matching, unambiguous item links from their own
  dialogs. Missing/ambiguous links stay unverified; a stale dialog is not reused.
- Buyer searches try the title and, on a miss, a normalized keyword fallback. Only
  an exact ID in search counts as verified. Keywords do not change the saved city/radius;
  adding ZIP text to a query is not a geographic filter.

Read-only live verification after the fix (sheet/history writes intercepted):

| Seller | Live | Rejected | Total |
|---|---:|---:|---:|
| `19 june id 6` | 64 | 2 | 66 |
| `19 june id 7` | 54 | 5 | 59 |

These are owner-side publication states, not proof of buyer search visibility.
The old checkpoint finding for `19 june id 6` is historical; this verification
successfully read its seller page.

All **six** selected seller-card IDs were recovered (three per seller). Both
configured buyers then checked the same six IDs. All **12 buyer outcomes were
`LINK_ONLY`**: the exact item pages opened with the expected title, but the IDs
were not observed in the bounded title/keyword searches. Neither seller was
shown to be banned or globally hidden. The buyer pass took **153 seconds**,
including buyer startup; the actual saved city/radius still needs checking near
the listing location before drawing a local-search conclusion. No sheet cells
or history rows were changed during this verification.

## 11. November 20 follow-up: readable results and incomplete US checks

The next user run preserved the owner totals (118 live, 7 rejected), but one
seller dialog stayed open after its first ID was read. The safe stale-dialog
guard then skipped two IDs. PK could only report possible title/price matches
for those two listings. All US search results were unreadable, yet the overall
cell said `NOT_IN_SEARCH`, obscuring the incomplete US checks.

Changes:
- Close supports native buttons as well as role buttons, retries Escape, and
  waits for the dialog to actually disappear before the next card.
- Search reads the landmark containing result cards, not a filters-only first
  main landmark. It allows a city redirect to settle and waits longer for slow
  results. A persistent unread search saves diagnostics once per buyer.
- Unfinished buyer checks now take precedence over search misses. All-unread
  results say **Could not check any of 3 listings**, never a misleading `0/3` score.
- Plain **Buyer Summary**, **Next Step**, and **Buyer Search Area** columns are
  created next to Profile Name by the updated setup script. The app also shows
  Next step; details/logs use readable words. Old deployments still save the
  core results and retain the same guidance in Visibility Details.

Live read-only re-test: seller `19 june id 6` again read 64 live, 2 rejected,
and recovered all three selected IDs, without stale-dialog skips. The US buyer
completed all three checks: each exact item link opened, none of the IDs was
observed in the sampled search. Its displayed search area was **McKinney,
Texas — within 20 mi**. This does not establish the cause of every earlier
unread search or rule out future network/markup problems. Sheet/history writes
were not sent; the new live layout still requires setup and redeployment.

Validation: 236 Python tests and the Apps Script schema tests passed.

## 12. November 20 follow-up: interrupted card opening and shutdown confirmation

The user's next run still counted 118 live and 7 rejected, but seller 6 lost all
three sample IDs: a title locator timed out, then a card click timed out after
opening its dialog. The `opened` flag was only set after a successful click
reply, so the interrupted click skipped cleanup. Subsequent targets then hit
the stale-dialog guard. Two buyer shutdowns were also reported as unconfirmed.

Fixes:
- Card title validation and opening now happen in one browser round trip,
  rather than separate count/text/click calls across Facebook rerenders.
- Lost card markers can be refreshed only against a unique complete seller
  snapshot. Identical ambiguous snapshots are not rebound by title/price.
- An interrupted opening reply may still have opened the dialog: read its exact
  link and always attempt verified cleanup before the next target. Recovery is
  time-bounded and logs how many selected links are ready.
- Missing seller IDs now produce **Seller link missing — retry the seller scan**.
  Buyer searches and browser launches are skipped for ID-less samples, even
  when a common title and price are available. Legacy possible-match helpers
  remain, but current buyer runs do not use lookalikes to identify a seller.
- Incogniton shutdown requests are serialized and followed by a bounded
  provider-status/exact-profile-process check. A failed stop reply can still
  be confirmed closed; an OK reply with a browser still running is not success.
  A real cleanup problem is preserved in Next Step without losing good listing
  results, including if the run was stopped.

Full live read-only re-test, with both sellers concurrent and PK/US concurrent:

| Check | Observed result |
|---|---|
| Seller 6 | 64 live, 2 rejected; 3/3 sample IDs recovered |
| Seller 7 | 54 live, 5 rejected; 3/3 sample IDs recovered |
| Buyer outcomes | All 12 checks opened the exact item link; none found the ID in the bounded local search |
| PK search area | Los Angeles, California — within 60 mi |
| US search area | McKinney, Texas — within 20 mi |
| Buyer pass duration | 140 seconds, including browser startup and verified shutdown |
| Browser cleanup | All four profiles Ready with zero matching CDP browser processes; no cleanup warnings |

This is not evidence of global hiding or an account ban. No sheet/history
writes were sent during verification. The existing deployed explanation columns
can display these changes on the next app run; this follow-up needs no new
sheet columns. Validation: **249 Python tests and Apps Script schema tests passed**.
