"""Turn a photographed shopping receipt into inventory entries.

Flow: the WhatsApp webhook hands us a media id, we download the image, ask a
vision model to extract the purchased lines, resolve each line to a product,
and hold the result as a pending session until the user confirms. Nothing is
written to the database before confirmation - receipt OCR misreads quantities
and picks up discount lines, so an unattended write would quietly corrupt the
inventory.

Resolution reuses the existing machinery rather than reimplementing it:
`BarcodeLookup` for printed barcodes (which also caches OpenFoodFacts hits),
`Database.find_similar_products` for name matching, and `normalize_category`
for the model's free-text category.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AI_TASK_ENTITY,
    DOMAIN,
    PERISHABLE_CATEGORIES,
    RECEIPT_ACCEPTED_MIME,
    RECEIPT_MAX_BYTES,
    RECEIPT_MIN_MATCH_SCORE,
    RECEIPT_SESSION_TTL,
    RECEIPT_STRONG_MATCH_SCORE,
    WHATSAPP_MEDIA_URL,
    WHATSAPP_TIMEOUT,
)
from .lookup import normalize_category

_LOGGER = logging.getLogger(__name__)

CONFIRM_WORDS = {"yes", "y", "ok", "okay", "confirm", "כן", "אישור", "אשר"}
CANCEL_WORDS = {"no", "n", "cancel", "stop", "לא", "ביטול", "בטל"}
SKIP_WORDS = {"skip", "none", "-", "דלג"}

# What the model must return. Kept flat: nested structures are handled
# inconsistently across AI Task providers.
EXTRACT_STRUCTURE = {
    "items": {
        "description": "One entry per purchased product line",
        "required": True,
        "selector": {
            "object": {
                "multiple": True,
                "fields": {
                    "name": {"selector": {"text": {}}, "required": True},
                    "name_he": {"selector": {"text": {}}},
                    "barcode": {"selector": {"text": {}}},
                    "quantity": {"selector": {"number": {"mode": "box"}}},
                    "unit": {"selector": {"text": {}}},
                    "category": {"selector": {"text": {}}},
                    "perishable": {"selector": {"boolean": {}}},
                },
            }
        },
    }
}

EXTRACT_INSTRUCTIONS = """Extract the purchased products from this shopping receipt.

Rules:
- One entry per product actually bought.
- Ignore any line that is not a product: totals, subtotals, VAT, change, \
discounts, loyalty points, bottle deposits, payment method, store address, \
phone numbers, dates and receipt numbers.
- Keep the product name exactly as printed. If it is Hebrew, put it in both \
`name` and `name_he`.
- `barcode` only when a barcode or long numeric product code is actually \
printed on that line. Never invent one, and never use the line number, price \
or receipt number as a barcode.
- `quantity` is how many units were bought (default 1). If the line is priced \
by weight, put the weight in `quantity` and the unit (kg/g) in `unit`.
- `category` is a short free-text guess such as dairy, produce, meat, frozen, \
bakery, cleaning, snacks.
- `perishable` is true for fresh food that spoils within weeks.

Return an empty list if this is not a receipt."""

DATE_STRUCTURE = {
    "dates": {
        "description": "Expiry date per numbered item the user answered for",
        "required": True,
        "selector": {
            "object": {
                "multiple": True,
                "fields": {
                    "index": {"selector": {"number": {"mode": "box"}}, "required": True},
                    "date": {"selector": {"text": {}}},
                },
            }
        },
    }
}


class ReceiptError(HomeAssistantError):
    """Raised when a receipt cannot be processed; message is user-facing."""


# ---------- media ----------


async def async_download_media(hass: HomeAssistant, options: dict, media_id: str):
    """Fetch WhatsApp media. Returns (bytes, mime_type).

    Two hops: the media id resolves to a URL that lives about five minutes and
    still requires the bearer token - fetching it unauthenticated returns 401.
    """
    from .const import CONF_WA_ACCESS_TOKEN

    token = options.get(CONF_WA_ACCESS_TOKEN)
    if not token:
        raise ReceiptError("WhatsApp access token is not configured.")

    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=WHATSAPP_TIMEOUT)

    try:
        async with session.get(
            WHATSAPP_MEDIA_URL.format(media_id=media_id),
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status >= 400:
                raise ReceiptError(
                    f"Could not look up the image ({resp.status})."
                )
            meta = await resp.json()

        url = meta.get("url")
        mime = (meta.get("mime_type") or "").split(";")[0].strip()
        if not url:
            raise ReceiptError("WhatsApp did not return a download URL.")
        if mime and mime not in RECEIPT_ACCEPTED_MIME:
            raise ReceiptError(f"I can't read {mime} files - send a photo or PDF.")

        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status >= 400:
                raise ReceiptError(f"Could not download the image ({resp.status}).")
            declared = resp.content_length
            if declared and declared > RECEIPT_MAX_BYTES:
                raise ReceiptError("That image is too large.")
            data = await resp.content.read(RECEIPT_MAX_BYTES + 1)
            if len(data) > RECEIPT_MAX_BYTES:
                raise ReceiptError("That image is too large.")
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise ReceiptError(f"Download failed: {err}") from err

    return data, mime or "image/jpeg"


def _media_dir(hass: HomeAssistant) -> Path:
    """The configured local media directory, which AI Task can read from."""
    local = (getattr(hass.config, "media_dirs", None) or {}).get("local")
    if not local:
        raise ReceiptError(
            "No local media directory is configured in Home Assistant, so the "
            "receipt cannot be handed to the AI. Add a `media_dirs` entry to "
            "configuration.yaml."
        )
    return Path(local)


def _extension(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
    }.get(mime, ".jpg")


async def async_stage_media(hass: HomeAssistant, data: bytes, mime: str):
    """Write the receipt where AI Task can reach it. Returns (path, media id)."""
    directory = _media_dir(hass) / "home_inventory_receipts"
    name = f"receipt_{uuid.uuid4().hex}{_extension(mime)}"
    path = directory / name

    def _write() -> None:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    await hass.async_add_executor_job(_write)
    return path, f"media-source://media_source/local/home_inventory_receipts/{name}"


async def async_cleanup(hass: HomeAssistant, path: Path) -> None:
    """Remove the staged file; a receipt is not something to leave lying about."""
    def _unlink() -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as err:  # pragma: no cover
            _LOGGER.debug("Could not remove %s: %s", path, err)

    await hass.async_add_executor_job(_unlink)


# ---------- extraction ----------


async def _async_generate(
    hass: HomeAssistant,
    *,
    task_name: str,
    instructions: str,
    structure: dict,
    attachment: str | None = None,
) -> dict:
    """Call ai_task.generate_data and return its structured data."""
    entity_id = hass.data.get(DOMAIN, {}).get("wa_options", {}).get(
        CONF_AI_TASK_ENTITY
    )
    payload: dict[str, Any] = {
        "task_name": task_name,
        "instructions": instructions,
        "structure": structure,
    }
    if entity_id:
        payload["entity_id"] = entity_id
    if attachment:
        payload["attachments"] = {"media_content_id": attachment,
                                  "media_content_type": "image/jpeg"}

    try:
        result = await hass.services.async_call(
            "ai_task", "generate_data", payload,
            blocking=True, return_response=True,
        )
    except Exception as err:  # noqa: BLE001 - surfaced to the user verbatim
        raise ReceiptError(f"The AI task failed: {err}") from err

    if not isinstance(result, dict):
        raise ReceiptError("The AI task returned nothing usable.")
    return result.get("data") or result


async def async_extract_items(
    hass: HomeAssistant, media_content_id: str
) -> list[dict]:
    """Ask the vision model for the purchased lines."""
    data = await _async_generate(
        hass,
        task_name="Home Inventory receipt",
        instructions=EXTRACT_INSTRUCTIONS,
        structure=EXTRACT_STRUCTURE,
        attachment=media_content_id,
    )
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and str(i.get("name") or "").strip()]


# ---------- resolution ----------


async def async_resolve_lines(hass: HomeAssistant, items: list[dict]) -> list[dict]:
    """Attach a product to each extracted line, or mark it as new.

    `match` is one of: barcode_known (already in the catalogue or resolvable
    via OpenFoodFacts), name_match (an existing product scored strongly
    enough), or new (nothing matched - a product will be created on commit).
    """
    store = hass.data.get(DOMAIN, {})
    db = store.get("db")
    lookup = store.get("lookup")
    if db is None:
        raise ReceiptError("The inventory database is not ready.")

    resolved: list[dict] = []
    for item in items:
        name = str(item.get("name") or "").strip()
        name_he = str(item.get("name_he") or "").strip() or None
        barcode = str(item.get("barcode") or "").strip() or None
        try:
            quantity = float(item.get("quantity") or 1)
        except (TypeError, ValueError):
            quantity = 1.0
        if quantity <= 0:
            quantity = 1.0

        line: dict[str, Any] = {
            "name": name,
            "name_he": name_he,
            "barcode": barcode,
            "quantity": quantity,
            "unit": str(item.get("unit") or "pcs").strip() or "pcs",
            "category": normalize_category(item.get("category")),
            "perishable": bool(item.get("perishable")),
            "product_id": None,
            "match": "new",
            "score": None,
        }

        if barcode and lookup is not None:
            found = await lookup.lookup(barcode)
            if found.get("found") in ("local", "remote"):
                line["product_id"] = found.get("id")
                line["match"] = "barcode_known"
                line["category"] = found.get("category") or line["category"]
                # Prefer the catalogue name; the receipt's is usually truncated.
                line["display"] = found.get("name_he") or found.get("name") or name
                resolved.append(line)
                continue

        # No barcode, or one nothing knows yet: fall back to name similarity.
        # Hebrew strings go in as both name and name_he, matching MatchView.
        query = name_he or name
        similar = await db.find_similar_products(
            name=name,
            name_he=query,
            category=line["category"],
            limit=3,
            min_score=RECEIPT_MIN_MATCH_SCORE,
        )
        if similar:
            best = similar[0]
            line["score"] = round(float(best.get("score") or 0), 1)
            if line["score"] >= RECEIPT_STRONG_MATCH_SCORE:
                line["product_id"] = best.get("id")
                line["match"] = "name_match"
                line["display"] = best.get("name_he") or best.get("name")

        line.setdefault("display", name_he or name)
        resolved.append(line)

    return resolved


def is_perishable(line: dict) -> bool:
    return bool(line.get("perishable")) or line.get("category") in PERISHABLE_CATEGORIES


# ---------- sessions ----------


def _sessions(hass: HomeAssistant) -> dict:
    return hass.data.setdefault(DOMAIN, {}).setdefault("receipt_sessions", {})


def session_get(hass: HomeAssistant, sender: str) -> dict | None:
    """Return the live session for a sender, discarding it once expired."""
    session = _sessions(hass).get(sender)
    if not session:
        return None
    age = (dt_util.utcnow() - session["created"]).total_seconds()
    if age > RECEIPT_SESSION_TTL:
        _sessions(hass).pop(sender, None)
        return None
    return session


def session_start(hass: HomeAssistant, sender: str, lines: list[dict]) -> dict:
    session = {
        "state": "awaiting_confirm",
        "lines": lines,
        "created": dt_util.utcnow(),
        "pending_dates": [],
    }
    _sessions(hass)[sender] = session
    return session


def session_clear(hass: HomeAssistant, sender: str) -> None:
    _sessions(hass).pop(sender, None)


# ---------- rendering ----------


def format_preview(lines: list[dict]) -> str:
    """The confirmation message. Flags what will be created rather than matched."""
    out = [f"Receipt: {len(lines)} item(s)"]
    for index, line in enumerate(lines, 1):
        qty = line["quantity"]
        qty_text = f"{qty:g}"
        unit = line.get("unit") or "pcs"
        bits = [f"{index}. {line.get('display') or line['name']} x{qty_text}"]
        if unit not in ("pcs", ""):
            bits[-1] += f" {unit}"
        if line["match"] == "barcode_known":
            bits.append("(known)")
        elif line["match"] == "name_match":
            bits.append("(matched)")
        else:
            bits.append("(new)")
        out.append(" ".join(bits))
    out.append("")
    out.append("Reply YES to add these, or NO to discard.")
    return "\n".join(out)


def format_date_prompt(perishables: list[tuple[int, dict]]) -> str:
    out = ["Expiry dates? Reply like \"1: Friday, 2: 12/09\" or SKIP."]
    for number, (_, line) in enumerate(perishables, 1):
        out.append(f"{number}. {line.get('display') or line['name']}")
    return "\n".join(out)


# ---------- commit ----------


async def async_parse_dates(
    hass: HomeAssistant, reply: str, perishables: list
) -> dict[int, str]:
    """Map the user's free-form date reply onto item indexes.

    Parsing this with the model rather than a regex is what makes "Friday" and
    "12/09" and "3 days" all work without a date-parsing dependency.
    """
    listing = "\n".join(
        f"{n}. {line.get('display') or line['name']}"
        for n, (_, line) in enumerate(perishables, 1)
    )
    today = dt_util.now().date().isoformat()
    data = await _async_generate(
        hass,
        task_name="Home Inventory receipt dates",
        instructions=(
            f"Today is {today}. The user was asked for expiry dates for these "
            f"numbered items:\n{listing}\n\nTheir reply: {reply!r}\n\n"
            "Return one entry per item they gave a date for, with `index` as "
            "the number above and `date` as an ISO YYYY-MM-DD date. Resolve "
            "relative dates like 'Friday' or 'in 3 days' against today. Omit "
            "items they skipped or did not mention."
        ),
        structure=DATE_STRUCTURE,
    )
    entries = data.get("dates") if isinstance(data, dict) else None
    result: dict[int, str] = {}
    for entry in entries or []:
        try:
            index = int(entry.get("index"))
            date = str(entry.get("date") or "").strip()
        except (TypeError, ValueError):
            continue
        if date and 1 <= index <= len(perishables):
            result[index] = date
    return result


async def async_commit(hass: HomeAssistant, lines: list[dict]) -> dict:
    """Write the confirmed lines to inventory.

    Products are created for lines nothing matched, then quantities are added
    and any matching open shopping rows are ticked off - buying something is
    the natural end of its life on the list.
    """
    store = hass.data.get(DOMAIN, {})
    db = store.get("db")
    if db is None:
        raise ReceiptError("The inventory database is not ready.")

    added: list[str] = []
    created = 0
    fulfilled: list[int] = []

    for line in lines:
        product_id = line.get("product_id")
        if not product_id:
            product_id = await db.upsert_product(
                barcode=line.get("barcode"),
                name=line["name"],
                name_he=line.get("name_he"),
                category=line.get("category"),
                source="receipt",
            )
            created += 1
        elif line.get("barcode") and line["match"] != "barcode_known":
            # The receipt taught us a barcode for a product matched by name.
            await db.link_barcode(barcode=line["barcode"], product_id=int(product_id))

        await db.add_inventory(
            product_id=int(product_id),
            quantity=float(line["quantity"]),
            unit=line.get("unit") or "pcs",
            expiration_date=line.get("expiration_date"),
            barcode=line.get("barcode"),
        )
        line["product_id"] = int(product_id)
        added.append(line.get("display") or line["name"])
        fulfilled += await async_complete_shopping_for(hass, db, int(product_id))

    hass.bus.async_fire("home_inventory_data_changed", {})
    return {
        "added": added,
        "count": len(added),
        "products_created": created,
        "fulfilled_shopping_ids": fulfilled,
    }


async def async_complete_shopping_for(hass, db, product_id: int) -> list[int]:
    """Tick off open shopping rows for a product. Mirrors the barcode scan path."""
    done: list[int] = []
    for row in await db.list_shopping(include_completed=False):
        if row.get("product_id") == product_id:
            await db.complete_shopping(int(row["id"]))
            done.append(int(row["id"]))
    return done
