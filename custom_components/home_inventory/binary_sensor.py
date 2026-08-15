"""Binary sensors used as triggers for automations."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_EXPIRING_DAYS, DOMAIN, EVENT_DATA_CHANGED
from .storage import Database


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    db: Database = hass.data[DOMAIN]["db"]
    async_add_entities(
        [HasExpiringBinarySensor(hass, db), HasLowStockBinarySensor(hass, db)],
        update_before_add=True,
    )


class _BaseBinary(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, db: Database) -> None:
        self.hass = hass
        self._db = db
        self._attr_is_on = False

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


class HasExpiringBinarySensor(_BaseBinary):
    _attr_name = "Has Expiring Items"
    _attr_unique_id = "home_inv_has_expiring"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:calendar-alert"

    async def _refresh(self) -> None:
        items = await self._db.get_expiring_items(DEFAULT_EXPIRING_DAYS)
        self._attr_is_on = len(items) > 0
        self.async_write_ha_state()


class HasLowStockBinarySensor(_BaseBinary):
    _attr_name = "Has Low Stock"
    _attr_unique_id = "home_inv_has_low_stock"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:package-down"

    async def _refresh(self) -> None:
        items = await self._db.get_low_stock_items()
        self._attr_is_on = len(items) > 0
        self.async_write_ha_state()
