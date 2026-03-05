"""
config.py — Single source of truth for all env vars.
Import this everywhere: from config import cfg
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Anthropic
    ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]

    # Google Maps
    GOOGLE_MAPS_API_KEY: str = os.environ["GOOGLE_MAPS_API_KEY"]

    # Apollo.io
    APOLLO_API_KEY: str = os.environ["APOLLO_API_KEY"]

    # Resend
    RESEND_API_KEY: str = os.environ["RESEND_API_KEY"]
    MAIL_FROM: str = os.environ["MAIL_FROM"]
    MAIL_FROM_NAME: str = os.environ.get("MAIL_FROM_NAME", "")

    # Stripe
    STRIPE_SECRET_KEY: str = os.environ["STRIPE_SECRET_KEY"]
    STRIPE_WEBHOOK_SECRET: str = os.environ["STRIPE_WEBHOOK_SECRET"]
    STRIPE_PRICE_PER_LEAD: int = int(os.environ.get("STRIPE_PRICE_PER_LEAD", "4900"))

    # Operator
    OPERATOR_EMAIL: str = os.environ["OPERATOR_EMAIL"]
    BASE_URL: str = os.environ["BASE_URL"].rstrip("/")

    # Dry run — when set, ALL outgoing emails are redirected to this address.
    # Real agent emails are never contacted. Unset (or empty) = live mode.
    DRY_RUN_EMAIL: str = os.environ.get("DRY_RUN_EMAIL", "")

    # Scheduler
    MONITOR_INTERVAL_MINUTES: int = int(os.environ.get("MONITOR_INTERVAL_MINUTES", "30"))

    # Database
    DB_PATH: str = os.path.join(os.path.dirname(__file__), "engine.db")

    # Scraping
    REQUEST_TIMEOUT: int = 15
    MAX_CONSECUTIVE_FAILURES: int = 3  # before switching adapter


cfg = Config()
