"""
src/db/models.py — SQLite state machine.

All DB access goes through this module. State transitions are enforced here —
nothing outside this file should write state directly.
"""
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Optional
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from config import cfg

# Valid one-way transitions
VALID_TRANSITIONS = {
    "FOUND":                  {"ENRICHED", "STALE", "ERROR"},
    "ENRICHED":               {"EMAILED_FREE", "STALE", "ERROR"},
    "EMAILED_FREE":           {"PENDING_MANUAL_REVIEW", "STALE", "ERROR"},
    "PENDING_MANUAL_REVIEW":  {"FULFILLED", "STALE", "ERROR"},
    # Stripe path — kept for later, not active in current flow
    "REPLIED":                {"INVOICED", "ERROR"},
    "INVOICED":               {"PAID", "ERROR"},
    "PAID":                   {"FULFILLED", "ERROR"},
    "FULFILLED":              set(),
    "STALE":                  set(),
    "ERROR":                  {"FOUND"},  # allows manual retry
}


@contextmanager
def get_conn():
    conn = sqlite3.connect(cfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def upsert_lead(
    property_address: str,
    zip_code: str,
    signal_type: str,
    listing_url: str = None,
    list_price: float = None,
    original_price: float = None,
    price_drop_pct: float = None,
    days_on_market: int = None,
    signal_score: float = 0,
) -> Optional[int]:
    """
    Insert a new lead or skip if already exists (same address — any state).
    Returns the lead id, or None if it was a duplicate skip.
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, state FROM leads WHERE property_address = ?",
            (property_address,)
        ).fetchone()

        if existing:
            return None  # already tracked

        cur = conn.execute("""
            INSERT INTO leads
                (property_address, zip_code, signal_type, listing_url,
                 list_price, original_price, price_drop_pct, days_on_market,
                 signal_score, state, found_at, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FOUND', ?, ?)
        """, (
            property_address, zip_code, signal_type, listing_url,
            list_price, original_price, price_drop_pct, days_on_market,
            signal_score, now(), now()
        ))
        lead_id = cur.lastrowid
        _log_transition(conn, lead_id, None, "FOUND", "initial discovery")
        return lead_id


def enrich_lead(
    lead_id: int,
    agent_name: str,
    agent_email: str,
    agent_phone: str = None,
    agent_company: str = None,
    agent_website: str = None,
    email_verified: bool = False,
):
    transition(lead_id, "ENRICHED", reason="agent info found")
    with get_conn() as conn:
        conn.execute("""
            UPDATE leads SET
                agent_name=?, agent_email=?, agent_phone=?,
                agent_company=?, agent_website=?, email_verified=?,
                enriched_at=?, last_updated=?
            WHERE id=?
        """, (
            agent_name, agent_email, agent_phone,
            agent_company, agent_website, int(email_verified),
            now(), now(), lead_id
        ))


def mark_emailed(lead_id: int, subject: str, body: str, message_id: str):
    transition(lead_id, "EMAILED_FREE", reason="outreach sent")
    with get_conn() as conn:
        conn.execute("""
            UPDATE leads SET
                email_subject=?, email_body=?, sendgrid_message_id=?,  -- stores Resend email ID
                emailed_at=?, last_updated=?
            WHERE id=?
        """, (subject, body, message_id, now(), now(), lead_id))


def mark_replied(lead_id: int):
    transition(lead_id, "REPLIED", reason="engagement detected")
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET replied_at=?, last_updated=? WHERE id=?",
            (now(), now(), lead_id)
        )


def mark_invoiced(lead_id: int, payment_link: str, session_id: str):
    transition(lead_id, "INVOICED", reason="stripe payment link sent")
    with get_conn() as conn:
        conn.execute("""
            UPDATE leads SET
                stripe_payment_link=?, stripe_session_id=?,
                invoiced_at=?, last_updated=?
            WHERE id=?
        """, (payment_link, session_id, now(), now(), lead_id))


def mark_paid(lead_id: int, session_id: str):
    transition(lead_id, "PAID", reason="stripe payment confirmed")
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET paid_at=?, last_updated=? WHERE id=?",
            (now(), now(), lead_id)
        )


def mark_fulfilled(lead_id: int):
    transition(lead_id, "FULFILLED", reason="lead package delivered to buyer")
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET fulfilled_at=?, last_updated=? WHERE id=?",
            (now(), now(), lead_id)
        )


def mark_pending_review(lead_id: int):
    transition(lead_id, "PENDING_MANUAL_REVIEW", reason="free email sent — awaiting operator review")


def mark_stale(lead_id: int, reason: str = "no engagement after 7 days"):
    _force_state(lead_id, "STALE", reason)


def mark_error(lead_id: int, reason: str):
    _force_state(lead_id, "ERROR", reason)


# ---------------------------------------------------------------------------
# State machine enforcement
# ---------------------------------------------------------------------------

def transition(lead_id: int, to_state: str, reason: str = ""):
    with get_conn() as conn:
        row = conn.execute("SELECT state FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not row:
            raise ValueError(f"Lead {lead_id} not found")
        from_state = row["state"]
        if to_state not in VALID_TRANSITIONS.get(from_state, set()):
            raise ValueError(
                f"Invalid transition: {from_state} → {to_state} for lead {lead_id}"
            )
        conn.execute(
            "UPDATE leads SET state=?, last_updated=? WHERE id=?",
            (to_state, now(), lead_id)
        )
        _log_transition(conn, lead_id, from_state, to_state, reason)


def _force_state(lead_id: int, to_state: str, reason: str):
    """Bypass transition rules — for STALE and ERROR only."""
    with get_conn() as conn:
        row = conn.execute("SELECT state FROM leads WHERE id=?", (lead_id,)).fetchone()
        from_state = row["state"] if row else None
        conn.execute(
            "UPDATE leads SET state=?, last_updated=? WHERE id=?",
            (to_state, now(), lead_id)
        )
        _log_transition(conn, lead_id, from_state, to_state, reason)


def _log_transition(conn, lead_id, from_state, to_state, reason):
    conn.execute("""
        INSERT INTO state_transitions (lead_id, from_state, to_state, reason, transitioned_at)
        VALUES (?, ?, ?, ?, ?)
    """, (lead_id, from_state, to_state, reason, now()))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_lead_by_id(lead_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(row) if row else None


def get_leads_in_state(state: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE state=? ORDER BY signal_score DESC",
            (state,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_lead_by_session(session_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE stripe_session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None


def get_stale_candidates(days_since_email: int = 7) -> list[dict]:
    """Leads in PENDING_MANUAL_REVIEW > N days with no operator action."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM leads
            WHERE state='PENDING_MANUAL_REVIEW'
              AND emailed_at IS NOT NULL
              AND (julianday('now') - julianday(emailed_at)) > ?
        """, (days_since_email,)).fetchall()
        return [dict(r) for r in rows]


def record_tracking_event(lead_id: int, event_type: str, token: str,
                           destination: str = None, ip: str = None, ua: str = None):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO tracking_events
                (lead_id, event_type, tracking_token, link_destination,
                 triggered_at, ip_address, user_agent, logged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (lead_id, event_type, token, destination, now(), ip, ua, now()))
        # Update lead flags
        if event_type == "open":
            conn.execute(
                "UPDATE leads SET email_opened=1, last_updated=? WHERE id=?",
                (now(), lead_id)
            )
        elif event_type == "click":
            conn.execute(
                "UPDATE leads SET link_clicked=1, last_updated=? WHERE id=?",
                (now(), lead_id)
            )


# ---------------------------------------------------------------------------
# Target zip codes
# ---------------------------------------------------------------------------

def get_active_zips() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM target_zips WHERE active=1"
        ).fetchall()
        return [dict(r) for r in rows]


def add_zip(zip_code: str, city: str = None, state_abbr: str = None):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO target_zips (zip_code, city, state, added_at)
            VALUES (?, ?, ?, ?)
        """, (zip_code, city, state_abbr, now()))


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

def start_run() -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO run_log (run_started_at, status) VALUES (?, 'running')",
            (now(),)
        )
        return cur.lastrowid


def finish_run(run_id: int, signals: int, enriched: int,
               emails: int, payments: int, errors: int):
    with get_conn() as conn:
        conn.execute("""
            UPDATE run_log SET
                run_finished_at=?, signals_found=?, agents_enriched=?,
                emails_sent=?, payments_received=?, errors=?, status='done'
            WHERE id=?
        """, (now(), signals, enriched, emails, payments, errors, run_id))


# ---------------------------------------------------------------------------
# Self-heal log
# ---------------------------------------------------------------------------

def log_self_heal(source: str, zip_code: str, error_type: str,
                  error_detail: str, action_taken: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO self_heal_log
                (source, zip_code, error_type, error_detail, action_taken, logged_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (source, zip_code, error_type, error_detail, action_taken, now()))
