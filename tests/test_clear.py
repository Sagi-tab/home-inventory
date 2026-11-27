"""Clearing the shopping list and the inventory."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.home_inventory.const import DOMAIN, EVENT_DATA_CHANGED
from custom_components.home_inventory.services import async_register_services
from custom_components.home_inventory.storage import Database


async def _db(hass: HomeAssistant, tmp_path) -> Database:
    db = Database(hass, tmp_path / "inventory.db")
    await db.async_init()
    milk = await db.upsert_product(name="Milk", barcode="0000000000017")
    eggs = await db.upsert_product(name="Eggs", barcode=None)
    await db.add_inventory(product_id=milk, quantity=2, location="fridge", threshold=1)
    await db.add_inventory(product_id=eggs, quantity=12)
    await db.add_shopping(name="Bread")
    await db.add_shopping(name="Milk", product_id=milk)
    done = await db.add_shopping(name="Butter")
    await db.complete_shopping(done)
    return db


async def test_storage_clear(hass: HomeAssistant, tmp_path) -> None:
    db = await _db(hass, tmp_path)

    assert await db.clear_shopping() == 2
    assert await db.list_shopping() == []
    # completed items are not part of the list and stay as they were
    assert [s["name"] for s in await db.list_shopping(include_completed=True)] == ["Butter"]

    assert await db.clear_inventory() == 2
    assert await db.list_inventory() == []
    # the product catalogue and barcode links survive
    assert (await db.get_product_by_barcode("0000000000017"))["name"] == "Milk"
    assert len(await db.search_products("Eggs")) == 1


async def test_clear_services(hass: HomeAssistant, tmp_path) -> None:
    db = await _db(hass, tmp_path)
    async_register_services(hass, db, MagicMock())
    events = []
    hass.bus.async_listen(EVENT_DATA_CHANGED, events.append)

    resp = await hass.services.async_call(
        DOMAIN, "clear_shopping_list", {}, blocking=True, return_response=True
    )
    assert resp == {"removed": 2}
    assert await db.list_shopping() == []
    assert len(await db.list_inventory()) == 2

    resp = await hass.services.async_call(
        DOMAIN, "clear_inventory", {}, blocking=True, return_response=True
    )
    assert resp == {"removed": 2}
    assert await db.list_inventory() == []

    # clearing an empty list is fine
    resp = await hass.services.async_call(
        DOMAIN, "clear_shopping_list", {}, blocking=True, return_response=True
    )
    assert resp == {"removed": 0}
    await hass.async_block_till_done()
    assert len(events) == 3
