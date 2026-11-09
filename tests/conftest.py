"""Shared fakes for the messaging tests.

The chat bridges are exercised directly (no full integration setup): the
WAHA client, the Telegram bot actions, the conversation agent, the reminder
manager and the receipt AI steps are replaced by small recording fakes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.home_inventory.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    return


class FakeWaha:
    """Records outgoing WhatsApp messages."""

    def __init__(self, ok: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.ok = ok
        self.last_error = None if ok else "HTTP 500: boom"
        self.configured = True
        self.session = "default"
        self.base_url = "http://waha:3000"
        self.headers = {"X-Api-Key": "k"}

    async def async_send_text(self, to: str, body: str) -> bool:
        self.sent.append((to, body))
        return self.ok

    async def async_resolve_lid(self, lid: str) -> str | None:
        return None

    def media_url(self, url: str) -> str | None:
        return f"{self.base_url}/api/files/x.jpg" if url else None

    async def async_session_status(self) -> dict:
        return {"status": "WORKING"}


class FakeReminders:
    """Just the reminder-manager surface the chat bridges use."""

    def __init__(self) -> None:
        self.people = {"972500000001": "person.alice"}
        self.tg_people = {"111": "person.alice"}
        self.users = {"person.alice": "user-alice"}
        self.location_reply: str | None = None
        self.maps_reply: str | None = None
        self.locations: list = []
        self.links: list = []

    def person_for_number(self, number: str) -> str | None:
        return self.people.get("".join(c for c in str(number) if c.isdigit()))

    def person_for_chat(self, chat_id: Any) -> str | None:
        return self.tg_people.get(str(chat_id))

    def user_for_number(self, number: str) -> str | None:
        person = self.person_for_number(number)
        return self.users.get(person) if person else None

    def user_for_person(self, person: str | None) -> str | None:
        return self.users.get(person) if person else None

    async def async_attach_shared_location(self, person, lat, lon):
        self.locations.append((person, lat, lon))
        return self.location_reply

    async def async_attach_maps_link(self, person, text):
        self.links.append((person, text))
        return self.maps_reply


@pytest.fixture
def agent():
    """Replace the conversation agent; records calls, answers 'agent says hi'."""
    calls: list[dict] = []

    async def converse(hass, **kwargs):
        calls.append(kwargs)
        speech = {"plain": {"speech": "agent says hi"}}
        return SimpleNamespace(response=SimpleNamespace(speech=speech))

    with patch(
        "homeassistant.components.conversation.async_converse", side_effect=converse
    ):
        yield calls


@pytest.fixture
def receipt_ai():
    """Replace download/AI/commit steps of receipt scanning with fakes."""
    from custom_components.home_inventory import receipts

    lines = [
        {"name": "Milk", "display": "Milk", "quantity": 1, "unit": "pcs",
         "match": "new", "category": "dairy", "perishable": True},
        {"name": "Rice", "display": "Rice", "quantity": 2, "unit": "pcs",
         "match": "new", "category": "pantry"},
    ]
    fakes = SimpleNamespace(
        download=AsyncMock(return_value=(b"img", "image/jpeg")),
        stage=AsyncMock(return_value=("/tmp/x.jpg", "media-source://x")),
        extract=AsyncMock(return_value=[{"name": "Milk"}, {"name": "Rice"}]),
        resolve=AsyncMock(side_effect=lambda hass, items: [dict(line) for line in lines]),
        commit=AsyncMock(return_value={"count": 2, "products_created": 1,
                                        "fulfilled_shopping_ids": []}),
        cleanup=AsyncMock(),
        parse_dates=AsyncMock(return_value={1: "2026-12-01"}),
    )
    with (
        patch.object(receipts, "async_download_media", fakes.download),
        patch.object(receipts, "async_stage_media", fakes.stage),
        patch.object(receipts, "async_extract_items", fakes.extract),
        patch.object(receipts, "async_resolve_lines", fakes.resolve),
        patch.object(receipts, "async_commit", fakes.commit),
        patch.object(receipts, "async_cleanup", fakes.cleanup),
        patch.object(receipts, "async_parse_dates", fakes.parse_dates),
    ):
        yield fakes


@pytest.fixture
def setup_data(hass):
    """Populate hass.data the way async_setup_entry does, with fakes."""

    def _setup(**options) -> SimpleNamespace:
        waha = FakeWaha(ok=options.pop("_send_ok", True))
        reminders = FakeReminders()
        opts = {
            "whatsapp_enabled": True,
            "waha_url": "http://waha:3000",
            "whatsapp_allowed_senders": "972500000001, 972500000002",
            "waha_webhook_id": "hook123",
            **options,
        }
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN].update(
            {"wa_options": opts, "wa_client": waha, "reminders": reminders}
        )
        return SimpleNamespace(waha=waha, reminders=reminders, options=opts)

    return _setup
