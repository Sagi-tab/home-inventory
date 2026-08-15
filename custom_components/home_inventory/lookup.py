"""Barcode lookup: local cache first, then Open Food Facts fallback."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import async_timeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CATEGORIES, OFF_API_URL, OFF_TIMEOUT, OFF_USER_AGENT
from .storage import Database

_LOGGER = logging.getLogger(__name__)


def is_israeli_barcode(barcode: str) -> bool:
    """EAN-13 country prefix 729 is Israel."""
    return len(barcode) == 13 and barcode.startswith("729")


# Keywords (lowercased) mapped to our enum categories. First match wins,
# so put more-specific contexts before ones that share keywords (e.g.
# beverages before dairy, since "milk-based drinks" mentions both).
_CATEGORY_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("beverage", "drink", "juice", "soda", "water", "coffee", "tea", "beer",
      "wine", "alcohol", "שתייה", "מים", "קפה", "תה", "יין", "בירה"), "beverages"),
    (("milk", "cheese", "yogurt", "yoghurt", "dairy", "dairie", "cream",
      "butter", "חלב", "גבינה", "יוגורט"), "dairy"),
    (("bread", "bakery", "bun", "roll", "pita", "baguette", "croissant",
      "לחם", "פיתה"), "bakery"),
    (("frozen", "ice cream", "popsicle", "קפוא", "גלידה"), "frozen"),
    (("meat", "fish", "beef", "chicken", "pork", "lamb", "salmon", "tuna",
      "seafood", "poultry", "בשר", "דג", "עוף"), "meat_fish"),
    (("fruit", "vegetable", "produce", "salad", "herb",
      "ירק", "פרי"), "produce"),
    (("snack", "chip", "chocolate", "candy", "cookie", "biscuit", "cracker",
      "cereal", "breakfast", "חטיף", "שוקולד", "עוגיה", "ממתק"), "snacks"),
    (("cleaning", "detergent", "bleach", "laundry", "dishwash",
      "ניקוי", "כביסה"), "cleaning"),
    (("shampoo", "toothpaste", "deodorant", "razor", "diaper",
      "personal care", "soap",
      "שמפו", "משחת שיניים", "דאודורנט", "חיתול", "סבון"), "personal_care"),
    (("pasta", "rice", "flour", "sugar", "oil", "sauce", "canned", "tin",
      "legume", "bean", "lentil", "spice", "condiment", "honey", "jam",
      "אורז", "פסטה", "קמח", "סוכר", "שמן"), "pantry"),
]


def normalize_category(raw: str | None) -> str | None:
    """Map a free-text category string to one of our enum categories.

    Returns None if no keyword matches, so callers can fall back to 'other'
    or leave the field empty.
    """
    if not raw:
        return None
    # If already one of our slugs, accept as-is
    if raw in CATEGORIES:
        return raw
    low = raw.lower()
    for keywords, target in _CATEGORY_KEYWORDS:
        if any(kw in low for kw in keywords):
            return target
    return None


class BarcodeLookup:
    """Hybrid resolver: local cache first, OFF fallback, else unknown."""

    def __init__(self, hass: HomeAssistant, db: Database) -> None:
        self.hass = hass
        self.db = db

    async def lookup(self, barcode: str) -> dict[str, Any]:
        """Return a product dict. 'found' key indicates local/remote/unknown."""
        barcode = barcode.strip()
        if not barcode:
            return {"found": "unknown", "barcode": barcode}

        # 1. Local cache (products table)
        local = await self.db.get_product_by_barcode(barcode)
        if local:
            local["found"] = "local"
            return local

        # 2. Open Food Facts
        off = await self._query_off(barcode)
        if off:
            product_id = await self.db.upsert_product(
                barcode=barcode,
                name=off.get("name") or f"Product {barcode}",
                name_he=off.get("name_he"),
                brand=off.get("brand"),
                category=off.get("category"),
                image_url=off.get("image_url"),
                source="openfoodfacts",
            )
            stored = await self.db.get_product(product_id)
            if stored:
                stored["found"] = "remote"
                return stored

        # 3. Unknown — let caller prompt user
        return {
            "found": "unknown",
            "barcode": barcode,
            "israeli": is_israeli_barcode(barcode),
        }

    async def _query_off(self, barcode: str) -> dict[str, Any] | None:
        session = async_get_clientsession(self.hass)
        url = OFF_API_URL.format(barcode=barcode)
        headers = {"User-Agent": OFF_USER_AGENT}
        try:
            async with async_timeout.timeout(OFF_TIMEOUT):
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        _LOGGER.debug("OFF returned %s for %s", resp.status, barcode)
                        return None
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("OFF lookup failed for %s: %s", barcode, err)
            return None

        if not data or data.get("status") != 1:
            return None

        product = data.get("product") or {}
        name = (
            product.get("product_name_he")
            or product.get("product_name_en")
            or product.get("product_name")
        )
        if not name:
            return None

        raw_category = (product.get("categories") or "").split(",")[0].strip() or None
        return {
            "name": name,
            "name_he": product.get("product_name_he"),
            "brand": (product.get("brands") or "").split(",")[0].strip() or None,
            "category": normalize_category(raw_category),
            "image_url": product.get("image_front_small_url")
                or product.get("image_url"),
        }
