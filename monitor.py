"""
monitor.py — Autonomous orchestration loop.

Runs every 30 minutes via APScheduler. Never crashes silently.
Each cycle:
  1. Fetch signals from Redfin (fallback: Zillow stealth)
  2. Discover agents in signal zip codes via Google Maps
  3. Enrich agent emails via Hunter.io
  4. Draft hyper-relevant emails via Claude
  5. Send via SendGrid
  6. Mark stale leads that haven't engaged in 7 days

Run: python monitor.py
"""
import logging
import sys
import time
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from config import cfg
from src.db import models
from src.signals.redfin import RedfinAdapter
from src.signals.zillow_stealth import ZillowStealthAdapter
from src.agents.google_maps import find_agents_in_zip
from src.agents.enrichment import find_email
from src.matchmaker.scorer import score_signal
from src.matchmaker.drafter import draft_email
from src.outreach.sender import send_outreach

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("monitor")
self_heal_logger = logging.getLogger("self_heal")
self_heal_logger.addHandler(logging.FileHandler("logs/self_heal.log", encoding="utf-8"))

# ---------------------------------------------------------------------------
# Adapter state (for self-healing)
# ---------------------------------------------------------------------------
_primary_adapter = RedfinAdapter()
_fallback_adapter = ZillowStealthAdapter()
_adapter_failures: dict[str, int] = {}  # zip_code → consecutive failure count


def _get_adapter(zip_code: str):
    failures = _adapter_failures.get(zip_code, 0)
    if failures >= cfg.MAX_CONSECUTIVE_FAILURES:
        self_heal_logger.warning(
            f"[SELF_HEAL] zip={zip_code} failed {failures}x on Redfin — switching to Zillow stealth"
        )
        models.log_self_heal(
            source="redfin", zip_code=zip_code,
            error_type="MAX_FAILURES",
            error_detail=f"{failures} consecutive failures",
            action_taken="switched to zillow_stealth adapter",
        )
        return _fallback_adapter
    return _primary_adapter


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step_signals(zip_code: str, run_id: int) -> list:
    """Fetch property signals for a zip code. Returns list of PropertySignal."""
    adapter = _get_adapter(zip_code)
    try:
        signals = adapter.fetch_signals(zip_code)
        _adapter_failures[zip_code] = 0  # reset on success
        return signals
    except Exception as e:
        _adapter_failures[zip_code] = _adapter_failures.get(zip_code, 0) + 1
        count = _adapter_failures[zip_code]
        logger.error(f"[signals] {zip_code} — {adapter.name} failed (#{count}): {e}")
        models.log_self_heal(
            source=adapter.name, zip_code=zip_code,
            error_type=type(e).__name__,
            error_detail=str(e),
            action_taken=f"logged failure #{count}, will retry or switch at {cfg.MAX_CONSECUTIVE_FAILURES}",
        )
        return []


def step_upsert_signals(signals: list) -> list[int]:
    """Insert new signals into DB. Returns list of new lead IDs."""
    new_ids = []
    for signal in signals:
        lead_id = models.upsert_lead(
            property_address=signal.address,
            zip_code=signal.zip_code,
            signal_type=signal.signal_type,
            listing_url=signal.listing_url,
            list_price=signal.list_price,
            original_price=signal.original_price,
            price_drop_pct=signal.price_drop_pct,
            days_on_market=signal.days_on_market,
            signal_score=signal.signal_score,
        )
        if lead_id:
            new_ids.append(lead_id)
            logger.info(f"[upsert] New lead #{lead_id}: {signal.address} (score: {signal.signal_score})")
    return new_ids


def step_enrich(run_id: int) -> int:
    """Find agents for all FOUND leads. Returns count enriched."""
    found_leads = models.get_leads_in_state("FOUND")
    enriched = 0

    for lead in found_leads:
        zip_code = lead["zip_code"]
        try:
            agents = find_agents_in_zip(zip_code, max_results=5)
            if not agents:
                logger.info(f"[enrich] No agents found for zip {zip_code}")
                continue

            # Match the first agent with a findable email
            for agent in agents:
                email_data = find_email(agent["name"], agent.get("website", ""), agent.get("name"))
                if email_data and email_data["email"]:
                    models.enrich_lead(
                        lead_id=lead["id"],
                        agent_name=agent["name"],
                        agent_email=email_data["email"],
                        agent_phone=agent.get("phone"),
                        agent_company=agent.get("name"),
                        agent_website=agent.get("website"),
                        email_verified=email_data.get("verified", False),
                    )
                    enriched += 1
                    logger.info(f"[enrich] Lead #{lead['id']} → {agent['name']} <{email_data['email']}>")
                    break  # one agent per lead
            else:
                logger.info(f"[enrich] Lead #{lead['id']}: no verified email found for zip {zip_code}")

        except Exception as e:
            logger.error(f"[enrich] Error for lead #{lead['id']}: {e}")
            models.mark_error(lead["id"], f"enrichment error: {e}")

    return enriched


def step_draft_and_send(run_id: int) -> int:
    """Draft + send free outreach for all ENRICHED leads. Returns count sent."""
    enriched_leads = models.get_leads_in_state("ENRICHED")
    sent = 0

    for lead in enriched_leads:
        try:
            agent_name = lead.get("agent_name", "")
            first_name = agent_name.split()[0] if agent_name else "there"

            draft = draft_email(
                agent_first_name=first_name,
                property_address=lead["property_address"],
                zip_code=lead["zip_code"],
                days_on_market=lead.get("days_on_market"),
                price_drop_pct=lead.get("price_drop_pct"),
                list_price=lead.get("list_price"),
                signal_type=lead["signal_type"],
                sender_name=cfg.MAIL_FROM_NAME,
            )

            message_id = send_outreach(
                lead_id=lead["id"],
                to_email=lead["agent_email"],
                to_name=agent_name,
                subject=draft["subject"],
                body=draft["body"],
            )

            if message_id:
                models.mark_emailed(lead["id"], draft["subject"], draft["body"], message_id)
                models.mark_pending_review(lead["id"])
                sent += 1
                logger.info(f"[outreach] Sent to {lead['agent_email']} for lead #{lead['id']}")
                print(
                    f"\n{'='*60}\n"
                    f"  ACTION REQUIRED — Lead #{lead['id']} ready for review\n"
                    f"  Property : {lead['property_address']}\n"
                    f"  Agent    : {lead.get('agent_name')} <{lead['agent_email']}>\n"
                    f"  Signal   : {lead['signal_type']} | Score: {lead.get('signal_score')}/100\n"
                    f"  To fulfill: python scripts/mock_fulfill.py {lead['id']}\n"
                    f"{'='*60}\n"
                )
            else:
                logger.warning(f"[outreach] Send failed for lead #{lead['id']}")

        except Exception as e:
            logger.error(f"[outreach] Error for lead #{lead['id']}: {e}")
            models.mark_error(lead["id"], f"outreach error: {e}")

    return sent


def step_expire_stale() -> int:
    """Mark leads STALE if no engagement after 7 days."""
    candidates = models.get_stale_candidates(days_since_email=7)
    for lead in candidates:
        models.mark_stale(lead["id"])
        logger.info(f"[stale] Lead #{lead['id']} marked STALE — no engagement after 7 days")
    return len(candidates)


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

def run_cycle():
    run_id = models.start_run()
    logger.info(f"=== Cycle start — run #{run_id} ===")

    total_signals = 0
    total_enriched = 0
    total_sent = 0
    total_errors = 0

    active_zips = models.get_active_zips()
    if not active_zips:
        logger.warning("[cycle] No active zip codes. Run: python src/db/seed.py")
        models.finish_run(run_id, 0, 0, 0, 0, 0)
        return

    # --- Step 1: Collect signals ---
    all_signals = []
    for zip_row in active_zips:
        zip_code = zip_row["zip_code"]
        signals = step_signals(zip_code, run_id)
        all_signals.extend(signals)

    new_lead_ids = step_upsert_signals(all_signals)
    total_signals = len(new_lead_ids)
    logger.info(f"[cycle] {total_signals} new signals found")

    # --- Step 2: Enrich ---
    try:
        total_enriched = step_enrich(run_id)
        logger.info(f"[cycle] {total_enriched} leads enriched")
    except Exception as e:
        logger.error(f"[cycle] Enrichment step failed: {e}")
        total_errors += 1

    # --- Step 3: Draft + Send ---
    try:
        total_sent = step_draft_and_send(run_id)
        logger.info(f"[cycle] {total_sent} emails sent")
    except Exception as e:
        logger.error(f"[cycle] Outreach step failed: {e}")
        total_errors += 1

    # --- Step 4: Expire stale leads ---
    stale_count = step_expire_stale()
    if stale_count:
        logger.info(f"[cycle] {stale_count} leads marked stale")

    models.finish_run(run_id, total_signals, total_enriched, total_sent, 0, total_errors)
    logger.info(f"=== Cycle complete — run #{run_id} | signals={total_signals} enriched={total_enriched} sent={total_sent} errors={total_errors} ===\n")


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def on_job_event(event):
    if event.exception:
        logger.critical(f"[scheduler] Job crashed: {event.exception}")
        self_heal_logger.critical(
            f"[CRITICAL] Scheduler job crashed at {datetime.utcnow().isoformat()} — {event.exception}"
        )


if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    logger.info("Real Estate Signal Bot starting...")

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_listener(on_job_event, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)

    # Run immediately on start, then every N minutes
    scheduler.add_job(
        run_cycle,
        trigger="interval",
        minutes=cfg.MONITOR_INTERVAL_MINUTES,
        max_instances=1,           # never overlap runs
        next_run_time=datetime.utcnow(),  # run immediately on start
        id="signal_cycle",
        name="Real Estate Signal Cycle",
        misfire_grace_time=300,    # allow 5min late start before skipping
    )

    logger.info(f"Scheduler running — cycle every {cfg.MONITOR_INTERVAL_MINUTES} minutes")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Monitor stopped.")
