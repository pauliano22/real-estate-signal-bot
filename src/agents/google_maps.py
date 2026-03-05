"""
src/agents/google_maps.py — Find real estate agents near a zip code.

Uses Places API (New) Text Search endpoint with field masking to stay within
the $200/month free credit tier. Only Basic Data fields are requested:
  places.displayName, places.formattedAddress, places.id

Field masking is set via the X-Goog-FieldMask request header, which tells
Google to bill only for the fields returned — Basic Data is the cheapest tier.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
"""
import logging
import time
import httpx
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from config import cfg

logger = logging.getLogger(__name__)

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Only Basic Data fields — keeps every call within the free credit tier.
# Adding contact fields (nationalPhoneNumber, websiteUri) bumps to a higher billing tier.
FIELD_MASK = "places.displayName,places.formattedAddress,places.id"


def find_agents_in_zip(zip_code: str, max_results: int = 10) -> list[dict]:
    """
    Returns a list of agent dicts:
      {name, address, place_id, zip_code}

    Phone and website are omitted intentionally — they require Contact Data
    fields which incur additional cost. Apollo.io enrichment handles email
    discovery using name + company name alone.
    """
    results = []

    payload = {
        "textQuery": f"real estate agent {zip_code}",
        "maxResultCount": min(max_results, 20),  # API max is 20
        "locationBias": {
            "circle": {
                "center": {"latitude": 0, "longitude": 0},  # overridden by textQuery zip
                "radius": 8000.0,
            }
        },
    }

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": cfg.GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(PLACES_SEARCH_URL, json=payload, headers=headers)

        if response.status_code == 200:
            places = response.json().get("places", [])
            logger.info(f"[google_maps] {zip_code}: {len(places)} places returned")

            for place in places:
                name = place.get("displayName", {}).get("text", "").strip()
                address = place.get("formattedAddress", "").strip()
                place_id = place.get("id", "")

                if not name:
                    continue

                results.append({
                    "name": name,
                    "address": address,
                    "place_id": place_id,
                    "zip_code": zip_code,
                    # website/phone intentionally absent — see module docstring
                })
                logger.debug(f"[google_maps] Found: {name} — {address}")

        elif response.status_code == 403:
            logger.error(f"[google_maps] 403 Forbidden — check API key and Places API (New) is enabled in Google Cloud Console")
        elif response.status_code == 400:
            logger.error(f"[google_maps] 400 Bad Request: {response.text}")
        else:
            logger.error(f"[google_maps] Unexpected status {response.status_code} for zip {zip_code}")

    except Exception as e:
        logger.error(f"[google_maps] Error for zip {zip_code}: {e}")

    return results
