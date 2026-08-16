"""Expose the inventory to a conversation agent as an LLM tool API.

The agent selected for WhatsApp needs to *act*, not just match the fixed
sentences in ``sentences_en.yaml`` / ``sentences_he.yaml``. Each tool here is a
thin wrapper over an already-registered service in ``services.py`` (so product
matching, upsert and shopping/inventory routing stay in one place) or over a
read-only ``Database`` query.

Select "Home Inventory" as the agent's control API in the conversation
integration's options for these tools to become available.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from .const import (
    CATEGORIES,
    DEFAULT_EXPIRING_DAYS,
    DOMAIN,
    LLM_API_ID,
    LLM_API_NAME,
    LOCATIONS,
    SERVICE_ADD_ITEM,
    SERVICE_ADD_TO_SHOPPING,
    SERVICE_REMOVE_ITEM,
    SERVICE_SET_THRESHOLD,
)
from .storage import Database

_LOGGER = logging.getLogger(__name__)

API_PROMPT = """You manage the user's home inventory (food, cleaning and \
personal-care stock) and their shopping list.

Guidelines:
- Use the tools for every add/remove/change; never claim a change you did not make.
- Items are identified by product_id. Call search_inventory first to resolve a \
name to a product_id before removing or changing an item.
- Expiration dates are ISO YYYY-MM-DD. Convert relative dates ("Friday", \
"in two weeks") using today's date, given below.
- Locations: {locations}.
- Categories: {categories}.
- Keep replies short and plain-text: they are delivered over WhatsApp, which \
has no markdown rendering.
"""


def _service_response(result: Any) -> dict:
    """Normalise a service call response into a JSON-serialisable dict."""
    if isinstance(result, dict):
        return result
    return {"success": True}


class _InventoryTool(llm.Tool):
    """Base for tools that need hass/db access."""

    @staticmethod
    def _db(hass: HomeAssistant) -> Database:
        return hass.data[DOMAIN]["db"]


class AddItemTool(_InventoryTool):
    """Add to inventory or the shopping list."""

    name = "add_item"
    description = (
        "Add an item to the inventory (things the user has at home) or to the "
        "shopping list (things to buy). Creates the product if it is new."
    )
    parameters = vol.Schema(
        {
            vol.Required("name"): str,
            vol.Optional("quantity"): vol.Coerce(float),
            vol.Optional("unit"): str,
            vol.Optional("category"): vol.In(CATEGORIES),
            vol.Optional("location"): vol.In(LOCATIONS),
            vol.Optional("expiration_date"): str,
            vol.Optional("notes"): str,
            vol.Optional("target"): vol.In(["inventory", "shopping"]),
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        data = dict(tool_input.tool_args)
        data.setdefault("quantity", 1)
        data.setdefault("target", "inventory")
        result = await hass.services.async_call(
            DOMAIN, SERVICE_ADD_ITEM, data, blocking=True, return_response=True
        )
        return _service_response(result)


class RemoveItemTool(_InventoryTool):
    """Consume/remove quantity from inventory."""

    name = "remove_item"
    description = (
        "Remove a quantity of a product from the inventory, e.g. when it has "
        "been used up. Requires product_id - resolve it with search_inventory."
    )
    parameters = vol.Schema(
        {
            vol.Required("product_id"): vol.Coerce(int),
            vol.Optional("quantity"): vol.Coerce(float),
            vol.Optional("location"): vol.In(LOCATIONS),
            vol.Optional("add_to_shopping_list"): bool,
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        data = dict(tool_input.tool_args)
        data.setdefault("quantity", 1)
        result = await hass.services.async_call(
            DOMAIN, SERVICE_REMOVE_ITEM, data, blocking=True, return_response=True
        )
        return _service_response(result)


class AddToShoppingTool(_InventoryTool):
    """Add directly to the shopping list."""

    name = "add_to_shopping_list"
    description = "Add an item to the shopping list."
    parameters = vol.Schema(
        {
            vol.Required("name"): str,
            vol.Optional("quantity"): vol.Coerce(float),
            vol.Optional("category"): vol.In(CATEGORIES),
            vol.Optional("notes"): str,
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        data = dict(tool_input.tool_args)
        data.setdefault("quantity", 1)
        result = await hass.services.async_call(
            DOMAIN, SERVICE_ADD_TO_SHOPPING, data, blocking=True, return_response=True
        )
        return _service_response(result)


class SetThresholdTool(_InventoryTool):
    """Set the low-stock threshold for a product."""

    name = "set_low_stock_threshold"
    description = (
        "Set the low-stock threshold for a product, so it is flagged when the "
        "quantity drops to or below this number."
    )
    parameters = vol.Schema(
        {
            vol.Required("product_id"): vol.Coerce(int),
            vol.Required("threshold"): vol.Coerce(float),
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        result = await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_THRESHOLD,
            dict(tool_input.tool_args),
            blocking=True,
            return_response=True,
        )
        return _service_response(result)


class SearchInventoryTool(_InventoryTool):
    """Resolve a name to products currently in stock."""

    name = "search_inventory"
    description = (
        "Search the inventory by product name. Returns product_id, quantity, "
        "location and expiration for each match. Use this to resolve a name to "
        "a product_id, or to answer 'do we have X?'."
    )
    parameters = vol.Schema({vol.Required("query"): str})

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        query = str(tool_input.tool_args["query"]).strip().lower()
        rows = await self._db(hass).list_inventory()
        matches = [
            {
                "product_id": r.get("product_id"),
                "name": r.get("product_name"),
                "name_he": r.get("product_name_he"),
                "quantity": r.get("quantity"),
                "unit": r.get("unit"),
                "location": r.get("location"),
                "expiration_date": r.get("expiration_date"),
                "category": r.get("category"),
            }
            for r in rows
            if query in str(r.get("product_name") or "").lower()
            or query in str(r.get("product_name_he") or "").lower()
        ]
        return {"count": len(matches), "items": matches[:25]}


class ListInventoryTool(_InventoryTool):
    """List stock, optionally filtered by location or category."""

    name = "list_inventory"
    description = (
        "List what is currently in the inventory, optionally filtered by "
        "location or category. Use for 'what's in the fridge?'."
    )
    parameters = vol.Schema(
        {
            vol.Optional("location"): vol.In(LOCATIONS),
            vol.Optional("category"): vol.In(CATEGORIES),
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        args = tool_input.tool_args
        rows = await self._db(hass).list_inventory()
        if location := args.get("location"):
            rows = [r for r in rows if r.get("location") == location]
        if category := args.get("category"):
            rows = [r for r in rows if r.get("category") == category]
        items = [
            {
                "product_id": r.get("product_id"),
                "name": r.get("product_name"),
                "quantity": r.get("quantity"),
                "unit": r.get("unit"),
                "location": r.get("location"),
                "expiration_date": r.get("expiration_date"),
            }
            for r in rows
        ]
        return {"count": len(items), "items": items[:50]}


class ExpiringItemsTool(_InventoryTool):
    """Items expiring within N days."""

    name = "list_expiring_items"
    description = "List inventory items expiring within the given number of days."
    parameters = vol.Schema({vol.Optional("days"): vol.Coerce(int)})

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        days = int(tool_input.tool_args.get("days") or DEFAULT_EXPIRING_DAYS)
        rows = await self._db(hass).get_expiring_items(days)
        items = [
            {
                "product_id": r.get("product_id"),
                "name": r.get("product_name"),
                "quantity": r.get("quantity"),
                "expiration_date": r.get("expiration_date"),
                "location": r.get("location"),
            }
            for r in rows
        ]
        return {"days": days, "count": len(items), "items": items}


class ShoppingListTool(_InventoryTool):
    """Read the shopping list."""

    name = "list_shopping_list"
    description = "List the open items on the shopping list."
    parameters = vol.Schema({})

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        rows = await self._db(hass).list_shopping()
        items = [
            {
                "item_id": r.get("id"),
                "name": r.get("name"),
                "quantity": r.get("quantity"),
                "category": r.get("category"),
            }
            for r in rows
        ]
        return {"count": len(items), "items": items}


class LowStockTool(_InventoryTool):
    """Products at or below their threshold."""

    name = "list_low_stock"
    description = "List products that are at or below their low-stock threshold."
    parameters = vol.Schema({})

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict:
        rows = await self._db(hass).get_low_stock_items()
        items = [
            {
                "product_id": r.get("product_id"),
                "name": r.get("product_name"),
                "quantity": r.get("total_quantity"),
                "threshold": r.get("threshold"),
            }
            for r in rows
        ]
        return {"count": len(items), "items": items}


TOOLS: list[type[llm.Tool]] = [
    AddItemTool,
    RemoveItemTool,
    AddToShoppingTool,
    SetThresholdTool,
    SearchInventoryTool,
    ListInventoryTool,
    ExpiringItemsTool,
    ShoppingListTool,
    LowStockTool,
]


class HomeInventoryLLMAPI(llm.API):
    """Tool API a conversation agent can be pointed at."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass=hass, id=LLM_API_ID, name=LLM_API_NAME)

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        prompt = API_PROMPT.format(
            locations=", ".join(LOCATIONS),
            categories=", ".join(CATEGORIES),
        )
        # The model needs today's date to resolve "expires Friday".
        prompt += f"\nToday's date is {dt_util.now().date().isoformat()}."
        return llm.APIInstance(
            api=self,
            api_prompt=prompt,
            llm_context=llm_context,
            tools=[tool() for tool in TOOLS],
        )


def async_register_llm_api(hass: HomeAssistant) -> None:
    """Register the inventory tool API (idempotent across reloads)."""
    if hass.data.setdefault(DOMAIN, {}).get("llm_api_registered"):
        return
    try:
        llm.async_register_api(hass, HomeInventoryLLMAPI(hass))
        hass.data[DOMAIN]["llm_api_registered"] = True
        _LOGGER.info("Registered Home Inventory LLM API (%s)", LLM_API_ID)
    except Exception as err:  # noqa: BLE001
        # Re-registering after a reload raises; harmless.
        _LOGGER.debug("LLM API registration skipped: %s", err)
