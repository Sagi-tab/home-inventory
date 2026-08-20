"""Config flow (single-instance) plus WhatsApp options."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.network import NoURLAvailableError, get_url

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
    CONF_AI_TASK_ENTITY,
    CONF_RECEIPTS_ENABLED,
    CONF_WA_DEBUG,
    CONF_WA_TEMPLATE_PARAM,
    DEFAULT_WA_TEMPLATE_LANG,
    DEFAULT_WA_TEMPLATE_NAME,
    DEFAULT_WA_TEMPLATE_PARAM,
    DOMAIN,
    WHATSAPP_WEBHOOK_PATH,
)


def validate_options(user_input: dict) -> dict[str, str]:
    """Reject option combinations that cannot work, keyed by field.

    Kept free of `self` and `hass` so the rules can be tested directly.

    Receipt scanning has no usable fallback without an AI Task entity: Home
    Assistant does not pick a preferred one implicitly, so leaving it blank
    fails only later, when a receipt is actually sent.
    """
    errors: dict[str, str] = {}
    if user_input.get(CONF_RECEIPTS_ENABLED) and not str(
        user_input.get(CONF_AI_TASK_ENTITY) or ""
    ).strip():
        errors[CONF_AI_TASK_ENTITY] = "ai_task_required"
    return errors


class HomeInventoryConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Home Inventory", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomeInventoryOptionsFlow()


class HomeInventoryOptionsFlow(OptionsFlow):
    """Configure the WhatsApp bridge."""

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_options(user_input)
            if not errors:
                # Blank secrets mean "keep the stored value" so the form can be
                # re-saved without retyping the token.
                current = dict(self.config_entry.options)
                for key in (
                    CONF_WA_ACCESS_TOKEN, CONF_WA_APP_SECRET, CONF_WA_VERIFY_TOKEN,
                ):
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
                    CONF_WA_PHONE_NUMBER_ID,
                    default=options.get(CONF_WA_PHONE_NUMBER_ID, ""),
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_WA_ACCESS_TOKEN, default=""
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_WA_VERIFY_TOKEN, default=""
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_WA_APP_SECRET, default=""
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
                    CONF_WA_TEMPLATE_NAME,
                    default=options.get(
                        CONF_WA_TEMPLATE_NAME, DEFAULT_WA_TEMPLATE_NAME
                    ),
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_WA_TEMPLATE_LANG,
                    default=options.get(
                        CONF_WA_TEMPLATE_LANG, DEFAULT_WA_TEMPLATE_LANG
                    ),
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_WA_TEMPLATE_PARAM,
                    default=options.get(
                        CONF_WA_TEMPLATE_PARAM, DEFAULT_WA_TEMPLATE_PARAM
                    ),
                ): selector.TextSelector(),
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
        # Show the exact callback URL to paste into the Meta app config.
        try:
            base_url = get_url(self.hass, prefer_external=True)
        except NoURLAvailableError:
            base_url = ""
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"webhook": f"{base_url}{WHATSAPP_WEBHOOK_PATH}"},
        )
