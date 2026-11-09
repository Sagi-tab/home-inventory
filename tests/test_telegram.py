"""Optional Telegram channel (through Home Assistant's telegram_bot integration)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_inventory import receipts
from custom_components.home_inventory.config_flow import validate_options
from custom_components.home_inventory.const import DOMAIN
from custom_components.home_inventory.reminders import ReminderManager, parse_people
from custom_components.home_inventory.telegram import (
    HELP_TEXT,
    UNSUPPORTED_TEXT,
    TelegramInbound,
    async_setup_telegram,
    async_unload_telegram,
    invalid_chat_parts,
    parse_chats,
    telegram_diagnostics,
)
from custom_components.home_inventory.whatsapp import async_send_expiry_digest

ALICE_CHAT = 111  # mapped to person.alice
GROUP_CHAT = -100222  # listed, not mapped
OTHER_CHAT = 999  # served by the same bot for something else

TG = {
    "telegram_enabled": True,
    "telegram_chats": f"{ALICE_CHAT}=person.alice, {GROUP_CHAT}",
}


@pytest.fixture
def bot(hass: HomeAssistant, tmp_path: Path):
    """A stand-in for the telegram_bot integration's actions."""
    fake = SimpleNamespace(sent=[], downloads=[], fail=False, files=tmp_path)

    async def send_message(call: ServiceCall) -> None:
        if fake.fail:
            raise HomeAssistantError("Bad Request: chat not found")
        fake.sent.append(dict(call.data))

    async def download_file(call: ServiceCall) -> dict:
        fake.downloads.append(dict(call.data))
        path = tmp_path / f"{call.data['file_id']}.jpg"
        path.write_bytes(b"\xff\xd8receipt")
        return {"file_path": str(path)}

    hass.services.async_register("telegram_bot", "send_message", send_message)
    hass.services.async_register(
        "telegram_bot", "download_file", download_file,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return fake


def texts(bot) -> list[tuple[list[int], str]]:
    return [(m["chat_id"], m["message"]) for m in bot.sent]


async def handle(hass, event_type: str, **data) -> None:
    await TelegramInbound(hass)._async_process(event_type, data)


# ---------- options ----------


def test_parse_and_validate_chats() -> None:
    assert parse_chats("111=person.alice, -100222\n 333 , junk, 4=switch.x") == {
        "111": "person.alice", "-100222": None, "333": None, "4": None,
    }
    assert invalid_chat_parts("111=person.alice, -100222") == []
    assert invalid_chat_parts("abc, 5=switch.x, 6=") == ["abc", "5=switch.x", "6="]
    base = {"receipts_enabled": False}
    assert validate_options({**base, "telegram_enabled": False, "telegram_chats": ""}) == {}
    assert validate_options({**base, "telegram_enabled": True, "telegram_chats": ""}) == {
        "telegram_chats": "telegram_chats_required"
    }
    assert validate_options({**base, "telegram_chats": "@alice"}) == {
        "telegram_chats": "invalid_telegram_chats"
    }


# ---------- inbound ----------


async def test_disabled_or_unlisted_chats_are_ignored(hass, setup_data, agent, bot) -> None:
    setup_data(**{**TG, "telegram_enabled": False})
    await handle(hass, "telegram_text", chat_id=ALICE_CHAT, text="hi")
    setup_data(**TG)
    await handle(hass, "telegram_text", chat_id=OTHER_CHAT, text="hi")
    assert agent == [] and bot.sent == []


async def test_text_goes_to_agent_as_mapped_user(hass, setup_data, agent, bot) -> None:
    setup_data(**TG)
    await handle(hass, "telegram_text", chat_id=ALICE_CHAT, text="what's expiring?")
    assert agent[0]["text"] == "what's expiring?"
    assert agent[0]["conversation_id"] == f"home_inventory_tg_{ALICE_CHAT}"
    assert agent[0]["context"].user_id == "user-alice"
    assert bot.sent == [{"chat_id": [ALICE_CHAT], "message": "agent says hi",
                         "parse_mode": "plain_text"}]


async def test_unmapped_chat_has_no_user_context(hass, setup_data, agent, bot) -> None:
    setup_data(**TG)
    await handle(hass, "telegram_text", chat_id=GROUP_CHAT, text="hi")
    assert agent[0]["context"] is None
    assert texts(bot) == [([GROUP_CHAT], "agent says hi")]


async def test_commands(hass, setup_data, agent, bot) -> None:
    setup_data(**TG)
    await handle(hass, "telegram_command", chat_id=ALICE_CHAT, command="/start@InventoryBot", args=[])
    await handle(hass, "telegram_command", chat_id=ALICE_CHAT, command="/shopping", args=["list"])
    assert texts(bot)[0] == ([ALICE_CHAT], HELP_TEXT)
    assert agent[0]["text"] == "shopping list"


async def test_maps_link_answers_reminder(hass, setup_data, agent, bot) -> None:
    env = setup_data(**TG)
    env.reminders.maps_reply = "📍 Location saved."
    await handle(hass, "telegram_text", chat_id=ALICE_CHAT, text="https://maps.app.goo.gl/x")
    assert env.reminders.links == [("person.alice", "https://maps.app.goo.gl/x")]
    assert agent == [] and texts(bot) == [([ALICE_CHAT], "📍 Location saved.")]


async def test_location_and_other_attachments_get_a_hint(hass, setup_data, agent, bot) -> None:
    setup_data(**TG)
    # Telegram's location share reaches Home Assistant with no file and no coordinates
    await handle(hass, "telegram_attachment", chat_id=ALICE_CHAT, text=None)
    await handle(hass, "telegram_attachment", chat_id=ALICE_CHAT, file_id="v1",
                 file_mime_type="audio/ogg")
    assert texts(bot) == [([ALICE_CHAT], UNSUPPORTED_TEXT)] * 2
    assert bot.downloads == []


async def test_other_bot_is_ignored_when_one_is_chosen(hass, setup_data, agent, bot) -> None:
    setup_data(**TG, telegram_bot_entry="bot-a")
    await handle(hass, "telegram_text", chat_id=ALICE_CHAT, text="hi",
                 bot={"config_entry_id": "bot-b"})
    assert agent == []
    await handle(hass, "telegram_text", chat_id=ALICE_CHAT, text="hi",
                 bot={"config_entry_id": "bot-a"})
    assert bot.sent[0]["config_entry_id"] == "bot-a"


async def test_send_failure_is_recorded_not_raised(hass, setup_data, agent, bot) -> None:
    setup_data(**TG)
    bot.fail = True
    await handle(hass, "telegram_text", chat_id=ALICE_CHAT, text="hi")
    diag = telegram_diagnostics(hass)
    assert "chat not found" in diag["last_send_error"]
    assert diag["inbound_activity"]["reply_failed"] == 1


async def test_receipt_flow(hass, setup_data, agent, bot, receipt_ai) -> None:
    setup_data(**TG)
    await handle(hass, "telegram_attachment", chat_id=ALICE_CHAT, file_id="ph1",
                 file_mime_type="image/jpeg", file_size=1000)
    # Fetched with the real download path: telegram_bot.download_file, then read
    receipt_ai.download.assert_not_called()  # (that's WAHA's downloader)
    assert bot.downloads == [{"file_id": "ph1"}]
    staged_bytes, staged_mime = receipt_ai.stage.call_args.args[1:]
    assert staged_bytes == b"\xff\xd8receipt" and staged_mime == "image/jpeg"
    assert not (bot.files / "ph1.jpg").exists()  # downloaded file is deleted
    assert texts(bot)[0][1] == "Got it - reading the receipt..."
    assert texts(bot)[1][1].startswith("Receipt: 2 item(s)")

    sender = TelegramInbound.sender_for(ALICE_CHAT)
    assert receipts.session_get(hass, sender) is not None
    assert receipts.session_get(hass, str(ALICE_CHAT)) is None  # kept apart from WhatsApp

    await handle(hass, "telegram_text", chat_id=ALICE_CHAT, text="yes")
    await handle(hass, "telegram_text", chat_id=ALICE_CHAT, text="skip")
    assert texts(bot)[-1][1].startswith("Added 2 item(s) to the inventory.")
    assert agent == []


async def test_receipt_limits(hass, setup_data, agent, bot, receipt_ai) -> None:
    setup_data(**TG)
    await handle(hass, "telegram_attachment", chat_id=ALICE_CHAT, file_id="big",
                 file_mime_type="image/jpeg", file_size=50 * 1024 * 1024)
    setup_data(**TG, receipts_enabled=False)
    await handle(hass, "telegram_attachment", chat_id=ALICE_CHAT, file_id="ph",
                 file_mime_type="image/jpeg", file_size=10)
    assert [t for _, t in texts(bot)] == ["That image is too large.",
                                          "Receipt scanning is turned off."]
    assert bot.downloads == []


async def test_receipt_download_unavailable(hass, setup_data, agent, receipt_ai) -> None:
    setup_data(**TG)
    sent = []

    async def send_message(call):
        sent.append(call.data["message"])

    hass.services.async_register("telegram_bot", "send_message", send_message)
    await handle(hass, "telegram_attachment", chat_id=ALICE_CHAT, file_id="ph",
                 file_mime_type="image/jpeg")
    assert "download_file action is missing" in sent[-1]


# ---------- wiring ----------


async def test_listener_follows_options(hass, setup_data, agent, bot) -> None:
    setup_data(**TG)
    entry = MockConfigEntry(domain=DOMAIN, options=TG)
    async_setup_telegram(hass, entry)
    hass.bus.async_fire("telegram_text", {"chat_id": ALICE_CHAT, "text": "hello"})
    await hass.async_block_till_done()
    assert texts(bot) == [([ALICE_CHAT], "agent says hi")]
    assert telegram_diagnostics(hass)["listening"] is True

    off = MockConfigEntry(domain=DOMAIN, options={**TG, "telegram_enabled": False})
    async_setup_telegram(hass, off)
    hass.bus.async_fire("telegram_text", {"chat_id": ALICE_CHAT, "text": "hello"})
    await hass.async_block_till_done()
    assert len(bot.sent) == 1
    async_unload_telegram(hass)


async def _db(items):
    db = MagicMock()
    db.get_expiring_items = AsyncMock(return_value=items)
    return db


async def test_digest_on_both_channels(hass, setup_data, bot) -> None:
    env = setup_data(**TG)
    hass.data[DOMAIN]["db"] = await _db([{"product_name": "Milk", "expiration_date": "2026-11-10"}])
    result = await async_send_expiry_digest(hass)
    assert result["sent"] is True
    assert result["whatsapp"]["delivered"] == 2 and result["telegram"]["delivered"] == 2
    assert sorted(c for m in bot.sent for c in m["chat_id"]) == [GROUP_CHAT, ALICE_CHAT]
    assert bot.sent[0]["message"] == env.waha.sent[0][1]  # same digest text


async def test_digest_telegram_only(hass, setup_data, bot) -> None:
    env = setup_data(**TG, whatsapp_enabled=False, waha_url="")
    env.waha.configured = False
    hass.data[DOMAIN]["db"] = await _db([{"product_name": "Milk", "expiration_date": "2026-11-10"}])
    result = await async_send_expiry_digest(hass)
    assert result["sent"] is True
    assert result["whatsapp"]["reason"] == "not_configured"
    assert result["telegram"]["delivered"] == 2 and env.waha.sent == []


async def test_reminder_reaches_telegram(hass: HomeAssistant, setup_data, bot) -> None:
    setup_data(**TG)
    hass.states.async_set("person.alice", "home", {"user_id": "user-alice"})
    hass.data[DOMAIN]["wa_client"] = None  # Alice has no WhatsApp here
    manager = ReminderManager(hass)
    manager.async_configure(parse_people(""), 150, parse_chats(TG["telegram_chats"]))
    assert manager.person_for_chat(ALICE_CHAT) == "person.alice"
    assert manager.person_for_user("user-alice") == "person.alice"
    reminder = await manager.async_create(
        "person.alice", "buy milk", "time", due=dt_util.utcnow() + timedelta(hours=1)
    )
    manager._timers.pop(reminder["id"])()  # as if the due-time timer had fired
    await manager._async_fire(reminder["id"])
    assert texts(bot) == [([ALICE_CHAT], "🔔 Reminder: buy milk")]
    assert manager.for_person("person.alice") == []
    manager.async_unload()


async def test_options_form_has_telegram_fields(hass: HomeAssistant) -> None:
    # The form only needs the flow handler; the frontend package isn't installed here
    hass.config.components.update({"frontend", "http", "webhook"})
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data={"waha_webhook_id": "hook"},
        options={"telegram_bot_entry": "bot-a", "receipts_enabled": False},
    )
    entry.add_to_hass(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    fields = [str(key) for key in form["data_schema"].schema]
    assert {"telegram_enabled", "telegram_chats", "telegram_bot_entry"} <= set(fields)
    assert fields.index("telegram_enabled") == fields.index("whatsapp_people") + 1

    bad = await hass.config_entries.options.async_configure(
        form["flow_id"], {"telegram_enabled": True, "telegram_chats": "", "receipts_enabled": False}
    )
    assert bad["errors"] == {"telegram_chats": "telegram_chats_required"}
    done = await hass.config_entries.options.async_configure(
        bad["flow_id"],
        {"telegram_enabled": True, "telegram_chats": "111=person.alice", "receipts_enabled": False},
    )
    assert done["type"] == "create_entry"
    assert "telegram_bot_entry" not in entry.options  # clearing the bot choice sticks
    assert entry.options["telegram_chats"] == "111=person.alice"
