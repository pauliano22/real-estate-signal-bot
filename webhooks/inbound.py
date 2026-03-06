"""
webhooks/inbound.py — Handle inbound emails forwarded by Resend.

Resend POSTs a webhook with only email metadata (body is omitted to avoid
payload size limits). We fetch the full email body via GET /emails/{email_id}.
"""
import logging
import re
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from config import cfg
from src.db import models
from src.conversation.handler import handle_reply

logger = logging.getLogger(__name__)

# Matches reply+123@ in a To address
_TAG_RE = re.compile(r"reply\+(\d+)@", re.IGNORECASE)


def _parse_lead_id(to_field: str) -> int | None:
    """Extract lead_id from reply+{id}@domain tag."""
    match = _TAG_RE.search(to_field or "")
    if match:
        return int(match.group(1))
    return None


def _fetch_email_body(email_id: str) -> str:
    """Fetch full email from Resend API and return plain text (or stripped HTML)."""
    url = f"https://api.resend.com/emails/receiving/{email_id}"
    try:
        resp = httpx.get(url, headers={"Authorization": f"Bearer {cfg.RESEND_API_KEY}"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"[inbound] Failed to fetch email {email_id} from Resend: {e}")
        return ""

    text = data.get("text") or ""
    if text.strip():
        return text.strip()

    html = data.get("html") or ""
    clean = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", clean).strip()


def handle_inbound(payload: dict) -> None:
    """
    Main entry point called by the Flask route.
    Expects a Resend inbound webhook payload (dict).
    """
    # Resend nests metadata under "data"
    data = payload.get("data") or payload
    email_id = data.get("email_id") or ""

    to_raw = data.get("to") or ""
    # "to" may be a list or a plain string
    to_field = to_raw[0] if isinstance(to_raw, list) else to_raw
    from_email = data.get("from") or ""
    subject = data.get("subject") or ""

    if not email_id:
        logger.warning("[inbound] No email_id in payload — ignoring")
        return

    body = _fetch_email_body(email_id)

    if not body:
        logger.warning(f"[inbound] Empty body for email_id={email_id} — ignoring")
        return

    # Resolve lead by +tag, then fall back to sender email
    lead_id = _parse_lead_id(to_field)
    lead = models.get_lead_by_id(lead_id) if lead_id else None

    if not lead:
        lead = models.get_lead_by_email(from_email)

    if not lead:
        logger.warning(
            f"[inbound] Could not resolve lead for to={to_field!r} from={from_email!r}"
        )
        return

    lead_id = lead["id"]
    logger.info(f"[inbound] Received reply for lead #{lead_id} from {from_email!r}")

    # Store inbound message
    models.store_message(
        lead_id=lead_id,
        direction="inbound",
        from_email=from_email,
        to_email=to_field,
        subject=subject,
        body=body,
    )

    # Load full thread and dispatch to conversation handler
    thread = models.get_thread(lead_id)
    classification = handle_reply(lead, thread)
    logger.info(f"[inbound] Lead #{lead_id} handled — classification: {classification}")
