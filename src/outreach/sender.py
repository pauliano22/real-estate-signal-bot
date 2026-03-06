"""
src/outreach/sender.py — Send emails via Resend with open/click tracking.
"""
import logging
import uuid
import re
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from config import cfg
from src.db import models

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r'https?://[^\s<>"]+')


def _wrap_links(body: str, lead_id: int) -> str:
    """Replace URLs in the email body with tracked redirect links."""
    def replace_url(match):
        original_url = match.group(0)
        token = str(uuid.uuid4())
        models.record_tracking_event(
            lead_id=lead_id,
            event_type="click",
            token=token,
            destination=original_url,
        )
        return f"{cfg.BASE_URL}/track/click/{token}"

    return _URL_PATTERN.sub(replace_url, body)


def send_reply(
    lead_id: int,
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
) -> str | None:
    """
    Send a conversational reply (no tracking pixel/link wrapping).
    Used by the conversation handler for follow-up messages.
    Returns Resend email ID on success, or None on failure.
    """
    try:
        import resend
    except ImportError:
        logger.error("[sender] resend package not installed")
        return None

    resend.api_key = cfg.RESEND_API_KEY

    from_address = (
        f"{cfg.MAIL_FROM_NAME} <{cfg.MAIL_FROM}>"
        if cfg.MAIL_FROM_NAME
        else cfg.MAIL_FROM
    )

    if cfg.DRY_RUN_EMAIL:
        actual_to = cfg.DRY_RUN_EMAIL
        logger.warning(f"[DRY RUN] Redirecting reply from {to_email} -> {actual_to}")
        subject = f"[DRY RUN] {subject} (intended for: {to_email})"
    else:
        actual_to = to_email

    to_address = f"{to_name} <{actual_to}>" if (to_name and not cfg.DRY_RUN_EMAIL) else actual_to

    payload: dict = {
        "from": from_address,
        "to": [to_address],
        "subject": subject,
        "html": body.replace("\n", "<br>"),
        "text": body,
    }

    if cfg.REPLY_DOMAIN:
        payload["reply_to"] = f"reply+{lead_id}@{cfg.REPLY_DOMAIN}"

    try:
        response = resend.Emails.send(payload)
        email_id = response.get("id", "")
        logger.info(f"[sender] Reply sent to {actual_to} — resend id: {email_id}")
        return email_id
    except Exception as e:
        logger.error(f"[sender] Failed to send reply to {to_email}: {e}")
        return None


def send_outreach(
    lead_id: int,
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
) -> str | None:
    """
    Send an outreach email via Resend.
    Injects an open-tracking pixel and wraps any URLs for click tracking.
    Returns the Resend email ID on success, or None on failure.
    """
    try:
        import resend
    except ImportError:
        logger.error("[sender] resend package not installed — run: pip install resend")
        return None

    resend.api_key = cfg.RESEND_API_KEY

    # Wrap links for click tracking
    tracked_body = _wrap_links(body, lead_id)

    # Inject open-tracking pixel
    open_token = str(uuid.uuid4())
    models.record_tracking_event(
        lead_id=lead_id,
        event_type="open",
        token=open_token,
    )
    pixel = f'<img src="{cfg.BASE_URL}/track/open/{open_token}" width="1" height="1" style="display:none" />'
    html_body = tracked_body.replace("\n", "<br>") + pixel

    from_address = (
        f"{cfg.MAIL_FROM_NAME} <{cfg.MAIL_FROM}>"
        if cfg.MAIL_FROM_NAME
        else cfg.MAIL_FROM
    )

    # --- DRY RUN INTERCEPT ---
    if cfg.DRY_RUN_EMAIL:
        actual_to = cfg.DRY_RUN_EMAIL
        logger.warning(
            f"[DRY RUN] Redirecting email from {to_email} -> {actual_to}"
        )
        subject = f"[DRY RUN] {subject} (intended for: {to_email})"
    else:
        actual_to = to_email

    to_address = f"{to_name} <{actual_to}>" if (to_name and not cfg.DRY_RUN_EMAIL) else actual_to

    payload: dict = {
        "from": from_address,
        "to": [to_address],
        "subject": subject,
        "html": html_body,
        "text": tracked_body,
    }

    # Set Reply-To so inbound replies route back through the conversation handler
    if cfg.REPLY_DOMAIN:
        payload["reply_to"] = f"reply+{lead_id}@{cfg.REPLY_DOMAIN}"

    try:
        response = resend.Emails.send(payload)
        email_id = response.get("id", "")
        logger.info(f"[sender] Sent to {actual_to} — resend id: {email_id}")
        return email_id
    except Exception as e:
        logger.error(f"[sender] Failed to send to {to_email}: {e}")
        return None
