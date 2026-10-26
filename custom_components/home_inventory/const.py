"""Constants for the Home Inventory integration."""
from __future__ import annotations

DOMAIN = "home_inventory"
PLATFORMS = ["sensor", "binary_sensor"]

# Storage
DB_FILENAME = "home_inventory.db"

# External APIs
OFF_API_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}"
OFF_USER_AGENT = "HomeInventory-HomeAssistant/0.1"
OFF_TIMEOUT = 6

# Defaults
DEFAULT_EXPIRING_DAYS = 3
DEFAULT_LOW_STOCK_THRESHOLD = 1

# Services
SERVICE_SCAN_BARCODE = "scan_barcode"
SERVICE_ADD_ITEM = "add_item"
SERVICE_REMOVE_ITEM = "remove_item"
SERVICE_ADD_TO_SHOPPING = "add_to_shopping"
SERVICE_COMPLETE_SHOPPING = "complete_shopping_item"
SERVICE_SET_THRESHOLD = "set_threshold"
SERVICE_LOOKUP_BARCODE = "lookup_barcode"
SERVICE_UPDATE_PRODUCT = "update_product"
SERVICE_SEND_EXPIRY_ALERT = "send_expiry_alert"
SERVICE_WA_DIAGNOSTICS = "whatsapp_diagnostics"
SERVICE_PROCESS_RECEIPT = "process_receipt"

# Event names
EVENT_BARCODE_UNKNOWN = "home_inventory_barcode_unknown"
EVENT_ITEM_EXPIRING = "home_inventory_item_expiring"
EVENT_LOW_STOCK = "home_inventory_low_stock"
EVENT_DATA_CHANGED = "home_inventory_data_changed"

# Actions
ACTION_ADD = "add"
ACTION_REMOVE = "remove"

# WebSocket / API
API_BASE = "/api/home_inventory"
FRONTEND_URL = "/home_inventory_static"
CARD_FILENAME = "home-inventory-card.js"

# ---------- WhatsApp (WAHA - WhatsApp HTTP API) ----------
WHATSAPP_TIMEOUT = 15

# Config entry option keys
CONF_WA_ENABLED = "whatsapp_enabled"
CONF_WAHA_URL = "waha_url"
CONF_WAHA_API_KEY = "waha_api_key"
CONF_WAHA_SESSION = "waha_session"
CONF_WAHA_HMAC_KEY = "waha_webhook_hmac_key"
CONF_WAHA_WEBHOOK_ID = "waha_webhook_id"
CONF_WA_ALLOWED_SENDERS = "whatsapp_allowed_senders"
CONF_WA_AGENT_ID = "whatsapp_agent_id"
CONF_WA_ALERT_TIME = "whatsapp_alert_time"
CONF_WA_ALERT_DAYS = "whatsapp_alert_days"
CONF_WA_DEBUG = "whatsapp_debug"
CONF_RECEIPTS_ENABLED = "receipts_enabled"
CONF_AI_TASK_ENTITY = "ai_task_entity"

# Meta Cloud API settings from before 0.8.0, dropped by the entry migration.
LEGACY_META_OPTIONS = (
    "whatsapp_phone_number_id",
    "whatsapp_access_token",
    "whatsapp_verify_token",
    "whatsapp_app_secret",
    "whatsapp_template_name",
    "whatsapp_template_language",
    "whatsapp_template_parameter",
)

DEFAULT_WAHA_SESSION = "default"
DEFAULT_WA_ALERT_TIME = "09:00:00"

# The LLM tool API exposing inventory control to a conversation agent.
LLM_API_ID = "home_inventory"
LLM_API_NAME = "Home Inventory"

# Cap on a single outgoing message. WhatsApp allows far more, but a reply this
# long is unreadable on a phone anyway.
WA_MAX_BODY = 4000

# Categories (used for seed + UI)
CATEGORIES = [
    "dairy",
    "bakery",
    "produce",
    "meat_fish",
    "frozen",
    "pantry",
    "beverages",
    "snacks",
    "cleaning",
    "personal_care",
    "other",
]

LOCATIONS = [
    "fridge",
    "freezer",
    "pantry",
    "bathroom",
    "cleaning_cabinet",
    "other",
]

# ---------- Receipt scanning ----------
# Refuse anything larger; a phone photo of a receipt is well under this.
RECEIPT_MAX_BYTES = 12 * 1024 * 1024
RECEIPT_ACCEPTED_MIME = ("image/jpeg", "image/png", "image/webp", "application/pdf")
# How long a pending receipt stays open for confirmation.
RECEIPT_SESSION_TTL = 900  # seconds
# Categories whose expiry actually matters enough to ask about.
PERISHABLE_CATEGORIES = ["dairy", "produce", "meat_fish", "bakery"]
# find_similar_products score at or above which a receipt line is linked to an
# existing product without asking. Matches the MatchView "strong" convention.
RECEIPT_STRONG_MATCH_SCORE = 8.0
RECEIPT_MIN_MATCH_SCORE = 4.0
