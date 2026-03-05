"""
src/db/seed.py — Seed target zip codes and run once to initialize.
Edit the ZIP_TARGETS list to focus on your markets.
Run: python src/db/seed.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from src.db.models import add_zip, get_active_zips

# Add your target zip codes here
ZIP_TARGETS = [
    # (zip_code, city, state)
    ("90210", "Beverly Hills", "CA"),
    ("10001", "New York", "NY"),
    ("77002", "Houston", "TX"),
    ("60601", "Chicago", "IL"),
    ("33101", "Miami", "FL"),
]


if __name__ == "__main__":
    for zip_code, city, state in ZIP_TARGETS:
        add_zip(zip_code, city, state)
        print(f"  Added {zip_code} — {city}, {state}")

    active = get_active_zips()
    print(f"\nTotal active zip codes: {len(active)}")
