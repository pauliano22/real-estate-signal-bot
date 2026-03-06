"""
src/signals/rentcast.py — Primary signal adapter using the RentCast API.

Docs: https://developers.rentcast.io/reference/listing-search-sale

RentCast returns structured listing data including agent contact info,
price history, and days on market — no scraping required.

Each listing includes listingAgent.email in ~95% of cases, so the
Google Maps + Apollo enrichment step is skipped for those leads.
"""
import httpx
import logging
from typing import Optional
from datetime import datetime

from src.signals.base import SignalAdapter, PropertySignal
from src.matchmaker.scorer import score_signal

logger = logging.getLogger(__name__)

RENTCAST_BASE = "https://api.rentcast.io/v1"

# Thresholds — lower these for testing, restore for production
PRICE_DROP_THRESHOLD_PCT = 1.0   # TEMP: 1% for dry run (production: 10.0)
STALE_DOM_THRESHOLD = 0          # TEMP: 0 for dry run (production: 90)


class RentCastAdapter(SignalAdapter):
    name = "rentcast"

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = httpx.Client(timeout=15)

    def is_available(self) -> bool:
        try:
            r = self._client.get(
                f"{RENTCAST_BASE}/listings/sale",
                headers=self._headers(),
                params={"zipCode": "33101", "status": "Active", "limit": 1},
            )
            return r.status_code == 200
        except Exception:
            return False

    def fetch_signals(self, zip_code: str) -> list[PropertySignal]:
        try:
            listings = self._fetch_listings(zip_code)
            signals = []
            for listing in listings:
                signal = self._qualify(listing, zip_code)
                if signal:
                    signals.append(signal)

            logger.info(
                f"[rentcast] {zip_code}: {len(signals)} signals from {len(listings)} listings"
            )
            return signals

        except httpx.HTTPStatusError as e:
            logger.error(f"[rentcast] HTTP {e.response.status_code} for zip {zip_code}: {e.response.text[:200]}")
            raise
        except Exception as e:
            logger.error(f"[rentcast] Error for zip {zip_code}: {e}")
            raise

    def _headers(self) -> dict:
        return {"X-Api-Key": self._api_key}

    def _fetch_listings(self, zip_code: str) -> list[dict]:
        listings = []
        offset = 0
        limit = 500

        while True:
            r = self._client.get(
                f"{RENTCAST_BASE}/listings/sale",
                headers=self._headers(),
                params={
                    "zipCode": zip_code,
                    "status": "Active",
                    "limit": limit,
                    "offset": offset,
                },
            )
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            listings.extend(page)
            if len(page) < limit:
                break
            offset += limit

        return listings

    def _qualify(self, listing: dict, zip_code: str) -> Optional[PropertySignal]:
        try:
            address = listing.get("formattedAddress", "").strip()
            current_price = listing.get("price")
            dom = listing.get("daysOnMarket") or 0

            if not address or not current_price:
                return None

            # Calculate price drop from history
            original_price, price_drop_pct = self._price_drop(listing)

            has_price_drop = (
                price_drop_pct is not None
                and price_drop_pct >= PRICE_DROP_THRESHOLD_PCT
            )
            has_stale = dom >= STALE_DOM_THRESHOLD if STALE_DOM_THRESHOLD > 0 else False

            # With DOM threshold of 0, every listing qualifies as stale —
            # instead only use it as a signal when DOM is actually notable (>0)
            if STALE_DOM_THRESHOLD == 0:
                has_stale = dom > 0

            if not has_price_drop and not has_stale:
                return None

            if has_price_drop and has_stale:
                signal_type = "BOTH"
            elif has_price_drop:
                signal_type = "PRICE_DROP"
            else:
                signal_type = "STALE_LISTING"

            # Build Redfin-style listing URL as a fallback reference
            slug = address.lower().replace(",", "").replace(" ", "-")
            listing_url = listing.get("mlsUrl") or f"https://www.redfin.com/FL/Cape-Coral/{slug}/home"

            # Pull agent contact from listing (present in ~95% of RentCast records)
            agent = listing.get("listingAgent") or {}

            return PropertySignal(
                address=address,
                zip_code=zip_code,
                listing_url=listing_url,
                list_price=current_price,
                original_price=original_price,
                price_drop_pct=price_drop_pct,
                days_on_market=dom,
                signal_type=signal_type,
                signal_score=score_signal(price_drop_pct, dom, signal_type),
                raw=listing,
                agent_name=agent.get("name") or None,
                agent_email=agent.get("email") or None,
                agent_phone=agent.get("phone") or None,
                agent_website=agent.get("website") or None,
            )

        except Exception as e:
            logger.debug(f"[rentcast] Could not qualify listing: {e}")
            return None

    def _price_drop(self, listing: dict) -> tuple[Optional[float], Optional[float]]:
        """
        Returns (original_price, drop_pct) using listing price history.
        Compares the earliest recorded price to the current price.
        Returns (None, None) if no history or price hasn't dropped.
        """
        history = listing.get("history") or {}
        if len(history) < 2:
            return None, None

        current_price = listing.get("price")
        if not current_price:
            return None, None

        # Sort history entries by date, get earliest price
        sorted_entries = sorted(history.items(), key=lambda x: x[0])
        original_price = sorted_entries[0][1].get("price")

        if not original_price or original_price <= current_price:
            return None, None  # no drop

        drop_pct = round((original_price - current_price) / original_price * 100, 1)
        return original_price, drop_pct
