"""WhatsApp bridge over WAHA (WhatsApp HTTP API): outbound messages + inbound webhook.

WAHA links a WhatsApp number as a companion device (a QR code scanned once,
like WhatsApp Web), so there are no message templates, no 24-hour window and
no per-message cost: any message can be sent at any time.

Outbound
    ``POST {waha}/api/sendText`` with the session name and a ``<digits>@c.us``
    chat id, authenticated with the WAHA API key.

Inbound
    WAHA POSTs every event to a Home Assistant webhook. The webhook only
    accepts requests from the local network and its id is random, so only
    something on the LAN that knows the id can reach it. On top of that an
    optional HMAC key is checked, the sender must be on the allowlist, and
    message ids are de-duplicated because WAHA retries failed deliveries.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from collections import deque
from datetime import datetime, time as dt_time
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import web
from homeassistant.components import conversation, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util
from yarl import URL

from .const import (
    CONF_RECEIPTS_ENABLED,
    CONF_WA_AGENT_ID,
    CONF_WA_ALERT_DAYS,
    CONF_WA_ALERT_TIME,
    CONF_WA_ALLOWED_SENDERS,
    CONF_WA_DEBUG,
    CONF_WA_ENABLED,
    CONF_WAHA_API_KEY,
    CONF_WAHA_HMAC_KEY,
    CONF_WAHA_SESSION,
    CONF_WAHA_URL,
    CONF_WAHA_WEBHOOK_ID,
    DEFAULT_EXPIRING_DAYS,
    DEFAULT_WA_ALERT_TIME,
    DEFAULT_WAHA_SESSION,
    DOMAIN,
    LLM_API_ID,
    RECEIPT_ACCEPTED_MIME,
    WA_MAX_BODY,
    WHATSAPP_TIMEOUT,
)
from .storage import Database

_LOGGER = logging.getLogger(__name__)

# Remember recently handled message ids so WAHA's retries are no-ops.
_SEEN_MAX = 200

_WHITESPACE_RE = re.compile(r"\s+")

# Chat id suffixes. Phone numbers are "<digits>@c.us" (some engines report
# "@s.whatsapp.net"); "@lid" is WhatsApp's hidden per-user id, which newer
# accounts send from and which has to be mapped back to a phone number.
_PHONE_SUFFIXES = ("@c.us", "@s.whatsapp.net")
_LID_SUFFIX = "@lid"


def _digits(value: str) -> str:
    """Normalise a phone number to digits only for comparison."""
    return "".join(ch for ch in str(value) if ch.isdigit())


def _mask(number: str | None) -> str:
    """Partially mask a phone number so logs stay shareable."""
    digits = _digits(number or "")
    if len(digits) <= 6:
        return "***"
    return f"{digits[:3]}***{digits[-4:]}"


def _chat_id(number: str) -> str:
    """WAHA chat id for a phone number (country code first, no +)."""
    return f"{_digits(number)}@c.us"


def _allowed_numbers(options: dict) -> list[str]:
    """The allowlist, which doubles as the digest recipient list."""
    numbers = options.get(CONF_WA_ALLOWED_SENDERS) or []
    if isinstance(numbers, str):
        numbers = [p.strip() for p in numbers.split(",")]
    return [_digits(p) for p in numbers if _digits(p)]


def _record(hass: HomeAssistant, event: str, detail: str | None = None) -> None:
    """Count inbound webhook activity so it can be inspected without logs.

    Home Assistant's log panel only renders warnings and above, so the info
    level trace is easy to miss. These counters make "did anything ever
    arrive?" answerable straight from the diagnostics service.
    """
    stats = hass.data.setdefault(DOMAIN, {}).setdefault(
        "wa_stats", {"counts": {}, "last_event": None, "last_event_at": None}
    )
    stats["counts"][event] = stats["counts"].get(event, 0) + 1
    stats["last_event"] = f"{event}: {detail}" if detail else event
    stats["last_event_at"] = dt_util.utcnow().isoformat(timespec="seconds")


def _trace(options: dict, msg: str, *args: Any) -> None:
    """Log the message flow.

    With the debug toggle on this logs at info, which _apply_debug_logging
    makes visible; otherwise it stays at debug. That saves the user editing
    configuration.yaml just to see why a message went nowhere.
    """
    if options.get(CONF_WA_DEBUG):
        _LOGGER.info("[whatsapp] " + msg, *args)
    else:
        _LOGGER.debug("[whatsapp] " + msg, *args)


# The integration's package logger (custom_components.home_inventory), so the
# receipt trace is covered too.
_PACKAGE_LOGGER = logging.getLogger(__package__)
_debug_level_raised = False


def _apply_debug_logging(enabled: bool) -> None:
    """Let info records through while the verbose toggle is on.

    Home Assistant's default log level is warning, which would swallow the
    info-level trace. Only ever raise verbosity, so a level the user set in
    configuration.yaml (e.g. debug) is left alone, and only undo our own change.
    """
    global _debug_level_raised
    if enabled:
        if _PACKAGE_LOGGER.getEffectiveLevel() > logging.INFO:
            _PACKAGE_LOGGER.setLevel(logging.INFO)
            _debug_level_raised = True
    elif _debug_level_raised:
        _PACKAGE_LOGGER.setLevel(logging.NOTSET)
        _debug_level_raised = False


def missing_config(options: dict) -> list[str]:
    """Settings still needed before a message can be sent out."""
    missing = []
    if not options.get(CONF_WA_ENABLED):
        missing.append("whatsapp_enabled")
    if not str(options.get(CONF_WAHA_URL) or "").strip():
        missing.append("waha_url")
    return missing


def missing_inbound_config(options: dict) -> list[str]:
    """Settings still needed before an incoming message can be accepted.

    Separate from the outbound list: sending can be fully configured while
    every incoming message is still rejected, which is exactly the state that
    looks like "nothing happens" from the outside.
    """
    missing = []
    if not options.get(CONF_WAHA_WEBHOOK_ID):
        missing.append("webhook_id")
    if not _allowed_numbers(options):
        missing.append("allowed_senders")
    return missing


class WahaClient:
    """Thin wrapper over the WAHA REST API."""

    def __init__(self, hass: HomeAssistant, options: dict) -> None:
        self.hass = hass
        self._options = options
        # WAHA's rejection reason from the most recent call, surfaced by the
        # send_expiry_alert and diagnostics services.
        self.last_error: str | None = None
        # lid -> phone digits. A lid never changes owner, so this only grows.
        self._lid_cache: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return not missing_config(self._options)

    @property
    def session(self) -> str:
        return str(self._options.get(CONF_WAHA_SESSION) or DEFAULT_WAHA_SESSION)

    @property
    def base_url(self) -> str:
        return str(self._options.get(CONF_WAHA_URL) or "").strip().rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if api_key := self._options.get(CONF_WAHA_API_KEY):
            headers["X-Api-Key"] = str(api_key)
        return headers

    async def _request(
        self, method: str, path: str, payload: dict | None = None
    ) -> tuple[int, Any]:
        """Call WAHA. Returns (status, parsed body); status 0 on a network error."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=WHATSAPP_TIMEOUT),
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    # WAHA's error body ("session not found", "unauthorized",
                    # ...) is the only diagnostic there is.
                    self.last_error = f"HTTP {resp.status}: {text[:300]}"
                    return resp.status, None
                try:
                    return resp.status, await resp.json(content_type=None)
                except ValueError:
                    return resp.status, text
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self.last_error = f"{type(err).__name__}: {err}"
            return 0, None

    async def async_send_text(self, to: str, body: str) -> bool:
        """Send a text message to a phone number."""
        if not self.configured:
            return False
        status, _ = await self._request(
            "POST",
            "/api/sendText",
            {"session": self.session, "chatId": _chat_id(to), "text": body[:WA_MAX_BODY]},
        )
        if status and status < 400:
            self.last_error = None
            return True
        _LOGGER.error("WhatsApp send to %s failed: %s", _mask(to), self.last_error)
        return False

    async def async_session_status(self) -> dict[str, Any]:
        """The WAHA session's state, e.g. WORKING or SCAN_QR_CODE."""
        if not self.base_url:
            return {"status": "not_configured"}
        status, data = await self._request(
            "GET", f"/api/sessions/{quote(self.session, safe='')}"
        )
        if not status:
            return {"status": "unreachable", "error": self.last_error}
        if status >= 400 or not isinstance(data, dict):
            return {"status": "error", "error": self.last_error}
        me = data.get("me") or {}
        return {
            "status": data.get("status"),
            "engine": (data.get("engine") or {}).get("engine"),
            "linked_number": _mask((me.get("id") or "").split("@")[0]) if me else None,
        }

    async def async_resolve_lid(self, lid: str) -> str | None:
        """Map a hidden ``@lid`` id to the sender's phone number, if WAHA knows it."""
        if lid in self._lid_cache:
            return self._lid_cache[lid]
        status, data = await self._request(
            "GET",
            f"/api/{quote(self.session, safe='')}/lids/{quote(lid, safe='')}",
        )
        phone = _digits(data.get("pn") or "") if isinstance(data, dict) else ""
        if status and status < 400 and phone:
            self._lid_cache[lid] = phone
            return phone
        return None

    def media_url(self, url: str) -> str | None:
        """Rebase a media URL from a webhook onto the configured WAHA address.

        WAHA builds media links from its own idea of its hostname (often
        ``localhost``), which is wrong from Home Assistant's side. Keeping only
        the path also means the API key is never sent anywhere but WAHA.
        """
        parsed = URL(url)
        if not parsed.path.startswith("/api/files/"):
            return None
        return f"{self.base_url}{parsed.raw_path_qs}"


async def _async_sender_number(
    client: WahaClient, message: dict
) -> str | None:
    """Phone number (digits) of whoever sent ``message``, or None to ignore it."""
    sender = str(message.get("from") or "")
    if sender.endswith(_PHONE_SUFFIXES):
        return _digits(sender.split("@")[0])
    if sender.endswith(_LID_SUFFIX):
        # Some engines put the phone number alongside the lid in their raw
        # data; otherwise ask WAHA's lid mapping.
        raw = message.get("_data")
        key = (raw.get("key") or {}) if isinstance(raw, dict) else {}
        for candidate in (key.get("senderPn"), key.get("remoteJidAlt")):
            if candidate and str(candidate).endswith(_PHONE_SUFFIXES):
                return _digits(str(candidate).split("@")[0])
        return await client.async_resolve_lid(sender)
    # Groups (@g.us), status updates and newsletters are never commands.
    return None


class WhatsAppInbound:
    """Handles messages delivered by the WAHA webhook."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._seen: deque[str] = deque(maxlen=_SEEN_MAX)

    @property
    def _options(self) -> dict:
        return self.hass.data.get(DOMAIN, {}).get("wa_options", {})

    @property
    def _client(self) -> WahaClient | None:
        return self.hass.data.get(DOMAIN, {}).get("wa_client")

    def _signature_ok(self, raw: bytes, header: str | None) -> bool:
        """Validate WAHA's X-Webhook-Hmac (sha512 of the raw body), when a key is set."""
        key = self._options.get(CONF_WAHA_HMAC_KEY)
        if not key:
            return True
        if not header:
            return False
        expected = hmac.new(str(key).encode(), raw, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, header.strip().lower())

    async def async_handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        raw = await request.read()
        options = self._options
        _trace(options, "webhook POST received (%s bytes)", len(raw))
        _record(hass, "post_received", f"{len(raw)} bytes")

        if not options.get(CONF_WA_ENABLED):
            _record(hass, "ignored_disabled")
            return web.Response(text="disabled")

        if not self._signature_ok(raw, request.headers.get("X-Webhook-Hmac")):
            _LOGGER.warning(
                "WhatsApp webhook HMAC rejected - check that the WAHA HMAC key "
                "matches the one in the Home Inventory options"
            )
            _record(hass, "rejected_bad_signature")
            return web.Response(status=401, text="bad signature")

        try:
            event = await request.json()
        except ValueError:
            _record(hass, "rejected_bad_json")
            return web.Response(status=400, text="bad json")

        kind = event.get("event")
        if kind != "message":
            # Acks, presence, session status... only incoming messages matter.
            _trace(options, "ignoring %s event", kind)
            _record(hass, "ignored_event", str(kind))
            return web.Response(text="ignored")

        client = self._client
        if client is not None and event.get("session") not in (None, client.session):
            _trace(options, "ignoring message for session %s", event.get("session"))
            _record(hass, "ignored_other_session", str(event.get("session")))
            return web.Response(text="ignored")

        # Answer immediately so WAHA does not retry; process in the background.
        hass.async_create_task(self._async_handle_message(event.get("payload") or {}))
        return web.Response(text="ok")

    async def _async_handle_message(self, message: dict) -> None:
        try:
            await self._async_process(message)
        except Exception:  # noqa: BLE001 - a bad message must not kill the bridge
            _LOGGER.exception("Error handling WhatsApp message")

    async def _async_process(self, message: dict) -> None:
        options = self._options
        client = self._client
        if client is None:
            return

        msg_id = message.get("id")
        if message.get("fromMe"):
            # The bot's own replies come back as events too.
            _trace(options, "own message %s ignored", msg_id)
            return
        if msg_id:
            if msg_id in self._seen:
                _trace(options, "duplicate %s ignored (WAHA retry)", msg_id)
                return
            self._seen.append(msg_id)

        sender = await _async_sender_number(client, message)
        _trace(
            options,
            "message %s from %s (%s) media=%s",
            msg_id,
            _mask(sender),
            message.get("from"),
            message.get("hasMedia"),
        )
        if sender is None:
            if str(message.get("from") or "").endswith(_LID_SUFFIX):
                _LOGGER.warning(
                    "Ignoring WhatsApp message: could not map %s to a phone "
                    "number. Save the sender as a contact on the bot's phone",
                    message.get("from"),
                )
                _record(self.hass, "rejected_unresolved_lid")
            return

        _record(self.hass, "message_received", f"from {_mask(sender)}")
        if not self._sender_allowed(sender):
            _LOGGER.warning(
                "Ignoring WhatsApp message from %s (not in allowed senders)",
                _mask(sender),
            )
            _record(self.hass, "rejected_sender_not_allowed", _mask(sender))
            return

        if message.get("hasMedia"):
            await self._async_handle_media(sender, message.get("media") or {})
            return

        text = str(message.get("body") or "").strip()
        if not text:
            _trace(options, "empty message body, nothing to do")
            return

        # A pending receipt gets first refusal on the reply: "yes" means confirm
        # the receipt, not something for the agent to interpret. Anything it
        # does not consume falls through to normal conversation.
        if await self._async_receipt_reply(sender, text):
            return

        _trace(options, "asking agent %r: %r",
               options.get(CONF_WA_AGENT_ID) or "<default>", text[:200])
        answer = await self._async_converse(text, sender)
        _trace(options, "agent answered: %r", (answer or "")[:200])
        if answer:
            await self._async_reply(sender, answer)
        else:
            _LOGGER.warning(
                "Conversation agent returned no reply for %s; nothing sent",
                _mask(sender),
            )

    async def _async_handle_media(self, sender: str, media: dict) -> None:
        """Route an attachment: receipts are read, anything else gets a hint."""
        mime = str(media.get("mimetype") or "").split(";")[0].strip()
        if mime not in RECEIPT_ACCEPTED_MIME:
            _trace(self._options, "unsupported attachment (%s), replying with a hint", mime)
            await self._async_reply(sender, "Send me text, or a photo of a receipt.")
            return
        await self._async_handle_receipt(sender, media, mime)

    async def _async_handle_receipt(self, sender: str, media: dict, mime: str) -> None:
        """Download a photo/PDF, extract the items, and offer them for confirm."""
        from . import receipts

        options = self._options
        if not options.get(CONF_RECEIPTS_ENABLED, True):
            await self._async_reply(sender, "Receipt scanning is turned off.")
            return

        client = self._client
        url = client.media_url(str(media.get("url") or "")) if client else None
        if not url:
            # WAHA reports why it could not fetch the file from WhatsApp.
            _LOGGER.warning(
                "WhatsApp attachment has no downloadable file: %s",
                media.get("error") or "no media url",
            )
            await self._async_reply(
                sender, "I couldn't download that file - please send it again."
            )
            return

        _record(self.hass, "receipt_received", mime)
        _trace(options, "receipt %s from %s", mime, _mask(sender))
        await self._async_reply(sender, "Got it - reading the receipt...")

        path = None
        try:
            data, mime = await receipts.async_download_media(
                self.hass, url, client.headers, mime
            )
            path, content_id = await receipts.async_stage_media(self.hass, data, mime)
            items = await receipts.async_extract_items(
                self.hass, content_id, mime
            )
            if not items:
                _record(self.hass, "receipt_empty")
                await self._async_reply(
                    sender,
                    "I couldn't find any products on that receipt. Try a "
                    "photo taken straight on, in good light, close enough "
                    "that the item lines are readable - and one photo per "
                    "part if the receipt is long.",
                )
                return

            lines = await receipts.async_resolve_lines(self.hass, items)
            receipts.session_start(self.hass, sender, lines)
            _record(self.hass, "receipt_extracted", f"{len(lines)} lines")
            await self._async_reply(sender, receipts.format_preview(lines))
        except receipts.ReceiptError as err:
            _record(self.hass, "receipt_failed", str(err)[:80])
            _LOGGER.error("Receipt processing failed: %s", err)
            await self._async_reply(sender, str(err))
        except Exception as err:  # noqa: BLE001
            _record(self.hass, "receipt_failed", type(err).__name__)
            _LOGGER.exception("Receipt processing crashed")
            await self._async_reply(sender, f"Something went wrong: {err}")
        finally:
            if path is not None:
                await receipts.async_cleanup(self.hass, path)

    async def _async_receipt_reply(self, sender: str, text: str) -> bool:
        """Handle a reply belonging to a pending receipt.

        Returns True when the message was consumed, so the caller knows not to
        pass it on to the conversation agent.
        """
        from . import receipts

        session = receipts.session_get(self.hass, sender)
        if not session:
            return False

        answer = text.strip().lower()

        if session["state"] == "awaiting_confirm":
            if answer in receipts.CANCEL_WORDS:
                receipts.session_clear(self.hass, sender)
                await self._async_reply(sender, "Discarded - nothing was added.")
                return True
            # "drop 3" before the confirm check: a misread line should cost
            # that line, not the whole receipt.
            drop = receipts.parse_drop_request(text, len(session["lines"]))
            if drop is not None:
                await self._async_drop_lines(sender, session, drop)
                return True

            if answer not in receipts.CONFIRM_WORDS:
                # Not an answer to us; let the agent field it and keep waiting.
                return False

            lines = session["lines"]
            perishables = [
                (i, line) for i, line in enumerate(lines) if receipts.is_perishable(line)
            ]
            if perishables:
                session["state"] = "awaiting_dates"
                session["pending_dates"] = perishables
                await self._async_reply(
                    sender, receipts.format_date_prompt(perishables)
                )
                return True
            await self._async_finish_receipt(sender, session)
            return True

        if session["state"] == "awaiting_dates":
            perishables = session["pending_dates"]
            if answer not in receipts.SKIP_WORDS:
                try:
                    dates = await receipts.async_parse_dates(
                        self.hass, text, perishables
                    )
                except receipts.ReceiptError as err:
                    await self._async_reply(
                        sender, f"{err} Adding without dates."
                    )
                    dates = {}
                for number, iso in dates.items():
                    line_index = perishables[number - 1][0]
                    session["lines"][line_index]["expiration_date"] = iso
            await self._async_finish_receipt(sender, session)
            return True

        return False

    async def _async_drop_lines(
        self, sender: str, session: dict, drop: list[int]
    ) -> None:
        """Remove the named lines and re-show the receipt for confirmation."""
        from . import receipts

        if not drop:
            await self._async_reply(
                sender,
                f"That receipt only has {len(session['lines'])} line(s) - "
                "reply with a number from the list, or YES to add them all.",
            )
            return

        removed = set(drop)
        session["lines"] = [
            line for index, line in enumerate(session["lines"])
            if index not in removed
        ]
        _record(self.hass, "receipt_lines_dropped", str(len(removed)))
        if not session["lines"]:
            receipts.session_clear(self.hass, sender)
            await self._async_reply(
                sender, "That was every line - nothing was added."
            )
            return
        await self._async_reply(
            sender,
            f"Dropped {len(removed)} line(s).\n\n"
            + receipts.format_preview(session["lines"]),
        )

    async def _async_finish_receipt(self, sender: str, session: dict) -> None:
        from . import receipts

        try:
            result = await receipts.async_commit(self.hass, session["lines"])
        except receipts.ReceiptError as err:
            _record(self.hass, "receipt_failed", str(err)[:80])
            await self._async_reply(sender, str(err))
            return
        finally:
            receipts.session_clear(self.hass, sender)

        _record(self.hass, "receipt_committed", f"{result['count']} items")
        summary = [f"Added {result['count']} item(s) to the inventory."]
        if result["products_created"]:
            summary.append(f"{result['products_created']} new product(s) created.")
        if result["fulfilled_shopping_ids"]:
            summary.append(
                f"{len(result['fulfilled_shopping_ids'])} shopping list "
                "item(s) marked as bought."
            )
        await self._async_reply(sender, " ".join(summary))

    def _sender_allowed(self, sender: str) -> bool:
        allowed = _allowed_numbers(self._options)
        if not allowed:
            # An empty allowlist would let anyone who learns the number drive
            # the inventory - refuse instead.
            _LOGGER.error("No WhatsApp allowed senders configured; ignoring message")
            return False
        return _digits(sender) in allowed

    async def _async_converse(self, text: str, sender: str) -> str | None:
        """Hand the text to the configured conversation agent."""
        agent_id = self._options.get(CONF_WA_AGENT_ID)
        # One conversation per sender keeps follow-ups ("make it 3 instead")
        # in context across messages.
        conversation_id = f"{DOMAIN}_wa_{_digits(sender)}"
        try:
            result = await conversation.async_converse(
                self.hass,
                text=text,
                conversation_id=conversation_id,
                context=None,
                language=self.hass.config.language,
                agent_id=agent_id,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Conversation agent failed")
            return f"Sorry, I couldn't process that ({err})."

        try:
            return result.response.speech["plain"]["speech"]
        except (AttributeError, KeyError, TypeError):
            return "Done."

    async def _async_reply(self, to: str, body: str) -> None:
        client = self._client
        if client is None:
            _LOGGER.error("Cannot reply: WhatsApp client not set up")
            return
        ok = await client.async_send_text(to, body)
        _trace(self._options, "reply to %s %s", _mask(to),
               "sent" if ok else f"FAILED: {client.last_error}")
        _record(
            self.hass,
            "reply_sent" if ok else "reply_failed",
            None if ok else client.last_error,
        )


def _collapse_whitespace(text: str) -> str:
    """Flatten whitespace so one item stays on one line."""
    return _WHITESPACE_RE.sub(" ", str(text)).strip()


def _entries(items: list[dict]) -> list[str]:
    """One sanitised description per expiring row, without a bullet."""
    entries = []
    for item in items:
        name = item.get("product_name") or item.get("product_name_he") or "Item"
        qty = item.get("quantity")
        expires = item.get("expiration_date") or "?"
        location = item.get("location")
        parts = [str(name)]
        if qty:
            parts.append(f"x{qty:g}" if isinstance(qty, (int, float)) else f"x{qty}")
        parts.append(f"— {expires}")
        if location:
            parts.append(f"({location})")
        entries.append(_collapse_whitespace(" ".join(parts)))
    return entries


def _format_expiring(items: list[dict], days: int) -> str:
    """Render the expiring rows as a WhatsApp message, one item per line."""
    header = f"🕒 {len(items)} item(s) expiring within {days} day(s):"
    return "\n".join([header, *(f"• {entry}" for entry in _entries(items))])


def _parse_time(value: str) -> dt_time:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value), fmt).time()
        except ValueError:
            continue
    return datetime.strptime(DEFAULT_WA_ALERT_TIME, "%H:%M:%S").time()


def webhook_url(hass: HomeAssistant, options: dict) -> str:
    """The address WAHA should POST events to (reachable from the LAN)."""
    webhook_id = options.get(CONF_WAHA_WEBHOOK_ID) or "<not set>"
    try:
        base = get_url(hass, allow_external=False, allow_ip=True)
    except NoURLAvailableError:
        base = "http://<home-assistant-ip>:8123"
    return f"{base}/api/webhook/{webhook_id}"


async def async_send_expiry_digest(hass: HomeAssistant) -> dict[str, Any]:
    """Send the expiring-items digest now.

    Returns a result dict rather than a bare boolean: "nothing was sent" has
    several very different causes (not configured, nothing expiring, WAHA
    rejected it) and the caller needs to tell them apart.
    """
    data = hass.data.get(DOMAIN, {})
    client: WahaClient | None = data.get("wa_client")
    options: dict = data.get("wa_options", {})
    db: Database | None = data.get("db")

    if client is None or db is None:
        return {"sent": False, "reason": "integration_not_ready"}

    if not client.configured:
        missing = missing_config(options)
        _LOGGER.warning("WhatsApp digest skipped; missing config: %s", missing)
        return {"sent": False, "reason": "not_configured", "missing": missing}

    days = int(options.get(CONF_WA_ALERT_DAYS) or DEFAULT_EXPIRING_DAYS)
    items = await db.get_expiring_items(days)
    if not items:
        # By far the most common reason for "nothing happened", so say it at a
        # level the user actually sees rather than hiding it behind debug.
        _LOGGER.info(
            "No items expiring within %s days; nothing to send", days
        )
        return {
            "sent": False,
            "reason": "no_expiring_items",
            "days": days,
            "item_count": 0,
        }

    recipients = _allowed_numbers(options)
    if not recipients:
        _LOGGER.warning("WhatsApp digest skipped; no recipient numbers configured")
        return {"sent": False, "reason": "no_recipients", "item_count": len(items)}

    body = _format_expiring(items, days)
    delivered = [r for r in recipients if await client.async_send_text(r, body)]

    result: dict[str, Any] = {
        "sent": bool(delivered),
        "reason": "sent" if delivered else "send_failed",
        "item_count": len(items),
        "recipients": len(recipients),
        "delivered": len(delivered),
        "preview": body,
    }
    if not delivered:
        result["error"] = client.last_error or "unknown"
    return result


async def async_whatsapp_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """Summarise the resolved WhatsApp setup, without revealing secrets.

    Reports only whether each secret is present, never its value, so the
    output is safe to paste when asking for help.
    """
    data = hass.data.get(DOMAIN, {})
    options: dict = data.get("wa_options", {})
    client: WahaClient | None = data.get("wa_client")
    db: Database | None = data.get("db")

    days = int(options.get(CONF_WA_ALERT_DAYS) or DEFAULT_EXPIRING_DAYS)
    expiring = len(await db.get_expiring_items(days)) if db else None

    stats = data.get(
        "wa_stats", {"counts": {}, "last_event": None, "last_event_at": None}
    )
    missing_inbound = missing_inbound_config(options)
    if missing_inbound:
        _LOGGER.warning(
            "WhatsApp cannot accept incoming messages; missing: %s",
            ", ".join(missing_inbound),
        )

    agent = options.get(CONF_WA_AGENT_ID)
    return {
        "enabled": bool(options.get(CONF_WA_ENABLED)),
        "waha_url": client.base_url if client else None,
        "waha_session": client.session if client else None,
        # Live check: WORKING means linked and ready, SCAN_QR_CODE means the
        # number still has to be linked in the WAHA dashboard.
        "waha_session_status": (
            await client.async_session_status() if client else None
        ),
        "webhook_url": webhook_url(hass, options),
        "webhook_registered": bool(data.get("wa_webhook_registered")),
        "llm_api_registered": bool(data.get("llm_api_registered")),
        "llm_api_id": LLM_API_ID,
        "debug_logging": bool(options.get(CONF_WA_DEBUG)),
        # Presence only - never the values themselves.
        "api_key_set": bool(options.get(CONF_WAHA_API_KEY)),
        "hmac_key_set": bool(options.get(CONF_WAHA_HMAC_KEY)),
        "missing_for_sending": missing_config(options),
        # Inbound is reported separately: outbound can be perfectly configured
        # while every incoming message is rejected.
        "missing_for_receiving": missing_inbound,
        "inbound_ready": not missing_inbound,
        "recipients": [_mask(s) for s in _allowed_numbers(options)],
        "conversation_agent": agent or "<default agent>",
        "agent_configured": bool(agent),
        "alert_time": options.get(CONF_WA_ALERT_TIME) or DEFAULT_WA_ALERT_TIME,
        "alert_days": days,
        "expiring_now": expiring,
        "last_send_error": client.last_error if client else None,
        # Inbound activity since the last restart. All-zero counts mean WAHA
        # has never delivered anything: check its webhook URL and events.
        "inbound_activity": stats["counts"] or "nothing received since restart",
        "inbound_last_event": stats["last_event"],
        "inbound_last_event_at": stats["last_event_at"],
        # What the model actually said on the last receipt. "No products
        # found" has several quite different causes that the WhatsApp reply
        # cannot tell apart - an empty list, a shape we could not read, or a
        # model that never looked at the image - and this separates them
        # without going near the log files. Product names, not secrets.
        "last_ai_task": stats.get("last_ai_task"),
        "last_ai_reply": stats.get("last_ai_reply") or "no AI reply since restart",
        "last_ai_reply_at": stats.get("last_ai_reply_at"),
        "last_ai_reply_truncated": bool(stats.get("last_ai_reply_truncated")),
    }


@callback
def async_setup_whatsapp(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the webhook, client and daily digest schedule."""
    options = {**entry.data, **entry.options}
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["wa_options"] = options
    hass.data[DOMAIN]["wa_client"] = WahaClient(hass, options)
    _apply_debug_logging(bool(options.get(CONF_WA_DEBUG)))

    # The handler reads options from hass.data on each request, so it only
    # needs registering once even when options are updated later.
    webhook_id = options.get(CONF_WAHA_WEBHOOK_ID)
    if webhook_id and not hass.data[DOMAIN].get("wa_webhook_registered"):
        inbound = WhatsAppInbound(hass)
        webhook.async_register(
            hass,
            DOMAIN,
            "Home Inventory WhatsApp (WAHA)",
            webhook_id,
            inbound.async_handle_webhook,
            local_only=True,
            allowed_methods=["POST"],
        )
        hass.data[DOMAIN]["wa_webhook_registered"] = webhook_id
        _LOGGER.info("WhatsApp webhook registered (LLM API id: %s)", LLM_API_ID)

    # Replace any previous schedule so option changes take effect immediately.
    if unsub := hass.data[DOMAIN].pop("wa_unsub_timer", None):
        unsub()

    if not options.get(CONF_WA_ENABLED):
        return

    # Surface a misconfiguration at startup: without these, messages go
    # nowhere silently as far as the user can tell.
    if missing := missing_config(options):
        _LOGGER.warning("WhatsApp is enabled but cannot send; missing: %s",
                        ", ".join(missing))
    if missing := missing_inbound_config(options):
        _LOGGER.warning(
            "WhatsApp is enabled but incoming messages will be rejected; "
            "missing: %s",
            ", ".join(missing),
        )

    alert_at = _parse_time(options.get(CONF_WA_ALERT_TIME) or DEFAULT_WA_ALERT_TIME)

    async def _scheduled(_now) -> None:
        await async_send_expiry_digest(hass)

    hass.data[DOMAIN]["wa_unsub_timer"] = async_track_time_change(
        hass,
        _scheduled,
        hour=alert_at.hour,
        minute=alert_at.minute,
        second=alert_at.second,
    )


@callback
def async_unload_whatsapp(hass: HomeAssistant) -> None:
    """Undo async_setup_whatsapp."""
    data = hass.data.get(DOMAIN, {})
    if unsub := data.pop("wa_unsub_timer", None):
        unsub()
    if webhook_id := data.pop("wa_webhook_registered", None):
        webhook.async_unregister(hass, webhook_id)
    _apply_debug_logging(False)
