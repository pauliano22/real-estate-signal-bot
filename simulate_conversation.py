"""
simulate_conversation.py — Console stress test for the conversation handler.

Tests the AI's classification + drafting without a live domain or real emails.
All sends are intercepted (DRY_RUN_EMAIL must be set, or --dry flag used).

Usage:
  python simulate_conversation.py                    # interactive menu
  python simulate_conversation.py --scenario skeptic
  python simulate_conversation.py --scenario ready
  python simulate_conversation.py --scenario complex
  python simulate_conversation.py --scenario stop
"""
import argparse
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# Force dry run so no real emails are sent during simulation
os.environ.setdefault("DRY_RUN_EMAIL", "simulate@localhost")

from config import cfg
from src.conversation.handler import handle_reply

# ---------------------------------------------------------------------------
# Fake lead — no DB needed
# ---------------------------------------------------------------------------
FAKE_LEAD = {
    "id": 9999,
    "property_address": "1234 Coral Way, Cape Coral, FL 33904",
    "zip_code": "33904",
    "signal_type": "price_drop",
    "signal_score": 82,
    "list_price": 389000,
    "original_price": 415000,
    "price_drop_pct": 6.3,
    "days_on_market": 47,
    "agent_name": "Sandra Reeves",
    "agent_email": "sandra@caprealty.example.com",
    "email_subject": "Quick note on 1234 Coral Way — 47 days on market",
    "state": "EMAILED_FREE",
}

SCENARIOS = {
    "skeptic": {
        "label": "Skeptical Agent",
        "description": "Agent pushes back on value, calls reports 'junk'",
        "inbound": (
            "I've seen these reports before and they're usually just recycled MLS data. "
            "Why would I pay $49 for something I can pull myself?"
        ),
    },
    "ready": {
        "label": "Ready to Buy",
        "description": "Agent clearly wants the report and asks how to pay",
        "inbound": "Okay, this actually looks interesting. How do I get the full report?",
    },
    "complex": {
        "label": "Complex / Negotiation",
        "description": "Agent wants to negotiate price and asks unusual questions",
        "inbound": (
            "Can you do $20 instead? Also, does this include rental comps? "
            "My broker wants to know if there are legal restrictions on using this data "
            "in listing presentations. Also what's your refund policy?"
        ),
    },
    "stop": {
        "label": "Stop / Unsubscribe",
        "description": "Agent asks to be removed from contact",
        "inbound": "Please remove me from your list. Not interested.",
    },
    "followup": {
        "label": "Simple Follow-up",
        "description": "Agent asks a straightforward market question",
        "inbound": (
            "Thanks for reaching out. How many similar properties have sold "
            "in this zip in the last 90 days?"
        ),
    },
}


def _build_thread(inbound_text: str) -> list[dict]:
    """Minimal thread: one outbound (our initial email) + one inbound (agent reply)."""
    return [
        {
            "direction": "outbound",
            "from_email": cfg.MAIL_FROM,
            "to_email": FAKE_LEAD["agent_email"],
            "subject": FAKE_LEAD["email_subject"],
            "body": (
                f"Hi Sandra,\n\n"
                f"I noticed 1234 Coral Way has been on the market for 47 days with a recent "
                f"price reduction. I have detailed buyer-activity data for the 33904 zip that "
                f"could help you reposition the listing. Happy to share the highlights.\n\n"
                f"Best,\n{cfg.MAIL_FROM_NAME}"
            ),
        },
        {
            "direction": "inbound",
            "from_email": FAKE_LEAD["agent_email"],
            "to_email": cfg.MAIL_FROM,
            "subject": f"Re: {FAKE_LEAD['email_subject']}",
            "body": inbound_text,
        },
    ]


def _mock_handle_reply(lead: dict, thread: list[dict]) -> dict:
    """
    Call handle_reply but intercept the send calls so we can print results
    without actually emailing. Returns the raw Claude JSON.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

    from src.conversation.handler import _SYSTEM_PROMPT, _build_thread_text

    thread_text = _build_thread_text(thread)
    lead_context = (
        f"Property: {lead.get('property_address')}\n"
        f"Signal: {lead.get('signal_type')} | Score: {lead.get('signal_score')}\n"
        f"List price: ${lead.get('list_price', 0):,.0f} | "
        f"Days on market: {lead.get('days_on_market', 'unknown')}\n"
        f"Agent: {lead.get('agent_name')} <{lead.get('agent_email')}>"
    )

    user_message = (
        f"Lead context:\n{lead_context}\n\n"
        f"Conversation thread:\n{thread_text}\n\n"
        "Classify the latest agent reply and draft our response."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def run_scenario(key: str):
    scenario = SCENARIOS[key]
    print(f"\n{'='*65}")
    print(f"  SCENARIO: {scenario['label']}")
    print(f"  {scenario['description']}")
    print(f"{'='*65}")
    print(f"\nAgent says:\n  \"{scenario['inbound']}\"\n")

    thread = _build_thread(scenario["inbound"])
    result = _mock_handle_reply(FAKE_LEAD, thread)

    classification = result.get("classification", "?")
    draft = result.get("draft_reply", "")
    reason = result.get("reason", "")

    STATE_OUTCOME = {
        "SIMPLE":        "Lead -> NEGOTIATING, reply sent",
        "PAYMENT_READY": "Lead -> INVOICED, Stripe link sent",
        "COMPLEX":       "Lead -> PENDING_OPERATOR, operator emailed",
        "STOP":          "Lead -> STALE, no further contact",
    }

    print(f"Classification : {classification}")
    print(f"State outcome  : {STATE_OUTCOME.get(classification, 'unknown')}")
    print(f"Reason         : {reason}")
    print(f"\nDraft reply:\n{'-'*40}")
    print(draft)
    print(f"{'-'*40}\n")

    # Validation assertions
    ok = True
    if key == "skeptic" and classification not in ("SIMPLE", "COMPLEX"):
        print(f"  WARN: Expected SIMPLE or COMPLEX, got {classification}")
        ok = False
    if key == "ready" and classification != "PAYMENT_READY":
        print(f"  WARN: Expected PAYMENT_READY, got {classification}")
        ok = False
    if key == "stop" and classification != "STOP":
        print(f"  WARN: Expected STOP, got {classification}")
        ok = False

    status = "PASS" if ok else "FAIL"
    print(f"  Result: {status}")
    return ok


def interactive_menu():
    print("\nConversation Handler Simulator")
    print("=" * 40)
    for i, (key, s) in enumerate(SCENARIOS.items(), 1):
        print(f"  {i}. {s['label']} — {s['description']}")
    print(f"  {len(SCENARIOS)+1}. Run ALL scenarios")
    print(f"  0. Exit")

    choice = input("\nSelect: ").strip()
    keys = list(SCENARIOS.keys())

    if choice == "0":
        return
    elif choice == str(len(SCENARIOS) + 1):
        results = [run_scenario(k) for k in keys]
        passed = sum(results)
        print(f"\nSummary: {passed}/{len(results)} scenarios passed")
    else:
        try:
            idx = int(choice) - 1
            run_scenario(keys[idx])
        except (ValueError, IndexError):
            print("Invalid choice.")


def main():
    parser = argparse.ArgumentParser(description="Simulate conversation handler scenarios")
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        help="Run a specific scenario",
    )
    args = parser.parse_args()

    if args.scenario:
        run_scenario(args.scenario)
    else:
        interactive_menu()


if __name__ == "__main__":
    main()
