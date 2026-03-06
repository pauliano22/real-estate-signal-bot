"""
scripts/respond_lead.py — Operator CLI to unblock PENDING_OPERATOR leads.

Usage:
  python scripts/respond_lead.py --list
  python scripts/respond_lead.py <lead_id> --approve
  python scripts/respond_lead.py <lead_id> --reply "custom reply text"
"""
import argparse
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import cfg
from src.db import models
from src.outreach.sender import send_reply


def cmd_list():
    leads = models.get_leads_in_state("PENDING_OPERATOR")
    if not leads:
        print("No leads in PENDING_OPERATOR state.")
        return

    print(f"\n{'ID':<6} {'Property':<45} {'Agent':<30} {'Updated'}")
    print("-" * 100)
    for lead in leads:
        print(
            f"{lead['id']:<6} "
            f"{(lead['property_address'] or '')[:44]:<45} "
            f"{(lead['agent_name'] or '')[:29]:<30} "
            f"{lead['last_updated']}"
        )
    print()


def cmd_approve(lead_id: int):
    """Send the most recent suggested draft (stored as last outbound operator message)."""
    lead = models.get_lead_by_id(lead_id)
    if not lead:
        print(f"Lead #{lead_id} not found.")
        sys.exit(1)
    if lead["state"] != "PENDING_OPERATOR":
        print(f"Lead #{lead_id} is in state '{lead['state']}', not PENDING_OPERATOR.")
        sys.exit(1)

    # Re-run the conversation handler to regenerate and send the draft
    thread = models.get_thread(lead_id)
    if not thread:
        print(f"No conversation thread for lead #{lead_id}.")
        sys.exit(1)

    from src.conversation.handler import handle_reply
    # Temporarily move back to NEGOTIATING so handler can act
    models.transition(lead_id, "NEGOTIATING", reason="operator approved — re-running handler")
    classification = handle_reply(lead, thread)
    print(f"Lead #{lead_id} handled. Classification: {classification}")


def cmd_reply(lead_id: int, text: str):
    """Send a custom reply and return the lead to NEGOTIATING."""
    lead = models.get_lead_by_id(lead_id)
    if not lead:
        print(f"Lead #{lead_id} not found.")
        sys.exit(1)
    if lead["state"] != "PENDING_OPERATOR":
        print(f"Lead #{lead_id} is in state '{lead['state']}', not PENDING_OPERATOR.")
        sys.exit(1)

    # Transition back to NEGOTIATING before sending
    models.transition(lead_id, "NEGOTIATING", reason="operator sent custom reply")

    subject = f"Re: {lead.get('email_subject', 'Your listing')}"
    sent = send_reply(
        lead_id=lead_id,
        to_email=lead["agent_email"],
        to_name=lead.get("agent_name", ""),
        subject=subject,
        body=text,
    )

    if sent:
        models.store_message(
            lead_id=lead_id,
            direction="outbound",
            from_email=cfg.MAIL_FROM,
            to_email=lead["agent_email"],
            subject=subject,
            body=text,
        )
        print(f"Reply sent to {lead['agent_email']} for lead #{lead_id}.")
    else:
        print(f"Send failed for lead #{lead_id}. Check logs.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Manage PENDING_OPERATOR leads")
    parser.add_argument("lead_id", nargs="?", type=int, help="Lead ID to act on")
    parser.add_argument("--list", action="store_true", help="List all pending leads")
    parser.add_argument("--approve", action="store_true", help="Approve and send suggested draft")
    parser.add_argument("--reply", metavar="TEXT", help="Send a custom reply")

    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.lead_id and args.approve:
        cmd_approve(args.lead_id)
    elif args.lead_id and args.reply:
        cmd_reply(args.lead_id, args.reply)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
