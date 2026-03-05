"""
src/signals/zillow_stealth.py — Fallback signal adapter using Playwright + stealth.

Only activated when Redfin fails MAX_CONSECUTIVE_FAILURES times in a row.
Uses playwright-stealth to evade bot detection.

To install browser: playwright install chromium
"""
import logging
import random
import time
from typing import Optional

from src.signals.base import SignalAdapter, PropertySignal

logger = logging.getLogger(__name__)

PRICE_DROP_THRESHOLD_PCT = 10.0
STALE_DOM_THRESHOLD = 90


class ZillowStealthAdapter(SignalAdapter):
    name = "zillow_stealth"

    def is_available(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
            return True
        except ImportError:
            return False

    def fetch_signals(self, zip_code: str) -> list[PropertySignal]:
        """
        Use Playwright with stealth to scrape Zillow search results.
        Navigates to zillow.com/homes/{zip_code}/ and extracts listing cards.
        """
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import stealth_sync
        except ImportError:
            logger.error("[zillow_stealth] playwright or playwright-stealth not installed")
            return []

        signals = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                user_agent=random.choice([
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
                ]),
            )
            page = context.new_page()
            stealth_sync(page)

            try:
                url = f"https://www.zillow.com/homes/for_sale/{zip_code}/"
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(3, 6))

                # Extract listing cards via Zillow's data attributes
                listings = page.evaluate("""
                    () => {
                        const cards = document.querySelectorAll('[data-test="property-card"]');
                        return Array.from(cards).map(card => {
                            const priceEl = card.querySelector('[data-test="property-card-price"]');
                            const addressEl = card.querySelector('address');
                            const linkEl = card.querySelector('a[href]');
                            const detailsEl = card.querySelector('.StyledPropertyCardDataWrapper');
                            return {
                                price: priceEl ? priceEl.textContent : null,
                                address: addressEl ? addressEl.textContent : null,
                                url: linkEl ? linkEl.href : null,
                                details: detailsEl ? detailsEl.textContent : null,
                            };
                        });
                    }
                """)

                for listing in listings:
                    signal = self._qualify(listing, zip_code)
                    if signal:
                        signals.append(signal)

                logger.info(f"[zillow_stealth] {zip_code}: {len(signals)} signals found")

            except Exception as e:
                logger.error(f"[zillow_stealth] Page error for {zip_code}: {e}")
            finally:
                browser.close()

        return signals

    def _qualify(self, listing: dict, zip_code: str) -> Optional[PropertySignal]:
        """
        Zillow's HTML doesn't expose DOM/price history directly on listing cards.
        We detect 'Price cut' badges and estimate DOM from 'Listed X days ago'.
        For full DOM data, individual listing pages must be visited — that's Phase 2.
        """
        try:
            details = listing.get("details", "") or ""
            address = listing.get("address", "") or ""
            url = listing.get("url", "") or ""
            price_text = listing.get("price", "") or ""

            if not address:
                return None

            # Detect price cut signal
            has_price_drop = "Price cut" in details or "Reduced" in details

            # Detect stale signal — "Listed X days ago"
            dom = 0
            has_stale = False
            import re
            dom_match = re.search(r"(\d+)\s+days?\s+ago", details, re.IGNORECASE)
            if dom_match:
                dom = int(dom_match.group(1))
                has_stale = dom >= STALE_DOM_THRESHOLD

            if not has_price_drop and not has_stale:
                return None

            # Parse price
            price = None
            price_match = re.search(r"\$([0-9,]+)", price_text)
            if price_match:
                price = float(price_match.group(1).replace(",", ""))

            if has_price_drop and has_stale:
                signal_type = "BOTH"
            elif has_price_drop:
                signal_type = "PRICE_DROP"
            else:
                signal_type = "STALE_LISTING"

            return PropertySignal(
                address=address,
                zip_code=zip_code,
                listing_url=url,
                list_price=price,
                original_price=None,
                price_drop_pct=None,    # not available from card; needs detail page
                days_on_market=dom,
                signal_type=signal_type,
                signal_score=50.0,       # default score — enriched on detail page later
                raw=listing,
            )
        except Exception as e:
            logger.debug(f"[zillow_stealth] Could not qualify listing: {e}")
            return None
