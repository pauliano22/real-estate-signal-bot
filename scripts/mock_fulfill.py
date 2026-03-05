"""
scripts/mock_fulfill.py — Manually trigger fulfillment for a lead.

Simulates a successful payment and sends the full lead report to the agent.
Use this to test the fulfillment email without Stripe.

Usage:
    python scripts/mock_fulfill.py <lead_id>
    python scripts/mock_fulfill.py --list        # show all leads pending review
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db import models
from src.fulfillment.deliver import deliver_lead


def list_pending():
    leads = models.get_leads_in_state("PENDING_MANUAL_REVIEW")
    if not leads:
        print("No leads are currently pending review.")
        return
    print(f"\n{'ID':<5} {'Score':<7} {'Signal':<16} {'Agent Email':<30} {'Property'}")
    print("-" * 90)
    for lead in leads:
        print(
            f"{lead['id']:<5} "
            f"{lead.get('signal_score', 0):<7} "
            f"{lead.get('signal_type', ''):<16} "
            f"{lead.get('agent_email', ''):<30} "
            f"{lead.get('property_address', '')}"
        )
    print()


def fulfill(lead_id: int):
    lead = models.get_lead_by_id(lead_id)

    if not lead:
        print(f"Error: lead #{lead_id} not found.")
        sys.exit(1)

    if lead["state"] != "PENDING_MANUAL_REVIEW":
        print(f"Error: lead #{lead_id} is in state '{lead['state']}', expected PENDING_MANUAL_REVIEW.")
        sys.exit(1)

    print(f"\nMocking fulfillment for lead #{lead_id}:")
    print(f"  Property : {lead['property_address']}")
    print(f"  Agent    : {lead.get('agent_name')} <{lead.get('agent_email')}>")
    print(f"  Signal   : {lead.get('signal_type')} | Score: {lead.get('signal_score')}/100")
    print(f"\nSending lead report to {lead.get('agent_email')}...")

    success = deliver_lead(lead)

    if success:
        models.mark_fulfilled(lead_id)
        print(f"Done. Lead #{lead_id} marked as FULFILLED.")
    else:
        print(f"Failed to send fulfillment email. Lead remains in PENDING_MANUAL_REVIEW.")
        print("Check your Resend API key and MAIL_FROM address.")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1]

    if arg == "--list":
        list_pending()
    else:
        try:
            fulfill(int(arg))
        except ValueError:
            print(f"Error: '{arg}' is not a valid lead ID. Pass an integer or --list.")
            sys.exit(1)
