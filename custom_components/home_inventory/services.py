"""Home Assistant service handlers for Home Inventory."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import (
    ACTION_ADD,
    ACTION_REMOVE,
    CATEGORIES,
    DOMAIN,
    EVENT_BARCODE_UNKNOWN,
    EVENT_DATA_CHANGED,
    LOCATIONS,
    SERVICE_ADD_ITEM,
    SERVICE_ADD_TO_SHOPPING,
    SERVICE_COMPLETE_SHOPPING,
    SERVICE_LOOKUP_BARCODE,
    SERVICE_REMOVE_ITEM,
    SERVICE_SCAN_BARCODE,
    SERVICE_SEND_EXPIRY_ALERT,
    SERVICE_SET_THRESHOLD,
    SERVICE_UPDATE_PRODUCT,
    SERVICE_WA_DIAGNOSTICS,
)
from .lookup import BarcodeLookup
from .storage import Database

# Service names added in v0.2.0 for multi-barcode support
SERVICE_LINK_BARCODE = "link_barcode"
SERVICE_UNLINK_BARCODE = "unlink_barcode"
SERVICE_MERGE_PRODUCTS = "merge_products"
SERVICE_SUGGEST_MATCHES = "suggest_matches"
SERVICE_RENAME_PRODUCT = "rename_product"
SERVICE_UPDATE_SHOPPING_QUANTITY = "update_shopping_quantity"

_LOGGER = logging.getLogger(__name__)


def _normalize_category_or_none(value):
    """Voluptuous normalizer: map free-text category to our enum, else None."""
    # Imported here to avoid circular import at module load
    from .lookup import normalize_category
    if value is None or value == "":
        return None
    return normalize_category(value)


SCAN_SCHEMA = vol.Schema({
    vol.Required("barcode"): cv.string,
    vol.Optional("action", default=ACTION_ADD): vol.In([ACTION_ADD, ACTION_REMOVE]),
    # mode is the new high-level intent. When set, it overrides 'action':
    #   "stock_up" -> always add (use quantity, expiration); never deduct
    #   "deduct"   -> always remove; if at zero, add+remove (net zero) and
    #                 the response signals was_at_zero=true so the UI can
    #                 prompt to switch to stock_up
    # Older callers without 'mode' fall back to 'action' as before.
    vol.Optional("mode"): vol.In(["stock_up", "deduct"]),
    vol.Optional("quantity", default=1): vol.Coerce(float),
    vol.Optional("location"): vol.In(LOCATIONS),
    vol.Optional("expiration_date"): cv.string,
    vol.Optional("add_to_shopping_list"): cv.boolean,
    vol.Optional("product_name"): cv.string,  # for unknown barcodes (new product)
    # Alternative for unknown barcodes: link to an existing product
    vol.Optional("link_to_product_id"): vol.Coerce(int),
    vol.Optional("brand_override"): cv.string,  # the brand for this specific barcode
    vol.Optional("product_category"): _normalize_category_or_none,
})

ADD_ITEM_SCHEMA = vol.Schema({
    vol.Required("name"): cv.string,
    vol.Optional("quantity", default=1): vol.Coerce(float),
    vol.Optional("unit", default="pcs"): cv.string,
    vol.Optional("category"): _normalize_category_or_none,
    vol.Optional("location"): vol.In(LOCATIONS),
    vol.Optional("expiration_date"): cv.string,
    vol.Optional("threshold"): vol.Coerce(float),
    vol.Optional("notes"): cv.string,
    vol.Optional("target", default="inventory"): vol.In(["inventory", "shopping"]),
    # When set, skip name-based product lookup and use this id directly.
    # Used by the card when the user picks a strong match from autocomplete.
    vol.Optional("link_to_product_id"): vol.Coerce(int),
})

REMOVE_ITEM_SCHEMA = vol.Schema({
    vol.Required("product_id"): vol.Coerce(int),
    vol.Optional("quantity", default=1): vol.Coerce(float),
    vol.Optional("location"): vol.In(LOCATIONS),
    vol.Optional("expiration_date"): cv.string,
    vol.Optional("add_to_shopping_list", default=False): cv.boolean,
})

ADD_SHOPPING_SCHEMA = vol.Schema({
    vol.Required("name"): cv.string,
    vol.Optional("product_id"): vol.Coerce(int),
    vol.Optional("quantity", default=1): vol.Coerce(float),
    vol.Optional("unit", default="pcs"): cv.string,
    vol.Optional("category"): _normalize_category_or_none,
    vol.Optional("notes"): cv.string,
})

COMPLETE_SHOPPING_SCHEMA = vol.Schema({
    vol.Required("item_id"): vol.Coerce(int),
    # Default changed to True in v0.4.1: checking off a shopping item should
    # naturally move it into the inventory unless the caller opts out.
    vol.Optional("add_to_inventory", default=True): cv.boolean,
    vol.Optional("location"): vol.In(LOCATIONS),
    vol.Optional("expiration_date"): cv.string,
})

THRESHOLD_SCHEMA = vol.Schema({
    vol.Required("product_id"): vol.Coerce(int),
    vol.Required("threshold"): vol.Coerce(float),
})

LOOKUP_SCHEMA = vol.Schema({
    vol.Required("barcode"): cv.string,
})

UPDATE_PRODUCT_SCHEMA = vol.Schema({
    vol.Required("barcode"): cv.string,
    vol.Required("name"): cv.string,
    vol.Optional("name_he"): cv.string,
    vol.Optional("brand"): cv.string,
    vol.Optional("category"): _normalize_category_or_none,
    vol.Optional("default_location"): vol.In(LOCATIONS),
})


def _fire_changed(hass: HomeAssistant) -> None:
    hass.bus.async_fire(EVENT_DATA_CHANGED, {})


def async_register_services(
    hass: HomeAssistant,
    db: Database,
    lookup: BarcodeLookup,
) -> None:
    """Register all services for the integration."""

    async def handle_lookup(call: ServiceCall) -> dict:
        result = await lookup.lookup(call.data["barcode"])
        if result.get("found") == "unknown":
            hass.bus.async_fire(EVENT_BARCODE_UNKNOWN, {"barcode": call.data["barcode"]})
        return {"product": result}

    async def handle_scan(call: ServiceCall) -> dict:
        barcode = call.data["barcode"]
        # 'mode' is the canonical input; fall back to 'action' for callers
        # that haven't migrated yet.
        mode = call.data.get("mode")
        if mode == "stock_up":
            action = ACTION_ADD
        elif mode == "deduct":
            action = ACTION_REMOVE
        else:
            action = call.data.get("action", ACTION_ADD)
        quantity = float(call.data.get("quantity", 1))
        brand_override = call.data.get("brand_override")

        result = await lookup.lookup(barcode)

        # Unknown barcode: three possible user intents —
        #   1. link_to_product_id was provided → attach to existing product
        #   2. product_name was provided → create new product
        #   3. neither → fire event + error (frontend should prompt)
        if result.get("found") == "unknown":
            link_to = call.data.get("link_to_product_id")
            name = call.data.get("product_name")
            if link_to is not None:
                existing = await db.get_product(int(link_to))
                if not existing:
                    raise HomeAssistantError(
                        f"link_to_product_id={link_to} does not exist"
                    )
                await db.link_barcode(
                    barcode=barcode,
                    product_id=int(link_to),
                    brand=brand_override,
                )
                result = await db.get_product_by_barcode(barcode)
                result["found"] = "linked"
            elif name:
                pid = await db.upsert_product(
                    barcode=barcode,
                    name=name,
                    brand=brand_override,
                    category=call.data.get("product_category"),
                    source="user",
                )
                result = await db.get_product_by_barcode(barcode)
                result["found"] = "user"
            else:
                hass.bus.async_fire(EVENT_BARCODE_UNKNOWN, {"barcode": barcode})
                raise HomeAssistantError(
                    f"Unknown barcode {barcode}. Provide 'product_name' or "
                    f"'link_to_product_id' to save it."
                )
        else:
            # Known barcode — if the brand differs and user passed an override,
            # update the link's brand.
            if brand_override:
                await db.link_barcode(
                    barcode=barcode,
                    product_id=result["id"],
                    brand=brand_override,
                )

        product_id = result["id"]

        # Bump scan-count for this barcode (useful for "most recent brand" UI)
        await db.bump_barcode_scan(barcode)

        if action == ACTION_ADD:
            await db.add_inventory(
                product_id=product_id,
                quantity=quantity,
                location=call.data.get("location"),
                expiration_date=call.data.get("expiration_date"),
                barcode=barcode,
            )
            # Auto-fulfill: if this product is on the shopping list, check it off.
            fulfilled_ids: list[int] = []
            open_shop = await db.list_shopping(include_completed=False)
            for s in open_shop:
                if s.get("product_id") == product_id:
                    await db.complete_shopping(int(s["id"]))
                    fulfilled_ids.append(int(s["id"]))
            _fire_changed(hass)
            return {
                "product": result,
                "action": "added",
                "quantity": quantity,
                "fulfilled_shopping_ids": fulfilled_ids,
            }

        # action == remove
        # First check current total. If we're trying to remove from empty
        # stock, signal back to the UI so it can offer to switch to
        # stocking-up mode instead. We still log a "no-op" history record
        # so the user sees the scan happened.
        current_total = await db.product_total_quantity(product_id)
        was_at_zero = current_total <= 0

        if was_at_zero:
            # Net-zero: add 1, then remove 1, leaving inventory at 0 but
            # bumping history with a clear sequence.
            await db.add_inventory(
                product_id=product_id,
                quantity=quantity,
                barcode=barcode,
            )
        total_after, went_to_zero = await db.remove_inventory(
            product_id=product_id,
            quantity=quantity,
            location=call.data.get("location"),
            barcode=barcode,
        )

        add_to_shop = call.data.get("add_to_shopping_list")
        if add_to_shop is None:
            # Don't auto-add to shopping when the user was scanning into
            # an already-empty stock — they probably meant stock-up.
            add_to_shop = went_to_zero and not was_at_zero
        if add_to_shop:
            await db.add_shopping(
                name=result.get("name") or f"Barcode {barcode}",
                product_id=product_id,
                quantity=1,
                category=result.get("category"),
                auto_added=went_to_zero,
            )
        _fire_changed(hass)
        return {
            "product": result,
            "action": "removed",
            "quantity": quantity,
            "remaining": total_after,
            "added_to_shopping": bool(add_to_shop),
            "was_at_zero": was_at_zero,
            "went_to_zero": went_to_zero,
        }

    async def handle_add_item(call: ServiceCall) -> dict:
        name = call.data["name"]
        target = call.data.get("target", "inventory")
        link_to = call.data.get("link_to_product_id")

        if link_to is not None:
            # Caller picked a specific product (e.g. from autocomplete).
            existing = await db.get_product(int(link_to))
            if not existing:
                raise HomeAssistantError(
                    f"link_to_product_id={link_to} does not exist"
                )
            pid = int(link_to)
        else:
            # Match or create product by name (case-insensitive)
            matches = await db.search_products(name, limit=5)
            product = next(
                (m for m in matches if m["name"].lower() == name.lower()
                 or (m.get("name_he") or "") == name),
                None,
            )
            if product is None:
                pid = await db.upsert_product(
                    barcode=None,
                    name=name,
                    category=call.data.get("category"),
                    default_location=call.data.get("location"),
                    source="user",
                )
            else:
                pid = product["id"]

        if target == "shopping":
            item_id = await db.add_shopping(
                name=name,
                product_id=pid,
                quantity=float(call.data.get("quantity", 1)),
                unit=call.data.get("unit", "pcs"),
                category=call.data.get("category"),
                notes=call.data.get("notes"),
            )
            _fire_changed(hass)
            return {"product_id": pid, "shopping_id": item_id}

        await db.add_inventory(
            product_id=pid,
            quantity=float(call.data.get("quantity", 1)),
            unit=call.data.get("unit", "pcs"),
            location=call.data.get("location"),
            expiration_date=call.data.get("expiration_date"),
            threshold=call.data.get("threshold"),
            notes=call.data.get("notes"),
        )
        _fire_changed(hass)
        return {"product_id": pid}

    async def handle_remove_item(call: ServiceCall) -> dict:
        product_id = int(call.data["product_id"])
        quantity = float(call.data.get("quantity", 1))
        total_after, went_to_zero = await db.remove_inventory(
            product_id=product_id,
            quantity=quantity,
            location=call.data.get("location"),
            expiration_date=call.data.get("expiration_date"),
        )
        add_to_shop = call.data.get("add_to_shopping_list", False) or went_to_zero
        if add_to_shop:
            product = await db.get_product(product_id)
            if product:
                await db.add_shopping(
                    name=product["name"],
                    product_id=product_id,
                    quantity=1,
                    category=product.get("category"),
                    auto_added=went_to_zero,
                )
        _fire_changed(hass)
        return {"remaining": total_after, "added_to_shopping": bool(add_to_shop)}

    async def handle_add_shopping(call: ServiceCall) -> dict:
        item_id = await db.add_shopping(
            name=call.data["name"],
            product_id=call.data.get("product_id"),
            quantity=float(call.data.get("quantity", 1)),
            unit=call.data.get("unit", "pcs"),
            category=call.data.get("category"),
            notes=call.data.get("notes"),
        )
        _fire_changed(hass)
        return {"id": item_id}

    async def handle_complete_shopping(call: ServiceCall) -> dict:
        item_id = int(call.data["item_id"])
        add_to_inv = bool(call.data.get("add_to_inventory", True))
        # Read item before completing
        shopping = await db.list_shopping(include_completed=True)
        item = next((s for s in shopping if s["id"] == item_id), None)
        await db.complete_shopping(item_id)
        added_qty = 0.0
        added_to_inventory = False
        item_name = item.get("name") if item else None
        if add_to_inv and item and item.get("product_id"):
            added_qty = float(item.get("quantity", 1))
            await db.add_inventory(
                product_id=item["product_id"],
                quantity=added_qty,
                location=call.data.get("location"),
                expiration_date=call.data.get("expiration_date"),
            )
            added_to_inventory = True
        _fire_changed(hass)
        return {
            "completed": item_id,
            "added_to_inventory": added_to_inventory,
            "added_quantity": added_qty,
            "name": item_name,
            "product_id": item.get("product_id") if item else None,
        }

    async def handle_set_threshold(call: ServiceCall) -> dict:
        await db.set_threshold(
            product_id=int(call.data["product_id"]),
            threshold=float(call.data["threshold"]),
        )
        _fire_changed(hass)
        return {}

    async def handle_update_product(call: ServiceCall) -> dict:
        pid = await db.upsert_product(
            barcode=call.data["barcode"],
            name=call.data["name"],
            name_he=call.data.get("name_he"),
            brand=call.data.get("brand"),
            category=call.data.get("category"),
            default_location=call.data.get("default_location"),
            source="user",
        )
        _fire_changed(hass)
        return {"product_id": pid}

    # --- Multi-barcode services (v0.2.0) ---

    async def handle_link_barcode(call: ServiceCall) -> dict:
        barcode = call.data["barcode"]
        product_id = int(call.data["product_id"])
        existing = await db.get_product(product_id)
        if not existing:
            raise HomeAssistantError(f"Product {product_id} not found")
        await db.link_barcode(
            barcode=barcode,
            product_id=product_id,
            brand=call.data.get("brand"),
            image_url=call.data.get("image_url"),
        )
        _fire_changed(hass)
        return {"barcode": barcode, "product_id": product_id}

    async def handle_unlink_barcode(call: ServiceCall) -> dict:
        barcode = call.data["barcode"]
        await db.unlink_barcode(barcode)
        _fire_changed(hass)
        return {"barcode": barcode}

    async def handle_merge_products(call: ServiceCall) -> dict:
        src = int(call.data["src_product_id"])
        dest = int(call.data["dest_product_id"])
        if src == dest:
            raise HomeAssistantError("src and dest must be different")
        src_exists = await db.get_product(src)
        dest_exists = await db.get_product(dest)
        if not src_exists or not dest_exists:
            raise HomeAssistantError("Both src and dest products must exist")
        await db.merge_products(src, dest)
        _fire_changed(hass)
        return {"merged_src": src, "into_dest": dest}

    async def handle_rename_product(call: ServiceCall) -> dict:
        product_id = int(call.data["product_id"])
        existing = await db.get_product(product_id)
        if not existing:
            raise HomeAssistantError(f"Product {product_id} not found")
        ok = await db.update_product_fields(
            product_id,
            name=call.data.get("name"),
            name_he=call.data.get("name_he"),
            brand=call.data.get("brand"),
            category=call.data.get("category"),
            default_location=call.data.get("default_location"),
        )
        _fire_changed(hass)
        return {"product_id": product_id, "updated": ok}

    async def handle_update_shopping_quantity(call: ServiceCall) -> dict:
        item_id = int(call.data["item_id"])
        qty = float(call.data["quantity"])
        prev = await db.update_shopping_quantity(item_id, qty)
        if prev is None:
            raise HomeAssistantError(f"Shopping item {item_id} not found")
        _fire_changed(hass)
        return {
            "item_id": item_id,
            "previous_quantity": float(prev.get("quantity", 0)),
            "new_quantity": qty,
            "deleted": qty <= 0,
        }

    async def handle_suggest_matches(call: ServiceCall) -> dict:
        # Either pass a barcode (we'll try OFF to populate name/category)
        # or pass name/name_he/category directly.
        name = call.data.get("name")
        name_he = call.data.get("name_he")
        category = call.data.get("category")
        brand = call.data.get("brand")

        barcode = call.data.get("barcode")
        if barcode and not (name or name_he):
            result = await lookup.lookup(barcode)
            if result.get("found") != "unknown":
                # Already known — return the current product itself as the
                # top "match" so the caller can offer "confirm"
                return {
                    "barcode": barcode,
                    "existing_product_id": result.get("id"),
                    "suggestions": [],
                }
            # (Lookup path stores OFF results in DB already, so an "unknown"
            # here really is unknown; no OFF metadata to extract.)
        matches = await db.find_similar_products(
            name=name, name_he=name_he,
            category=category, brand=brand,
        )
        return {
            "barcode": barcode,
            "suggestions": matches,
        }

    hass.services.async_register(
        DOMAIN, SERVICE_LOOKUP_BARCODE, handle_lookup,
        schema=LOOKUP_SCHEMA, supports_response="only",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SCAN_BARCODE, handle_scan,
        schema=SCAN_SCHEMA, supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_ITEM, handle_add_item,
        schema=ADD_ITEM_SCHEMA, supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_ITEM, handle_remove_item,
        schema=REMOVE_ITEM_SCHEMA, supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_TO_SHOPPING, handle_add_shopping,
        schema=ADD_SHOPPING_SCHEMA, supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COMPLETE_SHOPPING, handle_complete_shopping,
        schema=COMPLETE_SHOPPING_SCHEMA, supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_THRESHOLD, handle_set_threshold,
        schema=THRESHOLD_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_PRODUCT, handle_update_product,
        schema=UPDATE_PRODUCT_SCHEMA, supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LINK_BARCODE, handle_link_barcode,
        schema=vol.Schema({
            vol.Required("barcode"): cv.string,
            vol.Required("product_id"): vol.Coerce(int),
            vol.Optional("brand"): cv.string,
            vol.Optional("image_url"): cv.string,
        }),
        supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNLINK_BARCODE, handle_unlink_barcode,
        schema=vol.Schema({vol.Required("barcode"): cv.string}),
        supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_MERGE_PRODUCTS, handle_merge_products,
        schema=vol.Schema({
            vol.Required("src_product_id"): vol.Coerce(int),
            vol.Required("dest_product_id"): vol.Coerce(int),
        }),
        supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SUGGEST_MATCHES, handle_suggest_matches,
        schema=vol.Schema({
            vol.Optional("barcode"): cv.string,
            vol.Optional("name"): cv.string,
            vol.Optional("name_he"): cv.string,
            vol.Optional("category"): _normalize_category_or_none,
            vol.Optional("brand"): cv.string,
        }),
        supports_response="only",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RENAME_PRODUCT, handle_rename_product,
        schema=vol.Schema({
            vol.Required("product_id"): vol.Coerce(int),
            vol.Optional("name"): cv.string,
            vol.Optional("name_he"): cv.string,
            vol.Optional("brand"): cv.string,
            vol.Optional("category"): _normalize_category_or_none,
            vol.Optional("default_location"): vol.In(LOCATIONS),
        }),
        supports_response="optional",
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_SHOPPING_QUANTITY, handle_update_shopping_quantity,
        schema=vol.Schema({
            vol.Required("item_id"): vol.Coerce(int),
            vol.Required("quantity"): vol.Coerce(float),
        }),
        supports_response="optional",
    )

    async def handle_send_expiry_alert(call: ServiceCall) -> dict:
        # Imported here to keep the WhatsApp bridge optional at import time.
        from .whatsapp import async_send_expiry_digest

        return await async_send_expiry_digest(
            hass, as_text=bool(call.data.get("as_text", False))
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_EXPIRY_ALERT, handle_send_expiry_alert,
        schema=vol.Schema({vol.Optional("as_text"): cv.boolean}),
        supports_response="optional",
    )

    async def handle_whatsapp_diagnostics(call: ServiceCall) -> dict:
        from .whatsapp import async_whatsapp_diagnostics

        return await async_whatsapp_diagnostics(hass)

    hass.services.async_register(
        DOMAIN, SERVICE_WA_DIAGNOSTICS, handle_whatsapp_diagnostics,
        schema=vol.Schema({}), supports_response="only",
    )

    _LOGGER.debug("Registered Home Inventory services")
