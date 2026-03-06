"""
webhooks/inbound.py — Handle inbound emails forwarded by Resend.

Resend POSTs a JSON payload to POST /inbound/email when an agent replies.
We parse the lead_id from the reply+{id}@reply.domain tag in the To field,
store the message, and hand off to the conversation handler.
"""
import logging
import re
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


def _extract_body(payload: dict) -> str:
    """Prefer plain text; fall back to stripping HTML tags."""
    text = payload.get("text") or payload.get("plain") or ""
    if text:
        return text.strip()
    html = payload.get("html") or ""
    # Minimal HTML strip — good enough for reply context
    clean = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", clean).strip()


def handle_inbound(payload: dict) -> None:
    """
    Main entry point called by the Flask route.
    Expects a Resend inbound webhook payload (dict).
    """
    to_field = payload.get("to") or ""
    from_email = payload.get("from") or ""
    subject = payload.get("subject") or ""
    body = _extract_body(payload)

    if not body:
        logger.warning("[inbound] Empty body — ignoring")
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
