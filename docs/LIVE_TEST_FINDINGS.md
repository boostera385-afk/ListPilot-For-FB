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
