"""
src/signals/base.py — Abstract interface all signal adapters must implement.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class PropertySignal:
    address: str
    zip_code: str
    listing_url: str
    list_price: float
    original_price: Optional[float]
    price_drop_pct: Optional[float]
    days_on_market: int
    signal_type: str       # PRICE_DROP | STALE_LISTING | BOTH
    signal_score: float    # 0–100 urgency score
    raw: dict              # raw API/scrape response for debugging
    # Optional agent data — populated by sources that include it (e.g. RentCast)
    # When present, monitor.py skips the Google Maps + Apollo enrichment step
    agent_name: Optional[str] = None
    agent_email: Optional[str] = None
    agent_phone: Optional[str] = None
    agent_website: Optional[str] = None


class SignalAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def fetch_signals(self, zip_code: str) -> list[PropertySignal]:
        """
        Fetch all qualifying listings for a zip code.
        Must return [] (not raise) on a recoverable error.
        Must raise on unrecoverable errors.
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Quick check — returns False if source is currently blocked."""
        ...
