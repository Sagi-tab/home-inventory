"""Voice intents for HA Assist pipeline.

Handlers for:
- "Add X to my shopping list"
- "Add X to my inventory"
- "Remove X from my inventory"

Register sentences in intents/*.yaml (see sentences package).
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from .const import DOMAIN
from .storage import Database

_LOGGER = logging.getLogger(__name__)

INTENT_ADD_SHOPPING = "HomeInventoryAddShopping"
INTENT_ADD_INVENTORY = "HomeInventoryAddItem"
INTENT_REMOVE_INVENTORY = "HomeInventoryRemoveItem"
INTENT_QUERY_SHOPPING = "HomeInventoryQueryShopping"


class _AddShoppingHandler(intent.IntentHandler):
    intent_type = INTENT_ADD_SHOPPING
    description = "Add an item to the shopping list"
    slot_schema = {"item": str}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)
        item = slots["item"]["value"].strip()

        db: Database = hass.data[DOMAIN]["db"]
        # Try to match an existing product
        matches = await db.search_products(item, limit=1)
        product_id = matches[0]["id"] if matches else None
        await db.add_shopping(name=item, product_id=product_id)
        hass.bus.async_fire("home_inventory_data_changed", {})

        response = intent_obj.create_response()
        response.async_set_speech(f"Added {item} to the shopping list.")
        return response


class _AddInventoryHandler(intent.IntentHandler):
    intent_type = INTENT_ADD_INVENTORY
    description = "Add an item to the inventory"
    slot_schema = {"item": str}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)
        item = slots["item"]["value"].strip()

        db: Database = hass.data[DOMAIN]["db"]
        matches = await db.search_products(item, limit=1)
        if matches:
            pid = matches[0]["id"]
        else:
            pid = await db.upsert_product(
                barcode=None, name=item, source="voice"
            )
        await db.add_inventory(product_id=pid, quantity=1)
        hass.bus.async_fire("home_inventory_data_changed", {})

        response = intent_obj.create_response()
        response.async_set_speech(f"Added {item} to the inventory.")
        return response


class _RemoveInventoryHandler(intent.IntentHandler):
    intent_type = INTENT_REMOVE_INVENTORY
    description = "Remove an item from the inventory"
    slot_schema = {"item": str}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)
        item = slots["item"]["value"].strip()

        db: Database = hass.data[DOMAIN]["db"]
        matches = await db.search_products(item, limit=1)
        response = intent_obj.create_response()
        if not matches:
            response.async_set_speech(f"I couldn't find {item}.")
            return response
        product_id = matches[0]["id"]
        _total, went_zero = await db.remove_inventory(
            product_id=product_id, quantity=1
        )
        if went_zero:
            await db.add_shopping(
                name=matches[0]["name"],
                product_id=product_id,
                auto_added=True,
            )
            extra = " It's now on the shopping list."
        else:
            extra = ""
        hass.bus.async_fire("home_inventory_data_changed", {})
        response.async_set_speech(f"Removed {item} from the inventory.{extra}")
        return response


class _QueryShoppingHandler(intent.IntentHandler):
    intent_type = INTENT_QUERY_SHOPPING
    description = "Read the shopping list"
    slot_schema = {}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        db: Database = hass.data[DOMAIN]["db"]
        items = await db.list_shopping(include_completed=False)
        response = intent_obj.create_response()
        if not items:
            response.async_set_speech("Your shopping list is empty.")
            return response
        names = ", ".join(i["name"] for i in items[:15])
        response.async_set_speech(
            f"You have {len(items)} items on the shopping list: {names}."
        )
        return response


def async_register_intents(hass: HomeAssistant) -> None:
    # HA's intent.async_register silently overwrites, but logs a WARNING
    # on each overwrite. Use a flag on hass.data to register once per process.
    flag_key = "home_inventory_intents_registered"
    if hass.data.get(flag_key):
        return
    intent.async_register(hass, _AddShoppingHandler())
    intent.async_register(hass, _AddInventoryHandler())
    intent.async_register(hass, _RemoveInventoryHandler())
    intent.async_register(hass, _QueryShoppingHandler())
    hass.data[flag_key] = True
    _LOGGER.debug("Registered Home Inventory voice intents")
