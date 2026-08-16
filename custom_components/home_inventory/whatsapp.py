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
from collections import deque
from datetime import datetime, time as dt_time

import aiohttp
from aiohttp import web
from homeassistant.components import conversation
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change

from .const import (
    CONF_WA_ACCESS_TOKEN,
    CONF_WA_AGENT_ID,
    CONF_WA_ALERT_DAYS,
    CONF_WA_ALERT_TIME,
    CONF_WA_ALLOWED_SENDERS,
    CONF_WA_APP_SECRET,
    CONF_WA_ENABLED,
    CONF_WA_PHONE_NUMBER_ID,
    CONF_WA_TEMPLATE_LANG,
    CONF_WA_TEMPLATE_NAME,
    CONF_WA_VERIFY_TOKEN,
    DEFAULT_EXPIRING_DAYS,
    DEFAULT_WA_ALERT_TIME,
    DEFAULT_WA_TEMPLATE_LANG,
    DEFAULT_WA_TEMPLATE_NAME,
    DOMAIN,
    LLM_API_ID,
    WA_MAX_BODY,
    WHATSAPP_API_URL,
    WHATSAPP_TIMEOUT,
    WHATSAPP_WEBHOOK_PATH,
)
from .storage import Database

_LOGGER = logging.getLogger(__name__)

# Remember recently handled message ids so Meta's retries are no-ops.
_SEEN_MAX = 200


def _digits(value: str) -> str:
    """Normalise a phone number to digits only for comparison."""
    return "".join(ch for ch in str(value) if ch.isdigit())


class WhatsAppClient:
    """Thin wrapper over the Meta Cloud API messages endpoint."""

    def __init__(self, hass: HomeAssistant, options: dict) -> None:
        self.hass = hass
        self._options = options

    @property
    def configured(self) -> bool:
        return bool(
            self._options.get(CONF_WA_ENABLED)
            and self._options.get(CONF_WA_PHONE_NUMBER_ID)
            and self._options.get(CONF_WA_ACCESS_TOKEN)
        )

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
                    # window, ...) - log it, it is the only diagnostic there is.
                    body = await resp.text()
                    _LOGGER.error(
                        "WhatsApp send failed (%s): %s", resp.status, body[:500]
                    )
                    return False
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
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
        self, to: str, body_param: str, *, name: str, language: str
    ) -> bool:
        """Send a pre-approved template with a single body variable."""
        if not self.configured:
            return False
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": language},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": body_param[:WA_MAX_BODY]}
                            ],
                        }
                    ],
                },
            }
        )


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
            return web.Response(text=params.get("hub.challenge", ""))
        _LOGGER.warning("WhatsApp webhook verification failed")
        return web.Response(status=403, text="verification failed")

    def _signature_ok(self, raw: bytes, header: str | None) -> bool:
        """Validate X-Hub-Signature-256 against the Meta app secret."""
        secret = self._options.get(CONF_WA_APP_SECRET)
        if not secret:
            # No secret configured - cannot verify, so refuse rather than
            # accept unauthenticated writes on a public endpoint.
            _LOGGER.error("WhatsApp app secret not configured; rejecting webhook")
            return False
        if not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(
            str(secret).encode(), raw, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, header[len("sha256="):])

    async def post(self, request: web.Request) -> web.Response:
        raw = await request.read()
        if not self._signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
            _LOGGER.warning("WhatsApp webhook signature rejected")
            return web.Response(status=403, text="bad signature")

        try:
            payload = await request.json()
        except ValueError:
            return web.Response(status=400, text="bad json")

        # Always 200 quickly so Meta stops retrying; process in the background.
        self.hass.async_create_task(self._async_handle_payload(payload))
        return web.Response(text="ok")

    async def _async_handle_payload(self, payload: dict) -> None:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                # Delivery/read receipts arrive on the same webhook - ignore.
                if not value.get("messages"):
                    continue
                for message in value["messages"]:
                    try:
                        await self._async_handle_message(message)
                    except Exception:  # noqa: BLE001 - never break the loop
                        _LOGGER.exception("Error handling WhatsApp message")

    async def _async_handle_message(self, message: dict) -> None:
        msg_id = message.get("id")
        if msg_id:
            if msg_id in self._seen:
                _LOGGER.debug("Ignoring duplicate WhatsApp message %s", msg_id)
                return
            self._seen.append(msg_id)

        sender = message.get("from")
        if not self._sender_allowed(sender):
            _LOGGER.warning("Ignoring WhatsApp message from %s (not allowed)", sender)
            return

        if message.get("type") != "text":
            await self._async_reply(
                sender, "I can only read text messages right now."
            )
            return

        text = (message.get("text") or {}).get("body", "").strip()
        if not text:
            return

        answer = await self._async_converse(text, sender)
        if answer:
            await self._async_reply(sender, answer)

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
            return
        # The user just messaged us, so the 24h window is open: plain text is
        # allowed here and no template is needed.
        await client.async_send_text(to, body)


def _format_expiring(items: list[dict]) -> str:
    """Render expiring rows as a compact WhatsApp-friendly list."""
    lines = []
    for item in items:
        name = item.get("product_name") or item.get("product_name_he") or "Item"
        qty = item.get("quantity")
        expires = item.get("expiration_date") or "?"
        location = item.get("location")
        parts = [f"• {name}"]
        if qty:
            parts.append(f"x{qty:g}" if isinstance(qty, (int, float)) else f"x{qty}")
        parts.append(f"— {expires}")
        if location:
            parts.append(f"({location})")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _parse_time(value: str) -> dt_time:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value), fmt).time()
        except ValueError:
            continue
    return datetime.strptime(DEFAULT_WA_ALERT_TIME, "%H:%M:%S").time()


async def async_send_expiry_digest(hass: HomeAssistant) -> bool:
    """Send the expiring-items digest now. Returns True if a message was sent."""
    data = hass.data.get(DOMAIN, {})
    client: WhatsAppClient | None = data.get("wa_client")
    options: dict = data.get("wa_options", {})
    db: Database | None = data.get("db")
    if client is None or db is None or not client.configured:
        return False

    days = int(options.get(CONF_WA_ALERT_DAYS) or DEFAULT_EXPIRING_DAYS)
    items = await db.get_expiring_items(days)
    if not items:
        _LOGGER.debug("No expiring items; skipping WhatsApp digest")
        return False

    body = _format_expiring(items)
    template = options.get(CONF_WA_TEMPLATE_NAME) or DEFAULT_WA_TEMPLATE_NAME
    language = options.get(CONF_WA_TEMPLATE_LANG) or DEFAULT_WA_TEMPLATE_LANG

    recipients = options.get(CONF_WA_ALLOWED_SENDERS) or []
    if isinstance(recipients, str):
        recipients = [p.strip() for p in recipients.split(",")]
    recipients = [p for p in recipients if str(p).strip()]

    sent = False
    for recipient in recipients:
        # Unprompted, so it must be a template - free-form text would be
        # rejected outside the 24h window.
        if await client.async_send_template(
            recipient, body, name=template, language=language
        ):
            sent = True
    return sent


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
