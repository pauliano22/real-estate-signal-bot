"""
webhooks/app.py — Flask server for Stripe webhooks + email open/click tracking.

Run: python webhooks/app.py
Or with gunicorn: gunicorn webhooks.app:app

Endpoints:
  POST /stripe/webhook         — Stripe payment events
  POST /inbound/email          — Resend inbound email forwarding
  GET  /track/open/<token>     — Email open pixel
  GET  /track/click/<token>    — Email link redirect
  GET  /payment/success        — Post-payment landing page
  GET  /health                 — Health check
"""
import logging
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask, request, jsonify, redirect, Response
from config import cfg
from src.db import models
from src.fulfillment.deliver import deliver_lead
from webhooks.inbound import handle_inbound

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------

@app.post("/stripe/webhook")
def stripe_webhook():
    import stripe

    stripe.api_key = cfg.STRIPE_SECRET_KEY
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, cfg.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        logger.warning("[webhook] Invalid Stripe signature")
        return jsonify({"error": "invalid signature"}), 400
    except Exception as e:
        logger.error(f"[webhook] Webhook parse error: {e}")
        return jsonify({"error": str(e)}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session["id"]
        customer_email = session.get("customer_details", {}).get("email", "")

        logger.info(f"[webhook] Payment confirmed — session {session_id}")

        lead = models.get_lead_by_session(session_id)
        if not lead:
            logger.error(f"[webhook] No lead found for session {session_id}")
            return jsonify({"error": "lead not found"}), 404

        lead_id = lead["id"]
        models.mark_paid(lead_id, session_id)

        # Deliver the lead report to the buyer
        success = deliver_lead(lead)
        if success:
            models.mark_fulfilled(lead_id)
            logger.info(f"[webhook] Lead {lead_id} fulfilled — sent to {lead.get('agent_email')}")
        else:
            models.mark_error(lead_id, "fulfillment failed after payment")
            logger.error(f"[webhook] Fulfillment FAILED for lead {lead_id}")

    return jsonify({"received": True}), 200


# ---------------------------------------------------------------------------
# Inbound email
# ---------------------------------------------------------------------------

@app.post("/inbound/email")
def inbound_email():
    logger.info(f"[inbound] Headers: {request.headers}")
    logger.info(f"[inbound] Raw data: {request.get_data(as_text=True)}")
    payload = request.get_json(force=True, silent=True) or {}
    try:
        handle_inbound(payload)
    except Exception as e:
        logger.error(f"[inbound] Unhandled error: {e}")
    # Always return 200 so Resend doesn't retry
    return jsonify({"received": True}), 200


# ---------------------------------------------------------------------------
# Email tracking
# ---------------------------------------------------------------------------

TRANSPARENT_PIXEL = (
    b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00'
    b'\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00\x00'
    b'\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
)


@app.get("/track/open/<token>")
def track_open(token: str):
    from flask import request as req
    ip = req.remote_addr
    ua = req.headers.get("User-Agent", "")

    # Look up lead by token
    with models.get_conn() as conn:
        row = conn.execute(
            "SELECT lead_id FROM tracking_events WHERE tracking_token=? AND event_type='open'",
            (token,)
        ).fetchone()

    if row:
        lead_id = row["lead_id"]
        models.record_tracking_event(lead_id, "open", token, ip=ip, ua=ua)
        logger.info(f"[track] Open event for lead {lead_id}")

    return Response(TRANSPARENT_PIXEL, mimetype="image/gif")


@app.get("/track/click/<token>")
def track_click(token: str):
    from flask import request as req
    ip = req.remote_addr
    ua = req.headers.get("User-Agent", "")

    with models.get_conn() as conn:
        row = conn.execute(
            "SELECT lead_id, link_destination FROM tracking_events WHERE tracking_token=? AND event_type='click'",
            (token,)
        ).fetchone()

    if row:
        lead_id = row["lead_id"]
        destination = row["link_destination"] or cfg.BASE_URL
        models.record_tracking_event(lead_id, "click", token, destination=destination, ip=ip, ua=ua)
        logger.info(f"[track] Click event for lead {lead_id} → {destination}")
        return redirect(destination, code=302)

    return redirect(cfg.BASE_URL, code=302)


@app.get("/payment/success")
def payment_success():
    return "<h2>Payment received. Your lead report is on its way.</h2>", 200


@app.get("/payment/cancel")
def payment_cancel():
    return "<h2>No charge was made. Feel free to reach out with any questions.</h2>", 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_lead(lead_id: int) -> dict | None:
    with models.get_conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(row) if row else None


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
