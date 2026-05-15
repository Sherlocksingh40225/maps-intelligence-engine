"""
scraper.py — Google Maps Multi-City Review Scraper
Principal Data Architect Edition — v3.0
────────────────────────────────────────────────────
Features:
  • Multi-city loop  (Jhansi / Bangalore / Lucknow)
  • Up to 30 restaurants per city
  • Deep extraction: cuisine, price, service options,
    open/closed, phone, Local Guide badge, review count,
    owner response, relative date, star rating
  • headless=True + realistic User-Agent
  • Random 2–4 s delay between restaurants (bot-evasion)
  • Fault-tolerant: failed restaurants are logged & skipped
  • Supabase upsert on (restaurant_name, reviewer_name, review_text)
"""

import os
import re
import asyncio
import random
import sys
import io
import logging
from datetime import datetime, timezone

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from supabase import create_client, Client
from dotenv import load_dotenv

# ── I/O & Logging ────────────────────────────────────────────────────────────
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("scraper")

# ── Config ───────────────────────────────────────────────────────────────────
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TARGET_CITIES      = ["Jhansi", "Bangalore", "Lucknow"]
RESTAURANTS_LIMIT  = 30
SCROLL_COUNT       = 15
SCROLL_PAUSE_MS    = 1800

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_rating(aria_label: str) -> int:
    """'4 stars' → 4  |  '3.5 stars' → 3  (smallint column)"""
    try:
        return int(float(aria_label.strip().split()[0]))
    except Exception:
        return 0


async def scroll_review_panel(page, scrolls: int = SCROLL_COUNT, pause_ms: int = SCROLL_PAUSE_MS):
    """JS-scroll the inner review panel and wait for spinner to clear."""
    for i in range(scrolls):
        await page.evaluate("""
            () => {
                const card = document.querySelector('div[data-review-id]');
                if (!card) return;
                let el = card.parentElement;
                while (el) {
                    const ov = window.getComputedStyle(el).overflowY;
                    if (ov === 'auto' || ov === 'scroll') { el.scrollTop += 3000; return; }
                    el = el.parentElement;
                }
                const all = document.querySelectorAll('div[data-review-id]');
                if (all.length > 0) all[all.length - 1].scrollIntoView();
            }
        """)
        try:
            await page.wait_for_selector(
                'div[jsaction*="loading"], .DkEaL, .mMHnD',
                state="hidden", timeout=3000
            )
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(pause_ms)
    log.info(f"      ↕  {scrolls} scrolls complete")


async def expand_more_buttons(page) -> int:
    """Click every visible 'More' expand button."""
    clicked = 0
    btns = page.locator('button.w8nwRe, button[jsaction*="pane.review.expandReview"]')
    count = await btns.count()
    for i in range(count):
        try:
            btn = btns.nth(i)
            if await btn.is_visible():
                await btn.scroll_into_view_if_needed()
                await btn.click()
                await page.wait_for_timeout(250)
                clicked += 1
        except Exception:
            pass
    log.info(f"      🖱  Expanded {clicked} 'More' buttons")
    return clicked


async def safe_inner_text(locator) -> str:
    try:
        if await locator.count() > 0:
            t = await locator.first.inner_text()
            return t.strip()
    except Exception:
        pass
    return ""


async def safe_attr(locator, attr: str) -> str:
    try:
        if await locator.count() > 0:
            v = await locator.first.get_attribute(attr)
            return (v or "").strip()
    except Exception:
        pass
    return ""


# ── Business-level extraction ─────────────────────────────────────────────────

async def extract_business_info(page) -> dict:
    """
    Extract business metadata from the restaurant detail panel.
    Returns a dict with: cuisine_type, price_level, service_options,
    open_closed_status, phone_number.
    """
    info = {
        "cuisine_type": None,
        "price_level": None,
        "service_options": None,
        "open_closed_status": None,
        "phone_number": None,
    }

    try:
        # Category / Cuisine (e.g. "Cafe · ₹₹")
        category_raw = await safe_inner_text(
            page.locator('button.DkEaL, span.mgr77e, div[jsaction*="category"] span').first
        )
        # Also try the subtitle line
        if not category_raw:
            category_raw = await page.evaluate("""
                () => {
                    const el = document.querySelector('button[jsaction*="category"]');
                    return el ? el.innerText.trim() : '';
                }
            """)

        if category_raw:
            # Split on · — first part is cuisine, ₹ symbols are price
            parts = [p.strip() for p in category_raw.split("·")]
            info["cuisine_type"] = parts[0] if parts else None
            for p in parts:
                if "₹" in p or "$" in p:
                    info["price_level"] = p
                    break

        # Open/Closed status
        open_el = page.locator(
            'span[class*="ZDu9vd"], div[data-hide-tooltip-on-mouse-move] span, '
            'span.eXgbFb, span[class*="o0Svhf"]'
        )
        info["open_closed_status"] = await safe_inner_text(open_el) or None

        # Phone number
        phone_el = page.locator(
            'button[data-tooltip*="phone"] div.rogA2c, '
            'span[aria-label*="phone"], '
            'button[aria-label*="phone"] div'
        )
        raw_phone = await safe_inner_text(phone_el)
        if raw_phone:
            info["phone_number"] = raw_phone

        # Fallback phone via JS
        if not info["phone_number"]:
            info["phone_number"] = await page.evaluate("""
                () => {
                    const btns = [...document.querySelectorAll('button[data-item-id*="phone"]')];
                    for (const b of btns) {
                        const t = b.innerText.trim();
                        if (t) return t;
                    }
                    return null;
                }
            """) or None

        # Service options (Dine-in / Takeaway / Delivery)
        service_el = page.locator('div[aria-label*="Serves"], div.LTs0Rc span, span.hpLkke')
        service_text = await safe_inner_text(service_el)
        if not service_text:
            service_text = await page.evaluate("""
                () => {
                    const labels = [...document.querySelectorAll('span')]
                        .filter(s => /Dine.in|Takeaway|Delivery|Drive/.test(s.innerText))
                        .map(s => s.innerText.trim())
                        .filter(Boolean);
                    return labels.slice(0, 4).join(', ') || null;
                }
            """) or None
        info["service_options"] = service_text or None

    except Exception as e:
        log.warning(f"      ⚠  business_info partial failure: {e}")

    return info


# ── Review-level extraction ───────────────────────────────────────────────────

async def extract_review_text(block, page) -> str:
    selectors = ["span.wiI7pd", ".MyEned span", ".wiI7pd", ".My579c"]
    for sel in selectors:
        try:
            loc = block.locator(sel).first
            if await loc.count() > 0:
                t = await loc.inner_text()
                if t.strip():
                    return t.strip()
        except Exception:
            pass
    # JS fallback
    try:
        t = await block.evaluate("""
            el => {
                const spans = el.querySelectorAll('span[jsname], .wiI7pd, .My579c, .MyEned span');
                for (const s of spans) { const t = s.innerText.trim(); if (t.length > 5) return t; }
                return '';
            }
        """)
        if t and t.strip():
            return t.strip()
    except Exception:
        pass
    # One retry
    await page.wait_for_timeout(2000)
    for sel in selectors:
        try:
            loc = block.locator(sel).first
            if await loc.count() > 0:
                t = await loc.inner_text()
                if t.strip():
                    return t.strip()
        except Exception:
            pass
    return ""


async def extract_reviewer_name(block) -> str:
    for sel in [".d4r55", "div[class*='d4r55']", ".WNxzHc", "button.al6Kxe div"]:
        t = await safe_inner_text(block.locator(sel))
        if t:
            return t
    return "Unknown User"


async def extract_rating(block) -> int:
    for sel in ['span[role="img"][aria-label*="star"]', 'span[aria-label*="star"]']:
        label = await safe_attr(block.locator(sel), "aria-label")
        if label and "star" in label.lower():
            return parse_rating(label)
    return 0


async def extract_review_date(block) -> str | None:
    """Relative date like '3 months ago', 'a week ago'."""
    for sel in [".rsqaWe", "span.rsqaWe", ".dehysf", "span[class*='dehysf']"]:
        t = await safe_inner_text(block.locator(sel))
        if t and "ago" in t.lower():
            return t
    return None


async def extract_local_guide_info(block) -> tuple[bool, int | None]:
    """
    Returns (is_local_guide: bool, reviewer_review_count: int | None).
    Local Guides have a badge span containing 'Local Guide'.
    """
    is_local_guide = False
    review_count = None
    try:
        badge_text = await block.evaluate("""
            el => {
                const spans = [...el.querySelectorAll('span, div')];
                for (const s of spans) {
                    if (s.innerText.includes('Local Guide')) return s.innerText.trim();
                }
                return '';
            }
        """)
        if badge_text and "Local Guide" in badge_text:
            is_local_guide = True
            # Badge text pattern: "Local Guide · 47 reviews"
            m = re.search(r"(\d+)\s+review", badge_text)
            if m:
                review_count = int(m.group(1))
    except Exception:
        pass
    return is_local_guide, review_count


async def extract_owner_response(block) -> str | None:
    """Return owner response text if present."""
    try:
        resp_text = await block.evaluate("""
            el => {
                const resp = el.querySelector('div[class*="CDe7pd"], .wiI7pd + div, .rEx66b');
                return resp ? resp.innerText.trim() : null;
            }
        """)
        if resp_text and len(resp_text) > 5:
            return resp_text
    except Exception:
        pass
    # Try by label
    try:
        owner_el = block.locator('div[class*="CDe7pd"], span[class*="rEx66b"]')
        t = await safe_inner_text(owner_el)
        return t if t else None
    except Exception:
        return None


# ── Restaurant URL collector (with scrolling to load 30 results) ──────────────

async def collect_restaurant_urls(page, limit: int = RESTAURANTS_LIMIT) -> list[str]:
    """
    Scroll the search results panel until we have `limit` unique place URLs.
    """
    urls: list[str] = []
    seen: set[str] = set()
    max_scroll_attempts = 20

    for _ in range(max_scroll_attempts):
        links = await page.locator('a.hfpxzc, a[href*="/maps/place/"]').all()
        for link in links:
            try:
                href = await link.get_attribute("href")
                if href and "/maps/place/" in href and href not in seen:
                    seen.add(href)
                    urls.append(href)
            except Exception:
                pass

        if len(urls) >= limit:
            break

        # Scroll the results sidebar
        await page.evaluate("""
            () => {
                const panel = document.querySelector('div[role="feed"], div[aria-label*="Results"]');
                if (panel) panel.scrollTop += 2000;
            }
        """)
        await page.wait_for_timeout(1500)

    return urls[:limit]


# ── Navigate to Reviews tab ───────────────────────────────────────────────────

async def click_reviews_tab(page):
    clicked = False
    try:
        tab = page.locator(
            'button[role="tab"][aria-label*="Reviews"], '
            'button[role="tab"]:has-text("Reviews")'
        ).first
        if await tab.count() > 0 and await tab.is_visible():
            await tab.click()
            clicked = True
    except Exception:
        pass
    if not clicked:
        await page.evaluate("""
            () => {
                const t = [...document.querySelectorAll('button[role="tab"]')]
                    .find(b => b.textContent.includes('Reviews') ||
                               (b.getAttribute('aria-label') || '').includes('Reviews'));
                if (t) t.click();
            }
        """)


# ── Per-city scrape ───────────────────────────────────────────────────────────

async def scrape_city(page, city: str, totals: dict):
    search_query = f"Best restaurants in {city}"
    log.info(f"\n{'#'*70}")
    log.info(f"  🌆  CITY: {city.upper()}  —  {search_query}")
    log.info(f"{'#'*70}\n")

    # Search
    try:
        await page.goto("https://www.google.com/maps?hl=en",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning(f"Maps load warning: {e}")

    search_box = page.locator('input#searchboxinput, input[name="q"]').first
    try:
        await search_box.wait_for(state="visible", timeout=15000)
    except Exception as e:
        log.error(f"Search box not found for {city}: {e}")
        return

    await search_box.fill(search_query)
    await search_box.press("Enter")

    try:
        await page.wait_for_selector('a.hfpxzc, a[href*="/maps/place/"]', timeout=30000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        log.error(f"Result list timeout for {city}: {e}")
        return

    restaurant_urls = await collect_restaurant_urls(page, limit=RESTAURANTS_LIMIT)
    log.info(f"📋  Collected {len(restaurant_urls)} restaurant URLs for {city}\n")

    for idx, url in enumerate(restaurant_urls):
        log.info(f"\n{'='*60}")
        log.info(f"🏪  [{idx+1}/{len(restaurant_urls)}] {city}")

        # ── Random human-like delay ──────────────────────────────────
        delay = random.uniform(2.0, 4.0)
        log.info(f"   ⏱  Waiting {delay:.1f}s before next restaurant...")
        await asyncio.sleep(delay)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            log.warning(f"   ⚠  [SKIP] Could not load page: {e}")
            totals["skipped"] += 1
            continue

        # Restaurant name
        try:
            name_el = await page.wait_for_selector(
                'h1.DUwDvf, h1[class*="DUwDvf"]', timeout=8000
            )
            restaurant_name = (await name_el.inner_text()).strip()
        except Exception:
            restaurant_name = f"Unknown_{city}_{idx}"
        log.info(f"📍  Scraping: {restaurant_name}")

        # Business info
        biz = await extract_business_info(page)
        log.info(
            f"   ℹ️   cuisine={biz['cuisine_type']} | price={biz['price_level']} "
            f"| status={biz['open_closed_status']} | phone={biz['phone_number']}"
        )

        # Reviews tab
        await click_reviews_tab(page)
        try:
            await page.wait_for_selector(
                'div[data-review-id], div.jJc83c',
                state="attached", timeout=12000
            )
            await page.wait_for_timeout(1500)
        except PlaywrightTimeoutError:
            log.warning(f"   ⚠  [SKIP] No reviews loaded for {restaurant_name}")
            totals["skipped"] += 1
            continue

        # Scroll + expand
        log.info("   🔄  Scrolling review panel...")
        await scroll_review_panel(page)

        log.info("   🖱  Expanding 'More' buttons...")
        await expand_more_buttons(page)
        await page.wait_for_timeout(800)

        # Extract reviews
        review_blocks = await page.locator('div[data-review-id], div.jJc83c').all()
        log.info(f"   💬  Processing {len(review_blocks)} review cards...")

        restaurant_valid = 0
        restaurant_empty = 0

        for block in review_blocks:
            review_text = await extract_review_text(block, page)
            if not review_text:
                restaurant_empty += 1
                totals["empty"] += 1
                continue

            restaurant_valid += 1
            totals["valid"] += 1

            reviewer_name     = await extract_reviewer_name(block)
            rating            = await extract_rating(block)
            review_date       = await extract_review_date(block)
            is_local_guide, reviewer_review_count = await extract_local_guide_info(block)
            owner_response    = await extract_owner_response(block)

            record = {
                # Business
                "restaurant_name":    restaurant_name,
                "cuisine_type":       biz["cuisine_type"],
                "price_level":        biz["price_level"],
                "service_options":    biz["service_options"],
                "open_closed_status": biz["open_closed_status"],
                "phone_number":       biz["phone_number"],
                # Review
                "reviewer_name":      reviewer_name,
                "rating":             rating if rating > 0 else None,
                "review_text":        review_text,
                "review_date":        review_date,
                # Reviewer intelligence
                "is_local_guide":     is_local_guide,
                "reviewer_review_count": reviewer_review_count,
                # Engagement
                "owner_response":     owner_response,
                # Meta
                "location_tag":       city,
                "scraped_at":         datetime.now(timezone.utc).isoformat(),
            }

            try:
                supabase.table("restaurant_reviews").upsert(
                    record,
                    on_conflict="restaurant_name,reviewer_name,review_text"
                ).execute()
                totals["db_ok"] += 1
            except Exception as db_err:
                totals["db_err"] += 1
                log.warning(f"      ❌  DB error: {db_err}")

        log.info(
            f"   ✅  {restaurant_name}: "
            f"{restaurant_valid} valid | {restaurant_empty} empty"
        )

    log.info(f"\n{'='*60}")
    log.info(f"🌆  {city} done. Running totals → valid={totals['valid']} | "
             f"empty={totals['empty']} | db_ok={totals['db_ok']} | "
             f"db_err={totals['db_err']} | skipped={totals['skipped']}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    totals = {"valid": 0, "empty": 0, "db_ok": 0, "db_err": 0, "skipped": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            user_agent=USER_AGENT,
        )

        # Bypass cookie consent
        await context.add_cookies([{
            "name": "CONSENT",
            "value": "YES+cb.20230101-14-p0.en+FX+414",
            "domain": ".google.com",
            "path": "/",
        }])

        page = await context.new_page()

        for city in TARGET_CITIES:
            try:
                await scrape_city(page, city, totals)
            except Exception as e:
                log.error(f"💥  Fatal error for city '{city}': {e} — continuing to next city.")

        await browser.close()

    # ── Final summary ─────────────────────────────────────────────────────────
    log.info(f"\n{'#'*70}")
    log.info("✅  ALL CITIES COMPLETE — FINAL VERIFICATION SUMMARY")
    log.info(f"{'#'*70}")
    log.info(f"  🌆  Cities scraped        : {len(TARGET_CITIES)}")
    log.info(f"  📊  Valid text reviews    : {totals['valid']}")
    log.info(f"  📊  Empty reviews         : {totals['empty']}")
    log.info(f"  🗄️   DB upserts OK         : {totals['db_ok']}")
    log.info(f"  🗄️   DB upserts FAILED     : {totals['db_err']}")
    log.info(f"  ⚠️   Restaurants skipped   : {totals['skipped']}")
    log.info(f"{'#'*70}\n")


if __name__ == "__main__":
    asyncio.run(main())
