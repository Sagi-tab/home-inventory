"""Reminder delivery and people mapping."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.home_inventory.const import DOMAIN
from custom_components.home_inventory.reminders import ReminderManager, parse_people

from .conftest import FakeWaha


async def _manager(hass: HomeAssistant, waha: FakeWaha | None) -> ReminderManager:
    hass.states.async_set("person.alice", "home", {"user_id": "user-alice"})
    hass.data.setdefault(DOMAIN, {})["wa_client"] = waha
    manager = ReminderManager(hass)
    manager.async_configure(parse_people("972500000001=person.alice"), 150)
    return manager


async def test_parse_people() -> None:
    assert parse_people("+972 50-000-0001=person.alice, 15550001111=person.bob\nbad, 1=switch.x") == {
        "972500000001": "person.alice",
        "15550001111": "person.bob",
    }


async def test_people_lookups(hass: HomeAssistant) -> None:
    manager = await _manager(hass, FakeWaha())
    assert manager.person_for_number("+972-50-000-0001") == "person.alice"
    assert manager.number_for_person("person.alice") == "972500000001"
    assert manager.user_for_number("972500000001") == "user-alice"
    assert manager.person_for_user("user-alice") == "person.alice"
    assert manager.person_for_user("someone-else") is None


async def test_reminder_is_delivered_over_whatsapp(hass: HomeAssistant) -> None:
    waha = FakeWaha()
    manager = await _manager(hass, waha)
    reminder = await manager.async_create(
        "person.alice", "buy milk", "time", due=dt_util.utcnow() + timedelta(hours=1)
    )
    manager._timers.pop(reminder["id"])()  # as if the due-time timer had fired
    await manager._async_fire(reminder["id"])
    assert waha.sent == [("972500000001", "🔔 Reminder: buy milk")]
    assert manager.for_person("person.alice") == []  # delivered reminders are removed
    manager.async_unload()


async def test_failed_delivery_is_retried(hass: HomeAssistant) -> None:
    waha = FakeWaha(ok=False)
    manager = await _manager(hass, waha)
    reminder = await manager.async_create(
        "person.alice", "buy milk", "time", due=dt_util.utcnow() + timedelta(hours=1)
    )
    manager._timers.pop(reminder["id"])()  # as if the due-time timer had fired
    await manager._async_fire(reminder["id"])
    assert len(waha.sent) == 1
    assert manager.for_person("person.alice")  # kept for the retry
    assert reminder["id"] in manager._timers
    manager.async_unload()
