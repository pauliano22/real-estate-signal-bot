"""
src/agents/enrichment.py — Find agent email + LinkedIn via Apollo.io People Match API.

Apollo's /people/match endpoint takes a name + organization and returns a
full person record including verified email and LinkedIn URL.

Docs: https://apolloio.github.io/apollo-api-docs/#people-api
"""
import logging
import httpx
import re
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from config import cfg

logger = logging.getLogger(__name__)

APOLLO_MATCH_URL = "https://api.apollo.io/v1/people/match"


def get_domain(website: str) -> str | None:
    """Extract root domain from a URL."""
    if not website:
        return None
    website = website.lower().strip()
    if not website.startswith("http"):
        website = "https://" + website
    match = re.search(r"https?://(?:www\.)?([^/?\s]+)", website)
    return match.group(1) if match else None


def find_email(agent_name: str, website: str, company_name: str = None) -> dict | None:
    """
    Look up an agent via Apollo's People Match API.

    Returns:
        {email, verified, first_name, last_name, linkedin_url, apollo_id} or None.

    Apollo matches on name + organization_name and/or domain.
    The more context you provide, the better the match confidence.
    """
    parts = agent_name.strip().split()
    first_name = parts[0] if parts else ""
    last_name = parts[-1] if len(parts) > 1 else ""
    domain = get_domain(website)

    if not first_name or not last_name:
        logger.debug(f"[enrichment] Insufficient name data for '{agent_name}'")
        return None

    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "reveal_personal_emails": True,
    }

    # Give Apollo as much context as possible to narrow the match
    if company_name:
        payload["organization_name"] = company_name
    if domain:
        payload["domain"] = domain

    try:
        r = httpx.post(
            APOLLO_MATCH_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Api-Key": cfg.APOLLO_API_KEY,
            },
            timeout=15,
        )

        if r.status_code == 404:
            logger.debug(f"[enrichment] No Apollo match for {agent_name}")
            return None

        r.raise_for_status()
        person = r.json().get("person")

        if not person:
            logger.debug(f"[enrichment] Empty Apollo response for {agent_name}")
            return None

        # Apollo returns email in `email` field; personal emails in `personal_emails`
        email = person.get("email")
        if not email:
            personal = person.get("personal_emails", [])
            email = personal[0] if personal else None

        if not email:
            logger.debug(f"[enrichment] Apollo found {agent_name} but no email available")
            return None

        # Apollo email_status: "verified", "likely to engage", "unavailable", etc.
        email_status = person.get("email_status", "")
        verified = email_status in ("verified", "likely to engage")

        result = {
            "email": email,
            "verified": verified,
            "email_status": email_status,
            "first_name": person.get("first_name", first_name),
            "last_name": person.get("last_name", last_name),
            "linkedin_url": person.get("linkedin_url"),
            "apollo_id": person.get("id"),
            "title": person.get("title"),
        }

        logger.info(
            f"[enrichment] Apollo matched {agent_name} → {email} "
            f"(status: {email_status}, linkedin: {bool(result['linkedin_url'])})"
        )
        return result

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:
            logger.debug(f"[enrichment] Apollo 422 — insufficient data for {agent_name}")
        else:
            logger.error(f"[enrichment] Apollo HTTP error for {agent_name}: {e.response.status_code}")
        return None
    except Exception as e:
        logger.error(f"[enrichment] Unexpected error for {agent_name}: {e}")
        return None
