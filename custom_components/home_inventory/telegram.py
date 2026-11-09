"""Optional Telegram bridge, through Home Assistant's own Telegram bot integration.

Home Inventory holds no bot token and runs no poller of its own: the user sets
up the ``telegram_bot`` integration as usual (polling or webhooks), and this
module

* listens to its ``telegram_text`` / ``telegram_command`` / ``telegram_attachment``
  events,
* replies with its ``telegram_bot.send_message`` action, and
* fetches receipt photos with its ``telegram_bot.download_file`` action.

Only chats listed in the Home Inventory options are handled, so a bot that also
serves other automations (say, a family notification group) keeps doing that
untouched. Telegram passes shared locations to Home Assistant without their
coordinates, so a place for a location reminder is given as a Google Maps link.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .chat import ChatInbound
from .const import (
    CONF_TG_BOT,
    CONF_TG_CHATS,
    CONF_TG_ENABLED,
    CONF_WA_ALERT_DAYS,
    DEFAULT_EXPIRING_DAYS,
    DOMAIN,
    RECEIPT_ACCEPTED_MIME,
    RECEIPT_MAX_BYTES,
    TELEGRAM_DOMAIN,
    WA_MAX_BODY,
)

_LOGGER = logging.getLogger(__name__)

EVENT_TEXT = "telegram_text"
EVENT_COMMAND = "telegram_command"
EVENT_ATTACHMENT = "telegram_attachment"

HELP_TEXT = (
    "Hi! I manage the home inventory. Ask me things like \"what's expiring "
    "soon?\" or \"add 2 milk to the shopping list\", or send a photo of a "
    "receipt to add what you bought. For a reminder at a place, send a Google "
    "Maps link to it."
)
UNSUPPORTED_TEXT = "Send me text, a photo of a receipt, or a Google Maps link to a place."


def parse_chats(value: Any) -> dict[str, str | None]:
    """``"123=person.a, -100456"`` -> {"123": "person.a", "-100456": None}.

    Chat ids are integers (negative for groups). Anything else is skipped.
    """
    chats: dict[str, str | None] = {}
    for part in str(value or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        chat, _, person = (p.strip() for p in part.partition("="))
        try:
            chat = str(int(chat))
        except ValueError:
            continue
        chats[chat] = person if person.startswith("person.") else None
    return chats


def invalid_chat_parts(value: Any) -> list[str]:
    """The parts of the chats option that aren't ``<chat id>[=person.<id>]``."""
    bad = []
    for part in str(value or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        chat, sep, person = (p.strip() for p in part.partition("="))
        try:
            int(chat)
        except ValueError:
            bad.append(part)
            continue
        if sep and not person.startswith("person."):
            bad.append(part)
    return bad


def telegram_enabled(options: dict) -> bool:
    return bool(options.get(CONF_TG_ENABLED)) and bool(parse_chats(options.get(CONF_TG_CHATS)))


def _options(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {}).get("wa_options", {})


def _mask_chat(chat: Any) -> str:
    text = str(chat)
    return f"tg:***{text[-3:]}" if len(text) > 3 else "tg:***"


def _stats(hass: HomeAssistant) -> dict:
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "tg_stats", {"counts": {}, "last_event": None, "last_event_at": None}
    )


async def async_send(hass: HomeAssistant, chat_id: str | int, body: str) -> bool:
    """Send a plain-text message to a chat through the telegram_bot integration."""
    stats = _stats(hass)
    if not hass.services.has_service(TELEGRAM_DOMAIN, "send_message"):
        stats["last_send_error"] = "the Telegram bot integration is not set up"
        _LOGGER.error("Cannot send to Telegram: %s", stats["last_send_error"])
        return False
    data: dict[str, Any] = {
        "chat_id": [int(chat_id)],
        "message": body[:WA_MAX_BODY],
        # Item names may contain *, _ or <: never let them be read as markup.
        "parse_mode": "plain_text",
    }
    if bot := _options(hass).get(CONF_TG_BOT):
        data["config_entry_id"] = bot
    try:
        await hass.services.async_call(TELEGRAM_DOMAIN, "send_message", data, blocking=True)
    except (HomeAssistantError, vol.Invalid, ValueError) as err:
        stats["last_send_error"] = f"{type(err).__name__}: {err}"
        _LOGGER.error("Telegram send to %s failed: %s", _mask_chat(chat_id), err)
        return False
    stats["last_send_error"] = None
    return True


async def async_download(hass: HomeAssistant, file_id: str, mime: str) -> tuple[bytes, str]:
    """Fetch an attachment with ``telegram_bot.download_file``; the file is
    read and then deleted, so receipts aren't left lying around."""
    from .receipts import ReceiptError

    if not hass.services.has_service(TELEGRAM_DOMAIN, "download_file"):
        raise ReceiptError(
            "This Home Assistant can't download Telegram files (the Telegram "
            "integration's download_file action is missing) - update Home Assistant."
        )
    data: dict[str, Any] = {"file_id": file_id}
    if bot := _options(hass).get(CONF_TG_BOT):
        data["config_entry_id"] = bot
    try:
        response = await hass.services.async_call(
            TELEGRAM_DOMAIN, "download_file", data, blocking=True, return_response=True
        )
    except (HomeAssistantError, vol.Invalid, ValueError) as err:
        raise ReceiptError(f"Download failed: {err}") from err

    path = Path(str((response or {}).get("file_path") or ""))
    if not path.name:
        raise ReceiptError("Download failed: Telegram returned no file.")

    def _read_and_delete() -> bytes:
        try:
            if path.stat().st_size > RECEIPT_MAX_BYTES:
                raise ReceiptError("That image is too large.")
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

    try:
        content = await hass.async_add_executor_job(_read_and_delete)
    except OSError as err:
        raise ReceiptError(f"Download failed: {err}") from err
    return content, mime or "image/jpeg"


class TelegramInbound(ChatInbound):
    """Handles Telegram messages delivered as telegram_bot events."""

    channel = "telegram"
    stats_key = "tg_stats"

    # Senders are "tg:<chat id>", which keeps them apart from WhatsApp numbers
    # in the shared receipt sessions.
    @staticmethod
    def sender_for(chat_id: Any) -> str:
        return f"tg:{chat_id}"

    @staticmethod
    def _chat(sender: str) -> str:
        return sender.removeprefix("tg:")

    @property
    def _chats(self) -> dict[str, str | None]:
        return parse_chats(self._options.get(CONF_TG_CHATS))

    # ---------- ChatInbound hooks ----------

    def _sender_allowed(self, sender: str) -> bool:
        return self._chat(sender) in self._chats

    def _person_for(self, sender: str) -> str | None:
        return self._chats.get(self._chat(sender))

    def _user_for(self, sender: str) -> str | None:
        reminders = self._reminders
        person = self._person_for(sender)
        return reminders.user_for_person(person) if reminders and person else None

    def _conversation_id(self, sender: str) -> str:
        return f"{DOMAIN}_tg_{self._chat(sender)}"

    def _mask(self, sender: str | None) -> str:
        return _mask_chat(self._chat(str(sender)))

    async def _async_reply(self, to: str, body: str) -> None:
        ok = await async_send(self.hass, self._chat(to), body)
        self._trace("reply to %s %s", self._mask(to), "sent" if ok else "FAILED")
        self._record("reply_sent" if ok else "reply_failed")

    # ---------- events ----------

    async def async_handle_event(self, event: Event) -> None:
        try:
            await self._async_process(event.event_type, dict(event.data))
        except Exception:  # noqa: BLE001 - a bad message must not kill the bridge
            _LOGGER.exception("Error handling Telegram message")

    async def _async_process(self, event_type: str, data: dict) -> None:
        options = self._options
        if not options.get(CONF_TG_ENABLED):
            return
        # With more than one bot configured, only listen to the chosen one.
        wanted_bot = options.get(CONF_TG_BOT)
        bot_entry = (data.get("bot") or {}).get("config_entry_id")
        if wanted_bot and bot_entry and bot_entry != wanted_bot:
            return
        chat_id = data.get("chat_id")
        if chat_id is None:
            return
        sender = self.sender_for(chat_id)
        if not self._sender_allowed(sender):
            # Normal for a bot that also serves other chats: not ours to answer.
            self._trace("chat %s is not listed, ignoring", self._mask(sender))
            self._record("ignored_chat_not_listed", self._mask(sender))
            return
        self._record("message_received", f"{event_type} from {self._mask(sender)}")

        if event_type == EVENT_COMMAND:
            command = str(data.get("command") or "").lstrip("/").split("@")[0]
            if command in ("start", "help"):
                await self._async_reply(sender, HELP_TEXT)
                return
            text = " ".join([command, *map(str, data.get("args") or [])])
            await self.async_handle_text(sender, text)
            return

        if event_type == EVENT_ATTACHMENT:
            await self._async_handle_attachment(sender, data)
            return

        await self.async_handle_text(sender, str(data.get("text") or ""))

    async def _async_handle_attachment(self, sender: str, data: dict) -> None:
        file_id = data.get("file_id")
        mime = str(data.get("file_mime_type") or "").split(";")[0].strip()
        # Locations, contacts, stickers... arrive without a readable file.
        if not file_id or mime not in RECEIPT_ACCEPTED_MIME:
            self._trace("unsupported attachment (%s), replying with a hint", mime or "none")
            await self._async_reply(sender, UNSUPPORTED_TEXT)
            return
        if (data.get("file_size") or 0) > RECEIPT_MAX_BYTES:
            await self._async_reply(sender, "That image is too large.")
            return
        if not await self.async_receipts_enabled(sender):
            return

        async def fetch() -> tuple[bytes, str]:
            return await async_download(self.hass, str(file_id), mime)

        await self.async_read_receipt(sender, mime, fetch)


async def async_send_digest(hass: HomeAssistant) -> dict[str, Any]:
    """Send the expiring-items digest to every listed Telegram chat."""
    from .whatsapp import _format_expiring

    options = _options(hass)
    db = hass.data.get(DOMAIN, {}).get("db")
    if db is None:
        return {"sent": False, "reason": "integration_not_ready"}
    chats = list(parse_chats(options.get(CONF_TG_CHATS)))
    if not options.get(CONF_TG_ENABLED) or not chats:
        return {"sent": False, "reason": "not_configured"}
    days = int(options.get(CONF_WA_ALERT_DAYS) or DEFAULT_EXPIRING_DAYS)
    items = await db.get_expiring_items(days)
    if not items:
        return {"sent": False, "reason": "no_expiring_items", "days": days, "item_count": 0}
    body = _format_expiring(items, days)
    delivered = [chat for chat in chats if await async_send(hass, chat, body)]
    result: dict[str, Any] = {
        "sent": bool(delivered),
        "reason": "sent" if delivered else "send_failed",
        "item_count": len(items),
        "recipients": len(chats),
        "delivered": len(delivered),
    }
    if not delivered:
        result["error"] = _stats(hass).get("last_send_error") or "unknown"
    return result


def telegram_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """The Telegram setup, without chat ids or other identifying values."""
    options = _options(hass)
    chats = parse_chats(options.get(CONF_TG_CHATS))
    stats = _stats(hass)
    return {
        "enabled": bool(options.get(CONF_TG_ENABLED)),
        "telegram_bot_set_up": hass.services.has_service(TELEGRAM_DOMAIN, "send_message"),
        "can_download_files": hass.services.has_service(TELEGRAM_DOMAIN, "download_file"),
        "bot_entry_chosen": bool(options.get(CONF_TG_BOT)),
        "chats": len(chats),
        "chats_mapped_to_people": sum(1 for p in chats.values() if p),
        "invalid_chat_entries": len(invalid_chat_parts(options.get(CONF_TG_CHATS))),
        "listening": bool(hass.data.get(DOMAIN, {}).get("tg_unsub")),
        "last_send_error": stats.get("last_send_error"),
        "inbound_activity": stats["counts"] or "nothing received since restart",
        "inbound_last_event": stats["last_event"],
        "inbound_last_event_at": stats["last_event_at"],
    }


@callback
def async_setup_telegram(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Listen to the Telegram bot's events while Telegram is enabled."""
    async_unload_telegram(hass)
    options = {**entry.data, **entry.options}
    if not options.get(CONF_TG_ENABLED):
        return
    if not parse_chats(options.get(CONF_TG_CHATS)):
        _LOGGER.warning("Telegram is enabled but no chats are listed; nothing will be answered")
    if not hass.services.has_service(TELEGRAM_DOMAIN, "send_message"):
        _LOGGER.warning(
            "Telegram is enabled but the Telegram bot integration isn't set up yet; "
            "messages will be handled once it is"
        )
    inbound = TelegramInbound(hass)
    unsubs: list[CALLBACK_TYPE] = [
        hass.bus.async_listen(event_type, inbound.async_handle_event)
        for event_type in (EVENT_TEXT, EVENT_COMMAND, EVENT_ATTACHMENT)
    ]
    hass.data.setdefault(DOMAIN, {})["tg_unsub"] = unsubs
    _LOGGER.info("Telegram bridge listening to %s chat(s)", len(parse_chats(options.get(CONF_TG_CHATS))))


@callback
def async_unload_telegram(hass: HomeAssistant) -> None:
    for unsub in hass.data.get(DOMAIN, {}).pop("tg_unsub", None) or []:
        unsub()
