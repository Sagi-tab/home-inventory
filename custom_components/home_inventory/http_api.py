"""HTTP API endpoints used by the Lovelace card."""
from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import API_BASE, DOMAIN, DEFAULT_EXPIRING_DAYS
from .lookup import BarcodeLookup
from .storage import Database

_LOGGER = logging.getLogger(__name__)


class _BaseView(HomeAssistantView):
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @property
    def db(self) -> Database:
        return self.hass.data[DOMAIN]["db"]

    @property
    def lookup(self) -> BarcodeLookup:
        return self.hass.data[DOMAIN]["lookup"]


class InventoryView(_BaseView):
    url = f"{API_BASE}/inventory"
    name = f"{DOMAIN}:inventory"

    async def get(self, request: web.Request) -> web.Response:
        items = await self.db.list_inventory()
        totals = await self.db.totals()
        return self.json({"items": items, "totals": totals})


class ShoppingView(_BaseView):
    url = f"{API_BASE}/shopping"
    name = f"{DOMAIN}:shopping"

    async def get(self, request: web.Request) -> web.Response:
        include_completed = request.query.get("completed") == "1"
        items = await self.db.list_shopping(include_completed=include_completed)
        return self.json({"items": items})


class HistoryView(_BaseView):
    url = f"{API_BASE}/history"
    name = f"{DOMAIN}:history"

    async def get(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 50))
        days = int(request.query.get("stats_days", 30))
        recent = await self.db.recent_history(limit)
        stats = await self.db.consumption_stats(days)
        return self.json({"recent": recent, "stats": stats})


class ProductSearchView(_BaseView):
    url = f"{API_BASE}/products"
    name = f"{DOMAIN}:products"

    async def get(self, request: web.Request) -> web.Response:
        query = request.query.get("q", "")
        limit = int(request.query.get("limit", 20))
        results = await self.db.search_products(query, limit)
        return self.json({"results": results})


class LookupView(_BaseView):
    url = f"{API_BASE}/lookup/{{barcode}}"
    name = f"{DOMAIN}:lookup"

    async def get(self, request: web.Request, barcode: str) -> web.Response:
        result = await self.lookup.lookup(barcode)
        return self.json({"product": result})


class SummaryView(_BaseView):
    url = f"{API_BASE}/summary"
    name = f"{DOMAIN}:summary"

    async def get(self, request: web.Request) -> web.Response:
        totals = await self.db.totals()
        expiring = await self.db.get_expiring_items(DEFAULT_EXPIRING_DAYS)
        low = await self.db.get_low_stock_items()
        return self.json({
            "totals": totals,
            "expiring": expiring,
            "low_stock": low,
        })


class SuggestView(_BaseView):
    """Return similar existing products for a barcode / name."""

    url = f"{API_BASE}/suggest"
    name = f"{DOMAIN}:suggest"

    async def get(self, request: web.Request) -> web.Response:
        barcode = request.query.get("barcode")
        name = request.query.get("name")
        name_he = request.query.get("name_he")
        category = request.query.get("category")
        brand = request.query.get("brand")

        # If a barcode is given and already resolves, return nothing —
        # the caller shouldn't be asking for suggestions on a known barcode.
        if barcode:
            existing = await self.db.get_product_by_barcode(barcode)
            if existing:
                return self.json({
                    "barcode": barcode,
                    "existing_product": existing,
                    "suggestions": [],
                })

        matches = await self.db.find_similar_products(
            name=name, name_he=name_he,
            category=category, brand=brand,
        )
        return self.json({"barcode": barcode, "suggestions": matches})


class ProductDetailView(_BaseView):
    """Product with all linked barcodes — used by expandable card rows."""

    url = f"{API_BASE}/product/{{product_id}}"
    name = f"{DOMAIN}:product_detail"

    async def get(self, request: web.Request, product_id: str) -> web.Response:
        try:
            pid = int(product_id)
        except ValueError:
            return self.json({"error": "invalid id"}, status_code=400)
        product = await self.db.get_product(pid)
        if not product:
            return self.json({"error": "not found"}, status_code=404)
        barcodes = await self.db.get_product_barcodes(pid)
        return self.json({"product": product, "barcodes": barcodes})


class MatchView(_BaseView):
    """Score-thresholded product match for shopping/inventory autocomplete.

    Used when the user types free text like 'lactose free milk' — returns
    products whose normalized similarity is above a threshold so the UI can
    auto-link the shopping entry to a real product (so any matching barcode
    fulfills it).
    """

    url = f"{API_BASE}/match"
    name = f"{DOMAIN}:match"

    async def get(self, request: web.Request) -> web.Response:
        name = request.query.get("name", "").strip()
        if not name:
            return self.json({"matches": []})
        # The find_similar_products score scale: each shared token = +2,
        # category match = +3, fuzzy ratio up to +3. A "strong" match in
        # practice means the same product type — typically 8+ for two-word
        # exact-match queries. Lower min_score to catch close-but-not-exact;
        # the caller decides what to surface.
        results = await self.db.find_similar_products(
            name=name, name_he=name,
            min_score=4.0, limit=5,
        )
        # Mark matches at or above 8.0 as "strong" so the UI can auto-link.
        for r in results:
            r["strong"] = bool(r.get("score", 0) >= 8.0)
        return self.json({"matches": results, "query": name})


def async_register_views(hass: HomeAssistant) -> None:
    for view_cls in (
        InventoryView, ShoppingView, HistoryView,
        ProductSearchView, LookupView, SummaryView,
        SuggestView, ProductDetailView, MatchView,
    ):
        hass.http.register_view(view_cls(hass))
