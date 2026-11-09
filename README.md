# Home Inventory for Home Assistant

Track what's in the house, what's expiring and what to buy, from a dashboard
card, by voice, through any conversation agent, or by chatting over WhatsApp or
Telegram.

- **Inventory and shopping list** with barcode lookup (local cache first, then
  Open Food Facts) and a dashboard card with a camera barcode scanner.
- **Conversation tools**: a Home Inventory LLM API for conversation agents, plus
  built-in voice sentences (English and Hebrew).
- **Receipt scanning**: send a photo or PDF of a receipt; an AI Task entity
  reads it, you confirm (or drop lines), and the items are added.
- **Reminders**: time reminders and location reminders ("remind me to buy
  bread when I'm at the bakery") per person.
- **Daily expiry digest** over WhatsApp and/or Telegram.

## Installation

HACS → ⋮ → **Custom repositories** → `https://github.com/Sagi-tab/home-inventory`
(category **Integration**), install **Home Inventory**, restart, then
**Settings → Devices & services → Add integration → Home Inventory**.
Messaging is configured under the integration's **Configure** button.

## Messaging

Both channels share the same features: talk to the inventory in plain
language (through the conversation agent you pick), send receipt photos,
create reminders, and receive the daily digest. You can use either one or both.

Each sender can be mapped to a Home Assistant `person`. The conversation then
runs as that person's user, so reminders know who they're for, and a due
reminder reaches the person on every channel they're mapped on.

### WhatsApp (WAHA)

Uses a [WAHA](https://waha.devlike.pro/) server (WhatsApp HTTP API), for
example the WAHA add-on, with your WhatsApp number linked to it.

1. In the options, enable WhatsApp and enter the WAHA URL and API key.
2. In WAHA, set the webhook URL shown in the options form, with the `message`
   event. Optionally set an HMAC key in both places.
3. List the **allowed numbers** (country code first, no `+`); they may send
   commands and receive the digest. Map numbers to people with
   `15551234567=person.alex`.

Location reminders accept WhatsApp's location share or a Google Maps link.

### Telegram (optional)

Telegram goes through Home Assistant's own
[Telegram bot integration](https://www.home-assistant.io/integrations/telegram_bot/),
so Home Inventory holds no bot token and runs no poller of its own.

1. Set up the **Telegram bot** integration (polling or webhooks) and allow the
   chats that should use the inventory.
2. In the Home Inventory options, enable **Telegram** and list the **chat ids**,
   comma-separated, each optionally mapped to a person:
   `123456789=person.alex, -1001234567890` (group ids are negative).
3. If you have more than one bot, pick the one to use.

Only the listed chats are handled, so a bot that also serves other automations
(say, a family notifications group) keeps doing that untouched. Send `/start`
for a short help text.

Telegram passes a shared location to Home Assistant without its coordinates,
so for a location reminder send a **Google Maps link** to the place instead.
Receipt photos use the Telegram integration's `download_file` action; the
downloaded file is deleted as soon as it's read.

### Diagnostics

The `home_inventory.whatsapp_diagnostics` action reports the resolved setup of
both channels, recent inbound activity and the last send errors. Secrets and
chat ids are never included, so the output is safe to share.
`home_inventory.send_expiry_alert` sends the digest on demand.

## Development

```bash
pip install pytest-homeassistant-custom-component
pytest
```

The messaging tests use fakes for WAHA, the Telegram bot integration, the
conversation agent and the receipt AI, so they run without any service.
