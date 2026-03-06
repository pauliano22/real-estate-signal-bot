"""
monitor.py — Autonomous orchestration loop.

Runs every 30 minutes via APScheduler. Never crashes silently.
Each cycle:
  1. Fetch signals from RentCast API
  2. If agent data missing from listing, discover via Google Maps + Apollo
  3. Draft hyper-relevant emails via Claude
  4. Send via Resend
  5. Mark stale leads that haven't engaged in 7 days

Run: python monitor.py
Run one cycle: python monitor.py --once
"""
import logging
import sys
import time
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from config import cfg
from src.db import models
from src.signals.rentcast import RentCastAdapter
from src.agents.google_maps import find_agents_in_zip
from src.agents.enrichment import find_email, ApolloAuthError
from src.matchmaker.drafter import draft_email
from src.outreach.sender import send_outreach

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("monitor")
self_heal_logger = logging.getLogger("self_heal")
self_heal_logger.addHandler(logging.FileHandler("logs/self_heal.log", encoding="utf-8"))

# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
_adapter = RentCastAdapter(api_key=cfg.RENTCAST_API_KEY)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step_signals(zip_code: str, run_id: int) -> list:
    """Fetch property signals for a zip code. Returns list of PropertySignal."""
    try:
        signals = _adapter.fetch_signals(zip_code)
        return signals
    except Exception as e:
        logger.error(f"[signals] {zip_code} — rentcast failed: {e}")
        models.log_self_heal(
            source="rentcast", zip_code=zip_code,
            error_type=type(e).__name__,
            error_detail=str(e),
            action_taken="logged failure, will retry next cycle",
        )
        return []


def step_upsert_signals(signals: list) -> list[int]:
    """
    Insert new signals into DB.
    If a signal already carries agent data from RentCast, immediately
    advance it to ENRICHED so the outreach step picks it up this cycle.
    Returns list of new lead IDs.
    """
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
        if not lead_id:
            continue

        new_ids.append(lead_id)

        # RentCast includes agent contact — skip Google Maps + Apollo
        if signal.agent_email:
            models.enrich_lead(
                lead_id=lead_id,
                agent_name=signal.agent_name or "",
                agent_email=signal.agent_email,
                agent_phone=signal.agent_phone,
                agent_website=signal.agent_website,
                email_verified=True,
            )
            logger.info(
                f"[upsert] Lead #{lead_id}: {signal.address} "
                f"(score: {signal.signal_score}) -> auto-enriched: {signal.agent_email}"
            )
        else:
            logger.info(
                f"[upsert] Lead #{lead_id}: {signal.address} "
                f"(score: {signal.signal_score}) -> needs enrichment"
            )

    return new_ids


def step_enrich(run_id: int) -> int:
    """
    Find agents for FOUND leads that RentCast didn't include contact for (~5%).
    Falls back to Google Maps + Apollo enrichment.
    Bails immediately on Apollo auth errors (403) — no point hammering a
    broken key across all remaining leads.
    """
    found_leads = models.get_leads_in_state("FOUND")
    if not found_leads:
        return 0

    enriched = 0

    for lead in found_leads:
        zip_code = lead["zip_code"]
        try:
            agents = find_agents_in_zip(zip_code, max_results=5)
            if not agents:
                logger.info(f"[enrich] No agents found for zip {zip_code}")
                continue

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
                    logger.info(f"[enrich] Lead #{lead['id']} -> {agent['name']} <{email_data['email']}>")
                    break
            else:
                logger.info(f"[enrich] Lead #{lead['id']}: no email found for zip {zip_code}")

        except ApolloAuthError:
            logger.warning(
                f"[enrich] Apollo auth error — skipping remaining FOUND leads this cycle. "
                f"Check APOLLO_API_KEY in .env."
            )
            break
        except Exception as e:
            logger.error(f"[enrich] Error for lead #{lead['id']}: {e}")
            models.mark_error(lead["id"], f"enrichment error: {e}")

    return enriched


def step_draft_and_send(run_id: int, dry_run_limit: int = 0) -> int:
    """
    Draft + send free outreach for all ENRICHED leads.
    dry_run_limit: if > 0, stop after this many sends (use 1 for dry run).
    Returns count sent.
    """
    enriched_leads = models.get_leads_in_state("ENRICHED")
    sent = 0

    attempted = 0
    for lead in enriched_leads:
        if dry_run_limit and attempted >= dry_run_limit:
            logger.info(f"[outreach] Dry run limit of {dry_run_limit} reached — stopping.")
            break

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

            # Always print the draft so it can be reviewed before it goes out
            safe_subject = draft['subject'].encode('ascii', errors='replace').decode('ascii')
            safe_body = draft['body'].encode('ascii', errors='replace').decode('ascii')
            print(
                f"\n{'-'*60}\n"
                f"  DRAFT EMAIL - Lead #{lead['id']}\n"
                f"{'-'*60}\n"
                f"  To      : {lead['agent_email']}\n"
                f"  Subject : {safe_subject}\n"
                f"\n{safe_body}\n"
                f"{'-'*60}\n"
            )

            message_id = send_outreach(
                lead_id=lead["id"],
                to_email=lead["agent_email"],
                to_name=agent_name,
                subject=draft["subject"],
                body=draft["body"],
            )

            attempted += 1
            if message_id:
                models.mark_emailed(lead["id"], draft["subject"], draft["body"], message_id)
                sent += 1
                logger.info(f"[outreach] Sent to {lead['agent_email']} for lead #{lead['id']}")
                safe_addr = lead['property_address'].encode('ascii', errors='replace').decode('ascii')
                safe_name = (lead.get('agent_name') or '').encode('ascii', errors='replace').decode('ascii')
                print(
                    f"\n{'='*60}\n"
                    f"  EMAIL SENT — Lead #{lead['id']} in EMAILED_FREE\n"
                    f"  Property : {safe_addr}\n"
                    f"  Agent    : {safe_name} <{lead['agent_email']}>\n"
                    f"  Signal   : {lead['signal_type']} | Score: {lead.get('signal_score')}/100\n"
                    f"  Waiting for agent reply → conversation AI takes over\n"
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

def run_cycle(once: bool = False):
    run_id = models.start_run()
    logger.info(f"=== Cycle start — run #{run_id} {'[DRY RUN — ONCE]' if once else ''} ===")

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
        total_sent = step_draft_and_send(run_id, dry_run_limit=1 if once else 0)
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

    # --once: run a single cycle and exit (used for dry runs and testing)
    if "--once" in sys.argv:
        if cfg.DRY_RUN_EMAIL:
            print(f"\n*** DRY RUN MODE — all emails redirected to {cfg.DRY_RUN_EMAIL} ***\n")
        logger.info("Running single cycle (--once mode)...")
        run_cycle(once=True)
        logger.info("Single cycle complete. Exiting.")
        sys.exit(0)

    # Normal scheduler mode
    logger.info("Real Estate Signal Bot starting...")
    if cfg.DRY_RUN_EMAIL:
        logger.warning(f"*** DRY RUN MODE — all emails redirected to {cfg.DRY_RUN_EMAIL} ***")

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_listener(on_job_event, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)

    scheduler.add_job(
        run_cycle,
        trigger="interval",
        minutes=cfg.MONITOR_INTERVAL_MINUTES,
        max_instances=1,
        next_run_time=datetime.utcnow(),
        id="signal_cycle",
        name="Real Estate Signal Cycle",
        misfire_grace_time=300,
    )

    logger.info(f"Scheduler running — cycle every {cfg.MONITOR_INTERVAL_MINUTES} minutes")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Monitor stopped.")
