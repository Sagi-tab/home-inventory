"""SQLite storage for Home Inventory.

Uses a dedicated SQLite file (not HA's recorder DB) so the data
survives HA upgrades and can be backed up independently.

All DB operations run in the executor to avoid blocking the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from homeassistant.core import HomeAssistant

from .const import DB_FILENAME

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS schema_info (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barcode TEXT UNIQUE,
        name TEXT NOT NULL,
        name_he TEXT,
        brand TEXT,
        category TEXT,
        default_location TEXT,
        image_url TEXT,
        source TEXT DEFAULT 'manual',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_products_name ON products(name)",
    "CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode)",
    # product_barcodes: many barcodes can point to one product.
    # Allows scanning different brands/SKUs of 'the same thing'.
    """
    CREATE TABLE IF NOT EXISTS product_barcodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        barcode TEXT NOT NULL UNIQUE,
        brand TEXT,
        image_url TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        scan_count INTEGER DEFAULT 0,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pb_product ON product_barcodes(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_pb_barcode ON product_barcodes(barcode)",
    """
    CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        quantity REAL NOT NULL DEFAULT 1,
        unit TEXT DEFAULT 'pcs',
        location TEXT,
        expiration_date TEXT,
        threshold REAL DEFAULT 1,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
        UNIQUE(product_id, location, expiration_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_exp ON inventory(expiration_date)",
    """
    CREATE TABLE IF NOT EXISTS shopping_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        name TEXT NOT NULL,
        quantity REAL DEFAULT 1,
        unit TEXT DEFAULT 'pcs',
        category TEXT,
        completed INTEGER DEFAULT 0,
        auto_added INTEGER DEFAULT 0,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shopping_completed ON shopping_list(completed)",
    """
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        product_name TEXT NOT NULL,
        barcode TEXT,
        brand TEXT,
        action TEXT NOT NULL,
        quantity REAL DEFAULT 1,
        source TEXT,
        occurred_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_occurred ON history(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_history_product ON history(product_id)",
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply migrations for existing DBs that predate SCHEMA_VERSION=2.

    v1 -> v2:
      * product_barcodes table (created by SCHEMA_STATEMENTS via IF NOT EXISTS)
      * Backfill product_barcodes from products.barcode
      * history gains barcode / brand columns
    """
    current_row = conn.execute(
        "SELECT value FROM schema_info WHERE key = 'version'"
    ).fetchone()
    current = int(current_row["value"]) if current_row else 0

    if current < 2:
        # Add new history columns if missing (ALTER IF NOT EXISTS doesn't exist in SQLite)
        if not _column_exists(conn, "history", "barcode"):
            conn.execute("ALTER TABLE history ADD COLUMN barcode TEXT")
        if not _column_exists(conn, "history", "brand"):
            conn.execute("ALTER TABLE history ADD COLUMN brand TEXT")

        # Backfill product_barcodes from any products.barcode that isn't already linked
        now = _now_iso()
        rows = conn.execute(
            """
            SELECT id, barcode, brand, image_url
            FROM products
            WHERE barcode IS NOT NULL AND barcode != ''
              AND id NOT IN (SELECT DISTINCT product_id FROM product_barcodes)
            """
        ).fetchall()
        for r in rows:
            # Avoid collision if another product already owns the barcode in product_barcodes
            exists = conn.execute(
                "SELECT 1 FROM product_barcodes WHERE barcode = ?",
                (r["barcode"],),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO product_barcodes(
                    product_id, barcode, brand, image_url,
                    first_seen_at, last_seen_at, scan_count
                ) VALUES (?,?,?,?,?,?,0)
                """,
                (r["id"], r["barcode"], r["brand"], r["image_url"], now, now),
            )
        _LOGGER.info(
            "Migrated %d products into product_barcodes (v1 -> v2)", len(rows)
        )

    conn.execute(
        "INSERT OR REPLACE INTO schema_info(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Async-friendly wrapper around a dedicated SQLite DB."""

    def __init__(self, hass: HomeAssistant, path: Path | None = None) -> None:
        self.hass = hass
        self._path = path or Path(hass.config.path(DB_FILENAME))
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def async_init(self) -> None:
        """Create tables and run migrations."""
        await self.hass.async_add_executor_job(self._init_schema)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            for stmt in SCHEMA_STATEMENTS:
                conn.execute(stmt)
            _migrate(conn)
        _LOGGER.info("Initialized Home Inventory DB at %s", self._path)

    # ---------- Products ----------

    async def upsert_product(
        self,
        *,
        barcode: str | None,
        name: str,
        name_he: str | None = None,
        brand: str | None = None,
        category: str | None = None,
        default_location: str | None = None,
        image_url: str | None = None,
        source: str = "manual",
    ) -> int:
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._upsert_product,
                barcode, name, name_he, brand, category,
                default_location, image_url, source,
            )

    def _upsert_product(
        self, barcode, name, name_he, brand, category,
        default_location, image_url, source,
    ) -> int:
        now = _now_iso()
        with self._connect() as conn:
            pid = None

            # If we have a barcode, look it up in product_barcodes first
            # (this is the authoritative place now).
            if barcode:
                pb = conn.execute(
                    "SELECT product_id FROM product_barcodes WHERE barcode = ?",
                    (barcode,),
                ).fetchone()
                if pb:
                    pid = int(pb["product_id"])
                else:
                    # Legacy fallback: match products.barcode (pre-v2 rows)
                    row = conn.execute(
                        "SELECT id FROM products WHERE barcode = ?", (barcode,)
                    ).fetchone()
                    if row:
                        pid = int(row["id"])

            if pid is not None:
                conn.execute(
                    """
                    UPDATE products SET
                        name = COALESCE(?, name),
                        name_he = COALESCE(?, name_he),
                        brand = COALESCE(?, brand),
                        category = COALESCE(?, category),
                        default_location = COALESCE(?, default_location),
                        image_url = COALESCE(?, image_url),
                        source = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (name, name_he, brand, category, default_location,
                     image_url, source, now, pid),
                )
                # Ensure the barcode is linked in product_barcodes too
                if barcode:
                    self._ensure_barcode_link(
                        conn, pid, barcode, brand, image_url, now
                    )
                return pid

            cur = conn.execute(
                """
                INSERT INTO products(
                    barcode, name, name_he, brand, category,
                    default_location, image_url, source, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (barcode, name, name_he, brand, category,
                 default_location, image_url, source, now, now),
            )
            pid = int(cur.lastrowid)
            if barcode:
                self._ensure_barcode_link(
                    conn, pid, barcode, brand, image_url, now
                )
            return pid

    def _ensure_barcode_link(
        self,
        conn: sqlite3.Connection,
        product_id: int,
        barcode: str,
        brand: str | None,
        image_url: str | None,
        now: str,
    ) -> None:
        """Insert or update a row in product_barcodes for this (product, barcode)."""
        existing = conn.execute(
            "SELECT id, product_id FROM product_barcodes WHERE barcode = ?",
            (barcode,),
        ).fetchone()
        if existing:
            # If it points to a different product, don't silently move it.
            # The caller (link_barcode service) should handle that explicitly.
            if int(existing["product_id"]) != int(product_id):
                _LOGGER.warning(
                    "Barcode %s already linked to product %s; not overwriting "
                    "(product_id=%s). Use link_barcode service to reassign.",
                    barcode, existing["product_id"], product_id,
                )
                return
            conn.execute(
                """
                UPDATE product_barcodes SET
                    brand = COALESCE(?, brand),
                    image_url = COALESCE(?, image_url),
                    last_seen_at = ?
                WHERE id = ?
                """,
                (brand, image_url, now, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO product_barcodes(
                    product_id, barcode, brand, image_url,
                    first_seen_at, last_seen_at, scan_count
                ) VALUES (?,?,?,?,?,?,0)
                """,
                (product_id, barcode, brand, image_url, now, now),
            )

    async def get_product_by_barcode(self, barcode: str) -> dict | None:
        return await self.hass.async_add_executor_job(
            self._get_product_by_barcode, barcode
        )

    def _get_product_by_barcode(self, barcode: str) -> dict | None:
        with self._connect() as conn:
            # Authoritative source: product_barcodes
            pb = conn.execute(
                """
                SELECT pb.brand AS link_brand, pb.image_url AS link_image_url,
                       pb.scan_count, p.*
                FROM product_barcodes pb
                JOIN products p ON p.id = pb.product_id
                WHERE pb.barcode = ?
                """,
                (barcode,),
            ).fetchone()
            if pb:
                d = dict(pb)
                # Use the link-specific brand/image if set, else the product's
                d["brand"] = d.get("link_brand") or d.get("brand")
                d["image_url"] = d.get("link_image_url") or d.get("image_url")
                return d
            # Legacy fallback (pre-migration or unmigrated)
            row = conn.execute(
                "SELECT * FROM products WHERE barcode = ?", (barcode,)
            ).fetchone()
            return dict(row) if row else None

    # ---------- Multi-barcode management ----------

    async def link_barcode(
        self,
        *,
        barcode: str,
        product_id: int,
        brand: str | None = None,
        image_url: str | None = None,
    ) -> None:
        async with self._lock:
            await self.hass.async_add_executor_job(
                self._link_barcode, barcode, product_id, brand, image_url
            )

    def _link_barcode(
        self, barcode: str, product_id: int,
        brand: str | None, image_url: str | None,
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, product_id FROM product_barcodes WHERE barcode = ?",
                (barcode,),
            ).fetchone()
            if existing:
                # Reassign to the new product (explicit user action)
                conn.execute(
                    """
                    UPDATE product_barcodes SET
                        product_id = ?,
                        brand = COALESCE(?, brand),
                        image_url = COALESCE(?, image_url),
                        last_seen_at = ?
                    WHERE id = ?
                    """,
                    (product_id, brand, image_url, now, existing["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO product_barcodes(
                        product_id, barcode, brand, image_url,
                        first_seen_at, last_seen_at, scan_count
                    ) VALUES (?,?,?,?,?,?,0)
                    """,
                    (product_id, barcode, brand, image_url, now, now),
                )

    async def unlink_barcode(self, barcode: str) -> None:
        async with self._lock:
            await self.hass.async_add_executor_job(self._unlink_barcode, barcode)

    def _unlink_barcode(self, barcode: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM product_barcodes WHERE barcode = ?", (barcode,)
            )

    async def get_product_barcodes(self, product_id: int) -> list[dict]:
        return await self.hass.async_add_executor_job(
            self._get_product_barcodes, product_id
        )

    def _get_product_barcodes(self, product_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM product_barcodes
                WHERE product_id = ?
                ORDER BY last_seen_at DESC
                """,
                (product_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    async def product_total_quantity(self, product_id: int) -> float:
        """Sum of quantity across all inventory rows for a product."""
        return await self.hass.async_add_executor_job(
            self._product_total_quantity, product_id
        )

    def _product_total_quantity(self, product_id: int) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(quantity), 0) AS t "
                "FROM inventory WHERE product_id = ?",
                (product_id,),
            ).fetchone()
            return float(row["t"]) if row else 0.0

    async def bump_barcode_scan(self, barcode: str) -> None:
        """Increment scan_count and refresh last_seen_at after a successful scan."""
        async with self._lock:
            await self.hass.async_add_executor_job(self._bump_barcode_scan, barcode)

    def _bump_barcode_scan(self, barcode: str) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE product_barcodes
                SET scan_count = scan_count + 1, last_seen_at = ?
                WHERE barcode = ?
                """,
                (now, barcode),
            )

    async def merge_products(self, src_id: int, dest_id: int) -> None:
        """Move everything from src product to dest, then delete src.

        Moves: product_barcodes, inventory rows (merged by location/expiry),
        shopping_list rows, history rows.
        """
        async with self._lock:
            await self.hass.async_add_executor_job(
                self._merge_products, src_id, dest_id
            )

    def _merge_products(self, src_id: int, dest_id: int) -> None:
        if src_id == dest_id:
            return
        now = _now_iso()
        with self._connect() as conn:
            # 1. Barcodes
            conn.execute(
                "UPDATE product_barcodes SET product_id = ? WHERE product_id = ?",
                (dest_id, src_id),
            )
            # 2. Inventory — merge by (location, expiration_date)
            src_inv = conn.execute(
                "SELECT * FROM inventory WHERE product_id = ?", (src_id,)
            ).fetchall()
            for row in src_inv:
                dest_row = conn.execute(
                    """
                    SELECT id, quantity FROM inventory
                    WHERE product_id = ?
                      AND IFNULL(location,'') = IFNULL(?,'')
                      AND IFNULL(expiration_date,'') = IFNULL(?,'')
                    """,
                    (dest_id, row["location"], row["expiration_date"]),
                ).fetchone()
                if dest_row:
                    conn.execute(
                        "UPDATE inventory SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                        (row["quantity"], now, dest_row["id"]),
                    )
                    conn.execute("DELETE FROM inventory WHERE id = ?", (row["id"],))
                else:
                    conn.execute(
                        "UPDATE inventory SET product_id = ?, updated_at = ? WHERE id = ?",
                        (dest_id, now, row["id"]),
                    )
            # 3. Shopping list
            conn.execute(
                "UPDATE shopping_list SET product_id = ?, updated_at = ? WHERE product_id = ?",
                (dest_id, now, src_id),
            )
            # 4. History
            conn.execute(
                "UPDATE history SET product_id = ? WHERE product_id = ?",
                (dest_id, src_id),
            )
            # 5. Delete source product
            conn.execute("DELETE FROM products WHERE id = ?", (src_id,))

    async def search_products(self, query: str, limit: int = 20) -> list[dict]:
        return await self.hass.async_add_executor_job(
            self._search_products, query, limit
        )

    def _search_products(self, query: str, limit: int) -> list[dict]:
        with self._connect() as conn:
            like = f"%{query}%"
            rows = conn.execute(
                """
                SELECT * FROM products
                WHERE name LIKE ? OR name_he LIKE ? OR brand LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (like, like, like, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    async def get_product(self, product_id: int) -> dict | None:
        return await self.hass.async_add_executor_job(
            self._get_product, product_id
        )

    def _get_product(self, product_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM products WHERE id = ?", (product_id,)
            ).fetchone()
            return dict(row) if row else None

    async def update_product_fields(
        self,
        product_id: int,
        *,
        name: str | None = None,
        name_he: str | None = None,
        brand: str | None = None,
        category: str | None = None,
        default_location: str | None = None,
    ) -> bool:
        """Update one or more fields on a product. Returns True if it existed."""
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._update_product_fields,
                product_id, name, name_he, brand, category, default_location,
            )

    def _update_product_fields(
        self, product_id, name, name_he, brand, category, default_location,
    ) -> bool:
        # Sentinel-driven update: pass empty string to explicitly CLEAR a field,
        # None to leave it untouched.
        sets = []
        params = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if name_he is not None:
            sets.append("name_he = ?")
            params.append(name_he or None)
        if brand is not None:
            sets.append("brand = ?")
            params.append(brand or None)
        if category is not None:
            sets.append("category = ?")
            params.append(category or None)
        if default_location is not None:
            sets.append("default_location = ?")
            params.append(default_location or None)
        if not sets:
            return True  # no-op success
        sets.append("updated_at = ?")
        params.append(_now_iso())
        params.append(int(product_id))
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE products SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            return cur.rowcount > 0

    async def find_similar_products(
        self,
        *,
        name: str | None = None,
        name_he: str | None = None,
        category: str | None = None,
        brand: str | None = None,
        exclude_id: int | None = None,
        limit: int = 5,
        min_score: float = 2.0,
    ) -> list[dict]:
        """Score existing products by similarity to the given attributes.

        Scoring (cumulative, higher = better):
          +3.0  category match
          +2.0  per shared token (len >= 2) between name/name_he and product fields
          +1.0  brand match (informational — brands don't gate merging)
          +  fuzzy name ratio × 3 (SequenceMatcher ratio, 0..1)

        Returns at most `limit` candidates with `score >= min_score`,
        sorted by score desc. Each dict includes a `score` key.
        """
        return await self.hass.async_add_executor_job(
            self._find_similar_products,
            name, name_he, category, brand, exclude_id, limit, min_score,
        )

    def _find_similar_products(
        self, name, name_he, category, brand, exclude_id, limit, min_score,
    ) -> list[dict]:
        from difflib import SequenceMatcher
        import re

        def tokenize(s: str | None) -> set[str]:
            if not s:
                return set()
            # Split on whitespace, punctuation, and digits boundary — keep
            # Hebrew and Latin letters together, lowercase Latin.
            parts = re.split(r"[\s\-/\\,.;:()\[\]'\"!?%&*+=_|]+", s)
            return {p.lower() for p in parts if len(p) >= 2}

        q_tokens = tokenize(name) | tokenize(name_he)
        if not q_tokens and not category and not brand:
            return []

        with self._connect() as conn:
            # Pre-filter candidates to avoid a full table scan.
            clauses = []
            params: list = []
            if q_tokens:
                like_clauses = []
                for t in sorted(q_tokens):
                    like_clauses.append("(name LIKE ? OR name_he LIKE ?)")
                    params.extend([f"%{t}%", f"%{t}%"])
                clauses.append("(" + " OR ".join(like_clauses) + ")")
            if category:
                clauses.append("category = ?")
                params.append(category)
            where_sql = " OR ".join(clauses) if clauses else "1=1"
            sql = f"SELECT * FROM products WHERE {where_sql}"
            if exclude_id is not None:
                sql += f" AND id != {int(exclude_id)}"
            sql += " LIMIT 200"  # hard cap on fuzzy scoring
            candidates = conn.execute(sql, params).fetchall()

        def score(row) -> float:
            s = 0.0
            if category and row["category"] == category:
                s += 3.0
            if brand and row["brand"] and row["brand"].lower() == brand.lower():
                s += 1.0
            row_tokens = tokenize(row["name"]) | tokenize(row["name_he"])
            if q_tokens and row_tokens:
                overlap = q_tokens & row_tokens
                s += 2.0 * len(overlap)
                # Fuzzy similarity of the full names (best of EN/HE)
                best_ratio = 0.0
                for q in (name, name_he):
                    for r in (row["name"], row["name_he"]):
                        if q and r:
                            ratio = SequenceMatcher(
                                None, q.lower(), r.lower()
                            ).ratio()
                            if ratio > best_ratio:
                                best_ratio = ratio
                s += best_ratio * 3.0
            return s

        scored = [(score(r), dict(r)) for r in candidates]
        scored = [(s, r) for s, r in scored if s >= min_score]
        scored.sort(key=lambda p: p[0], reverse=True)
        out = []
        for s, r in scored[:limit]:
            r["score"] = round(s, 2)
            out.append(r)
        return out

    # ---------- Inventory ----------

    async def add_inventory(
        self,
        *,
        product_id: int,
        quantity: float = 1,
        unit: str = "pcs",
        location: str | None = None,
        expiration_date: str | None = None,
        threshold: float | None = None,
        notes: str | None = None,
        barcode: str | None = None,
    ) -> int:
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._add_inventory, product_id, quantity, unit,
                location, expiration_date, threshold, notes, barcode,
            )

    def _add_inventory(
        self, product_id, quantity, unit, location,
        expiration_date, threshold, notes, barcode,
    ) -> int:
        now = _now_iso()
        with self._connect() as conn:
            # Try to find an existing row with the same product/location/expiry
            existing = conn.execute(
                """
                SELECT id, quantity FROM inventory
                WHERE product_id = ?
                  AND IFNULL(location,'') = IFNULL(?,'')
                  AND IFNULL(expiration_date,'') = IFNULL(?,'')
                """,
                (product_id, location, expiration_date),
            ).fetchone()

            if existing:
                new_qty = float(existing["quantity"]) + float(quantity)
                conn.execute(
                    """
                    UPDATE inventory SET
                        quantity = ?,
                        threshold = COALESCE(?, threshold),
                        notes = COALESCE(?, notes),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (new_qty, threshold, notes, now, existing["id"]),
                )
                self._log_history(
                    conn, product_id=product_id,
                    action="add", quantity=quantity,
                    source="scan_or_manual", barcode=barcode,
                )
                return int(existing["id"])

            cur = conn.execute(
                """
                INSERT INTO inventory(
                    product_id, quantity, unit, location,
                    expiration_date, threshold, notes, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (product_id, quantity, unit, location,
                 expiration_date, threshold or 1, notes, now, now),
            )
            self._log_history(
                conn, product_id=product_id,
                action="add", quantity=quantity,
                source="scan_or_manual", barcode=barcode,
            )
            return int(cur.lastrowid)

    async def remove_inventory(
        self,
        *,
        product_id: int,
        quantity: float = 1,
        location: str | None = None,
        expiration_date: str | None = None,
        barcode: str | None = None,
    ) -> tuple[float, bool]:
        """Decrement inventory. Returns (remaining_total, went_to_zero)."""
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._remove_inventory, product_id, quantity, location, expiration_date, barcode
            )

    def _remove_inventory(
        self, product_id, quantity, location, expiration_date, barcode,
    ) -> tuple[float, bool]:
        now = _now_iso()
        with self._connect() as conn:
            if expiration_date is not None:
                # Target only rows with this specific expiration date; do not
                # spill into other dates — the caller explicitly chose this one.
                rows = conn.execute(
                    """
                    SELECT id, quantity FROM inventory
                    WHERE product_id = ?
                      AND IFNULL(expiration_date,'') = IFNULL(?,'')
                    ORDER BY
                        CASE WHEN IFNULL(location,'') = IFNULL(?,'') THEN 0 ELSE 1 END
                    """,
                    (product_id, expiration_date, location),
                ).fetchall()
            else:
                # Default: prefer the row matching location, then FIFO by expiry
                rows = conn.execute(
                    """
                    SELECT id, quantity FROM inventory
                    WHERE product_id = ?
                    ORDER BY
                        CASE WHEN IFNULL(location,'') = IFNULL(?,'') THEN 0 ELSE 1 END,
                        CASE WHEN expiration_date IS NULL THEN 1 ELSE 0 END,
                        expiration_date ASC
                    """,
                    (product_id, location),
                ).fetchall()

            remaining = float(quantity)
            total_before = sum(float(r["quantity"]) for r in rows)

            for r in rows:
                if remaining <= 0:
                    break
                row_qty = float(r["quantity"])
                take = min(row_qty, remaining)
                new_qty = row_qty - take
                if new_qty <= 0:
                    conn.execute("DELETE FROM inventory WHERE id = ?", (r["id"],))
                else:
                    conn.execute(
                        "UPDATE inventory SET quantity = ?, updated_at = ? WHERE id = ?",
                        (new_qty, now, r["id"]),
                    )
                remaining -= take

            # Recalculate total
            total_after_row = conn.execute(
                "SELECT COALESCE(SUM(quantity), 0) as t FROM inventory WHERE product_id = ?",
                (product_id,),
            ).fetchone()
            total_after = float(total_after_row["t"])

            self._log_history(
                conn, product_id=product_id,
                action="remove",
                quantity=min(quantity, total_before),
                source="scan_or_manual", barcode=barcode,
            )

            went_to_zero = total_before > 0 and total_after <= 0
            return total_after, went_to_zero

    async def list_inventory(self) -> list[dict]:
        return await self.hass.async_add_executor_job(self._list_inventory)

    def _list_inventory(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.*,
                    p.name AS product_name,
                    p.name_he AS product_name_he,
                    p.brand,
                    p.category,
                    p.barcode,
                    p.image_url,
                    (SELECT pb.brand FROM product_barcodes pb
                       WHERE pb.product_id = p.id AND pb.brand IS NOT NULL
                       ORDER BY pb.last_seen_at DESC LIMIT 1) AS latest_brand
                FROM inventory i
                JOIN products p ON p.id = i.product_id
                ORDER BY p.category, p.name
                """
            ).fetchall()
            return [dict(r) for r in rows]

    async def get_expiring_items(self, days: int) -> list[dict]:
        return await self.hass.async_add_executor_job(
            self._get_expiring_items, days
        )

    def _get_expiring_items(self, days: int) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.*, p.name AS product_name, p.name_he AS product_name_he
                FROM inventory i
                JOIN products p ON p.id = i.product_id
                WHERE i.expiration_date IS NOT NULL
                  AND i.expiration_date >= ?
                  AND i.expiration_date <= ?
                ORDER BY i.expiration_date ASC
                """,
                (today, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]

    async def get_low_stock_items(self) -> list[dict]:
        return await self.hass.async_add_executor_job(self._get_low_stock_items)

    def _get_low_stock_items(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    p.id AS product_id,
                    p.name AS product_name,
                    p.name_he AS product_name_he,
                    p.category,
                    COALESCE(SUM(i.quantity), 0) AS total_quantity,
                    MAX(i.threshold) AS threshold
                FROM products p
                LEFT JOIN inventory i ON i.product_id = p.id
                GROUP BY p.id
                HAVING total_quantity <= IFNULL(threshold, 1)
                   AND EXISTS (SELECT 1 FROM history h WHERE h.product_id = p.id)
                ORDER BY p.name
                """
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- Shopping list ----------

    async def add_shopping(
        self,
        *,
        name: str,
        product_id: int | None = None,
        quantity: float = 1,
        unit: str = "pcs",
        category: str | None = None,
        auto_added: bool = False,
        notes: str | None = None,
    ) -> int:
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._add_shopping, name, product_id, quantity, unit,
                category, auto_added, notes,
            )

    def _add_shopping(
        self, name, product_id, quantity, unit, category, auto_added, notes
    ) -> int:
        now = _now_iso()
        with self._connect() as conn:
            # Merge with an existing uncompleted entry for same product / name
            if product_id is not None:
                existing = conn.execute(
                    "SELECT id, quantity FROM shopping_list "
                    "WHERE product_id = ? AND completed = 0",
                    (product_id,),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT id, quantity FROM shopping_list "
                    "WHERE name = ? AND completed = 0",
                    (name,),
                ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE shopping_list SET quantity = ?, updated_at = ? WHERE id = ?",
                    (float(existing["quantity"]) + float(quantity), now, existing["id"]),
                )
                return int(existing["id"])

            cur = conn.execute(
                """
                INSERT INTO shopping_list(
                    product_id, name, quantity, unit, category,
                    auto_added, notes, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (product_id, name, quantity, unit, category,
                 1 if auto_added else 0, notes, now, now),
            )
            return int(cur.lastrowid)

    async def list_shopping(self, include_completed: bool = False) -> list[dict]:
        return await self.hass.async_add_executor_job(
            self._list_shopping, include_completed
        )

    def _list_shopping(self, include_completed: bool) -> list[dict]:
        with self._connect() as conn:
            if include_completed:
                rows = conn.execute(
                    "SELECT * FROM shopping_list ORDER BY completed, created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM shopping_list WHERE completed = 0 "
                    "ORDER BY category, created_at"
                ).fetchall()
            return [dict(r) for r in rows]

    async def complete_shopping(self, item_id: int) -> None:
        async with self._lock:
            await self.hass.async_add_executor_job(self._complete_shopping, item_id)

    def _complete_shopping(self, item_id: int) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE shopping_list SET completed = 1, updated_at = ? WHERE id = ?",
                (now, item_id),
            )

    async def update_shopping_quantity(self, item_id: int, quantity: float) -> dict | None:
        """Set the quantity on an existing shopping row to an absolute value.

        Negative or zero quantity deletes the row. Returns the row before
        update so callers can compare/log; returns None if the item doesn't
        exist.
        """
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._update_shopping_quantity, item_id, quantity,
            )

    def _update_shopping_quantity(self, item_id: int, quantity: float) -> dict | None:
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM shopping_list WHERE id = ?", (item_id,),
            ).fetchone()
            if not row:
                return None
            if quantity <= 0:
                conn.execute("DELETE FROM shopping_list WHERE id = ?", (item_id,))
            else:
                conn.execute(
                    "UPDATE shopping_list SET quantity = ?, updated_at = ? WHERE id = ?",
                    (float(quantity), now, item_id),
                )
            return dict(row)

    async def delete_shopping(self, item_id: int) -> None:
        async with self._lock:
            await self.hass.async_add_executor_job(self._delete_shopping, item_id)

    def _delete_shopping(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM shopping_list WHERE id = ?", (item_id,))

    # ---------- Threshold ----------

    async def set_threshold(self, product_id: int, threshold: float) -> None:
        async with self._lock:
            await self.hass.async_add_executor_job(
                self._set_threshold, product_id, threshold
            )

    def _set_threshold(self, product_id: int, threshold: float) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE inventory SET threshold = ?, updated_at = ? WHERE product_id = ?",
                (threshold, now, product_id),
            )

    # ---------- History ----------

    def _log_history(
        self, conn: sqlite3.Connection, *,
        product_id: int | None, action: str,
        quantity: float, source: str,
        barcode: str | None = None, brand: str | None = None,
    ) -> None:
        name_row = None
        if product_id:
            name_row = conn.execute(
                "SELECT name FROM products WHERE id = ?", (product_id,)
            ).fetchone()
        # If caller didn't pass brand but did pass barcode, look it up
        if barcode and brand is None:
            pb = conn.execute(
                "SELECT brand FROM product_barcodes WHERE barcode = ?", (barcode,)
            ).fetchone()
            if pb:
                brand = pb["brand"]
        conn.execute(
            """
            INSERT INTO history(
                product_id, product_name, barcode, brand,
                action, quantity, source, occurred_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (product_id, name_row["name"] if name_row else "(unknown)",
             barcode, brand, action, quantity, source, _now_iso()),
        )

    async def recent_history(self, limit: int = 50) -> list[dict]:
        return await self.hass.async_add_executor_job(self._recent_history, limit)

    def _recent_history(self, limit: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM history ORDER BY occurred_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    async def consumption_stats(self, days: int = 30) -> list[dict]:
        return await self.hass.async_add_executor_job(self._consumption_stats, days)

    def _consumption_stats(self, days: int) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    product_id,
                    product_name,
                    SUM(CASE WHEN action='add' THEN quantity ELSE 0 END) AS added,
                    SUM(CASE WHEN action='remove' THEN quantity ELSE 0 END) AS removed,
                    COUNT(*) AS actions
                FROM history
                WHERE occurred_at >= ?
                GROUP BY product_id, product_name
                ORDER BY actions DESC
                """,
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- Totals ----------

    async def totals(self) -> dict[str, Any]:
        return await self.hass.async_add_executor_job(self._totals)

    def _totals(self) -> dict[str, Any]:
        with self._connect() as conn:
            inv = conn.execute(
                "SELECT COUNT(*) AS items, COALESCE(SUM(quantity),0) AS qty FROM inventory"
            ).fetchone()
            shop = conn.execute(
                "SELECT COUNT(*) AS items FROM shopping_list WHERE completed = 0"
            ).fetchone()
            return {
                "inventory_lines": int(inv["items"]),
                "inventory_quantity": float(inv["qty"]),
                "shopping_open": int(shop["items"]),
            }
