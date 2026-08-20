"""WhatsApp (Meta Cloud API) bridge: outbound messages + inbound webhook.

Outbound
    Free-form text replies are only permitted inside the 24-hour customer
    service window that the user's own message opens, so unprompted alerts
    (the daily expiry digest) go out as a pre-approved template instead.

Inbound
    Meta cannot present a Home Assistant bearer token, so the webhook view is
    unauthenticated (``requires_auth = False``) and instead verified three ways:
    the ``X-Hub-Signature-256`` HMAC, a sender allowlist, and message-id
    de-duplication (Meta retries deliveries that aren't acknowledged quickly).
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

import aiohttp
from aiohttp import web
from homeassistant.components import conversation
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util

from .const import (
    CONF_WA_ACCESS_TOKEN,
    CONF_WA_AGENT_ID,
    CONF_WA_DEBUG,
    CONF_WA_ALERT_DAYS,
    CONF_WA_ALERT_TIME,
    CONF_WA_ALLOWED_SENDERS,
    CONF_WA_APP_SECRET,
    CONF_RECEIPTS_ENABLED,
    CONF_WA_ENABLED,
    CONF_WA_PHONE_NUMBER_ID,
    CONF_WA_TEMPLATE_LANG,
    CONF_WA_TEMPLATE_NAME,
    CONF_WA_TEMPLATE_PARAM,
    CONF_WA_VERIFY_TOKEN,
    DEFAULT_EXPIRING_DAYS,
    DEFAULT_WA_ALERT_TIME,
    DEFAULT_WA_TEMPLATE_LANG,
    DEFAULT_WA_TEMPLATE_NAME,
    DEFAULT_WA_TEMPLATE_PARAM,
    DOMAIN,
    LLM_API_ID,
    WA_MAX_BODY,
    WA_MAX_TEMPLATE_PARAM,
    WHATSAPP_API_URL,
    WHATSAPP_TIMEOUT,
    WHATSAPP_WEBHOOK_PATH,
)
from .storage import Database

_LOGGER = logging.getLogger(__name__)

# Remember recently handled message ids so Meta's retries are no-ops.
_SEEN_MAX = 200

_WHITESPACE_RE = re.compile(r"\s+")
_TEMPLATE_SEPARATOR = " • "


def _digits(value: str) -> str:
    """Normalise a phone number to digits only for comparison."""
    return "".join(ch for ch in str(value) if ch.isdigit())


def _mask(number: str | None) -> str:
    """Partially mask a phone number so logs stay shareable."""
    digits = _digits(number or "")
    if len(digits) <= 6:
        return "***"
    return f"{digits[:3]}***{digits[-4:]}"


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

    With the debug toggle on this logs at info so it shows up in the default
    Home Assistant log; otherwise it stays at debug. That saves the user
    editing configuration.yaml just to see why a message went nowhere.
    """
    if options.get(CONF_WA_DEBUG):
        _LOGGER.info("[whatsapp] " + msg, *args)
    else:
        _LOGGER.debug("[whatsapp] " + msg, *args)


class WhatsAppClient:
    """Thin wrapper over the Meta Cloud API messages endpoint."""

    def __init__(self, hass: HomeAssistant, options: dict) -> None:
        self.hass = hass
        self._options = options
        # Meta's rejection reason from the most recent send, surfaced by the
        # send_expiry_alert service so failures are diagnosable from the UI.
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self._options.get(CONF_WA_ENABLED)
            and self._options.get(CONF_WA_PHONE_NUMBER_ID)
            and self._options.get(CONF_WA_ACCESS_TOKEN)
        )

    def missing_config(self) -> list[str]:
        """Settings still needed before a message can be sent out."""
        missing = []
        if not self._options.get(CONF_WA_ENABLED):
            missing.append("whatsapp_enabled")
        if not self._options.get(CONF_WA_PHONE_NUMBER_ID):
            missing.append("phone_number_id")
        if not self._options.get(CONF_WA_ACCESS_TOKEN):
            missing.append("access_token")
        return missing

    @property
    def _url(self) -> str:
        return WHATSAPP_API_URL.format(
            phone_number_id=self._options[CONF_WA_PHONE_NUMBER_ID]
        )

    async def _post(self, payload: dict) -> bool:
        session = async_get_clientsession(self.hass)
        headers = {
            "Authorization": f"Bearer {self._options[CONF_WA_ACCESS_TOKEN]}",
            "Content-Type": "application/json",
        }
        try:
            async with session.post(
                self._url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=WHATSAPP_TIMEOUT),
            ) as resp:
                if resp.status >= 400:
                    # Meta returns a JSON error body explaining the rejection
                    # (expired token, template not approved, outside the 24h
                    # window, ...) - it is the only diagnostic there is.
                    body = await resp.text()
                    self.last_error = f"HTTP {resp.status}: {body[:300]}"
                    _LOGGER.error(
                        "WhatsApp send failed (%s): %s", resp.status, body[:500]
                    )
                    return False
                self.last_error = None
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self.last_error = str(err)
            _LOGGER.error("WhatsApp send error: %s", err)
            return False

    async def async_send_text(self, to: str, body: str) -> bool:
        """Send a free-form text message (24h customer service window only)."""
        if not self.configured:
            return False
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body[:WA_MAX_BODY]},
            }
        )

    async def async_send_template(
        self,
        to: str,
        body_param: str,
        *,
        name: str,
        language: str,
        parameter_name: str | None = None,
    ) -> bool:
        """Send a pre-approved template with a single body variable.

        ``parameter_name`` selects the template's variable style. Named
        variables ({{expired_items}}) require the name to be sent alongside the
        value and must match the approved template exactly; omitting it sends a
        positional variable ({{1}}) instead.
        """
        if not self.configured:
            return False
        # Defensive: Meta rejects the whole send (132018) if a parameter carries
        # a newline, tab or a long run of spaces, whatever the caller passed.
        parameter: dict[str, str] = {
            "type": "text",
            "text": _collapse_whitespace(body_param)[:WA_MAX_TEMPLATE_PARAM],
        }
        if parameter_name:
            parameter["parameter_name"] = parameter_name
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": language},
                    "components": [{"type": "body", "parameters": [parameter]}],
                },
            }
        )


def missing_inbound_config(options: dict) -> list[str]:
    """Settings still needed before an incoming message can be accepted.

    Separate from the outbound list: sending can be fully configured while
    every incoming message is still rejected, which is exactly the state that
    looks like "nothing happens" from the outside.
    """
    missing = []
    if not options.get(CONF_WA_APP_SECRET):
        missing.append("app_secret")
    if not options.get(CONF_WA_VERIFY_TOKEN):
        missing.append("verify_token")
    senders = options.get(CONF_WA_ALLOWED_SENDERS) or []
    if isinstance(senders, str):
        senders = [p.strip() for p in senders.split(",")]
    if not [p for p in senders if str(p).strip()]:
        missing.append("allowed_senders")
    return missing


class WhatsAppWebhookView(HomeAssistantView):
    """Receives Meta webhook callbacks. Unauthenticated by necessity."""

    url = WHATSAPP_WEBHOOK_PATH
    name = f"{DOMAIN}:whatsapp"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._seen: deque[str] = deque(maxlen=_SEEN_MAX)

    @property
    def _options(self) -> dict:
        return self.hass.data.get(DOMAIN, {}).get("wa_options", {})

    async def get(self, request: web.Request) -> web.Response:
        """Meta's subscription handshake: echo hub.challenge back verbatim."""
        params = request.query
        verify_token = self._options.get(CONF_WA_VERIFY_TOKEN)
        if (
            params.get("hub.mode") == "subscribe"
            and verify_token
            and hmac.compare_digest(
                str(params.get("hub.verify_token", "")), str(verify_token)
            )
        ):
            _trace(self._options, "verification handshake accepted")
            _record(self.hass, "verification_accepted")
            return web.Response(text=params.get("hub.challenge", ""))
        _LOGGER.warning(
            "WhatsApp webhook verification failed (mode=%s, verify token %s)",
            params.get("hub.mode"),
            "configured" if verify_token else "MISSING in options",
        )
        _record(self.hass, "verification_failed")
        return web.Response(status=403, text="verification failed")

    def _signature_ok(self, raw: bytes, header: str | None) -> bool:
        """Validate X-Hub-Signature-256 against the Meta app secret."""
        secret = self._options.get(CONF_WA_APP_SECRET)
        if not secret:
            # No secret configured - cannot verify, so refuse rather than
            # accept unauthenticated writes on a public endpoint.
            _LOGGER.error(
                "Rejecting WhatsApp webhook: no app secret configured, so no "
                "incoming message can be verified. Set it in the Home "
                "Inventory options (Meta: App settings -> Basic -> App secret)"
            )
            return False
        if not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(
            str(secret).encode(), raw, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, header[len("sha256="):])

    async def post(self, request: web.Request) -> web.Response:
        raw = await request.read()
        _trace(self._options, "POST received (%s bytes)", len(raw))
        _record(self.hass, "post_received", f"{len(raw)} bytes")
        if not self._signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
            _LOGGER.warning("WhatsApp webhook signature rejected")
            _record(self.hass, "rejected_bad_signature")
            return web.Response(status=403, text="bad signature")

        try:
            payload = await request.json()
        except ValueError:
            _record(self.hass, "rejected_bad_json")
            return web.Response(status=400, text="bad json")

        # Always 200 quickly so Meta stops retrying; process in the background.
        self.hass.async_create_task(self._async_handle_payload(payload))
        return web.Response(text="ok")

    async def _async_handle_payload(self, payload: dict) -> None:
        options = self._options
        handled = 0
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                # Delivery/read receipts arrive on the same webhook - ignore.
                if not value.get("messages"):
                    # Worth surfacing: receipts arriving while messages never do
                    # means the webhook works but the 'messages' field is not
                    # subscribed in the Meta app configuration.
                    _trace(
                        options,
                        "payload carried no messages (field=%s, keys=%s)",
                        change.get("field"),
                        sorted(value.keys()),
                    )
                    _record(
                        self.hass,
                        "payload_without_messages",
                        ",".join(sorted(value.keys())) or "empty",
                    )
                    continue
                for message in value["messages"]:
                    handled += 1
                    try:
                        await self._async_handle_message(message)
                    except Exception:  # noqa: BLE001 - never break the loop
                        _LOGGER.exception("Error handling WhatsApp message")
        _trace(options, "payload processed, %s message(s)", handled)

    async def _async_handle_message(self, message: dict) -> None:
        options = self._options
        msg_id = message.get("id")
        sender = message.get("from")
        _trace(
            options,
            "message %s from %s type=%s",
            msg_id,
            _mask(sender),
            message.get("type"),
        )

        if msg_id:
            if msg_id in self._seen:
                _trace(options, "duplicate %s ignored (Meta retry)", msg_id)
                return
            self._seen.append(msg_id)

        _record(self.hass, "message_received", f"from {_mask(sender)}")
        if not self._sender_allowed(sender):
            _LOGGER.warning(
                "Ignoring WhatsApp message from %s (not in allowed senders)",
                _mask(sender),
            )
            _record(self.hass, "rejected_sender_not_allowed", _mask(sender))
            return

        msg_type = message.get("type")
        if msg_type in ("image", "document"):
            await self._async_handle_receipt(sender, message, msg_type)
            return

        if msg_type != "text":
            _trace(options, "non-text message (%s), replying with a hint", msg_type)
            await self._async_reply(
                sender, "Send me text, or a photo of a receipt."
            )
            return

        text = (message.get("text") or {}).get("body", "").strip()
        if not text:
            _trace(options, "empty text body, nothing to do")
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

    async def _async_handle_receipt(self, sender: str, message: dict, kind: str) -> None:
        """Download a photo/PDF, extract the items, and offer them for confirm."""
        from . import receipts

        options = self._options
        if not options.get(CONF_RECEIPTS_ENABLED, True):
            await self._async_reply(sender, "Receipt scanning is turned off.")
            return

        payload = message.get(kind) or {}
        media_id = payload.get("id")
        if not media_id:
            await self._async_reply(sender, "I couldn't find an image in that.")
            return

        _record(self.hass, "receipt_received", kind)
        _trace(options, "receipt %s from %s, media=%s", kind, _mask(sender), media_id)
        await self._async_reply(sender, "Got it - reading the receipt...")

        path = None
        try:
            data, mime = await receipts.async_download_media(
                self.hass, options, media_id
            )
            path, content_id = await receipts.async_stage_media(self.hass, data, mime)
            items = await receipts.async_extract_items(self.hass, content_id)
            if not items:
                _record(self.hass, "receipt_empty")
                await self._async_reply(
                    sender, "I couldn't find any products on that receipt."
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

    def _sender_allowed(self, sender: str | None) -> bool:
        if not sender:
            return False
        allowed = self._options.get(CONF_WA_ALLOWED_SENDERS) or []
        if isinstance(allowed, str):
            allowed = [p.strip() for p in allowed.split(",")]
        allowed = [_digits(p) for p in allowed if str(p).strip()]
        if not allowed:
            # An empty allowlist on a public endpoint would let anyone who
            # learns the number drive the inventory - refuse instead.
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
        client: WhatsAppClient | None = self.hass.data.get(DOMAIN, {}).get("wa_client")
        if client is None:
            _LOGGER.error("Cannot reply: WhatsApp client not set up")
            return
        # The user just messaged us, so the 24h window is open: plain text is
        # allowed here and no template is needed.
        ok = await client.async_send_text(to, body)
        _trace(self._options, "reply to %s %s", _mask(to),
               "sent" if ok else f"FAILED: {client.last_error}")
        _record(
            self.hass,
            "reply_sent" if ok else "reply_failed",
            None if ok else client.last_error,
        )


def _collapse_whitespace(text: str) -> str:
    """Flatten whitespace so a value is safe to use as a template parameter."""
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


def _join_for_template(entries: list[str], limit: int) -> str:
    """Join entries onto one line, dropping the overflow into a "+N more" tail."""
    kept: list[str] = []
    used = 0
    for index, entry in enumerate(entries):
        cost = len(entry) + (len(_TEMPLATE_SEPARATOR) if kept else 0)
        remaining = len(entries) - index - 1
        tail = len(f" (+{remaining} more)") if remaining else 0
        if used + cost + tail > limit:
            break
        kept.append(entry)
        used += cost

    if not kept:
        # A single oversized entry still beats sending nothing at all.
        return entries[0][: limit - 1].rstrip() + "…" if entries else ""

    joined = _TEMPLATE_SEPARATOR.join(kept)
    dropped = len(entries) - len(kept)
    if dropped:
        joined += f" (+{dropped} more)"
    return joined


def _format_expiring(items: list[dict], *, multiline: bool = True) -> str:
    """Render expiring rows as a WhatsApp-friendly list.

    Template parameters may not contain newlines, tabs or runs of more than
    four spaces (Meta error 132018), and the rendered body is capped at 1024
    characters - so the template form is a single sanitised line. Free-form
    text messages have no such restriction and stay one item per line.
    """
    entries = _entries(items)
    if multiline:
        return "\n".join(f"• {entry}" for entry in entries)
    return _join_for_template(entries, WA_MAX_TEMPLATE_PARAM)


def _parse_time(value: str) -> dt_time:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value), fmt).time()
        except ValueError:
            continue
    return datetime.strptime(DEFAULT_WA_ALERT_TIME, "%H:%M:%S").time()


async def async_send_expiry_digest(
    hass: HomeAssistant, *, as_text: bool = False
) -> dict[str, Any]:
    """Send the expiring-items digest now.

    Returns a result dict rather than a bare boolean: "nothing was sent" has
    several very different causes (not configured, nothing expiring, Meta
    rejected it) and the caller needs to tell them apart.

    ``as_text`` sends the digest as a free-form message instead of the
    template. That only works inside the 24h window opened by the user's own
    message, but it allows the digest to be tested before Meta has approved
    the template.
    """
    data = hass.data.get(DOMAIN, {})
    client: WhatsAppClient | None = data.get("wa_client")
    options: dict = data.get("wa_options", {})
    db: Database | None = data.get("db")

    if client is None or db is None:
        return {"sent": False, "reason": "integration_not_ready"}

    if not client.configured:
        missing = client.missing_config()
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

    recipients = options.get(CONF_WA_ALLOWED_SENDERS) or []
    if isinstance(recipients, str):
        recipients = [p.strip() for p in recipients.split(",")]
    recipients = [p for p in recipients if str(p).strip()]
    if not recipients:
        _LOGGER.warning("WhatsApp digest skipped; no recipient numbers configured")
        return {"sent": False, "reason": "no_recipients", "item_count": len(items)}

    # One item per line for free-form text; a single sanitised line for the
    # template, whose parameter may not contain newlines.
    body = _format_expiring(items, multiline=as_text)
    template = options.get(CONF_WA_TEMPLATE_NAME) or DEFAULT_WA_TEMPLATE_NAME
    language = options.get(CONF_WA_TEMPLATE_LANG) or DEFAULT_WA_TEMPLATE_LANG
    # Missing key -> named default; explicitly blank -> positional {{1}}.
    param_name = options.get(CONF_WA_TEMPLATE_PARAM, DEFAULT_WA_TEMPLATE_PARAM)

    delivered: list[str] = []
    for recipient in recipients:
        if as_text:
            ok = await client.async_send_text(recipient, body)
        else:
            # Unprompted, so it must be a template - free-form text would be
            # rejected outside the 24h window.
            ok = await client.async_send_template(
                recipient,
                body,
                name=template,
                language=language,
                parameter_name=param_name,
            )
        if ok:
            delivered.append(recipient)

    result: dict[str, Any] = {
        "sent": bool(delivered),
        "reason": "sent" if delivered else "send_failed",
        "item_count": len(items),
        "recipients": len(recipients),
        "delivered": len(delivered),
        "mode": "text" if as_text else "template",
        "preview": body,
    }
    if not delivered:
        result["error"] = client.last_error or "unknown"
        if not as_text:
            result["template"] = template
    return result


async def async_whatsapp_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """Summarise the resolved WhatsApp setup, without revealing secrets.

    Reports only whether each secret is present, never its value, so the
    output is safe to paste when asking for help.
    """
    data = hass.data.get(DOMAIN, {})
    options: dict = data.get("wa_options", {})
    client: WhatsAppClient | None = data.get("wa_client")
    db: Database | None = data.get("db")

    senders = options.get(CONF_WA_ALLOWED_SENDERS) or []
    if isinstance(senders, str):
        senders = [p.strip() for p in senders.split(",")]
    senders = [p for p in senders if str(p).strip()]

    try:
        webhook_url = f"{get_url(hass, prefer_external=True)}{WHATSAPP_WEBHOOK_PATH}"
    except NoURLAvailableError:
        webhook_url = f"(no external URL configured){WHATSAPP_WEBHOOK_PATH}"

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
        "webhook_url": webhook_url,
        "webhook_registered": bool(data.get("wa_view_registered")),
        "llm_api_registered": bool(data.get("llm_api_registered")),
        "llm_api_id": LLM_API_ID,
        "debug_logging": bool(options.get(CONF_WA_DEBUG)),
        # Presence only - never the values themselves.
        "phone_number_id_set": bool(options.get(CONF_WA_PHONE_NUMBER_ID)),
        "access_token_set": bool(options.get(CONF_WA_ACCESS_TOKEN)),
        "verify_token_set": bool(options.get(CONF_WA_VERIFY_TOKEN)),
        "app_secret_set": bool(options.get(CONF_WA_APP_SECRET)),
        "missing_for_sending": (
            client.missing_config() if client else ["client_not_setup"]
        ),
        # Inbound is reported separately: outbound can be perfectly configured
        # while every incoming message is rejected.
        "missing_for_receiving": missing_inbound,
        "inbound_ready": not missing_inbound,
        "recipients": [_mask(s) for s in senders],
        "conversation_agent": agent or "<default agent>",
        "agent_configured": bool(agent),
        "alert_time": options.get(CONF_WA_ALERT_TIME) or DEFAULT_WA_ALERT_TIME,
        "alert_days": days,
        "expiring_now": expiring,
        "template": options.get(CONF_WA_TEMPLATE_NAME) or DEFAULT_WA_TEMPLATE_NAME,
        "template_language": (
            options.get(CONF_WA_TEMPLATE_LANG) or DEFAULT_WA_TEMPLATE_LANG
        ),
        "template_parameter": options.get(
            CONF_WA_TEMPLATE_PARAM, DEFAULT_WA_TEMPLATE_PARAM
        ),
        "last_send_error": client.last_error if client else None,
        # Inbound activity since the last restart. All-zero counts mean Meta
        # has never delivered anything, which is a Meta-side problem rather
        # than anything to fix in Home Assistant.
        "inbound_activity": stats["counts"] or "nothing received since restart",
        "inbound_last_event": stats["last_event"],
        "inbound_last_event_at": stats["last_event_at"],
    }


@callback
def async_setup_whatsapp(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the webhook view, client and daily digest schedule."""
    options = {**entry.data, **entry.options}
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["wa_options"] = options
    hass.data[DOMAIN]["wa_client"] = WhatsAppClient(hass, options)

    # The view reads options from hass.data on each request, so it only needs
    # registering once even when options are updated later.
    if not hass.data[DOMAIN].get("wa_view_registered"):
        hass.http.register_view(WhatsAppWebhookView(hass))
        hass.data[DOMAIN]["wa_view_registered"] = True
        _LOGGER.info(
            "WhatsApp webhook available at %s (LLM API id: %s)",
            WHATSAPP_WEBHOOK_PATH,
            LLM_API_ID,
        )

    # Replace any previous schedule so option changes take effect immediately.
    if unsub := hass.data[DOMAIN].pop("wa_unsub_timer", None):
        unsub()

    if not options.get(CONF_WA_ENABLED):
        return

    # Surface an inbound misconfiguration at startup: without these, incoming
    # messages are rejected silently as far as the user can tell.
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
