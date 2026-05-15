import os
import asyncio
import sys
import io
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from supabase import create_client, Client
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─────────────────────────────────────────────
# HELPER: extract star rating from aria-label
# ─────────────────────────────────────────────
def parse_rating(aria_label: str) -> float:
    """Parse '4 stars' or '3.5 stars' from an aria-label string."""
    try:
        parts = aria_label.strip().split()
        return float(parts[0])
    except Exception:
        return 0.0


# ─────────────────────────────────────────────
# HELPER: scroll the inner review panel
# ─────────────────────────────────────────────
async def scroll_review_panel(page, scrolls: int = 15, pause_ms: int = 1800):
    """
    Finds the inner scrollable review container and scrolls it
    `scrolls` times, waiting for the loading spinner to disappear
    after each scroll.
    """
    # The review panel on Google Maps is a div that is scrollable —
    # it is the closest scrollable ancestor of the review cards.
    # We target it via JS to ensure we scroll the right element.
    panel_js = """
        () => {
            // Walk up from a review card to find the scrollable panel
            const card = document.querySelector('div[data-review-id]');
            if (!card) return null;
            let el = card.parentElement;
            while (el) {
                const style = window.getComputedStyle(el);
                if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
                    return el;
                }
                el = el.parentElement;
            }
            return document.querySelector('div[role="main"]');
        }
    """

    for i in range(scrolls):
        # Scroll the panel by a big chunk
        await page.evaluate("""
            () => {
                const card = document.querySelector('div[data-review-id]');
                if (!card) return;
                let el = card.parentElement;
                while (el) {
                    const style = window.getComputedStyle(el);
                    if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
                        el.scrollTop += 3000;
                        return;
                    }
                    el = el.parentElement;
                }
                // fallback: scroll last review into view
                const all = document.querySelectorAll('div[data-review-id]');
                if (all.length > 0) all[all.length - 1].scrollIntoView();
            }
        """)

        # Wait for loading spinner to disappear (class varies; common ones shown below)
        try:
            await page.wait_for_selector(
                'div[jsaction*="loading"], .DkEaL, .mMHnD',
                state="hidden",
                timeout=3000
            )
        except PlaywrightTimeoutError:
            pass  # spinner may not have appeared — that's fine

        await page.wait_for_timeout(pause_ms)
        print(f"      ↕  Scroll {i + 1}/{scrolls} done", flush=True)


# ─────────────────────────────────────────────
# HELPER: click all 'More' buttons in panel
# ─────────────────────────────────────────────
async def expand_all_more_buttons(page):
    """
    Finds every visible 'More' / 'See more' expand button in the
    review panel and clicks each one, waiting briefly between clicks.
    Returns the number of buttons clicked.
    """
    # Google Maps uses button.w8nwRe for the 'More' expand button
    clicked = 0
    more_buttons = page.locator('button.w8nwRe, button[jsaction*="pane.review.expandReview"]')
    count = await more_buttons.count()
    for idx in range(count):
        btn = more_buttons.nth(idx)
        try:
            if await btn.is_visible():
                await btn.scroll_into_view_if_needed()
                await btn.click()
                await page.wait_for_timeout(300)
                clicked += 1
        except Exception:
            pass
    print(f"      🖱  Expanded {clicked} 'More' buttons", flush=True)
    return clicked


# ─────────────────────────────────────────────
# HELPER: extract text from a single review block
# ─────────────────────────────────────────────
async def extract_review_text(block, page) -> str:
    """
    Tries multiple selectors in priority order.
    Falls back to JS innerText on the whole block if all fail.
    Retries once after 2 s if text is still empty.
    """
    # Priority selectors for review body text (Google Maps 2024-2025)
    selectors = [
        'span.wiI7pd',      # primary review text (span inside the block)
        '.MyEned span',     # alternate wrapper
        '.wiI7pd',          # class without tag constraint
        '.My579c',          # older class name
        'span[jsname]',     # catch-all span with any jsname
    ]

    async def _try_selectors():
        for sel in selectors:
            try:
                loc = block.locator(sel).first
                if await loc.count() > 0:
                    txt = await loc.inner_text()
                    if txt.strip():
                        return txt.strip()
            except Exception:
                pass
        # Last resort: pull all text from the block via JS
        try:
            txt = await block.evaluate("""
                el => {
                    // Try to get just the review text span, not name/stars
                    const spans = el.querySelectorAll('span[jsname], .wiI7pd, .My579c, .MyEned span');
                    for (const s of spans) {
                        const t = s.innerText.trim();
                        if (t.length > 5) return t;
                    }
                    return '';
                }
            """)
            return txt.strip() if txt else ""
        except Exception:
            return ""

    text = await _try_selectors()
    if not text:
        # Retry once after waiting 2 s
        await page.wait_for_timeout(2000)
        text = await _try_selectors()

    return text


# ─────────────────────────────────────────────
# HELPER: extract reviewer name
# ─────────────────────────────────────────────
async def extract_reviewer_name(block) -> str:
    name_selectors = ['.d4r55', 'div[class*="d4r55"]', '.WNxzHc', 'button.al6Kxe div']
    for sel in name_selectors:
        try:
            loc = block.locator(sel).first
            if await loc.count() > 0:
                name = await loc.inner_text()
                if name.strip():
                    return name.strip()
        except Exception:
            pass
    return "Unknown User"


# ─────────────────────────────────────────────
# HELPER: extract star rating
# ─────────────────────────────────────────────
async def extract_rating(block) -> float:
    """
    Reads the aria-label from the star rating widget inside the block.
    Example aria-label: '4 stars' or '3.5 stars'
    """
    rating_selectors = [
        'span[role="img"][aria-label*="star"]',
        'span[aria-label*="star"]',
        'span[role="img"]',
    ]
    for sel in rating_selectors:
        try:
            loc = block.locator(sel).first
            if await loc.count() > 0:
                label = await loc.get_attribute("aria-label") or ""
                if "star" in label.lower():
                    return parse_rating(label)
        except Exception:
            pass
    return 0.0


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def scrape_google_maps(search_query: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=300)
        context = await browser.new_context(
            viewport={'width': 1366, 'height': 900},
            locale='en-US',
        )

        # Bypass cookie consent banner
        await context.add_cookies([{
            "name": "CONSENT",
            "value": "YES+cb.20230101-14-p0.en+FX+414",
            "domain": ".google.com",
            "path": "/"
        }])

        page = await context.new_page()

        # ── Navigate & Search ──────────────────────────────────────────
        print("🔍 Navigating to Google Maps...", flush=True)
        try:
            await page.goto("https://www.google.com/maps?hl=en",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            print(f"⚠️  Page load warning: {e}", flush=True)

        print(f"📡 Searching for: {search_query}", flush=True)
        search_box = page.locator('input#searchboxinput, input[name="q"]').first
        try:
            await search_box.wait_for(state="visible", timeout=15000)
        except Exception as e:
            await page.screenshot(path="error_screenshot.png")
            print("📸 Saved error_screenshot.png — could not find search box.", flush=True)
            raise e

        await search_box.fill(search_query)
        await page.keyboard.press("Enter")

        # Wait for results list
        try:
            await page.wait_for_selector('a.hfpxzc, a[href*="/maps/place/"]', timeout=30000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            print(f"❌ Timeout: Could not find restaurant list. {e}", flush=True)
            await page.screenshot(path="list_error.png")
            await browser.close()
            return

        # ── Collect top-10 restaurant URLs first ──────────────────────
        # Snapshot the hrefs so we don't fight stale element references
        result_links = await page.locator('a.hfpxzc, a[href*="/maps/place/"]').all()
        restaurant_urls = []
        for link in result_links[:10]:
            try:
                href = await link.get_attribute("href")
                if href and "/maps/place/" in href:
                    restaurant_urls.append(href)
            except Exception:
                pass

        print(f"📋 Collected {len(restaurant_urls)} restaurant URLs to scrape.\n", flush=True)

        # ── Global counters ────────────────────────────────────────────
        valid_reviews_count = 0
        empty_reviews_count = 0
        db_success_count = 0
        db_error_count = 0

        # ── Per-restaurant loop ────────────────────────────────────────
        for idx, url in enumerate(restaurant_urls):
            print(f"\n{'='*60}", flush=True)
            print(f"🏪 [{idx+1}/{len(restaurant_urls)}] Navigating to restaurant page...", flush=True)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
            except Exception as e:
                print(f"⚠️  Could not load page: {e}", flush=True)
                continue

            # Get restaurant name
            try:
                name_el = await page.wait_for_selector('h1.DUwDvf, h1[class*="DUwDvf"]', timeout=8000)
                restaurant_name = (await name_el.inner_text()).strip()
            except Exception:
                restaurant_name = f"Unknown_Restaurant_{idx}"
            print(f"📍 Scraping: {restaurant_name}", flush=True)

            # ── Click the Reviews tab ──────────────────────────────────
            tab_clicked = False
            try:
                # Try Playwright locator first
                reviews_tab = page.locator(
                    'button[role="tab"][aria-label*="Reviews"], '
                    'button[role="tab"]:has-text("Reviews")'
                ).first
                if await reviews_tab.count() > 0 and await reviews_tab.is_visible():
                    await reviews_tab.click()
                    tab_clicked = True
            except Exception:
                pass

            if not tab_clicked:
                # JS fallback
                await page.evaluate("""
                    () => {
                        const tabs = Array.from(document.querySelectorAll('button[role="tab"]'));
                        const t = tabs.find(b =>
                            b.textContent.includes('Reviews') ||
                            (b.getAttribute('aria-label') || '').includes('Reviews')
                        );
                        if (t) t.click();
                    }
                """)

            # Wait for at least one review card to appear
            try:
                await page.wait_for_selector(
                    'div[data-review-id], div.jJc83c',
                    state="attached", timeout=12000
                )
                await page.wait_for_timeout(1500)
            except PlaywrightTimeoutError:
                print(f"   ⚠️  No reviews loaded for {restaurant_name} — skipping.", flush=True)
                continue

            # ── Deep Scroll ────────────────────────────────────────────
            print(f"   🔄 Scrolling reviews panel...", flush=True)
            await scroll_review_panel(page, scrolls=15, pause_ms=1800)

            # ── Expand all 'More' buttons ──────────────────────────────
            print(f"   🖱  Expanding 'More' buttons...", flush=True)
            await expand_all_more_buttons(page)
            await page.wait_for_timeout(800)

            # ── Extract Reviews ────────────────────────────────────────
            review_blocks = await page.locator('div[data-review-id], div.jJc83c').all()
            print(f"   💬 Processing {len(review_blocks)} review cards...", flush=True)

            for block in review_blocks:
                review_text = await extract_review_text(block, page)

                if review_text:
                    valid_reviews_count += 1
                    reviewer_name = await extract_reviewer_name(block)
                    rating = await extract_rating(block)

                    data = {
                        "restaurant_name": restaurant_name,
                        "reviewer_name": reviewer_name,
                        "rating": int(rating) if rating > 0 else None,  # smallint column
                        "review_text": review_text,
                        "location_tag": search_query,
                    }

                    try:
                        supabase.table("restaurant_reviews").upsert(
                            data,
                            on_conflict="restaurant_name,reviewer_name,review_text"
                        ).execute()
                        db_success_count += 1
                    except Exception as db_err:
                        db_error_count += 1
                        print(f"      ❌ DB error: {db_err}", flush=True)
                else:
                    empty_reviews_count += 1

            print(
                f"   ✅ {restaurant_name}: "
                f"{valid_reviews_count} valid so far | "
                f"{empty_reviews_count} empty so far",
                flush=True
            )

        # ── Final Verification Summary ─────────────────────────────────
        print(f"\n{'='*60}", flush=True)
        print("✅  SCRAPE COMPLETE — VERIFICATION SUMMARY", flush=True)
        print(f"{'='*60}", flush=True)
        print(f"  📊 Reviews with actual text : {valid_reviews_count}", flush=True)
        print(f"  📊 Empty / No-text reviews  : {empty_reviews_count}", flush=True)
        print(f"  🗄️  DB upserts succeeded     : {db_success_count}", flush=True)
        print(f"  🗄️  DB upserts failed        : {db_error_count}", flush=True)
        print(f"{'='*60}\n", flush=True)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(scrape_google_maps("Best restaurants in Jhansi"))
