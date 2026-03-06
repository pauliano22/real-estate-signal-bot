"""
scripts/seed_prod_lead.py — Insert a test lead into the production Postgres database.

Requires DATABASE_URL in the environment. Run via Railway CLI:
    railway run python scripts/seed_prod_lead.py

Or locally with DATABASE_URL set:
    DATABASE_URL=postgresql://... python scripts/seed_prod_lead.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db import models

models.init_db()

lead_id = models.upsert_lead(
    property_address="123 Test Street, Miami, FL 33101",
    zip_code="33101",
    signal_type="price_drop",
    listing_url="https://example.com/listing/123",
    list_price=450000,
    original_price=475000,
    price_drop_pct=5.26,
    days_on_market=21,
    signal_score=0.85,
)

if lead_id is None:
    print("Lead already exists — skipping insert.")
    with models.get_conn() as conn:
        row = conn.execute(
            "SELECT id, state FROM leads WHERE property_address=?",
            ("123 Test Street, Miami, FL 33101",)
        ).fetchone()
        lead_id = row["id"]
        print(f"Existing lead id={lead_id}, state={row['state']}")
else:
    print(f"Inserted lead id={lead_id}")

# Enrich with your test email so inbound lookup by sender address works
models.enrich_lead(
    lead_id=lead_id,
    agent_name="Paul Test",
    agent_email="pauliano2005@gmail.com",
    email_verified=True,
)
print(f"Lead #{lead_id} enriched with agent_email=pauliano2005@gmail.com")

# Advance to EMAILED_FREE so the conversation handler will accept replies
models.mark_emailed(
    lead_id=lead_id,
    subject="We found a motivated seller in your area",
    body="Hi Paul, we spotted a price drop...",
    message_id="seed-message-id",
)
print(f"Lead #{lead_id} marked EMAILED_FREE — ready to receive replies.")
print(f"\nDone. Send your test email from pauliano2005@gmail.com to trigger the flow.")
