"""
src/matchmaker/drafter.py — Claude AI email drafting.

Given a property signal + agent profile, returns a 3-sentence email
that is specific, helpful in tone, and never corporate-fluff.
"""
import logging
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import anthropic
from config import cfg

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

BANNED_WORDS = ["leverage", "unleash", "synergy", "excited to", "I hope this finds you",
                "game-changer", "reach out", "touch base", "circle back"]

SYSTEM_PROMPT = """You are a real estate data analyst writing a short, direct cold email to a listing agent.

Rules:
- Exactly 3 sentences. No more.
- Sentence 1: A specific observation about their listing (use the address and the exact signal data provided).
- Sentence 2: What this typically signals in the market + what data you have ready.
- Sentence 3: A low-friction offer. Something like "I have [N] buyers in [zip] right now, happy to share if useful."
- Tone: helpful, peer-to-peer. Like one professional texting another.
- Never use these words: leverage, unleash, synergy, "excited to", "I hope this finds you", game-changer, "reach out", "touch base", "circle back"
- Never mention money, fees, or payment.
- Never use exclamation marks.
- Sign off with just the sender name, no title.
- Output only the email body. No subject line. No "Dear" or "Hi [Name]" — start directly with the observation.
"""


def draft_email(
    agent_first_name: str,
    property_address: str,
    zip_code: str,
    days_on_market: int | None,
    price_drop_pct: float | None,
    list_price: float | None,
    signal_type: str,
    sender_name: str,
) -> dict:
    """
    Returns {subject, body} — both strings.
    """
    # Build the signal description
    signal_parts = []
    if signal_type in ("STALE_LISTING", "BOTH") and days_on_market:
        signal_parts.append(f"{days_on_market} days on market")
    if signal_type in ("PRICE_DROP", "BOTH") and price_drop_pct:
        price_str = f"${list_price:,.0f}" if list_price else "current price"
        signal_parts.append(f"{price_drop_pct:.1f}% price reduction to {price_str}")

    signal_description = " and ".join(signal_parts) if signal_parts else "notable market signal"

    user_prompt = f"""Write a 3-sentence email to {agent_first_name}.

Property: {property_address} (zip: {zip_code})
Signal: {signal_description}
Sender name: {sender_name}

The email should feel like it came from someone who actually looked at their specific listing, not a template."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        body = message.content[0].text.strip()

        # Sanity-check for banned words
        body_lower = body.lower()
        for word in BANNED_WORDS:
            if word.lower() in body_lower:
                logger.warning(f"[drafter] Banned word '{word}' found in draft — regenerating")
                return draft_email(agent_first_name, property_address, zip_code,
                                   days_on_market, price_drop_pct, list_price,
                                   signal_type, sender_name)

        # Generate subject line separately — concise and specific
        subject_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{
                "role": "user",
                "content": f"Write a 6-10 word email subject line for this: {property_address} — {signal_description}. No punctuation at the end. No quotes. Just the subject line text."
            }],
        )
        subject = subject_response.content[0].text.strip().strip('"').strip("'")

        logger.info(f"[drafter] Drafted email for {agent_first_name} — {property_address}")
        return {"subject": subject, "body": body}

    except anthropic.APIError as e:
        logger.error(f"[drafter] Anthropic API error: {e}")
        raise
