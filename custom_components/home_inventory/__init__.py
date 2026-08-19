"""Home Inventory integration."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CARD_FILENAME, DOMAIN, FRONTEND_URL, PLATFORMS
from .http_api import async_register_views
from .intents import async_register_intents
from .llm_api import async_register_llm_api
from .lookup import BarcodeLookup
from .seed_data import seed_if_empty
from .services import async_register_services
from .storage import Database
from .whatsapp import async_setup_whatsapp

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up via YAML is not supported; use the UI flow."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Inventory from a config entry."""
    db = Database(hass)
    await db.async_init()
    await seed_if_empty(db)

    lookup = BarcodeLookup(hass, db)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["db"] = db
    hass.data[DOMAIN]["lookup"] = lookup
    hass.data[DOMAIN]["entry"] = entry

    async_register_services(hass, db, lookup)
    async_register_views(hass)
    async_register_intents(hass)
    async_register_llm_api(hass)
    async_setup_whatsapp(hass, entry)

    # Re-apply WhatsApp settings (and reschedule the digest) on options change.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Register the frontend static path first so the card JS is reachable.
    www_dir = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths([
        StaticPathConfig(FRONTEND_URL, str(www_dir), cache_headers=False)
    ])

    # Version is used to cache-bust the card URL. Read it from the already
    # loaded integration rather than the manifest file: opening the file here
    # would be blocking I/O inside the event loop.
    try:
        integration = await async_get_integration(hass, DOMAIN)
        version = str(integration.version or "0")
    except Exception:  # noqa: BLE001 - cache busting must never block setup
        version = "0"
    card_url = f"{FRONTEND_URL}/{CARD_FILENAME}?v={version}"

    # Strategy 1: add_extra_js_url (works on most HA versions as a
    # pre-loaded module). This gets injected into every frontend page.
    try:
        add_extra_js_url(hass, card_url)
        _LOGGER.info("Added extra JS URL: %s", card_url)
    except Exception as err:  # pragma: no cover
        _LOGGER.warning("add_extra_js_url failed: %s", err)

    # Strategy 2: register as a Lovelace resource so dashboards loaded
    # from storage mode pick it up even if add_extra_js_url doesn't
    # apply (e.g. certain HA front-end configurations).
    await _async_register_lovelace_resource(hass, card_url)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("Home Inventory set up successfully; card at %s", card_url)
    return True


async def _async_register_lovelace_resource(
    hass: HomeAssistant, card_url: str
) -> None:
    """Register the card as a Lovelace module resource (storage mode only)."""
    try:
        lovelace = hass.data.get("lovelace")
        if lovelace is None:
            _LOGGER.debug("Lovelace not yet loaded; skipping resource registration.")
            return

        # HA exposes resources via a ResourceStorageCollection in lovelace.resources
        resources = getattr(lovelace, "resources", None)
        if resources is None:
            _LOGGER.debug("Lovelace resources collection unavailable.")
            return

        # Some versions lazy-load; ensure the store is loaded.
        if hasattr(resources, "async_load") and not getattr(resources, "loaded", True):
            try:
                await resources.async_load()
            except Exception:  # pragma: no cover
                pass

        existing = []
        if hasattr(resources, "async_items"):
            try:
                existing = resources.async_items() or []
            except Exception:  # pragma: no cover
                existing = []

        # Match on base URL (ignore version query) so we update, not duplicate
        base = card_url.split("?")[0]
        match = next(
            (r for r in existing if str(r.get("url", "")).split("?")[0] == base),
            None,
        )

        if match is None:
            if hasattr(resources, "async_create_item"):
                await resources.async_create_item(
                    {"res_type": "module", "url": card_url}
                )
                _LOGGER.info("Registered Lovelace resource: %s", card_url)
        elif match.get("url") != card_url:
            # Update the URL (to bust cache on version change)
            if hasattr(resources, "async_update_item"):
                await resources.async_update_item(
                    match["id"], {"res_type": "module", "url": card_url}
                )
                _LOGGER.info("Updated Lovelace resource: %s", card_url)
    except Exception as err:
        _LOGGER.warning(
            "Could not register Lovelace resource automatically: %s. "
            "If the card doesn't load, add it manually via "
            "Settings → Dashboards → Resources: URL=%s, Type=JavaScript Module.",
            err, card_url,
        )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed WhatsApp options without restarting Home Assistant."""
    async_setup_whatsapp(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        if unsub := hass.data.get(DOMAIN, {}).get("wa_unsub_timer"):
            unsub()
        hass.data.pop(DOMAIN, None)
    return unloaded
