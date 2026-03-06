"""
src/fulfillment/deliver.py — Deliver full lead package to the buyer after payment.

Builds a formatted lead report and emails it to the agent who paid.
"""
import logging
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from config import cfg
from src.db import models

logger = logging.getLogger(__name__)


def _build_lead_report(lead: dict) -> str:
    """Build plain-text lead report."""
    dom = lead.get("days_on_market", "N/A")
    drop = lead.get("price_drop_pct")
    drop_str = f"{drop:.1f}%" if drop else "N/A"
    price = lead.get("list_price")
    price_str = f"${price:,.0f}" if price else "N/A"
    original = lead.get("original_price")
    original_str = f"${original:,.0f}" if original else "N/A"

    return f"""
LEAD REPORT
===========
Property: {lead.get('property_address', 'N/A')}
Zip Code: {lead.get('zip_code', 'N/A')}
Listing URL: {lead.get('listing_url', 'N/A')}

SIGNAL DATA
-----------
Signal Type: {lead.get('signal_type', 'N/A')}
Days on Market: {dom}
List Price: {price_str}
Original Price: {original_str}
Price Drop: {drop_str}
Urgency Score: {lead.get('signal_score', 0)}/100

AGENT CONTACT
-------------
Name: {lead.get('agent_name', 'N/A')}
Email: {lead.get('agent_email', 'N/A')}
Phone: {lead.get('agent_phone', 'N/A')}
Company: {lead.get('agent_company', 'N/A')}
Website: {lead.get('agent_website', 'N/A')}

TIMELINE
--------
Signal Detected: {lead.get('found_at', 'N/A')}
First Contacted: {lead.get('emailed_at', 'N/A')}
Payment Confirmed: {lead.get('paid_at', 'N/A')}

---
This report was generated automatically by Real Estate Signal Bot.
""".strip()


def deliver_lead(lead: dict) -> bool:
    """
    Email the full lead report to the buyer (the agent who paid).
    Returns True on success.
    """
    try:
        import resend
    except ImportError:
        logger.error("[deliver] resend package not installed — run: pip install resend")
        return False

    buyer_email = lead.get("agent_email")
    buyer_name = lead.get("agent_name", "")
    property_address = lead.get("property_address", "property")

    if not buyer_email:
        logger.error(f"[deliver] No buyer email for lead {lead['id']}")
        return False

    report = _build_lead_report(lead)
    resend.api_key = cfg.RESEND_API_KEY

    from_address = (
        f"{cfg.MAIL_FROM_NAME} <{cfg.MAIL_FROM}>"
        if cfg.MAIL_FROM_NAME
        else cfg.MAIL_FROM
    )

    # --- DRY RUN INTERCEPT ---
    if cfg.DRY_RUN_EMAIL:
        actual_to = cfg.DRY_RUN_EMAIL
        logger.warning(f"[DRY RUN] Redirecting fulfillment from {buyer_email} -> {actual_to}")
        subject = f"[DRY RUN] Lead Report: {property_address} (intended for: {buyer_email})"
    else:
        actual_to = buyer_email
        subject = f"Your Lead Report: {property_address}"

    to_address = actual_to

    payload = {
        "from": from_address,
        "to": [to_address],
        "subject": subject,
        "text": report,
    }
    if cfg.OPERATOR_EMAIL:
        payload["bcc"] = [cfg.OPERATOR_EMAIL]

    try:
        response = resend.Emails.send(payload)
        logger.info(f"[deliver] Lead report sent to {buyer_email} (lead {lead['id']}) — resend id: {response.get('id')}")
        return True
    except Exception as e:
        logger.error(f"[deliver] Failed to deliver lead {lead['id']} to {buyer_email}: {e}")
        return False
