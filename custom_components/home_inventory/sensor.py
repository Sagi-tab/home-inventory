"""Sensors: inventory totals, shopping count, expiring, low stock."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_EXPIRING_DAYS, DOMAIN, EVENT_DATA_CHANGED
from .storage import Database

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    db: Database = hass.data[DOMAIN]["db"]
    sensors = [
        InventoryLinesSensor(hass, db),
        InventoryQuantitySensor(hass, db),
        ShoppingOpenSensor(hass, db),
        ExpiringSoonSensor(hass, db),
        LowStockSensor(hass, db),
    ]
    async_add_entities(sensors, update_before_add=True)


class _BaseSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, db: Database) -> None:
        self.hass = hass
        self._db = db
        self._attr_native_value = 0
        self._attr_extra_state_attributes: dict = {}

    async def async_added_to_hass(self) -> None:
        self._unsub = self.hass.bus.async_listen(
            EVENT_DATA_CHANGED, self._handle_change
        )
        await self._refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()

    @callback
    def _handle_change(self, _event) -> None:
        self.hass.async_create_task(self._refresh())

    async def _refresh(self) -> None:
        raise NotImplementedError


class InventoryLinesSensor(_BaseSensor):
    _attr_name = "Inventory Lines"
    _attr_unique_id = "home_inventory_lines"
    _attr_icon = "mdi:format-list-bulleted"
    _attr_state_class = SensorStateClass.MEASUREMENT

    async def _refresh(self) -> None:
        totals = await self._db.totals()
        self._attr_native_value = totals["inventory_lines"]
        self.async_write_ha_state()


class InventoryQuantitySensor(_BaseSensor):
    _attr_name = "Inventory Total Quantity"
    _attr_unique_id = "home_inventory_quantity"
    _attr_icon = "mdi:package-variant"
    _attr_state_class = SensorStateClass.MEASUREMENT

    async def _refresh(self) -> None:
        totals = await self._db.totals()
        self._attr_native_value = totals["inventory_quantity"]
        self.async_write_ha_state()


class ShoppingOpenSensor(_BaseSensor):
    _attr_name = "Shopping List Count"
    _attr_unique_id = "home_inventory_shopping_count"
    _attr_icon = "mdi:cart"
    _attr_state_class = SensorStateClass.MEASUREMENT

    async def _refresh(self) -> None:
        totals = await self._db.totals()
        self._attr_native_value = totals["shopping_open"]
        self.async_write_ha_state()


class ExpiringSoonSensor(_BaseSensor):
    _attr_name = "Expiring Soon"
    _attr_unique_id = "home_inventory_expiring"
    _attr_icon = "mdi:calendar-alert"
    _attr_state_class = SensorStateClass.MEASUREMENT

    async def _refresh(self) -> None:
        items = await self._db.get_expiring_items(DEFAULT_EXPIRING_DAYS)
        self._attr_native_value = len(items)
        self._attr_extra_state_attributes = {
            "items": [
                {
                    "name": i.get("product_name_he") or i["product_name"],
                    "expires": i["expiration_date"],
                    "quantity": i["quantity"],
                }
                for i in items
            ],
            "days_ahead": DEFAULT_EXPIRING_DAYS,
        }
        self.async_write_ha_state()


class LowStockSensor(_BaseSensor):
    _attr_name = "Low Stock"
    _attr_unique_id = "home_inventory_low_stock"
    _attr_icon = "mdi:package-down"
    _attr_state_class = SensorStateClass.MEASUREMENT

    async def _refresh(self) -> None:
        items = await self._db.get_low_stock_items()
        self._attr_native_value = len(items)
        self._attr_extra_state_attributes = {
            "items": [
                {
                    "name": i.get("product_name_he") or i["product_name"],
                    "quantity": i["total_quantity"],
                    "threshold": i["threshold"],
                }
                for i in items
            ],
        }
        self.async_write_ha_state()
