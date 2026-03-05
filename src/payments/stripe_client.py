"""
src/payments/stripe_client.py — Create Stripe Payment Links for leads.
"""
import logging
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from config import cfg

logger = logging.getLogger(__name__)


def create_payment_link(lead_id: int, property_address: str) -> dict | None:
    """
    Creates a Stripe Checkout Session and returns {url, session_id}.
    The session ID is stored in the DB so the webhook can match payment → lead.
    """
    try:
        import stripe
    except ImportError:
        logger.error("[stripe] stripe package not installed")
        return None

    stripe.api_key = cfg.STRIPE_SECRET_KEY

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": cfg.STRIPE_PRICE_PER_LEAD,
                    "product_data": {
                        "name": "Real Estate Lead Report",
                        "description": f"Full buyer/seller lead package for: {property_address}",
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{cfg.BASE_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{cfg.BASE_URL}/payment/cancel",
            metadata={
                "lead_id": str(lead_id),
                "property_address": property_address,
            },
        )
        logger.info(f"[stripe] Payment link created for lead {lead_id}: {session.url}")
        return {"url": session.url, "session_id": session.id}

    except Exception as e:
        logger.error(f"[stripe] Error creating payment link for lead {lead_id}: {e}")
        return None
