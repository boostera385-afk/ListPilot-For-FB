import time
import threading
_tab_lock = threading.Lock()
import random
from playwright.sync_api import sync_playwright
from core.engine.state_machine import AutomationState
from core.engine.exceptions import RetryableError, PermanentError, AutomationStopped
from core.automation.profile_builder import get_val
from core.automation.ocr_service import (
    find_number_in_images,
    text_contains_target_number,
)

FB_HOME = "https://www.facebook.com/"
FB_MARKETPLACE = "https://www.facebook.com/marketplace/"

def run_dominancy_checker_flow(debug_url, post, logger=print, state_callback=None, stop_event=None):
    """
    1. Connects to the profile via debug_url
    2. Goes to FB Home and checks login
    3. Scrolls the feed for 15s (human-like)
    4. Opens Marketplace
    5. Types the search query into the search bar
    6. Waits for results (to be implemented further per user instructions)
    """

    def log(msg):
        logger(msg)

    def set_state(state, msg=""):
        if state_callback:
            state_callback(state, msg)

    def check_stop():
        if stop_event and stop_event.is_set():
            raise AutomationStopped("User manually stopped automation.")

    def interruptible_sleep(seconds):
        check_stop()
        if stop_event:
            if stop_event.wait(seconds):
                raise AutomationStopped("User manually stopped automation.")
        else:
            time.sleep(seconds)

    def wait_for_tab_load(p):
        try:
            p.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        interruptible_sleep(0.3)

    def check_for_checkpoint(p):
        try:
            texts_to_check = [
                "Confirm your identity",
                "Confirm you're a real person",
                "Start video selfie",
                "Account restricted",
                "We suspended your account",
                "Your account has been locked",
                "We need more info to make sure you're human"
            ]
            for t in texts_to_check:
                loc = p.get_by_text(t, exact=False).first
                if loc.is_visible():
                    return True
        except Exception:
            pass
        return False

    def is_logged_out(page):
        try:
            login_selectors = ['input[name="email"]', '#email', 'input[name="pass"]']
            for sel in login_selectors:
                if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                    return True
            return False
        except Exception:
            return False

    def is_logged_in(page):
        try:
            indicators = ['div[aria-label="Create"]', 'div[aria-label="Messenger"]', 'div[aria-label="Account"]', 'div[role="navigation"]']
            for sel in indicators:
                if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                    return True
            return False
        except Exception:
            return False

    with sync_playwright() as p:
        check_stop()
        log("Connecting to browser...")
        try:
            browser = p.chromium.connect_over_cdp(debug_url, timeout=30000)
        except Exception as e:
            max_retries = 3
            for attempt in range(max_retries):
                check_stop()
                try:
                    browser = p.chromium.connect_over_cdp(debug_url, timeout=30000)
                    break
                except Exception:
                    if attempt < max_retries - 1:
                        log(f"Browser not ready yet. Retrying in 3s ({attempt+1}/{max_retries})...")
                        interruptible_sleep(3)
                    else:
                        raise e

        context = browser.contexts[0] if browser.contexts else browser.new_context()

        # 1. Reuse existing Facebook tab or open a new one
        check_stop()
        set_state(AutomationState.PAGE_LOADING)
        
        page = None
        with _tab_lock:
            for p in context.pages:
                try:
                    # Use Javascript window.name to track state natively inside the browser tab
                    # so different CDP connections see the same state
                    if p.evaluate("window.name") != "in_use":
                        p.evaluate("window.name = 'in_use'")
                        page = p
                        log("Reusing an existing idle tab...")
                        break
                except Exception:
                    pass
                    
            if not page:
                log("Opening a new tab for this query...")
                page = context.new_page()
                try:
                    page.evaluate("window.name = 'in_use'")
                except Exception:
                    pass

        page.bring_to_front()

        global _scrolled_profiles
        try:
            _scrolled_profiles
        except NameError:
            _scrolled_profiles = set()

        already_on_marketplace = debug_url in _scrolled_profiles
        if not already_on_marketplace:
            _scrolled_profiles.add(debug_url)

        if already_on_marketplace:
            log("Profile already active. Skipping 15s feed scroll for faster sequential query.")
            try:
                page.goto(FB_MARKETPLACE, timeout=30000, wait_until="commit")
            except Exception:
                pass
            interruptible_sleep(0.3)
        else:
            log("Navigating to Facebook homepage and waiting for it to load...")
            try:
                page.goto(FB_HOME, timeout=60000, wait_until="commit")
            except Exception:
                pass
            wait_for_tab_load(page)

            # 2. Verify Login & Checkpoints
            check_stop()
            set_state(AutomationState.LOGIN_CHECK if hasattr(AutomationState, "LOGIN_CHECK") else AutomationState.PAGE_LOADING)
            if is_logged_out(page) and not is_logged_in(page):
                raise PermanentError("Facebook profile is not logged in. Please log in to this profile first.")
            
            if check_for_checkpoint(page):
                raise PermanentError("Confirm your identity")
                
            log("Facebook session is logged in.")
            set_state(AutomationState.READY)

            # 3. Scroll the feed slowly (human-like) for 15s
            check_stop()
            log("Scrolling the Facebook feed slowly (human-like) for 15 seconds...")
            scroll_start = time.time()
            while time.time() - scroll_start < 15:
                check_stop()
                try:
                    scroll_amt = random.randint(150, 400)
                    page.evaluate(f"window.scrollBy({{top: {scroll_amt}, left: 0, behavior: 'smooth'}})")
                except Exception:
                    pass
                interruptible_sleep(random.uniform(0.5, 1.2))

            # 4. Open Marketplace
            check_stop()
            log("Opening Marketplace...")
            clicked_marketplace = False
            for sel in ['a[aria-label="Marketplace"]', 'a[href="/marketplace/"]', 'div[role="navigation"] a[href*="marketplace"]']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible():
                        btn.click(timeout=5000)
                        log("Waiting for Marketplace tab to load...")
                        wait_for_tab_load(page)
                        clicked_marketplace = True
                        break
                except Exception:
                    continue
            
            if not clicked_marketplace:
                log("Could not click Marketplace icon. Navigating directly...")
                try:
                    page.goto(FB_MARKETPLACE, timeout=60000, wait_until="commit")
                except Exception:
                    pass
                wait_for_tab_load(page)

        if check_for_checkpoint(page):
            raise PermanentError("Confirm your identity")

        log("Marketplace is open.")

        # 5. Search for query
        query = get_val(post or {}, "query", "search", "keyword")
        if not query:
            raise PermanentError("No query provided in the Google Sheet for this row.")
            
        log(f"Searching Marketplace for query: '{query}'")
        try:
            search_input = page.locator('input[aria-label="Search Marketplace"]').first
            search_input.wait_for(state="visible", timeout=15000)
            search_input.click(timeout=5000)
            
            # Clear input gracefully
            try:
                search_input.fill("", timeout=3000)
            except Exception:
                pass
                
            # Type search query
            search_input.press_sequentially(str(query), delay=80)
            interruptible_sleep(0.1)
            search_input.press("Enter")
            log("Search submitted. Waiting 4 seconds for Facebook's background search to render...")
            interruptible_sleep(4)
            wait_for_tab_load(page)
            
        except Exception as e:
            try:
                page.evaluate("window.name = 'idle'")
            except:
                pass
            raise PermanentError(f"Failed to search on Marketplace: {e}")

        log("Search completed successfully.")
        
        # 6. Change location if provided
        location = get_val(post or {}, "location", "city")
        if location:
            log(f"Location provided in sheet: '{location}'. Clicking location filter...")
            try:
                # The location filter button has aria-label starting with "Location:"
                loc_btn = page.locator('div[role="button"][aria-label^="Location:"]').first
                loc_btn.wait_for(state="visible", timeout=10000)
                
                clicked_loc = False
                for _ in range(5):
                    try:
                        page.locator('div[role="button"][aria-label^="Location:"]').first.click(timeout=3000)
                        clicked_loc = True
                        break
                    except Exception:
                        interruptible_sleep(1)
                
                if not clicked_loc:
                    raise Exception("Timeout clicking Location button (Element kept detaching).")
                    
                log("Location modal opened. Setting location...")
                interruptible_sleep(0.3)
                
                # Type into Location input
                loc_input = page.locator('input[aria-label="Location"]').first
                
                # Increase timeout to 10s and retry if it fails
                try:
                    loc_input.wait_for(state="visible", timeout=10000)
                except Exception:
                    interruptible_sleep(1)
                    loc_input.wait_for(state="visible", timeout=10000)
                
                loc_input.click()
                # Clear existing by selecting all and deleting
                loc_input.press("Control+A")
                loc_input.press("Backspace")
                interruptible_sleep(0.1)
                
                # Type new location
                loc_input.press_sequentially(str(location), delay=80)
                log(f"Typed location '{location}'. Waiting for dropdown suggestions...")
                interruptible_sleep(0.1)
                
                # Wait and Click the first suggestion
                interruptible_sleep(2.5) # Wait for Facebook to fetch the list from server
                
                try:
                    options = page.locator('ul[role="listbox"] li[role="option"], div[role="listbox"] div[role="option"], div[role="option"]')
                    options.first.wait_for(state="visible", timeout=8000)
                    options.first.click(timeout=5000)
                    log("Clicked location suggestion from dropdown.")
                except Exception:
                    log("Warning: Could not find dropdown suggestion. Pressing Enter as fallback.")
                    loc_input.press("Enter")
                    
                interruptible_sleep(0.3)

                # Set Radius if provided
                radius = get_val(post or {}, "radius", "miles", "distance")
                if radius:
                    log(f"Setting radius to {radius}...")
                    # The radius dropdown has text "Radius" inside a combobox label
                    radius_combobox = page.locator('label[role="combobox"]').filter(has_text="Radius").first
                    if radius_combobox.is_visible():
                        radius_combobox.click(timeout=5000)
                        interruptible_sleep(0.1)
                        # Find the option that matches the radius (e.g., "20", "20 miles")
                        rad_option = page.get_by_role("option").filter(has_text=str(radius)).first
                        try:
                            rad_option.wait_for(state="visible", timeout=5000)
                            rad_option.click(timeout=5000)
                            log(f"Selected radius: {radius}")
                        except Exception:
                            try:
                                fallback = page.get_by_text(str(radius), exact=False).first
                                fallback.wait_for(state="visible", timeout=3000)
                                fallback.click(timeout=5000)
                            except Exception:
                                log(f"Warning: Could not select radius {radius}.")
                        log(f"Clicked radius option for {radius}.")
                        interruptible_sleep(0.1)

                # Click Apply button
                apply_btn = page.locator('div[role="button"][aria-label="Apply"]').first
                apply_btn.wait_for(state="visible", timeout=5000)
                apply_btn.click(timeout=5000)
                log("Clicked Apply. Waiting for results to refresh...")
                
                wait_for_tab_load(page)

            except Exception as e:
                raise PermanentError(f"Failed to set Location/Radius: {e}")
        else:
            log("No location provided in sheet. Skipping location change.")

        # 7. Dominancy Check (OCR on images)
        target_num = get_val(post or {}, "target number", "number")
        if not target_num:
            log("No 'Target Number' provided. Cannot run Dominancy Checker.")
            return True, f"Searched: {query}. (No target number provided)"

        check_stop()
        log("Scrolling to load enough listings...")
        for _ in range(4):
            page.mouse.wheel(0, 1000)
            interruptible_sleep(0.3)

        log("Extracting search results...")
        try:
            page.wait_for_selector('img[referrerpolicy="origin-when-cross-origin"]', timeout=10000)
        except Exception:
            raise PermanentError("No search results found.")

        # Find images that are likely marketplace listings
        img_locators = page.locator('img[referrerpolicy="origin-when-cross-origin"]').all()
        top_imgs = img_locators[:20]  # Get up to top 20 listings
        
        if not top_imgs:
            try:
                page.evaluate("window.name = 'idle'")
            except:
                pass
            raise PermanentError("Failed to extract listing thumbnails.")

        log(f"Found {len(top_imgs)} listings. Preparing a fast number scan for '{target_num}'...")

        # Extract image URLs first, skip ads, and use visible card text before
        # OCR. This avoids model work when Facebook already exposes the number.
        listing_items = []
        dom_match_positions = []
        # We need to iterate over all locators until we find 20 non-ad listings
        for img in img_locators:
            if len(listing_items) >= 20:
                break

            src = img.get_attribute("src")
            if not src:
                continue

            # Check if this listing is an Ad
            try:
                is_ad = img.evaluate('''img => {
                    let curr = img;
                    while (curr && curr !== document.body) {
                        curr = curr.parentElement;
                        if (!curr) break;
                        
                        if (curr.querySelectorAll('img[referrerpolicy="origin-when-cross-origin"]').length > 1) {
                            return false; 
                        }
                        
                        let walker = document.createTreeWalker(curr, NodeFilter.SHOW_TEXT, null, false);
                        let node;
                        while (node = walker.nextNode()) {
                            if (node.nodeValue.trim() === "Ad") {
                                return true;
                            }
                        }
                    }
                    return false;
                }''')
                
                if is_ad:
                    log("Skipping an Ad listing...")
                    continue
            except Exception:
                pass

            position = len(listing_items) + 1
            card_text = ""
            try:
                card_text = img.evaluate('''img => {
                    const link = img.closest('a[href*="/marketplace/item/"]') || img.closest('a');
                    return (link && link.innerText) || img.alt || "";
                }''') or ""
            except Exception:
                pass

            listing_items.append((position, src))
            if text_contains_target_number(target_num, card_text):
                dom_match_positions.append(position)

        dom_matches = set(dom_match_positions)
        images_to_scan = [item for item in listing_items if item[0] not in dom_matches]
        if dom_matches:
            log(f"Found {len(dom_matches)} match(es) directly in listing text; OCR will scan the remaining images.")

        ocr_match_positions = find_number_in_images(
            images_to_scan,
            target_num,
            logger=log,
            stop_event=stop_event,
        )
        match_positions = sorted(dom_matches.union(ocr_match_positions))
        for position in match_positions:
            log(f"Match found at Position {position}")

        match_count = len(match_positions)
        positions_str = ','.join(str(p) for p in match_positions)

        result_str = f"Found {match_count}/{len(listing_items)} (Pos: {positions_str if positions_str else 'None'})"
        log(f"Dominancy Check Complete: {result_str}")
        try:
            page.evaluate("window.name = 'idle'")
        except:
            pass
        return True, result_str
