"""
src/signals/redfin.py — Primary signal adapter using Redfin's JSON API.

Redfin exposes undocumented JSON endpoints that are far more stable than
scraping HTML. We hit the search endpoint directly and parse the response.

Self-healing: tracks consecutive failures and raises after MAX_FAILURES
so the orchestrator can switch to the stealth fallback.
"""
import httpx
import random
import time
import logging
from typing import Optional

from src.signals.base import SignalAdapter, PropertySignal

logger = logging.getLogger(__name__)

# Minimum thresholds for a signal to qualify
# TEMP: lowered for dry run testing — restore to 10.0 / 90 for production
PRICE_DROP_THRESHOLD_PCT = 1.0    # > 1% price reduction (test)
STALE_DOM_THRESHOLD = 0           # any DOM (test)

# Redfin's region search endpoint
REDFIN_SEARCH_URL = "https://www.redfin.com/stingray/api/gis"
REDFIN_REGION_URL = "https://www.redfin.com/stingray/api/region"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def _headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.redfin.com/",
        "X-Requested-With": "XMLHttpRequest",
    }


def _compute_score(price_drop_pct: Optional[float], dom: int) -> float:
    """Higher is more urgent. Max 100."""
    score = 0.0
    if price_drop_pct and price_drop_pct >= PRICE_DROP_THRESHOLD_PCT:
        score += min(price_drop_pct * 2, 50)  # up to 50 pts
    if dom >= STALE_DOM_THRESHOLD:
        score += min((dom - STALE_DOM_THRESHOLD) * 0.3, 50)  # up to 50 pts
    return round(min(score, 100), 1)


class RedfinAdapter(SignalAdapter):
    name = "redfin"

    def __init__(self):
        self._consecutive_failures = 0
        self._client = httpx.Client(timeout=15, follow_redirects=True)

    def is_available(self) -> bool:
        try:
            r = self._client.get(
                "https://www.redfin.com/stingray/api/gis-csv",
                headers=_headers(),
                params={"al": 1, "num_homes": 1, "region_id": "1", "region_type": "2"},
            )
            return r.status_code < 400
        except Exception:
            return False

    def fetch_signals(self, zip_code: str) -> list[PropertySignal]:
        try:
            region_id = self._get_region_id(zip_code)
            if not region_id:
                logger.warning(f"[redfin] No region ID for zip {zip_code}")
                return []

            listings = self._fetch_listings(region_id, zip_code)
            signals = []

            for listing in listings:
                signal = self._qualify(listing, zip_code)
                if signal:
                    signals.append(signal)

            self._consecutive_failures = 0
            logger.info(f"[redfin] {zip_code}: {len(signals)} signals from {len(listings)} listings")
            return signals

        except httpx.HTTPStatusError as e:
            self._consecutive_failures += 1
            logger.error(f"[redfin] HTTP {e.response.status_code} for zip {zip_code} (failure #{self._consecutive_failures})")
            raise
        except Exception as e:
            self._consecutive_failures += 1
            logger.error(f"[redfin] Error for zip {zip_code}: {e} (failure #{self._consecutive_failures})")
            raise

    def _get_region_id(self, zip_code: str) -> Optional[str]:
        """Resolve zip code to Redfin region ID."""
        time.sleep(random.uniform(1.5, 3.5))
        r = self._client.get(
            REDFIN_REGION_URL,
            headers=_headers(),
            params={"region_id": zip_code, "region_type": 2, "tz": True, "v": 8},
        )
        r.raise_for_status()
        # Strip Redfin's CSRF prefix: {}&&{"payload":...}
        text = r.text
        if text.startswith("{}&&"):
            text = text[4:]
        import json
        data = json.loads(text)
        return data.get("payload", {}).get("rootDefaults", {}).get("region_id")

    def _fetch_listings(self, region_id: str, zip_code: str) -> list[dict]:
        """Fetch active listings for a region."""
        time.sleep(random.uniform(2, 5))
        params = {
            "al": 1,
            "num_homes": 350,
            "ord": "days-on-market-desc",
            "page_number": 1,
            "region_id": region_id,
            "region_type": 2,
            "sf": "1,2,3,5,6,7",   # single family, condo, etc.
            "start": 0,
            "status": 1,            # active
            "uipt": "1,2,3,4",
            "v": 8,
        }
        r = self._client.get(REDFIN_SEARCH_URL, headers=_headers(), params=params)
        r.raise_for_status()
        text = r.text
        if text.startswith("{}&&"):
            text = text[4:]
        import json
        data = json.loads(text)
        homes = data.get("payload", {}).get("homes", [])
        return homes

    def _qualify(self, listing: dict, zip_code: str) -> Optional[PropertySignal]:
        """Return a PropertySignal if listing meets thresholds, else None."""
        try:
            price = listing.get("price", {}).get("value")
            original = listing.get("originalPrice", {}).get("value")
            dom = listing.get("daysOnMarket", {}).get("value", 0)
            address_parts = listing.get("streetLine", {})
            address = address_parts.get("value", "") if isinstance(address_parts, dict) else str(address_parts)
            url_path = listing.get("url", "")
            url = f"https://www.redfin.com{url_path}"

            if not address or not price:
                return None

            price_drop_pct = None
            if original and original > price:
                price_drop_pct = round((original - price) / original * 100, 1)

            has_price_drop = price_drop_pct and price_drop_pct >= PRICE_DROP_THRESHOLD_PCT
            has_stale = dom >= STALE_DOM_THRESHOLD

            if not has_price_drop and not has_stale:
                return None

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
                original_price=original,
                price_drop_pct=price_drop_pct,
                days_on_market=dom,
                signal_type=signal_type,
                signal_score=_compute_score(price_drop_pct, dom),
                raw=listing,
            )
        except Exception as e:
            logger.debug(f"[redfin] Could not qualify listing: {e}")
            return None
