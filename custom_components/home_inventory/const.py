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
