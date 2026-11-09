"""WhatsApp (WAHA) bridge behaviour.

Written against 0.9.0 before the chat logic was shared with Telegram, so the
refactor has to keep every one of these passing unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant

from custom_components.home_inventory import receipts
from custom_components.home_inventory.whatsapp import (
    WhatsAppInbound,
    async_send_expiry_digest,
)

ALICE = "972500000001"
BOB = "972500000002"


def msg(body: str = "", sender: str = ALICE, msg_id: str = "m1", **extra) -> dict:
    return {"id": msg_id, "from": f"{sender}@c.us", "body": body, **extra}


async def test_text_goes_to_agent_and_reply_comes_back(hass, setup_data, agent) -> None:
    env = setup_data()
    await WhatsAppInbound(hass)._async_process(msg("how much milk?"))
    assert agent[0]["text"] == "how much milk?"
    assert agent[0]["conversation_id"] == f"home_inventory_wa_{ALICE}"
    assert agent[0]["context"].user_id == "user-alice"  # sender mapped to a person
    assert env.waha.sent == [(ALICE, "agent says hi")]


async def test_unmapped_sender_has_no_user_context(hass, setup_data, agent) -> None:
    env = setup_data()
    await WhatsAppInbound(hass)._async_process(msg("hi", sender=BOB))
    assert agent[0]["context"] is None
    assert env.waha.sent == [(BOB, "agent says hi")]


async def test_sender_not_allowed_is_ignored(hass, setup_data, agent) -> None:
    env = setup_data()
    await WhatsAppInbound(hass)._async_process(msg("hi", sender="15550001111"))
    assert agent == [] and env.waha.sent == []


async def test_empty_allowlist_rejects_everyone(hass, setup_data, agent) -> None:
    env = setup_data(whatsapp_allowed_senders="")
    await WhatsAppInbound(hass)._async_process(msg("hi"))
    assert agent == [] and env.waha.sent == []


async def test_own_messages_duplicates_and_groups_are_ignored(hass, setup_data, agent) -> None:
    env = setup_data()
    inbound = WhatsAppInbound(hass)
    await inbound._async_process(msg("mine", fromMe=True))
    await inbound._async_process(msg("first", msg_id="dup"))
    await inbound._async_process(msg("first", msg_id="dup"))  # WAHA retry
    await inbound._async_process({"id": "g", "from": "123@g.us", "body": "group"})
    assert [c["text"] for c in agent] == ["first"]
    assert len(env.waha.sent) == 1


async def test_shared_location_completes_pending_reminder(hass, setup_data, agent) -> None:
    env = setup_data()
    env.reminders.location_reply = "📍 Location saved."
    loc = {"latitude": 32.1, "longitude": 34.8, "name": "Shop"}
    await WhatsAppInbound(hass)._async_process(msg(location=loc))
    assert env.reminders.locations == [("person.alice", 32.1, 34.8)]
    assert agent == []
    assert env.waha.sent == [(ALICE, "📍 Location saved.")]


async def test_shared_location_without_pending_reminder_goes_to_agent(hass, setup_data, agent) -> None:
    env = setup_data()
    loc = {"latitude": 32.1, "longitude": 34.8, "name": "Shop"}
    await WhatsAppInbound(hass)._async_process(msg(location=loc))
    assert agent[0]["text"].startswith("[shared location: 32.100000, 34.800000] Shop")
    assert env.waha.sent == [(ALICE, "agent says hi")]


async def test_maps_link_answers_reminder(hass, setup_data, agent) -> None:
    env = setup_data()
    env.reminders.maps_reply = "📍 Location saved."
    await WhatsAppInbound(hass)._async_process(msg("https://maps.app.goo.gl/abc"))
    assert env.reminders.links == [("person.alice", "https://maps.app.goo.gl/abc")]
    assert agent == [] and env.waha.sent == [(ALICE, "📍 Location saved.")]


async def test_unsupported_attachment_gets_a_hint(hass, setup_data, agent) -> None:
    env = setup_data()
    await WhatsAppInbound(hass)._async_process(
        msg(hasMedia=True, media={"mimetype": "audio/ogg", "url": "u"})
    )
    assert env.waha.sent == [(ALICE, "Send me text, or a photo of a receipt.")]


async def test_receipts_turned_off(hass, setup_data, receipt_ai) -> None:
    env = setup_data(receipts_enabled=False)
    await WhatsAppInbound(hass)._async_process(
        msg(hasMedia=True, media={"mimetype": "image/jpeg", "url": "http://x/api/files/a.jpg"})
    )
    assert env.waha.sent == [(ALICE, "Receipt scanning is turned off.")]
    receipt_ai.download.assert_not_called()


async def test_receipt_without_downloadable_file(hass, setup_data, receipt_ai) -> None:
    env = setup_data()
    await WhatsAppInbound(hass)._async_process(
        msg(hasMedia=True, media={"mimetype": "image/jpeg", "url": "", "error": "gone"})
    )
    assert env.waha.sent == [(ALICE, "I couldn't download that file - please send it again.")]
    receipt_ai.download.assert_not_called()


async def test_receipt_full_flow(hass, setup_data, agent, receipt_ai) -> None:
    env = setup_data()
    inbound = WhatsAppInbound(hass)
    await inbound._async_process(
        msg(hasMedia=True, media={"mimetype": "image/jpeg", "url": "http://x/api/files/a.jpg"})
    )
    # Downloaded from WAHA with the API key headers, staged, cleaned up
    url, headers, mime = receipt_ai.download.call_args.args[1:]
    assert url == "http://waha:3000/api/files/x.jpg" and headers == {"X-Api-Key": "k"}
    receipt_ai.cleanup.assert_awaited_once()
    assert env.waha.sent[0] == (ALICE, "Got it - reading the receipt...")
    assert env.waha.sent[1][1].startswith("Receipt: 2 item(s)")

    await inbound._async_process(msg("drop 2", msg_id="m2"))
    assert env.waha.sent[2][1].startswith("Dropped 1 line(s).")

    await inbound._async_process(msg("yes", msg_id="m3"))  # Milk is perishable
    assert "Milk" in env.waha.sent[3][1]
    assert receipts.session_get(hass, ALICE)["state"] == "awaiting_dates"

    await inbound._async_process(msg("1 Dec", msg_id="m4"))
    committed = receipt_ai.commit.call_args.args[1]
    assert [line["name"] for line in committed] == ["Milk"]
    assert committed[0]["expiration_date"] == "2026-12-01"
    assert env.waha.sent[4][1].startswith("Added 2 item(s) to the inventory.")
    assert receipts.session_get(hass, ALICE) is None
    assert agent == []  # the whole flow never reached the agent


async def test_receipt_cancel_and_unrelated_reply(hass, setup_data, agent, receipt_ai) -> None:
    env = setup_data()
    inbound = WhatsAppInbound(hass)
    await inbound._async_process(
        msg(hasMedia=True, media={"mimetype": "image/jpeg", "url": "http://x/api/files/a.jpg"})
    )
    # Something that isn't an answer goes to the agent; the receipt keeps waiting
    await inbound._async_process(msg("what's for dinner?", msg_id="m2"))
    assert agent[0]["text"] == "what's for dinner?"
    assert receipts.session_get(hass, ALICE) is not None
    await inbound._async_process(msg("cancel", msg_id="m3"))
    assert env.waha.sent[-1] == (ALICE, "Discarded - nothing was added.")
    receipt_ai.commit.assert_not_called()


async def test_webhook_rejections_and_acceptance(hass: HomeAssistant, setup_data, agent) -> None:
    env = setup_data(waha_webhook_hmac_key="secret")
    inbound = WhatsAppInbound(hass)
    body = json.dumps({"event": "message", "session": "default", "payload": msg("hi")}).encode()

    def request(raw: bytes, sig: str | None):
        req = MagicMock()
        req.read = AsyncMock(return_value=raw)
        req.json = AsyncMock(side_effect=lambda: json.loads(raw))
        req.headers = {"X-Webhook-Hmac": sig} if sig else {}
        return req

    good = hmac.new(b"secret", body, hashlib.sha512).hexdigest()
    assert (await inbound.async_handle_webhook(hass, "hook123", request(body, "bad"))).status == 401
    ack = json.dumps({"event": "message.ack"}).encode()
    ack_sig = hmac.new(b"secret", ack, hashlib.sha512).hexdigest()
    assert (await inbound.async_handle_webhook(hass, "hook123", request(ack, ack_sig))).text == "ignored"
    resp = await inbound.async_handle_webhook(hass, "hook123", request(body, good))
    assert resp.text == "ok"
    await hass.async_block_till_done()
    assert env.waha.sent == [(ALICE, "agent says hi")]

    env.options["whatsapp_enabled"] = False
    assert (await inbound.async_handle_webhook(hass, "hook123", request(body, good))).text == "disabled"


async def test_expiry_digest(hass, setup_data) -> None:
    env = setup_data()
    db = MagicMock()
    db.get_expiring_items = AsyncMock(return_value=[
        {"product_name": "Milk", "quantity": 1, "expiration_date": "2026-11-10", "location": "Fridge"},
    ])
    hass.data["home_inventory"]["db"] = db
    result = await async_send_expiry_digest(hass)
    assert result["sent"] is True and result["delivered"] == 2
    assert [to for to, _ in env.waha.sent] == [ALICE, BOB]
    assert env.waha.sent[0][1].startswith("🕒 1 item(s) expiring within")
    assert "telegram" not in result  # unchanged shape when Telegram is not used

    db.get_expiring_items = AsyncMock(return_value=[])
    assert (await async_send_expiry_digest(hass))["reason"] == "no_expiring_items"

    env.options["waha_url"] = ""
    env.waha.configured = False
    assert (await async_send_expiry_digest(hass))["reason"] == "not_configured"
