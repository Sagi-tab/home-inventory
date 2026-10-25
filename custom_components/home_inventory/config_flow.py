"""Config flow (single-instance) plus WhatsApp (WAHA) options."""
from __future__ import annotations

import secrets

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AI_TASK_ENTITY,
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
)


def validate_options(user_input: dict) -> dict[str, str]:
    """Reject option combinations that cannot work, keyed by field.

    Kept free of `self` and `hass` so the rules can be tested directly.

    Receipt scanning has no usable fallback without an AI Task entity: Home
    Assistant does not pick a preferred one implicitly, so leaving it blank
    fails only later, when a receipt is actually sent.
    """
    errors: dict[str, str] = {}
    url = str(user_input.get(CONF_WAHA_URL) or "").strip()
    if url and not url.startswith(("http://", "https://")):
        errors[CONF_WAHA_URL] = "invalid_url"
    if user_input.get(CONF_RECEIPTS_ENABLED) and not str(
        user_input.get(CONF_AI_TASK_ENTITY) or ""
    ).strip():
        errors[CONF_AI_TASK_ENTITY] = "ai_task_required"
    return errors


def new_webhook_id() -> str:
    """Random id for the WAHA webhook; it is the only thing naming the endpoint."""
    return secrets.token_hex(16)


class HomeInventoryConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Home Inventory", data={CONF_WAHA_WEBHOOK_ID: new_webhook_id()}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomeInventoryOptionsFlow()


class HomeInventoryOptionsFlow(OptionsFlow):
    """Configure the WhatsApp bridge (WAHA)."""

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_options(user_input)
            if not errors:
                # Blank secrets mean "keep the stored value" so the form can be
                # re-saved without retyping the token.
                current = dict(self.config_entry.options)
                for key in (CONF_WAHA_API_KEY, CONF_WAHA_HMAC_KEY):
                    if not user_input.get(key) and current.get(key):
                        user_input[key] = current[key]
                return self.async_create_entry(title="", data=user_input)

        # On a validation error redisplay what was typed rather than the stored
        # values, so fixing one field does not silently revert the others.
        options = user_input if user_input is not None else self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_WA_ENABLED,
                    default=options.get(CONF_WA_ENABLED, False),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_WAHA_URL,
                    default=options.get(CONF_WAHA_URL, ""),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                ),
                vol.Optional(
                    CONF_WAHA_API_KEY, default=""
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_WAHA_SESSION,
                    default=options.get(CONF_WAHA_SESSION, DEFAULT_WAHA_SESSION),
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_WAHA_HMAC_KEY, default=""
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_WA_ALLOWED_SENDERS,
                    default=options.get(CONF_WA_ALLOWED_SENDERS, ""),
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_WA_AGENT_ID,
                    default=options.get(CONF_WA_AGENT_ID, ""),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="conversation")
                ),
                vol.Optional(
                    CONF_WA_ALERT_TIME,
                    default=options.get(CONF_WA_ALERT_TIME, DEFAULT_WA_ALERT_TIME),
                ): selector.TimeSelector(),
                vol.Optional(
                    CONF_WA_ALERT_DAYS,
                    default=options.get(CONF_WA_ALERT_DAYS, DEFAULT_EXPIRING_DAYS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    CONF_WA_DEBUG,
                    default=options.get(CONF_WA_DEBUG, False),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_RECEIPTS_ENABLED,
                    default=options.get(CONF_RECEIPTS_ENABLED, True),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_AI_TASK_ENTITY,
                    default=options.get(CONF_AI_TASK_ENTITY, ""),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="ai_task")
                ),
            }
        )
        # Show the exact webhook URL to put in the WAHA configuration.
        from .whatsapp import webhook_url

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "webhook": webhook_url(
                    self.hass, {**self.config_entry.data, **self.config_entry.options}
                )
            },
        )
