"""Location and time reminders delivered over WhatsApp.

A reminder belongs to a Home Assistant person (the WhatsApp sender, mapped in
the integration options) and fires once, as a WhatsApp message to that person:

- ``home``: when the person has been home (the HA home zone) for a while.
- ``point``: when the person's GPS position has been within a radius of a
  shared location for a while.
- ``time``: at a given moment.

A location reminder only fires on an *arrival*: one created while the person is
already there waits until they have left and come back. A reminder for a place
the agent could not resolve is stored as ``pending_location`` until the person
shares the place (WhatsApp location or a Google Maps link).

Reminders are persisted in ``.storage``, so they survive restarts. The dwell
timer does not: after a restart it starts again from the current state.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_REMINDER_RADIUS,
    DOMAIN,
    REMINDER_DWELL_SECONDS,
    REMINDER_RETRY_SECONDS,
    REMINDERS_STORAGE_KEY,
)
from .geo import coords_from_url, find_maps_urls, haversine_m, is_short_link, valid_coords

_LOGGER = logging.getLogger(__name__)

STATUS_PENDING_LOCATION = "pending_location"
STATUS_ACTIVE = "active"

KIND_HOME = "home"
KIND_POINT = "point"
KIND_TIME = "time"

_TEXTS = {
    "he": {
        "fire": "🔔 תזכורת: {text}",
        "location_saved": "📍 המיקום נשמר. אזכיר לך \"{text}\" אחרי {minutes} דקות במקום.",
        "location_failed": (
            "לא הצלחתי לזהות קואורדינטות בקישור. אפשר לשלוח את המיקום דרך "
            "WhatsApp (📎 ← מיקום) או קישור Google Maps למקום עצמו."
        ),
    },
    "en": {
        "fire": "🔔 Reminder: {text}",
        "location_saved": "📍 Location saved. I'll remind you \"{text}\" after {minutes} minutes there.",
        "location_failed": (
            "I couldn't find coordinates in that link. Share the place with "
            "WhatsApp's location option (📎 → Location) or a Google Maps link to the place."
        ),
    },
}


def _text(hass: HomeAssistant, key: str, **kwargs: Any) -> str:
    lang = "he" if str(hass.config.language or "").startswith("he") else "en"
    return _TEXTS[lang][key].format(**kwargs)


def parse_people(value: Any) -> dict[str, str]:
    """``"<number>=person.x, <number>=person.y"`` -> {digits: person entity_id}."""
    people: dict[str, str] = {}
    for part in str(value or "").replace("\n", ",").split(","):
        if "=" not in part:
            continue
        number, entity_id = (p.strip() for p in part.split("=", 1))
        digits = "".join(ch for ch in number if ch.isdigit())
        if digits and entity_id.startswith("person."):
            people[digits] = entity_id
    return people


async def async_coords_from_maps_url(
    hass: HomeAssistant, url: str
) -> tuple[float, float] | None:
    """Coordinates of a Google Maps link, following short-link redirects."""
    if found := coords_from_url(url):
        return found
    if not is_short_link(url):
        return None
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            hops = [str(r.url) for r in resp.history] + [str(resp.url)]
            if location := resp.headers.get("Location"):
                hops.append(location)
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Could not resolve maps link: %s", err)
        return None
    for hop in reversed(hops):
        if found := coords_from_url(hop):
            return found
    return None


class ReminderManager:
    """Stores reminders and fires them on arrival or at their time."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, 1, REMINDERS_STORAGE_KEY)
        self._reminders: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self._people: dict[str, str] = {}  # WhatsApp number -> person
        self._tg_people: dict[str, str] = {}  # Telegram chat id -> person
        self._radius = DEFAULT_REMINDER_RADIUS
        self._dwell: dict[str, CALLBACK_TYPE] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._unsub_state: CALLBACK_TYPE | None = None

    # ---------- lifecycle ----------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._reminders = {r["id"]: r for r in data.get("reminders", [])}
        self._next_id = int(data.get("next_id", 1))
        self._unsub_state = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._async_state_changed, event_filter=_is_person_event
        )
        for rid in list(self._reminders):
            self._async_arm(rid)

    @callback
    def async_unload(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        for unsub in [*self._dwell.values(), *self._timers.values()]:
            unsub()
        self._dwell.clear()
        self._timers.clear()

    @callback
    def async_configure(
        self,
        people: dict[str, str],
        radius: float,
        telegram_chats: dict[str, str | None] | None = None,
    ) -> None:
        self._people = people
        self._tg_people = {
            chat: person for chat, person in (telegram_chats or {}).items() if person
        }
        self._radius = float(radius or DEFAULT_REMINDER_RADIUS)

    # ---------- people ----------

    def person_for_number(self, number: str) -> str | None:
        return self._people.get("".join(ch for ch in str(number) if ch.isdigit()))

    def number_for_person(self, person: str) -> str | None:
        return next((n for n, p in self._people.items() if p == person), None)

    def person_for_chat(self, chat_id: str | int) -> str | None:
        return self._tg_people.get(str(chat_id))

    def chat_for_person(self, person: str) -> str | None:
        return next((c for c, p in self._tg_people.items() if p == person), None)

    def user_for_person(self, person: str | None) -> str | None:
        state = self.hass.states.get(person) if person else None
        return state.attributes.get("user_id") if state else None

    def person_for_user(self, user_id: str | None) -> str | None:
        """The mapped person whose HA user is ``user_id``."""
        if not user_id:
            return None
        for person in dict.fromkeys([*self._people.values(), *self._tg_people.values()]):
            state = self.hass.states.get(person)
            if state and state.attributes.get("user_id") == user_id:
                return person
        return None

    def user_for_number(self, number: str) -> str | None:
        person = self.person_for_number(number)
        state = self.hass.states.get(person) if person else None
        return state.attributes.get("user_id") if state else None

    # ---------- CRUD ----------

    def for_person(self, person: str) -> list[dict[str, Any]]:
        return [r for r in self._reminders.values() if r["person"] == person]

    def latest_pending_location(self, person: str) -> dict[str, Any] | None:
        pending = [
            r for r in self.for_person(person) if r["status"] == STATUS_PENDING_LOCATION
        ]
        return max(pending, key=lambda r: r["created"]) if pending else None

    async def async_create(
        self,
        person: str,
        text: str,
        kind: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        place: str | None = None,
        due: datetime | None = None,
    ) -> dict[str, Any]:
        rid = f"r{self._next_id}"
        self._next_id += 1
        reminder: dict[str, Any] = {
            "id": rid,
            "person": person,
            "text": text.strip(),
            "kind": kind,
            "status": STATUS_ACTIVE,
            "created": dt_util.utcnow().isoformat(),
        }
        if place:
            reminder["place"] = place.strip()
        if kind == KIND_TIME:
            reminder["due"] = dt_util.as_utc(due).isoformat()
        elif kind == KIND_POINT:
            if latitude is None or longitude is None:
                reminder["status"] = STATUS_PENDING_LOCATION
            else:
                reminder.update(latitude=latitude, longitude=longitude, radius=self._radius)
        if kind in (KIND_HOME, KIND_POINT) and reminder["status"] == STATUS_ACTIVE:
            # Only an arrival counts: already being there does not.
            reminder["armed"] = self._inside(person, reminder) is False
        self._reminders[rid] = reminder
        await self._async_save()
        self._async_arm(rid)
        return reminder

    async def async_set_location(
        self, rid: str, latitude: float, longitude: float
    ) -> dict[str, Any] | None:
        reminder = self._reminders.get(rid)
        if reminder is None or not valid_coords(latitude, longitude):
            return None
        reminder.update(
            kind=KIND_POINT,
            status=STATUS_ACTIVE,
            latitude=latitude,
            longitude=longitude,
            radius=self._radius,
        )
        reminder["armed"] = self._inside(reminder["person"], reminder) is False
        await self._async_save()
        self._async_arm(rid)
        return reminder

    async def async_cancel(self, person: str, rid: str) -> bool:
        reminder = self._reminders.get(rid)
        if reminder is None or reminder["person"] != person:
            return False
        self._async_disarm(rid)
        del self._reminders[rid]
        await self._async_save()
        return True

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"next_id": self._next_id, "reminders": list(self._reminders.values())}
        )

    # ---------- shared-location input ----------

    async def async_attach_shared_location(
        self, person: str, latitude: float, longitude: float
    ) -> str | None:
        """Use a shared location for the person's pending reminder; reply text or None."""
        pending = self.latest_pending_location(person)
        if pending is None:
            return None
        reminder = await self.async_set_location(pending["id"], latitude, longitude)
        if reminder is None:
            return _text(self.hass, "location_failed")
        return _text(
            self.hass,
            "location_saved",
            text=reminder["text"],
            minutes=REMINDER_DWELL_SECONDS // 60,
        )

    async def async_attach_maps_link(self, person: str, text: str) -> str | None:
        """If the person has a pending reminder and sent a Maps link, use it."""
        if self.latest_pending_location(person) is None:
            return None
        urls = find_maps_urls(text)
        if not urls:
            return None
        for url in urls:
            if coords := await async_coords_from_maps_url(self.hass, url):
                return await self.async_attach_shared_location(person, *coords)
        return _text(self.hass, "location_failed")

    # ---------- presence ----------

    def _person_coords(self, state: State) -> tuple[float, float] | None:
        """Best GPS fix for a person: their own position, else their freshest tracker."""
        lat, lon = state.attributes.get("latitude"), state.attributes.get("longitude")
        if lat is not None and lon is not None:
            return float(lat), float(lon)
        best: State | None = None
        for tracker in state.attributes.get("device_trackers") or []:
            t = self.hass.states.get(tracker)
            if t and t.attributes.get("latitude") is not None:
                if best is None or t.last_updated > best.last_updated:
                    best = t
        if best is None:
            return None
        return float(best.attributes["latitude"]), float(best.attributes["longitude"])

    def _inside(self, person: str, reminder: dict[str, Any]) -> bool | None:
        """Whether the person is at the reminder's place; None if unknown."""
        state = self.hass.states.get(person)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        if reminder["kind"] == KIND_HOME:
            return state.state == "home"
        coords = self._person_coords(state)
        if coords is None:
            return None
        distance = haversine_m(*coords, reminder["latitude"], reminder["longitude"])
        return distance <= float(reminder.get("radius", self._radius))

    @callback
    def _async_state_changed(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        if entity_id.startswith("person."):
            persons = {entity_id}
        else:
            # A tracker update may not change the person entity (e.g. when a
            # home-only WiFi tracker decides the person's state).
            persons = {
                p
                for p in set(self._people.values())
                if (st := self.hass.states.get(p))
                and entity_id in (st.attributes.get("device_trackers") or [])
            }
        for person in persons:
            for reminder in self.for_person(person):
                if reminder["status"] == STATUS_ACTIVE and reminder["kind"] != KIND_TIME:
                    self._async_evaluate(reminder["id"])

    @callback
    def _async_evaluate(self, rid: str) -> None:
        reminder = self._reminders.get(rid)
        if reminder is None:
            return
        inside = self._inside(reminder["person"], reminder)
        if inside is None:
            return
        if not inside:
            self._cancel_dwell(rid)
            if not reminder.get("armed"):
                reminder["armed"] = True
                self.hass.async_create_task(self._async_save())
            return
        if reminder.get("armed") and rid not in self._dwell:

            @callback
            def _dwelled(_now: datetime) -> None:
                self._dwell.pop(rid, None)
                current = self._reminders.get(rid)
                if current and self._inside(current["person"], current):
                    self.hass.async_create_task(self._async_fire(rid))

            self._dwell[rid] = async_call_later(
                self.hass, REMINDER_DWELL_SECONDS, _dwelled
            )

    @callback
    def _cancel_dwell(self, rid: str) -> None:
        if unsub := self._dwell.pop(rid, None):
            unsub()

    # ---------- scheduling / firing ----------

    @callback
    def _async_arm(self, rid: str) -> None:
        reminder = self._reminders.get(rid)
        if reminder is None or reminder["status"] != STATUS_ACTIVE:
            return
        if reminder["kind"] == KIND_TIME:
            due = dt_util.parse_datetime(reminder["due"])
            if due is None or due <= dt_util.utcnow():
                # Missed while Home Assistant was down: deliver now.
                self.hass.async_create_task(self._async_fire(rid))
                return

            @callback
            def _due(_now: datetime) -> None:
                self._timers.pop(rid, None)
                self.hass.async_create_task(self._async_fire(rid))

            self._timers[rid] = async_track_point_in_time(self.hass, _due, due)
        else:
            self._async_evaluate(rid)

    @callback
    def _async_disarm(self, rid: str) -> None:
        self._cancel_dwell(rid)
        if unsub := self._timers.pop(rid, None):
            unsub()

    async def _async_fire(self, rid: str) -> None:
        reminder = self._reminders.get(rid)
        if reminder is None:
            return
        sent = await self._async_deliver(
            reminder["person"], _text(self.hass, "fire", text=reminder["text"])
        )
        if not sent:
            _LOGGER.warning(
                "Reminder %s could not be delivered; retrying in %s s",
                rid, REMINDER_RETRY_SECONDS,
            )

            @callback
            def _retry(_now: datetime) -> None:
                self._timers.pop(rid, None)
                self.hass.async_create_task(self._async_fire(rid))

            self._timers[rid] = async_call_later(self.hass, REMINDER_RETRY_SECONDS, _retry)
            return
        self._async_disarm(rid)
        self._reminders.pop(rid, None)
        await self._async_save()
        _LOGGER.info("Reminder %s delivered", rid)

    async def _async_deliver(self, person: str, body: str) -> bool:
        """Send to every channel the person is reachable on; True if any worked."""
        sent = False
        number = self.number_for_person(person)
        client = self.hass.data.get(DOMAIN, {}).get("wa_client")
        if number and client is not None:
            sent = await client.async_send_text(number, body)
        chat = self.chat_for_person(person)
        if chat:
            from .telegram import async_send, telegram_enabled

            options = self.hass.data.get(DOMAIN, {}).get("wa_options", {})
            if telegram_enabled(options):
                sent = await async_send(self.hass, chat, body) or sent
        return sent

    # ---------- presentation ----------

    def describe(self, reminder: dict[str, Any]) -> dict[str, Any]:
        """Reminder as shown to the agent (no coordinates)."""
        out: dict[str, Any] = {
            "id": reminder["id"],
            "text": reminder["text"],
            "trigger": reminder["kind"],
            "status": reminder["status"],
        }
        if reminder.get("place"):
            out["place"] = reminder["place"]
        if reminder["kind"] == KIND_TIME:
            out["due"] = dt_util.as_local(dt_util.parse_datetime(reminder["due"])).isoformat()
        return out


@callback
def _is_person_event(event_data: dict[str, Any]) -> bool:
    return str(event_data.get("entity_id", "")).startswith(("person.", "device_tracker."))
