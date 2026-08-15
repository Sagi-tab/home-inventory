/**
 * Home Inventory — Lovelace Custom Card
 * ----------------------------------------
 *   type: custom:home-inventory-card
 *
 * Uses html5-qrcode (loaded from unpkg CDN) for in-browser barcode scanning.
 * Calls the home_inventory integration's HTTP API + services.
 */

// Served from the integration's own static path — no CDN dependency
const HTML5_QRCODE_URL = "/home_inventory_static/html5-qrcode.min.js";
const API = "/api/home_inventory";
const CATEGORIES = ["dairy", "bakery", "produce", "meat_fish", "frozen", "pantry", "beverages", "snacks", "cleaning", "personal_care", "other"];
const CATEGORY_LABELS = {
  dairy: "🧀 Dairy",
  bakery: "🍞 Bakery",
  produce: "🥬 Produce",
  meat_fish: "🥩 Meat & Fish",
  frozen: "❄️ Frozen",
  pantry: "🥫 Pantry",
  beverages: "🥤 Beverages",
  snacks: "🍪 Snacks",
  cleaning: "🧽 Cleaning",
  personal_care: "🧴 Personal Care",
  other: "📦 Other"
};

/* Load html5-qrcode once */
let _scannerLibPromise = null;
function loadScannerLib() {
  if (window.Html5Qrcode) return Promise.resolve();
  if (_scannerLibPromise) return _scannerLibPromise;
  _scannerLibPromise = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = HTML5_QRCODE_URL;
    s.onload = resolve;
    s.onerror = () => reject(new Error("Failed to load html5-qrcode"));
    document.head.appendChild(s);
  });
  return _scannerLibPromise;
}

/* Beep on successful scan */
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 880;
    gain.gain.value = 0.1;
    osc.start();
    osc.stop(ctx.currentTime + 0.12);
  } catch (e) {/* ignore */}
}
class HomeInventoryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({
      mode: "open"
    });
    this._tab = "shopping";
    // Inventory is always shown grouped (one row per product). The flat
    // by-barcode listing is still fetched and used to populate the
    // expandable per-product barcode panel.
    this._expanded = new Set(); // product_ids with expanded barcode list
    this._inventory = [];
    this._shopping = [];
    this._history = [];
    this._stats = [];
    this._filter = "";
    this._inventoryGrouped = [];
    this._unsub = null;
    // Mode: "default" (deduct on scan) or "stocking" (add on scan).
    // Persisted across reloads so the user can stock up over multiple sessions.
    try {
      this._mode = window.localStorage.getItem("home_inventory_mode") || "default";
    } catch (e) {
      this._mode = "default";
    }
  }
  _setMode(mode) {
    this._mode = mode;
    try {
      window.localStorage.setItem("home_inventory_mode", mode);
    } catch (e) {}
    this._render();
  }
  setConfig(config) {
    this._config = config || {};
  }
  getCardSize() {
    return 8;
  }
  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._render();
      this._refresh();
      this._subscribe();
    }
  }
  disconnectedCallback() {
    if (this._unsub) {
      this._unsub();
      this._unsub = null;
    }
  }
  async _subscribe() {
    if (!this._hass || !this._hass.connection) return;
    try {
      this._unsub = await this._hass.connection.subscribeEvents(() => this._refresh(), "home_inventory_data_changed");
    } catch (e) {
      console.warn("subscribe failed", e);
    }
  }
  async _api(path) {
    const resp = await this._hass.fetchWithAuth(API + path);
    if (!resp.ok) throw new Error(`API ${path} -> ${resp.status}`);
    return resp.json();
  }
  _groupInventory(rows) {
    const map = new Map();
    rows.forEach(row => {
      const pid = row.product_id;
      if (!map.has(pid)) {
        map.set(pid, {
          product_id: pid,
          product_name: row.product_name,
          product_name_he: row.product_name_he,
          brand: row.brand,
          latest_brand: row.latest_brand,
          category: row.category,
          unit: row.unit,
          rows: []
        });
      }
      map.get(pid).rows.push(row);
    });
    const groups = [];
    map.forEach(g => {
      g.quantity = g.rows.reduce((s, r) => s + (parseFloat(r.quantity) || 0), 0);
      const dates = g.rows.filter(r => r.expiration_date).map(r => r.expiration_date).sort();
      g.expiration_date = dates[0] || null;
      groups.push(g);
    });
    return groups;
  }
  async _refresh() {
    try {
      const [inv, shop, hist] = await Promise.all([this._api("/inventory"), this._api("/shopping"), this._api("/history?limit=30&stats_days=30")]);
      this._inventory = inv.items || [];
      this._inventoryGrouped = this._groupInventory(this._inventory);
      this._shopping = shop.items || [];
      this._history = hist.recent || [];
      this._stats = hist.stats || [];
      this._render();
    } catch (e) {
      console.error("refresh failed", e);
    }
  }
  async _callService(domain, service, data, returnResponse = false) {
    return this._hass.callService(domain, service, data, undefined, true, returnResponse);
  }

  /* ---------- Top-level render ---------- */

  _render() {
    if (!this.shadowRoot) return;
    const stocking = this._mode === "stocking";
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card class="${stocking ? 'stocking-active' : ''}">
        <div class="header">
          <div class="title">🛒 Home Inventory</div>
          <div class="header-actions">
            <button class="scan-btn ${stocking ? 'stock-mode' : ''}" id="scan">
              ${stocking ? "📷 Stock Up" : "📷 Scan"}
            </button>
            <button class="mode-btn ${stocking ? 'on' : ''}" id="mode-toggle"
                    title="${stocking ? 'Exit Stock Up mode' : 'Enter Stock Up mode'}">
              ${stocking ? "✓ Done" : "📦"}
            </button>
          </div>
        </div>
        <div class="tabs">
          ${["shopping", "inventory", "history"].map(t => `
            <button class="tab ${this._tab === t ? "active" : ""}" data-tab="${t}">
              ${t === "inventory" ? "Inventory" : t === "shopping" ? "Shopping" : "History"}
              ${t === "shopping" && this._shopping.length ? `<span class="badge">${this._shopping.length}</span>` : ""}
            </button>
          `).join("")}
        </div>
        <div class="filter-row">
          <input id="filter" type="text" placeholder="Search or type to add…" value="${this._escape(this._filter)}"/>
          <button id="quick-add" title="Add as inventory">＋</button>
        </div>
        <div class="content">
          ${this._renderTab()}
        </div>
      </ha-card>
    `;
    this._bind();
  }
  _renderTab() {
    if (this._tab === "inventory") return this._renderInventory();
    if (this._tab === "shopping") return this._renderShopping();
    return this._renderHistory();
  }
  _renderInventory() {
    const q = this._filter.toLowerCase();
    const filtered = this._inventoryGrouped.filter(g => !q || (g.product_name || "").toLowerCase().includes(q) || (g.product_name_he || "").includes(this._filter) || (g.brand || g.latest_brand || "").toLowerCase().includes(q));
    if (!filtered.length) {
      return `<div class="empty">${this._filter ? `No match. Tap ＋ to add "${this._escape(this._filter)}" to inventory.` : "Your inventory is empty. Scan a barcode or add an item."}</div>`;
    }
    // Group by category
    const groups = {};
    filtered.forEach(g => {
      const cat = g.category || "other";
      (groups[cat] = groups[cat] || []).push(g);
    });
    return Object.keys(groups).sort().map(cat => `
      <div class="group-header">${CATEGORY_LABELS[cat] || cat}</div>
      ${groups[cat].map(g => this._renderInventoryRow(g)).join("")}
    `).join("");
  }
  _renderInventoryRow(g) {
    const name = g.product_name_he || g.product_name;
    const rtl = !!g.product_name_he;
    const exp = g.expiration_date ? this._expirationPill(g.expiration_date) : "";
    const isExpanded = this._expanded.has(g.product_id);
    const hasExpDates = g.rows.some(r => r.expiration_date);
    const hasUndatedRows = g.rows.some(r => !r.expiration_date);
    const canExpand = hasExpDates || hasUndatedRows;
    return `
      <div class="row ${canExpand ? 'expandable' : ''}" data-product-id="${g.product_id}">
        <div class="row-main" data-act="${canExpand ? 'toggle-expand' : ''}" data-product-id="${g.product_id}">
          <div class="row-title" ${rtl ? 'dir="rtl"' : ""}>
            ${this._escape(name)}
            ${canExpand ? `<span class="expand-caret">${isExpanded ? "▾" : "▸"}</span>` : ""}
          </div>
          <div class="row-sub">
            <span class="qty">${this._fmtQty(g.quantity)} ${g.unit || ""}</span>
            ${exp}
          </div>
        </div>
        <div class="row-actions">
          <button class="mini" data-act="dec" data-product-id="${g.product_id}">−</button>
          <button class="mini" data-act="inc" data-product-id="${g.product_id}">＋</button>
          <button class="mini cart" data-act="add-to-shop" data-product-id="${g.product_id}" data-name="${this._escape(name)}" data-cat="${this._escape(g.category || "")}" title="Add to shopping list">🛒</button>
          <button class="mini ghost" data-act="edit-product" data-product-id="${g.product_id}" title="Edit product">✎</button>
        </div>
      </div>
      ${isExpanded ? this._renderExpandedDetails(g) : ""}
    `;
  }
  _renderExpandedDetails(g) {
    const sections = [];
    const productName = this._escape(g.product_name_he || g.product_name);
    const rowsWithDates = g.rows.filter(r => r.expiration_date);
    const rowsWithoutDates = g.rows.filter(r => !r.expiration_date);

    // --- Dated units: show each with a ✕ remove button ---
    if (rowsWithDates.length > 0) {
      const units = [];
      rowsWithDates.forEach(r => {
        const qty = Math.max(1, Math.round(parseFloat(r.quantity) || 1));
        for (let i = 0; i < qty; i++) {
          units.push({ exp: r.expiration_date, location: r.location });
        }
      });
      units.sort((a, b) => a.exp.localeCompare(b.exp));
      sections.push(`
        <div class="expanded-title">Expiration dates:</div>
        ${units.map(u => `
          <div class="expanded-row">
            ${this._expirationPill(u.exp)}
            <button class="mini danger" data-act="remove-exp"
                    data-product-id="${g.product_id}"
                    data-exp="${this._escape(u.exp)}"
                    title="Remove this unit">✕</button>
          </div>
        `).join("")}
      `);
    }

    // --- Undated units: show each with an inline date picker ---
    if (rowsWithoutDates.length > 0) {
      const undatedUnits = [];
      rowsWithoutDates.forEach(r => {
        const qty = Math.max(1, Math.round(parseFloat(r.quantity) || 1));
        for (let i = 0; i < qty; i++) {
          undatedUnits.push({ location: r.location });
        }
      });
      const titleMargin = rowsWithDates.length > 0 ? ' style="margin-top:8px;"' : '';
      sections.push(`
        <div class="expanded-title"${titleMargin}>No expiry date set:</div>
        ${undatedUnits.map(u => `
          <div class="expanded-row">
            <span class="pill muted">No expiry</span>
            <input type="date" class="exp-picker" />
            <button class="mini" data-act="set-exp-new"
                    data-product-id="${g.product_id}"
                    data-name="${productName}"
                    title="Set expiration date">✓</button>
          </div>
        `).join("")}
      `);
    }

    if (!sections.length) return "";
    return `<div class="expanded">${sections.join("")}</div>`;
  }
  _renderShopping() {
    if (!this._shopping.length) {
      return `<div class="empty">Shopping list is empty. 🎉</div>`;
    }
    // Group by category
    const groups = {};
    this._shopping.forEach(i => {
      const cat = i.category || "other";
      (groups[cat] = groups[cat] || []).push(i);
    });
    return Object.keys(groups).sort().map(cat => `
      <div class="group-header">${CATEGORY_LABELS[cat] || cat}</div>
      ${groups[cat].map(i => `
        <div class="row shop" data-id="${i.id}">
          <input type="checkbox" class="chk" data-id="${i.id}"/>
          <div class="row-main">
            <div class="row-title">${this._escape(i.name)}</div>
            <div class="row-sub">
              <span class="qty">${this._fmtQty(i.quantity)} ${i.unit || "pcs"}</span>
              ${i.auto_added ? `<span class="pill auto">auto</span>` : ""}
            </div>
          </div>
          <div class="row-actions">
            <button class="mini" data-act="shop-dec" data-id="${i.id}" data-qty="${i.quantity}">−</button>
            <button class="mini" data-act="shop-inc" data-id="${i.id}" data-qty="${i.quantity}">＋</button>
            <button class="mini danger" data-act="del-shop" data-id="${i.id}">✕</button>
          </div>
        </div>
      `).join("")}
    `).join("");
  }
  _renderHistory() {
    if (!this._history.length && !this._stats.length) {
      return `<div class="empty">No activity yet.</div>`;
    }
    const stats = this._stats.slice(0, 8);
    const recent = this._history.slice(0, 20);
    return `
      <div class="group-header">Top items (last 30 days)</div>
      ${stats.map(s => `
        <div class="row stat">
          <div class="row-main">
            <div class="row-title">${this._escape(s.product_name)}</div>
            <div class="row-sub">
              <span class="pill">added ${this._fmtQty(s.added)}</span>
              <span class="pill">used ${this._fmtQty(s.removed)}</span>
              <span class="pill muted">${s.actions} actions</span>
            </div>
          </div>
        </div>
      `).join("")}
      <div class="group-header">Recent activity</div>
      ${recent.map(h => `
        <div class="row">
          <div class="row-main">
            <div class="row-title">
              ${h.action === "add" ? "➕" : "➖"} ${this._escape(h.product_name)}
            </div>
            <div class="row-sub">
              <span class="qty">${this._fmtQty(h.quantity)}</span>
              <span class="pill muted">${new Date(h.occurred_at).toLocaleString()}</span>
            </div>
          </div>
        </div>
      `).join("")}
    `;
  }
  _expirationPill(iso) {
    try {
      const d = new Date(iso);
      const diff = Math.ceil((d - new Date()) / 86400000);
      const cls = diff < 0 ? "expired" : diff <= 3 ? "warn" : "";
      const label = diff < 0 ? `expired ${-diff}d ago` : diff === 0 ? "expires today" : `in ${diff}d`;
      return `<span class="pill exp ${cls}">${label}</span>`;
    } catch {
      return "";
    }
  }

  /* ---------- Event binding ---------- */

  _bind() {
    var _$, _$2, _$3;
    const $ = s => this.shadowRoot.querySelector(s);
    const $$ = s => this.shadowRoot.querySelectorAll(s);
    (_$ = $("#scan")) === null || _$ === void 0 || _$.addEventListener("click", () => this._openScanner());
    (_$2 = $("#mode-toggle")) === null || _$2 === void 0 || _$2.addEventListener("click", () => this._setMode(this._mode === "stocking" ? "default" : "stocking"));
    $$(".tab").forEach(b => b.addEventListener("click", () => {
      this._tab = b.dataset.tab;
      this._render();
    }));
    const filter = $("#filter");
    if (filter) {
      filter.addEventListener("input", e => {
        this._filter = e.target.value;
      });
      filter.addEventListener("keydown", async e => {
        if (e.key === "Enter" && this._filter.trim()) {
          await this._openAddDialog(this._filter.trim());
          this._filter = "";
        }
      });
    }
    (_$3 = $("#quick-add")) === null || _$3 === void 0 || _$3.addEventListener("click", () => {
      if (this._filter.trim()) this._openAddDialog(this._filter.trim());
    });
    $$("[data-act='inc']").forEach(b => b.addEventListener("click", e => {
      e.stopPropagation();
      this._adjustQuantity(b.dataset.productId, +1);
    }));
    $$("[data-act='dec']").forEach(b => b.addEventListener("click", e => {
      e.stopPropagation();
      this._adjustQuantity(b.dataset.productId, -1);
    }));
    $$("[data-act='toggle-expand']").forEach(el => el.addEventListener("click", () => {
      const pid = parseInt(el.dataset.productId, 10);
      if (this._expanded.has(pid)) this._expanded.delete(pid);else this._expanded.add(pid);
      this._render();
    }));
    $$("[data-act='unlink-barcode']").forEach(b => b.addEventListener("click", async e => {
      e.stopPropagation();
      const bc = b.dataset.barcode;
      if (!confirm(`Unlink barcode ${bc} from this product?`)) return;
      await this._callService("home_inventory", "unlink_barcode", {
        barcode: bc
      });
      this._refresh();
    }));
    $$("[data-act='edit-product']").forEach(b => b.addEventListener("click", e => {
      e.stopPropagation();
      const pid = parseInt(b.dataset.productId, 10);
      this._openProductDialog(pid);
    }));
    $$("[data-act='add-to-shop']").forEach(b => b.addEventListener("click", e => {
      e.stopPropagation();
      const pid = parseInt(b.dataset.productId, 10);
      this._openAddToShoppingDialog(pid, b.dataset.name, b.dataset.cat);
    }));
    // Delegated handler for expanded-section buttons — bound once on .content so
    // it survives partial DOM replacements of individual expanded sections.
    const contentEl = this.shadowRoot.querySelector(".content");
    if (contentEl) contentEl.addEventListener("click", async e => {
      const removeBtn = e.target.closest("[data-act='remove-exp']");
      if (removeBtn) {
        e.stopPropagation();
        const pid = parseInt(removeBtn.dataset.productId, 10);
        const exp = removeBtn.dataset.exp;
        try {
          await this._callService("home_inventory", "remove_item", { product_id: pid, quantity: 1, expiration_date: exp });
          beep(); this._refresh();
        } catch (err) { alert("Error: " + ((err === null || err === void 0 ? void 0 : err.message) || err)); }
        return;
      }
      const setExpBtn = e.target.closest("[data-act='set-exp-new']");
      if (!setExpBtn) return;
      e.stopPropagation();
      const row = setExpBtn.closest(".expanded-row");
      const date = row ? row.querySelector(".exp-picker").value : null;
      if (!date) { alert("Please pick a date first."); return; }
      const pid = parseInt(setExpBtn.dataset.productId, 10);
      const name = setExpBtn.dataset.name;
      // Save values from the other undated pickers so we can restore them after
      // the partial DOM update (avoids a full re-render that would clear them).
      const expandedEl = setExpBtn.closest(".expanded");
      const savedValues = expandedEl
        ? [...expandedEl.querySelectorAll("[data-act='set-exp-new']")]
            .filter(x => x !== setExpBtn)
            .map(btn => { const r = btn.closest(".expanded-row"); return r ? r.querySelector(".exp-picker").value : ""; })
        : [];
      try {
        await this._callService("home_inventory", "add_item", { name, link_to_product_id: pid, quantity: 1, expiration_date: date });
        await this._callService("home_inventory", "remove_item", { product_id: pid, quantity: 1, expiration_date: "" });
        // Update the local group so the partial re-render reflects the change.
        const group = this._inventoryGrouped.find(g => g.product_id === pid);
        if (group) {
          const undatedRow = group.rows.find(r => !r.expiration_date);
          if (undatedRow) {
            undatedRow.quantity = (parseFloat(undatedRow.quantity) || 1) - 1;
            if (undatedRow.quantity <= 0) group.rows = group.rows.filter(r => r !== undatedRow);
          }
          const datedRow = group.rows.find(r => r.expiration_date === date);
          if (datedRow) { datedRow.quantity = (parseFloat(datedRow.quantity) || 0) + 1; }
          else { group.rows.push({ product_id: pid, product_name: group.product_name, product_name_he: group.product_name_he, category: group.category, unit: group.unit, expiration_date: date, quantity: 1 }); }
          group.quantity = group.rows.reduce((s, r) => s + (parseFloat(r.quantity) || 0), 0);
          const allDates = group.rows.filter(r => r.expiration_date).map(r => r.expiration_date).sort();
          group.expiration_date = allDates[0] || null;
          // Replace just the expanded section in the DOM.
          const curExpEl = this.shadowRoot.querySelector(`.row[data-product-id="${pid}"] + .expanded`);
          if (curExpEl) {
            const tmp = document.createElement("div");
            tmp.innerHTML = this._renderExpandedDetails(group);
            const newExpEl = tmp.firstElementChild;
            if (newExpEl) {
              curExpEl.replaceWith(newExpEl);
              // Restore previously-typed values to the remaining undated pickers.
              const updEl = this.shadowRoot.querySelector(`.row[data-product-id="${pid}"] + .expanded`);
              if (updEl && savedValues.length) {
                [...updEl.querySelectorAll("[data-act='set-exp-new']")].forEach((btn, i) => {
                  if (savedValues[i]) { const r = btn.closest(".expanded-row"); if (r) r.querySelector(".exp-picker").value = savedValues[i]; }
                });
              }
            } else { curExpEl.remove(); }
          }
        } else { this._refresh(); }
      } catch (err) { alert("Error: " + ((err === null || err === void 0 ? void 0 : err.message) || err)); }
    });
    $$(".chk").forEach(c => c.addEventListener("change", () => {
      if (c.checked) this._completeShopping(c.dataset.id);
    }));
    $$("[data-act='shop-inc']").forEach(b => b.addEventListener("click", e => {
      e.stopPropagation();
      const id = parseInt(b.dataset.id, 10);
      const qty = parseFloat(b.dataset.qty) || 1;
      this._setShoppingQuantity(id, qty + 1);
    }));
    $$("[data-act='shop-dec']").forEach(b => b.addEventListener("click", e => {
      e.stopPropagation();
      const id = parseInt(b.dataset.id, 10);
      const qty = parseFloat(b.dataset.qty) || 1;
      // Decrement; if it would go to <=0, fall through to delete via 0-update
      this._setShoppingQuantity(id, Math.max(0, qty - 1));
    }));
    $$("[data-act='del-shop']").forEach(b => b.addEventListener("click", e => {
      e.stopPropagation();
      // Remove without adding to inventory (qty=0 deletes the row)
      this._setShoppingQuantity(parseInt(b.dataset.id, 10), 0);
    }));
  }

  /* ---------- Actions ---------- */

  async _adjustQuantity(productId, delta) {
    const pid = parseInt(productId, 10);
    if (delta > 0) {
      const row = this._inventoryGrouped.find(g => g.product_id == pid);
      const payload = {
        name: row ? row.product_name : "Unknown",
        quantity: 1
      };
      // Only pass category if it's one of our known slugs — OFF returns
      // free-text categories that won't match our enum.
      if (row && CATEGORIES.includes(row.category)) {
        payload.category = row.category;
      }
      await this._callService("home_inventory", "add_item", payload);
    } else {
      const resp = await this._callService("home_inventory", "remove_item", {
        product_id: pid,
        quantity: 1
      }, true);
      // If depleted and returned, offer to add to shopping
      if (resp && resp.response && resp.response.remaining <= 0) {
        const row = this._inventoryGrouped.find(g => g.product_id == pid);
        if (row && !resp.response.added_to_shopping) {
          if (confirm(`"${row.product_name}" is now depleted. Add to shopping list?`)) {
            const shopPayload = {
              name: row.product_name,
              product_id: pid
            };
            if (CATEGORIES.includes(row.category)) {
              shopPayload.category = row.category;
            }
            await this._callService("home_inventory", "add_to_shopping", shopPayload);
          }
        }
      }
    }
    beep();
    this._refresh();
  }
  async _completeShopping(id) {
    var _resp;
    let resp;
    try {
      resp = await this._callService("home_inventory", "complete_shopping_item", {
        item_id: parseInt(id, 10),
        add_to_inventory: true
      }, true);
    } catch (e) {
      alert("Couldn't complete: " + ((e === null || e === void 0 ? void 0 : e.message) || e));
      this._refresh();
      return;
    }
    this._refresh();
    const r = (_resp = resp) === null || _resp === void 0 ? void 0 : _resp.response;
    if (r !== null && r !== void 0 && r.added_to_inventory && (r === null || r === void 0 ? void 0 : r.added_quantity) > 0) {
      const name = r.name || "item";
      const qty = this._fmtQty(r.added_quantity);
      this._showToast(`✓ ${qty} × ${name} → inventory`, async () => {
        // Undo: remove from inventory + restore the shopping item
        if (r.product_id) {
          try {
            await this._callService("home_inventory", "remove_item", {
              product_id: r.product_id,
              quantity: r.added_quantity
            });
          } catch (e) {/* ignore */}
        }
        // Re-add to shopping list
        try {
          await this._callService("home_inventory", "add_to_shopping", {
            name,
            product_id: r.product_id,
            quantity: r.added_quantity
          });
        } catch (e) {/* ignore */}
        this._refresh();
      }, 6000);
    } else if (r !== null && r !== void 0 && r.completed && !r.product_id) {
      this._showToast(`✓ "${r.name || 'item'}" checked off`, async () => {
        // Undo: re-add the shopping item
        try {
          await this._callService("home_inventory", "add_to_shopping", {
            name: r.name,
            quantity: r.added_quantity || 1
          });
          this._refresh();
        } catch (e) {/* ignore */}
      }, 5000);
    }
  }
  async _setShoppingQuantity(id, quantity) {
    try {
      await this._callService("home_inventory", "update_shopping_quantity", {
        item_id: id,
        quantity
      });
    } catch (e) {
      alert("Update failed: " + ((e === null || e === void 0 ? void 0 : e.message) || e));
    }
    this._refresh();
  }

  /* ---------- Scanner modal ----------
   *
   * IMPORTANT: html5-qrcode uses document.getElementById internally,
   * which cannot traverse into a shadow root. So the modal must be
   * attached to document.body (light DOM), NOT to this.shadowRoot.
   *
   * Also: we bind Cancel / manual-entry listeners BEFORE attempting to
   * start the camera, so they still work even if camera init fails.
   */

  async _openScanner() {
    // Build modal first so user can always cancel / enter manually
    const uid = Date.now();
    const readerId = `hi-reader-${uid}`;
    const modal = document.createElement("div");
    modal.className = "hi-modal";
    modal.innerHTML = `
      <style>${this._modalStyles()}</style>
      <div class="hi-dialog">
        <div class="hi-mdl-title">Scan Barcode</div>
        <div class="hi-reader-wrap">
          <div id="${readerId}" class="hi-reader"></div>
          <div class="hi-reader-status" id="hi-status-${uid}">Starting camera…</div>
        </div>
        <div class="hi-mdl-row">
          <input class="hi-input" id="hi-manual-${uid}" type="text"
                 inputmode="numeric" placeholder="Or type barcode and press Go"/>
          <button class="hi-btn" id="hi-go-${uid}">Go</button>
        </div>
        <div class="hi-mdl-actions">
          <button class="hi-btn ghost" id="hi-cancel-${uid}">Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
    const qs = id => modal.querySelector(`#${id}`);
    const statusEl = qs(`hi-status-${uid}`);
    const manualInput = qs(`hi-manual-${uid}`);
    const goBtn = qs(`hi-go-${uid}`);
    const cancelBtn = qs(`hi-cancel-${uid}`);
    let scanner = null;
    let stopped = false;
    const stop = async () => {
      if (stopped) return;
      stopped = true;
      if (scanner) {
        try {
          await scanner.stop();
        } catch (e) {/* ignore */}
        try {
          scanner.clear();
        } catch (e) {/* ignore */}
      }
      modal.remove();
    };
    const submitManual = async () => {
      const v = (manualInput.value || "").trim();
      if (!v) return;
      await stop();
      this._handleScan(v);
    };

    // Bind these FIRST so they work even if camera init fails
    cancelBtn.addEventListener("click", stop);
    goBtn.addEventListener("click", submitManual);
    manualInput.addEventListener("keydown", e => {
      if (e.key === "Enter") submitManual();
    });
    // Tap outside dialog to cancel
    modal.addEventListener("click", e => {
      if (e.target === modal) stop();
    });

    // Try to load the library
    try {
      await loadScannerLib();
    } catch (e) {
      statusEl.textContent = "Scanner library could not load. Type barcode manually below.";
      manualInput.focus();
      return;
    }
    if (!window.Html5Qrcode) {
      statusEl.textContent = "Scanner library not available. Type barcode manually below.";
      manualInput.focus();
      return;
    }

    // Try to start the camera
    try {
      // Pre-flight: detect missing mediaDevices (typical in sandboxed iframes
      // without `allow="camera"`, or insecure contexts). Surface a more
      // useful error than html5-qrcode's generic "streaming not supported".
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        const isSecure = window.isSecureContext;
        const inIframe = window.self !== window.top;
        let detail;
        if (!isSecure) {
          detail = "Camera needs HTTPS. Use your Nabu Casa URL.";
        } else if (inIframe) {
          detail = "Camera blocked in iframe. Open the dashboard directly (not from a panel that frames it).";
        } else {
          detail = "Camera API unavailable in this browser/WebView.";
        }
        statusEl.textContent = detail + " — type the barcode below.";
        manualInput.focus();
        return;
      }
      scanner = new window.Html5Qrcode(readerId, {
        verbose: false
      });
      const formats = this._supportedFormats();
      // Continuous autofocus is critical for hands-free barcode scanning.
      // 'advanced' is silently ignored on browsers that don't support it,
      // so it's safe to always pass.
      const sharedConfig = {
        fps: 15,
        qrbox: {
          width: 260,
          height: 160
        },
        aspectRatio: 1.777,
        formatsToSupport: formats,
        videoConstraints: {
          facingMode: {
            ideal: "environment"
          },
          width: {
            ideal: 1280
          },
          height: {
            ideal: 720
          },
          advanced: [{
            focusMode: "continuous"
          }, {
            focusDistance: 0
          }]
        }
      };
      // First try with facingMode (preferred for mobile rear camera).
      // If that fails, fall back to enumerating devices and picking one.
      try {
        await scanner.start({
          facingMode: "environment"
        }, sharedConfig, async decodedText => {
          if (stopped) return;
          beep();
          await stop();
          this._handleScan(decodedText);
        }, () => {/* ignore per-frame scan failures */});
      } catch (firstErr) {
        // Some WebViews choke on facingMode constraints. Fall back to
        // device enumeration + deviceId.
        console.warn("[home-inventory] facingMode failed, trying deviceId:", firstErr);
        const cams = await window.Html5Qrcode.getCameras();
        if (!cams || cams.length === 0) throw firstErr;
        // Prefer a back/rear/environment camera if labelled as such
        const back = cams.find(c => /back|rear|environment/i.test(c.label || ""));
        const target = (back || cams[cams.length - 1]).id;
        await scanner.start(target, sharedConfig, async decodedText => {
          if (stopped) return;
          beep();
          await stop();
          this._handleScan(decodedText);
        }, () => {/* ignore per-frame scan failures */});
      }
      statusEl.textContent = "Tap to focus";
      // After the camera is running, the dark backdrop on the status overlay
      // should fade so the user can actually see the video. The overlay still
      // captures no clicks (pointer-events: none) so taps reach the reader.
      statusEl.style.background = "transparent";
      statusEl.style.opacity = "0.85";
      statusEl.style.alignItems = "flex-end";
      statusEl.style.justifyContent = "center";
      statusEl.style.paddingBottom = "8px";
      statusEl.style.fontSize = "0.8em";
      // Tap-to-focus: nudge the camera to refocus on the tapped area.
      // Useful on devices that don't honor focusMode: continuous.
      const reader = modal.querySelector(`#${readerId}`);
      if (reader) {
        reader.addEventListener("click", async () => {
          try {
            var _scanner$getRunningTr, _scanner, _scanner$getRunningTr2, _video$srcObject, _video$srcObject$getV;
            const stream = ((_scanner$getRunningTr = (_scanner = scanner).getRunningTrackCameraCapabilities) === null || _scanner$getRunningTr === void 0 || (_scanner$getRunningTr = _scanner$getRunningTr.call(_scanner)) === null || _scanner$getRunningTr === void 0 || (_scanner$getRunningTr2 = _scanner$getRunningTr.getMediaStream) === null || _scanner$getRunningTr2 === void 0 ? void 0 : _scanner$getRunningTr2.call(_scanner$getRunningTr)) || scanner._localMediaStream || null;
            // Best-effort: re-apply the constraint via the underlying track
            const video = reader.querySelector("video");
            const track = video === null || video === void 0 || (_video$srcObject = video.srcObject) === null || _video$srcObject === void 0 || (_video$srcObject$getV = _video$srcObject.getVideoTracks) === null || _video$srcObject$getV === void 0 ? void 0 : _video$srcObject$getV.call(_video$srcObject)[0];
            if (track && track.applyConstraints) {
              await track.applyConstraints({
                advanced: [{
                  focusMode: "single-shot"
                }, {
                  focusMode: "continuous"
                }]
              });
              statusEl.textContent = "Refocusing…";
              setTimeout(() => {
                statusEl.textContent = "Tap to focus";
              }, 700);
            }
          } catch (_) {/* ignore — best effort */}
        });
      }
    } catch (e) {
      console.error("Camera start failed:", e);
      let msg = "Camera unavailable.";
      if (e && (e.name === "NotAllowedError" || /permission/i.test(String(e)))) {
        msg = "Camera permission denied. Allow camera access in your browser settings, then retry — or type the barcode below.";
      } else if (e && /secure context|https/i.test(String(e))) {
        msg = "Camera needs HTTPS. Use your Nabu Casa URL or an HTTPS reverse proxy — or type the barcode below.";
      } else if (e) {
        msg = `Camera error: ${String(e).slice(0, 120)} — type the barcode below.`;
      }
      statusEl.textContent = msg;
      manualInput.focus();
    }
  }
  _supportedFormats() {
    // If the library exposes the enum, prefer 1D formats used on groceries.
    const F = window.Html5QrcodeSupportedFormats;
    if (!F) return undefined;
    return [F.EAN_13, F.EAN_8, F.UPC_A, F.UPC_E, F.CODE_128, F.CODE_39, F.ITF, F.CODABAR, F.QR_CODE].filter(v => v !== undefined);
  }
  _modalStyles() {
    // Styles for the light-DOM modal (scoped via .hi-* class prefix)
    return `
      .hi-modal {
        position: fixed; inset: 0; z-index: 10000;
        background: rgba(0,0,0,0.65);
        display: flex; align-items: center; justify-content: center;
        padding: 16px;
        font-family: var(--primary-font-family, system-ui, sans-serif);
      }
      .hi-dialog {
        background: #fff; color: #222;
        border-radius: 12px;
        width: 100%; max-width: 420px;
        max-height: 95vh; overflow: auto;
        box-shadow: 0 8px 40px rgba(0,0,0,0.4);
      }
      @media (prefers-color-scheme: dark) {
        .hi-dialog { background: #1e1e1e; color: #eee; }
      }
      .hi-mdl-title {
        padding: 14px 18px;
        font-weight: 600; font-size: 1.1em;
        border-bottom: 1px solid rgba(128,128,128,0.25);
      }
      .hi-reader-wrap {
        position: relative;
        background: #000;
        min-height: 180px;
      }
      .hi-reader { width: 100%; }
      .hi-reader-status {
        position: absolute; inset: 0;
        display: flex; align-items: center; justify-content: center;
        text-align: center; padding: 12px;
        color: #fff; font-size: 0.95em;
        pointer-events: none;
        background: rgba(0,0,0,0.35);
      }
      .hi-reader video, .hi-reader canvas {
        width: 100% !important; height: auto !important;
      }
      .hi-mdl-product {
        padding: 14px 16px; text-align: center;
      }
      .hi-prod-img {
        max-width: 120px; max-height: 120px; border-radius: 8px;
      }
      .hi-prod-name { font-size: 1.1em; font-weight: 500; margin-top: 6px; }
      .hi-prod-brand { font-size: 0.9em; opacity: 0.7; }
      .hi-prod-barcode {
        font-family: monospace; font-size: 0.85em; opacity: 0.6; margin-top: 4px;
      }
      .hi-mdl-row, .hi-mdl-actions {
        padding: 10px 16px;
        display: flex; gap: 8px; align-items: center;
        flex-wrap: wrap;
      }
      .hi-mdl-actions {
        justify-content: flex-end;
        border-top: 1px solid rgba(128,128,128,0.25);
      }
      .hi-lbl {
        display: flex; align-items: center; gap: 4px;
        font-size: 0.95em;
      }
      .hi-lbl.stacked { flex-direction: column; align-items: stretch; flex: 1; gap: 2px; }
      .hi-field-label { font-size: 0.8em; color: var(--secondary-text-color); font-weight: 500; }
      .hi-input {
        flex: 1; padding: 8px 12px;
        border: 1px solid rgba(128,128,128,0.35);
        border-radius: 6px;
        background: transparent; color: inherit;
        font-size: 1em;
        min-width: 0;
      }
      .hi-input.qty { flex: 0 0 70px; }
      .hi-btn {
        padding: 8px 14px;
        border-radius: 6px;
        border: 1px solid rgba(128,128,128,0.35);
        background: var(--card-background-color, transparent);
        color: inherit;
        font-size: 1em;
        cursor: pointer;
      }
      .hi-btn.primary {
        background: var(--primary-color, #03a9f4); color: #fff;
        border-color: transparent;
      }
      .hi-btn.danger {
        background: #c33; color: #fff; border-color: transparent;
      }
      .hi-btn.ghost { background: transparent; }

      /* Suggestion picker */
      .hi-suggest {
        padding: 10px 16px;
        display: flex; flex-direction: column; gap: 6px;
        border-top: 1px solid rgba(128,128,128,0.15);
        border-bottom: 1px solid rgba(128,128,128,0.15);
      }
      .hi-suggest-label {
        font-size: 0.85em; opacity: 0.75;
        margin-bottom: 4px;
      }
      .hi-suggest-row {
        display: flex; align-items: center; gap: 8px;
        padding: 6px 8px;
        border: 1px solid rgba(128,128,128,0.2);
        border-radius: 6px;
        cursor: pointer;
      }
      .hi-suggest-row input[type="radio"] { flex: 0 0 auto; }
      .hi-suggest-row span { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
      .hi-suggest-action {
        width: 100%; text-align: left;
        padding: 8px 10px;
      }

      /* Search results (existing products autocomplete) */
      .hi-search-results {
        padding: 0 16px;
        max-height: 220px; overflow-y: auto;
      }
      .hi-search-row {
        padding: 8px 10px;
        border: 1px solid rgba(128,128,128,0.2);
        border-radius: 6px;
        margin-bottom: 4px;
        cursor: pointer;
        display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
      }
      .hi-search-row.selected {
        border-color: var(--primary-color, #03a9f4);
        background: rgba(3, 169, 244, 0.1);
      }
      .hi-search-row.strong {
        border-color: #2e7d32;
        background: rgba(46, 125, 50, 0.07);
      }
      .hi-search-row.strong.selected {
        border-color: #2e7d32;
        background: rgba(46, 125, 50, 0.18);
      }
      .hi-match-clear { padding: 6px 10px; }

      .hi-create-hint {
        padding: 10px 16px;
        margin: 0 16px 8px;
        background: rgba(255, 193, 7, 0.10);
        border: 1px solid rgba(255, 193, 7, 0.4);
        border-radius: 8px;
        font-size: 0.85em;
        line-height: 1.4;
      }
      .hi-create-hint strong {
        color: #b8860b;
      }

      .hi-section-header {
        padding: 10px 16px 4px;
        font-size: 0.78em;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        opacity: 0.7;
      }
      .hi-barcode-list {
        padding: 0 16px;
      }
      .hi-barcode-item {
        display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
        padding: 6px 0;
        border-bottom: 1px solid rgba(128,128,128,0.15);
      }
      .hi-barcode-item:last-child { border-bottom: none; }
      .pill {
        background: rgba(128,128,128,0.15); border-radius: 10px;
        padding: 1px 8px; font-size: 0.78em; display: inline-block;
      }
      .pill.muted { opacity: 0.7; }
      .hi-empty-row {
        padding: 8px 0;
        font-size: 0.85em;
        opacity: 0.7;
      }

      /* Toast */
      .hi-toast {
        position: fixed;
        left: 50%; bottom: 24px;
        transform: translateX(-50%);
        z-index: 10001;
        max-width: 92vw;
      }
      .hi-toast-inner {
        background: #333; color: #fff;
        padding: 10px 14px;
        border-radius: 8px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.3);
        display: flex; gap: 14px; align-items: center;
      }
      .hi-toast-msg { font-size: 0.95em; }
      .hi-toast-btn {
        background: transparent; color: #ffeb3b;
        border: none; cursor: pointer;
        font-weight: 600; font-size: 0.9em;
      }
    `;
  }
  async _handleScan(barcode) {
    // Lookup
    let product;
    try {
      const resp = await this._api(`/lookup/${encodeURIComponent(barcode)}`);
      product = resp.product;
    } catch (e) {
      alert("Lookup failed: " + e.message);
      return;
    }

    // ---- KNOWN PRODUCT ----
    if (product.found !== "unknown") {
      if (this._mode === "stocking") {
        // Stocking-up: minimal dialog (qty + expiration), then re-open scanner
        this._openStockingDialog(barcode, product);
      } else {
        // Default mode: silent deduct + toast (with UNDO)
        this._fastDeduct(barcode, product);
      }
      return;
    }

    // ---- UNKNOWN PRODUCT ----
    // Same suggestion flow in both modes (link / search / create new).
    let suggestions = [];
    try {
      const qs = new URLSearchParams({
        barcode
      });
      const r = await this._api("/suggest?" + qs.toString());
      suggestions = r.suggestions || [];
    } catch (e) {/* ignore */}
    this._openScanDialog(barcode, product, suggestions);
  }

  /* ---------- Default mode: silent deduct ---------- */

  async _fastDeduct(barcode, product) {
    var _resp2;
    const displayName = product.name_he || product.name || `(${barcode})`;
    let resp;
    try {
      resp = await this._callService("home_inventory", "scan_barcode", {
        barcode,
        mode: "deduct",
        quantity: 1
      }, true);
    } catch (e) {
      alert("Error: " + ((e === null || e === void 0 ? void 0 : e.message) || e));
      return;
    }
    const r = (_resp2 = resp) === null || _resp2 === void 0 ? void 0 : _resp2.response;
    this._refresh();

    // CASE A: scanned an item that was already at 0
    if (r !== null && r !== void 0 && r.was_at_zero) {
      this._showToast(`"${displayName}" is at 0 — switch to Stock Up?`, async () => {
        this._setMode("stocking");
      }, 7000, "Stock Up");
      return;
    }

    // CASE B: scan went through; if it just hit zero, prompt to add to shopping
    if (r !== null && r !== void 0 && r.went_to_zero && !(r !== null && r !== void 0 && r.added_to_shopping)) {
      // Use a confirm — keeps it simple; current behaviour preserved per user
      // preference ("ask once, current behavior").
      if (confirm(`"${displayName}" is now depleted. Add to shopping list?`)) {
        const shopPayload = {
          name: displayName,
          product_id: product.id
        };
        if (CATEGORIES.includes(product.category)) {
          shopPayload.category = product.category;
        }
        await this._callService("home_inventory", "add_to_shopping", shopPayload);
        this._refresh();
      }
    }

    // CASE C: normal deduct — toast with undo
    this._showToast(`Used 1 × ${displayName}`, async () => {
      // Undo: re-add 1 of this product (no expiration — just put it back)
      try {
        await this._callService("home_inventory", "scan_barcode", {
          barcode,
          mode: "stock_up",
          quantity: 1
        });
        this._refresh();
      } catch (e) {
        console.error("Undo failed", e);
      }
    }, 6000);
  }

  /* ---------- Stocking-up mode: minimal dialog ---------- */

  _openStockingDialog(barcode, product) {
    const modal = this._makeModal();
    const displayName = product.name_he || product.name || `(${barcode})`;
    const rtl = !!product.name_he;
    modal._dialog.innerHTML = `
      <div class="hi-mdl-title">📦 Stock Up</div>
      <div class="hi-mdl-product">
        ${product.image_url ? `<img src="${this._escape(product.image_url)}" class="hi-prod-img"/>` : ""}
        <div class="hi-prod-name" ${rtl ? 'dir="rtl"' : ""}>${this._escape(displayName)}</div>
        ${product.brand ? `<div class="hi-prod-brand">${this._escape(product.brand)}</div>` : ""}
        <div class="hi-prod-barcode">${this._escape(barcode)}</div>
      </div>
      <div class="hi-mdl-row">
        <label class="hi-lbl">Qty <input class="hi-input qty" id="s-qty" type="number" step="0.1" min="0.1" value="1"/></label>
        <input class="hi-input" id="s-exp" type="date" placeholder="Expiration (optional)"/>
      </div>
      <div class="hi-mdl-actions">
        <button class="hi-btn ghost" id="s-cancel">Cancel</button>
        <button class="hi-btn primary" id="s-add">➕ Add &amp; scan next</button>
      </div>
    `;
    const q = sel => modal._dialog.querySelector(sel);
    const close = () => modal.remove();
    q("#s-cancel").addEventListener("click", close);

    // Auto-focus the date field — most common reason users open this dialog
    setTimeout(() => {
      var _q;
      return (_q = q("#s-exp")) === null || _q === void 0 ? void 0 : _q.focus();
    }, 50);
    q("#s-add").addEventListener("click", async () => {
      const qty = parseFloat(q("#s-qty").value) || 1;
      const exp = q("#s-exp").value || null;
      const data = {
        barcode,
        mode: "stock_up",
        quantity: qty
      };
      if (exp) data.expiration_date = exp;
      try {
        const resp = await this._callService("home_inventory", "scan_barcode", data, true);
        const r = resp === null || resp === void 0 ? void 0 : resp.response;
        close();
        this._refresh();
        // Show fulfillment toast if applicable
        if (r && Array.isArray(r.fulfilled_shopping_ids) && r.fulfilled_shopping_ids.length) {
          this._showToast(`✓ Checked off "${displayName}" from shopping list`, async () => {
            for (const item of this._shopping.filter(s => r.fulfilled_shopping_ids.includes(s.id))) {
              await this._callService("home_inventory", "add_to_shopping", {
                name: item.name,
                product_id: item.product_id,
                quantity: item.quantity || 1,
                category: CATEGORIES.includes(item.category) ? item.category : undefined
              });
            }
            this._refresh();
          });
        }
        // Loop: re-open scanner for the next item
        setTimeout(() => this._openScanner(), 100);
      } catch (e) {
        alert("Error: " + ((e === null || e === void 0 ? void 0 : e.message) || e));
      }
    });
  }
  _openScanDialog(barcode, product, suggestions = []) {
    const modal = this._makeModal();
    const isUnknown = product.found === "unknown";
    const displayName = product.name_he || product.name || `(${barcode})`;
    const rtl = !!product.name_he;

    // For unknown barcode, the dialog has three modes (tracked in local state):
    //   "suggest"  — show suggestions + "search existing" + "create new" options
    //   "search"   — free-text autocomplete of existing products
    //   "create"   — fill in name/category for new product
    // For known products: simple confirm.
    let mode = isUnknown ? "suggest" : "confirm";
    let chosenProductId = null;
    let chosenProductName = null;
    let chosenProductBrand = null;
    const getOptions = () => {
      var _modal$_dialog$queryS, _modal$_dialog$queryS2;
      const qty = parseFloat((_modal$_dialog$queryS = modal._dialog.querySelector("#z-qty")) === null || _modal$_dialog$queryS === void 0 ? void 0 : _modal$_dialog$queryS.value) || 1;
      const exp = ((_modal$_dialog$queryS2 = modal._dialog.querySelector("#z-exp")) === null || _modal$_dialog$queryS2 === void 0 ? void 0 : _modal$_dialog$queryS2.value) || null;
      return {
        qty,
        exp
      };
    };
    const renderBody = async () => {
      // Common header
      let html = `
        <div class="hi-mdl-title">
          ${isUnknown ? "New barcode — choose product" : "Confirm product"}
        </div>
        <div class="hi-mdl-product">
          ${product.image_url ? `<img src="${this._escape(product.image_url)}" class="hi-prod-img"/>` : ""}
          <div class="hi-prod-name" ${rtl ? 'dir="rtl"' : ""}>${this._escape(displayName)}</div>
          ${product.brand ? `<div class="hi-prod-brand">${this._escape(product.brand)}</div>` : ""}
          <div class="hi-prod-barcode">${this._escape(barcode)}</div>
        </div>
      `;
      if (mode === "suggest") {
        html += `
          <div class="hi-suggest">
            ${suggestions.length ? `
              <div class="hi-suggest-label">Similar products — pick one to link this barcode to:</div>
              ${suggestions.map((s, idx) => `
                <label class="hi-suggest-row">
                  <input type="radio" name="z-suggest" value="${s.id}"
                         data-name="${this._escape(s.name_he || s.name)}"
                         data-brand="${this._escape(s.brand || '')}"
                         ${idx === 0 ? 'checked' : ''}/>
                  <span>
                    <strong>${this._escape(s.name_he || s.name)}</strong>
                    ${s.brand ? `<span class="pill muted">${this._escape(s.brand)}</span>` : ""}
                    ${s.category ? `<span class="pill">${CATEGORY_LABELS[s.category] || s.category}</span>` : ""}
                  </span>
                </label>
              `).join("")}
            ` : `
              <div class="hi-suggest-label">No similar products found — choose below:</div>
            `}
            <button class="hi-btn ghost hi-suggest-action" data-mode="search">🔍 Search existing products</button>
            <button class="hi-btn ghost hi-suggest-action" data-mode="create">✨ Create new product</button>
          </div>
        `;
      } else if (mode === "search") {
        html += `
          <div class="hi-mdl-row">
            <input class="hi-input" id="z-searchname" placeholder="Search existing products…" autofocus/>
          </div>
          <div id="z-searchresults" class="hi-search-results"></div>
          <div class="hi-mdl-row">
            <button class="hi-btn ghost" data-act="back-to-suggest">← Back</button>
          </div>
        `;
      } else if (mode === "create") {
        html += `
          <div class="hi-create-hint">
            💡 Tip: use a <strong>generic name</strong> (e.g. "Lactose-free milk 1L"),
            not a brand-specific one. Other brands of the same product can be linked
            to this name on future scans, so any of them fulfills "lactose-free milk"
            on your shopping list.
          </div>
          <div class="hi-mdl-row">
            <input class="hi-input" id="z-newname" placeholder="Generic product name"/>
          </div>
          <div class="hi-mdl-row">
            <input class="hi-input" id="z-newbrand" placeholder="Brand (this barcode)"/>
            <select class="hi-input" id="z-newcat">
              <option value="">Category…</option>
              ${CATEGORIES.map(c => `<option value="${c}">${CATEGORY_LABELS[c]}</option>`).join("")}
            </select>
          </div>
          <div class="hi-mdl-row">
            <button class="hi-btn ghost" data-act="back-to-suggest">← Back</button>
          </div>
        `;
      }

      // Common footer (qty / expiry / actions)
      // Location intentionally NOT shown — user opted out of location tracking
      // for the scan flow (manual Add dialog still has it).
      html += `
        <div class="hi-mdl-row">
          <label class="hi-lbl">Qty <input class="hi-input qty" id="z-qty" type="number" step="0.1" min="0.1" value="1"/></label>
          <input class="hi-input" id="z-exp" type="date" placeholder="Expiration (optional)"/>
        </div>
        <div class="hi-mdl-actions">
          <button class="hi-btn ghost" id="z-cancel">Cancel</button>
          ${isUnknown ? "" : `<button class="hi-btn danger" id="z-remove">➖ Remove</button>`}
          <button class="hi-btn primary" id="z-add">➕ Add</button>
        </div>
      `;
      modal._dialog.innerHTML = html;
      bindBody();
    };
    const bindBody = () => {
      var _q2, _q3, _q4;
      const q = sel => modal._dialog.querySelector(sel);
      const close = () => modal.remove();
      (_q2 = q("#z-cancel")) === null || _q2 === void 0 || _q2.addEventListener("click", close);

      // Mode switcher buttons
      modal._dialog.querySelectorAll(".hi-suggest-action").forEach(b => b.addEventListener("click", () => {
        mode = b.dataset.mode;
        renderBody();
      }));
      modal._dialog.querySelectorAll("[data-act='back-to-suggest']").forEach(b => b.addEventListener("click", () => {
        mode = "suggest";
        renderBody();
      }));

      // Live autocomplete for "search existing" mode
      if (mode === "search") {
        const input = q("#z-searchname");
        const results = q("#z-searchresults");
        let timer;
        input === null || input === void 0 || input.addEventListener("input", () => {
          clearTimeout(timer);
          timer = setTimeout(async () => {
            const v = input.value.trim();
            if (!v) {
              results.innerHTML = "";
              return;
            }
            try {
              const r = await this._api("/products?" + new URLSearchParams({
                q: v,
                limit: "8"
              }));
              results.innerHTML = (r.results || []).map(p => `
                <div class="hi-search-row" data-product-id="${p.id}"
                     data-name="${this._escape(p.name_he || p.name)}"
                     data-brand="${this._escape(p.brand || '')}">
                  <strong>${this._escape(p.name_he || p.name)}</strong>
                  ${p.brand ? ` — ${this._escape(p.brand)}` : ""}
                  ${p.category ? `<span class="pill muted">${p.category}</span>` : ""}
                </div>
              `).join("");
              results.querySelectorAll(".hi-search-row").forEach(row => {
                row.addEventListener("click", () => {
                  chosenProductId = parseInt(row.dataset.productId, 10);
                  chosenProductName = row.dataset.name;
                  chosenProductBrand = row.dataset.brand || null;
                  // Visually mark as selected
                  results.querySelectorAll(".hi-search-row").forEach(r => r.classList.remove("selected"));
                  row.classList.add("selected");
                });
              });
            } catch (e) {/* ignore */}
          }, 200);
        });
      }

      // Add button
      (_q3 = q("#z-add")) === null || _q3 === void 0 || _q3.addEventListener("click", async () => {
        const {
          qty,
          exp
        } = getOptions();
        // Use the card-level mode (this._mode). In stocking mode -> stock_up;
        // otherwise the unknown-barcode path is always an "add" (we don't
        // deduct from stock the moment we're learning about a new product).
        const data = {
          barcode,
          mode: "stock_up",
          quantity: qty
        };
        if (exp) data.expiration_date = exp;
        if (isUnknown) {
          if (mode === "suggest") {
            // Grab the selected radio
            const sel = modal._dialog.querySelector("input[name='z-suggest']:checked");
            if (sel) {
              data.link_to_product_id = parseInt(sel.value, 10);
              chosenProductName = sel.dataset.name;
              chosenProductBrand = sel.dataset.brand || null;
              if (product.brand) data.brand_override = product.brand;
            } else {
              alert("Pick a suggestion, or choose Search/Create below.");
              return;
            }
          } else if (mode === "search") {
            if (!chosenProductId) {
              alert("Select a product from the list.");
              return;
            }
            data.link_to_product_id = chosenProductId;
            if (product.brand) data.brand_override = product.brand;
          } else if (mode === "create") {
            const name = q("#z-newname").value.trim();
            if (!name) {
              alert("Enter a product name.");
              return;
            }
            data.product_name = name;
            const brand = q("#z-newbrand").value.trim();
            if (brand) data.brand_override = brand;
            const cat = q("#z-newcat").value;
            if (cat) data.product_category = cat;
            chosenProductName = name;
            chosenProductBrand = brand || null;
          }
        }
        try {
          const resp = await this._callService("home_inventory", "scan_barcode", data, true);
          const r = resp === null || resp === void 0 ? void 0 : resp.response;
          close();
          // If shopping-list items were auto-fulfilled, show an undo toast
          if (r && Array.isArray(r.fulfilled_shopping_ids) && r.fulfilled_shopping_ids.length) {
            var _r$product, _r$product2;
            const fulfillName = chosenProductName || ((_r$product = r.product) === null || _r$product === void 0 ? void 0 : _r$product.name_he) || ((_r$product2 = r.product) === null || _r$product2 === void 0 ? void 0 : _r$product2.name) || displayName;
            this._showToast(`✓ Checked off "${fulfillName}" from shopping list`, async () => {
              // Undo: re-add the fulfilled items
              for (const item of this._shopping.filter(s => r.fulfilled_shopping_ids.includes(s.id))) {
                await this._callService("home_inventory", "add_to_shopping", {
                  name: item.name,
                  product_id: item.product_id,
                  quantity: item.quantity || 1,
                  category: CATEGORIES.includes(item.category) ? item.category : undefined
                });
              }
              this._refresh();
            });
          }
          this._refresh();
          // In stocking mode, loop back to scanner immediately so the user
          // can keep scanning without tapping "Scan" again.
          if (this._mode === "stocking") {
            setTimeout(() => this._openScanner(), 100);
          }
        } catch (e) {
          alert("Error: " + ((e === null || e === void 0 ? void 0 : e.message) || e));
        }
      });

      // Remove button (only meaningful for known products)
      (_q4 = q("#z-remove")) === null || _q4 === void 0 || _q4.addEventListener("click", async () => {
        const { qty } = getOptions();
        if (isUnknown) {
          alert("Can't remove an unknown product. Add it first.");
          return;
        }
        const data = {
          barcode,
          action: "remove",
          quantity: qty
        };
        try {
          const resp = await this._callService("home_inventory", "scan_barcode", data, true);
          const r = resp === null || resp === void 0 ? void 0 : resp.response;
          if (r && r.remaining <= 0 && !r.added_to_shopping) {
            if (confirm(`"${displayName}" is now depleted. Add to shopping list?`)) {
              const shopPayload = {
                name: displayName,
                product_id: product.id
              };
              if (CATEGORIES.includes(product.category)) {
                shopPayload.category = product.category;
              }
              await this._callService("home_inventory", "add_to_shopping", shopPayload);
            }
          }
          close();
          this._refresh();
        } catch (e) {
          alert("Error: " + ((e === null || e === void 0 ? void 0 : e.message) || e));
        }
      });
    };
    renderBody();
  }

  /* ---------- Undo toast ---------- */

  _showToast(message, actionFn, timeoutMs = 6000, btnLabel = "UNDO") {
    const toast = document.createElement("div");
    toast.className = "hi-toast";
    const showBtn = !!actionFn;
    toast.innerHTML = `
      <style>${this._modalStyles()}</style>
      <div class="hi-toast-inner">
        <span class="hi-toast-msg">${this._escape(message)}</span>
        ${showBtn ? `<button class="hi-toast-btn">${this._escape(btnLabel)}</button>` : ""}
      </div>
    `;
    document.body.appendChild(toast);
    const close = () => {
      try {
        toast.remove();
      } catch (e) {}
    };
    const t = setTimeout(close, timeoutMs);
    if (showBtn) {
      toast.querySelector(".hi-toast-btn").addEventListener("click", async () => {
        clearTimeout(t);
        close();
        try {
          await actionFn();
        } catch (e) {
          console.error("Toast action failed", e);
        }
      });
    }
  }

  /* ---------- Product detail / edit dialog ---------- */

  async _openProductDialog(productId) {
    const modal = this._makeModal();
    // Loading state while we fetch
    modal._dialog.innerHTML = `
      <div class="hi-mdl-title">Edit product</div>
      <div class="hi-mdl-row"><span style="opacity:0.7;">Loading…</span></div>
    `;
    let detail;
    try {
      detail = await this._api(`/product/${productId}`);
    } catch (e) {
      var _modal$_dialog$queryS3;
      modal._dialog.innerHTML = `
        <div class="hi-mdl-title">Edit product</div>
        <div class="hi-mdl-row" style="color:#c33;">Error loading product: ${this._escape(String((e === null || e === void 0 ? void 0 : e.message) || e))}</div>
        <div class="hi-mdl-actions"><button class="hi-btn ghost" id="p-cancel">Close</button></div>
      `;
      (_modal$_dialog$queryS3 = modal._dialog.querySelector("#p-cancel")) === null || _modal$_dialog$queryS3 === void 0 || _modal$_dialog$queryS3.addEventListener("click", () => modal.remove());
      return;
    }
    const product = detail.product;
    const barcodes = detail.barcodes || [];
    modal._dialog.innerHTML = `
      <div class="hi-mdl-title">Edit product</div>
      <div class="hi-mdl-row">
        <label class="hi-lbl stacked">
          <span class="hi-field-label">Product name</span>
          <input class="hi-input" id="p-name" placeholder="e.g. Lactose-free milk 1L"
                 value="${this._escape(product.name || "")}"/>
        </label>
      </div>
      <div class="hi-mdl-row">
        <label class="hi-lbl stacked">
          <span class="hi-field-label">Hebrew name</span>
          <input class="hi-input" id="p-name-he" placeholder="Optional" dir="rtl"
                 value="${this._escape(product.name_he || "")}"/>
        </label>
      </div>
      <div class="hi-mdl-row">
        <label class="hi-lbl stacked">
          <span class="hi-field-label">Category</span>
          <select class="hi-input" id="p-cat">
            <option value="">Select…</option>
            ${CATEGORIES.map(c => `<option value="${c}" ${product.category === c ? "selected" : ""}>${CATEGORY_LABELS[c]}</option>`).join("")}
          </select>
        </label>
      </div>
      <div class="hi-section-header">Linked barcodes (${barcodes.length})</div>
      <div class="hi-barcode-list">
        ${barcodes.length === 0 ? `
          <div class="hi-empty-row">No barcodes linked yet — they'll be added automatically as you scan.</div>
        ` : barcodes.map(b => `
          <div class="hi-barcode-item">
            <code class="barcode">${this._escape(b.barcode)}</code>
            ${b.brand ? `<span class="pill">${this._escape(b.brand)}</span>` : ""}
            <button class="mini danger" data-act="p-unlink" data-barcode="${this._escape(b.barcode)}" title="Unlink barcode"><svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle"><path d="M18.84 12.25l1.72-1.71h-.02a5.004 5.004 0 0 0-.12-7.07 5.006 5.006 0 0 0-6.95 0l-1.72 1.71"/><path d="M5.17 11.75l-1.71 1.71a5.004 5.004 0 0 0 .12 7.07 5.006 5.006 0 0 0 6.95 0l1.71-1.71"/><line x1="8" y1="2" x2="8" y2="5"/><line x1="2" y1="8" x2="5" y2="8"/><line x1="16" y1="19" x2="16" y2="22"/><line x1="19" y1="16" x2="22" y2="16"/></svg></button>
          </div>
        `).join("")}
      </div>
      <div class="hi-section-header">Advanced</div>
      <div class="hi-mdl-row">
        <button class="hi-btn ghost" id="p-merge">🔗 Merge into another product…</button>
      </div>
      <div class="hi-mdl-actions">
        <button class="hi-btn ghost" id="p-cancel">Cancel</button>
        <button class="hi-btn primary" id="p-save">Save</button>
      </div>
    `;
    const q = s => modal._dialog.querySelector(s);
    const close = () => modal.remove();
    q("#p-cancel").addEventListener("click", close);
    q("#p-save").addEventListener("click", async () => {
      const data = {
        product_id: productId,
        name: q("#p-name").value.trim(),
        name_he: q("#p-name-he").value.trim(),
        category: q("#p-cat").value || ""
      };
      if (!data.name) {
        alert("Name is required.");
        return;
      }
      try {
        await this._callService("home_inventory", "rename_product", data);
        close();
        this._refresh();
      } catch (e) {
        alert("Save failed: " + ((e === null || e === void 0 ? void 0 : e.message) || e));
      }
    });
    modal._dialog.querySelectorAll("[data-act='p-unlink']").forEach(b => b.addEventListener("click", async () => {
      const bc = b.dataset.barcode;
      if (!confirm(`Unlink barcode ${bc} from this product?`)) return;
      try {
        await this._callService("home_inventory", "unlink_barcode", {
          barcode: bc
        });
        // Re-open dialog to show updated list
        close();
        this._openProductDialog(productId);
      } catch (e) {
        alert("Unlink failed: " + ((e === null || e === void 0 ? void 0 : e.message) || e));
      }
    }));
    q("#p-merge").addEventListener("click", () => this._openMergeDialog(productId, product));
  }
  async _openMergeDialog(srcId, srcProduct) {
    const modal = this._makeModal();
    modal._dialog.innerHTML = `
      <div class="hi-mdl-title">Merge into another product</div>
      <div class="hi-mdl-product">
        <div class="hi-prod-name">${this._escape(srcProduct.name_he || srcProduct.name)}</div>
        <div style="font-size:0.85em; opacity:0.7; margin-top:4px;">
          will be merged into the product you pick below.<br/>
          All barcodes, inventory and shopping items will move there.<br/>
          <strong>This product will be deleted.</strong>
        </div>
      </div>
      <div class="hi-mdl-row">
        <input class="hi-input" id="m-search" placeholder="Search destination product…" autofocus/>
      </div>
      <div id="m-results" class="hi-search-results"></div>
      <div class="hi-mdl-actions">
        <button class="hi-btn ghost" id="m-cancel">Cancel</button>
        <button class="hi-btn danger" id="m-merge" disabled>Merge</button>
      </div>
    `;
    const q = s => modal._dialog.querySelector(s);
    const close = () => modal.remove();
    q("#m-cancel").addEventListener("click", close);
    let chosen = null;
    const mergeBtn = q("#m-merge");
    const results = q("#m-results");
    let timer = null;
    q("#m-search").addEventListener("input", e => {
      clearTimeout(timer);
      const v = e.target.value.trim();
      if (!v) {
        results.innerHTML = "";
        return;
      }
      timer = setTimeout(async () => {
        try {
          const r = await this._api("/products?" + new URLSearchParams({
            q: v,
            limit: "10"
          }));
          const filtered = (r.results || []).filter(p => p.id !== srcId);
          results.innerHTML = filtered.map(p => `
            <div class="hi-search-row" data-product-id="${p.id}"
                 data-name="${this._escape(p.name_he || p.name)}">
              <strong>${this._escape(p.name_he || p.name)}</strong>
              ${p.brand ? ` — ${this._escape(p.brand)}` : ""}
              ${p.category ? `<span class="pill muted">${this._escape(p.category)}</span>` : ""}
            </div>
          `).join("");
          results.querySelectorAll(".hi-search-row").forEach(row => row.addEventListener("click", () => {
            chosen = parseInt(row.dataset.productId, 10);
            results.querySelectorAll(".hi-search-row").forEach(r => r.classList.remove("selected"));
            row.classList.add("selected");
            mergeBtn.disabled = false;
          }));
        } catch (e) {/* ignore */}
      }, 200);
    });
    mergeBtn.addEventListener("click", async () => {
      var _results$querySelecto;
      if (chosen === null) return;
      const destName = (_results$querySelecto = results.querySelector(".hi-search-row.selected")) === null || _results$querySelecto === void 0 || (_results$querySelecto = _results$querySelecto.dataset) === null || _results$querySelecto === void 0 ? void 0 : _results$querySelecto.name;
      if (!confirm(`Merge "${srcProduct.name_he || srcProduct.name}" into "${destName}"?\nThis cannot be undone.`)) return;
      try {
        await this._callService("home_inventory", "merge_products", {
          src_product_id: srcId,
          dest_product_id: chosen
        });
        close();
        this._refresh();
      } catch (e) {
        alert("Merge failed: " + ((e === null || e === void 0 ? void 0 : e.message) || e));
      }
    });
  }

  /* ---------- Free text add dialog ---------- */

  async _openAddDialog(prefillName) {
    const modal = this._makeModal();
    modal._dialog.innerHTML = `
      <div class="hi-mdl-title">Add item</div>
      <div class="hi-mdl-row">
        <label class="hi-lbl stacked">
          <span class="hi-field-label">Item name</span>
          <input class="hi-input" id="a-name" placeholder="e.g. Milk" value="${this._escape(prefillName)}"/>
        </label>
      </div>
      <div id="a-matches" class="hi-search-results" style="display:none;"></div>
      <div class="hi-mdl-row">
        <label class="hi-lbl">Quantity <input class="hi-input qty" id="a-qty" type="number" step="0.1" min="0.1" value="1"/></label>
        <label class="hi-lbl stacked">
          <span class="hi-field-label">Category</span>
          <select class="hi-input" id="a-cat">
            <option value="">Select…</option>
            ${CATEGORIES.map(c => `<option value="${c}">${CATEGORY_LABELS[c]}</option>`).join("")}
          </select>
        </label>
      </div>
      <div class="hi-mdl-row">
        <label class="hi-lbl stacked">
          <span class="hi-field-label">Expiration date</span>
          <input class="hi-input" id="a-exp" type="date"/>
        </label>
      </div>
      <div class="hi-mdl-actions">
        <button class="hi-btn ghost" id="a-cancel">Cancel</button>
        <button class="hi-btn" id="a-shop">🛒 To shopping list</button>
        <button class="hi-btn primary" id="a-inv">📦 To inventory</button>
      </div>
    `;
    const q = sel => modal._dialog.querySelector(sel);
    const close = () => modal.remove();
    q("#a-cancel").addEventListener("click", close);

    // Tracks the currently-picked match (if any). When set, submission
    // links the entry to that product so any matching barcode fulfills it.
    let chosenProductId = null;
    const matchesEl = q("#a-matches");
    const nameInput = q("#a-name");
    const renderMatches = matches => {
      if (!matches || !matches.length) {
        matchesEl.style.display = "none";
        matchesEl.innerHTML = "";
        return;
      }
      matchesEl.style.display = "block";
      matchesEl.innerHTML = matches.map(m => `
        <div class="hi-search-row ${m.strong ? 'strong' : ''} ${chosenProductId === m.id ? 'selected' : ''}"
             data-product-id="${m.id}">
          ${m.strong ? '<span class="pill multi">match</span>' : ''}
          <strong>${this._escape(m.name_he || m.name)}</strong>
          ${m.brand ? ` — <em>${this._escape(m.brand)}</em>` : ""}
          ${m.category ? `<span class="pill muted">${this._escape(m.category)}</span>` : ""}
        </div>
      `).join("") + (chosenProductId !== null ? `
        <div class="hi-match-clear">
          <button class="hi-btn ghost" id="a-match-clear">Clear selection (add as new)</button>
        </div>
      ` : "");
      matchesEl.querySelectorAll(".hi-search-row").forEach(row => row.addEventListener("click", () => {
        const pid = parseInt(row.dataset.productId, 10);
        chosenProductId = chosenProductId === pid ? null : pid;
        // Re-render to update selection highlight
        renderMatches(matches);
      }));
      const clearBtn = matchesEl.querySelector("#a-match-clear");
      if (clearBtn) {
        clearBtn.addEventListener("click", () => {
          chosenProductId = null;
          renderMatches(matches);
        });
      }
    };
    const fetchMatches = async text => {
      if (!text || text.length < 2) {
        renderMatches(null);
        return;
      }
      try {
        const r = await this._api("/match?" + new URLSearchParams({
          name: text
        }));
        const matches = r.matches || [];
        // Auto-select the strongest match (if any) so submission links to it
        // by default. User can clear it via the "Clear selection" button.
        const strongest = matches.find(m => m.strong);
        if (strongest && chosenProductId === null) {
          chosenProductId = strongest.id;
        }
        renderMatches(matches);
      } catch (e) {/* ignore */}
    };

    // Debounced live autocomplete as the user types
    let timer = null;
    nameInput.addEventListener("input", () => {
      // Typing breaks any prior auto-selection
      chosenProductId = null;
      clearTimeout(timer);
      timer = setTimeout(() => fetchMatches(nameInput.value.trim()), 200);
    });
    // Run once for the prefilled name
    if (prefillName) fetchMatches(prefillName.trim());
    const gather = target => ({
      name: q("#a-name").value.trim(),
      quantity: parseFloat(q("#a-qty").value) || 1,
      category: q("#a-cat").value || null,
      expiration_date: q("#a-exp").value || null,
      target
    });
    const submit = async target => {
      const d = gather(target);
      if (!d.name) {
        alert("Name required");
        return;
      }
      const data = {
        name: d.name,
        quantity: d.quantity,
        target
      };
      if (chosenProductId !== null) {
        data.link_to_product_id = chosenProductId;
      }
      if (d.category) data.category = d.category;
      if (target === "inventory") {
        if (d.expiration_date) data.expiration_date = d.expiration_date;
      }
      await this._callService("home_inventory", "add_item", data);
      close();
      this._refresh();
    };
    q("#a-inv").addEventListener("click", () => submit("inventory"));
    q("#a-shop").addEventListener("click", () => submit("shopping"));
  }

  /* ---------- Add-to-shopping dialog ---------- */

  _openAddToShoppingDialog(productId, productName, productCategory) {
    const modal = this._makeModal();
    modal._dialog.innerHTML = `
      <div class="hi-mdl-title">Add to shopping list</div>
      <div class="hi-mdl-product">
        <div class="hi-prod-name">${this._escape(productName)}</div>
      </div>
      <div class="hi-mdl-row">
        <label class="hi-lbl">Qty <input class="hi-input qty" id="shop-qty" type="number" step="0.1" min="0.1" value="1"/></label>
      </div>
      <div class="hi-mdl-actions">
        <button class="hi-btn ghost" id="shop-cancel">Cancel</button>
        <button class="hi-btn primary" id="shop-add">🛒 Add to list</button>
      </div>
    `;
    const q = s => modal._dialog.querySelector(s);
    const close = () => modal.remove();
    q("#shop-cancel").addEventListener("click", close);
    setTimeout(() => { var _q = q("#shop-qty"); if (_q) { _q.select(); } }, 50);
    q("#shop-add").addEventListener("click", async () => {
      const qty = parseFloat(q("#shop-qty").value) || 1;
      const payload = { name: productName, product_id: productId, quantity: qty };
      if (productCategory && CATEGORIES.includes(productCategory)) {
        payload.category = productCategory;
      }
      try {
        await this._callService("home_inventory", "add_to_shopping", payload);
        close();
        this._showToast(`✓ Added ${this._fmtQty(qty)} × ${productName} to shopping list`, null, 3000);
        this._refresh();
      } catch (err) {
        alert("Error: " + ((err === null || err === void 0 ? void 0 : err.message) || err));
      }
    });
  }

  /* ---------- Helpers ---------- */

  _makeModal() {
    // Light-DOM modal so nested widgets (scanner, native inputs) work reliably.
    const m = document.createElement("div");
    m.className = "hi-modal";
    m.innerHTML = `<style>${this._modalStyles()}</style><div class="hi-dialog"></div>`;
    // Callers append content into the .hi-dialog; the outer .hi-modal is the overlay.
    m._dialog = m.querySelector(".hi-dialog");
    // Click outside the dialog closes
    m.addEventListener("click", e => {
      if (e.target === m) m.remove();
    });
    document.body.appendChild(m);
    return m;
  }
  _fmtQty(q) {
    if (q == null) return "";
    const n = Number(q);
    return Number.isInteger(n) ? String(n) : n.toFixed(1);
  }
  _escape(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[c]);
  }
  _styles() {
    return `
      :host { display: block; }
      ha-card { padding: 12px; display: block; transition: box-shadow 0.2s; }
      ha-card.stocking-active {
        box-shadow: inset 0 0 0 2px #ff8c00;
      }
      .header {
        display: flex; justify-content: space-between; align-items: center;
        margin-bottom: 8px;
      }
      .title { font-size: 1.2em; font-weight: 600; }
      .header-actions {
        display: flex; gap: 6px; align-items: center;
      }
      .scan-btn {
        background: var(--primary-color);
        color: var(--text-primary-color, #fff);
        border: none; border-radius: 18px;
        padding: 8px 14px; font-size: 0.95em; cursor: pointer;
      }
      .scan-btn.stock-mode {
        background: #ff8c00; /* warm orange so it's visually distinct */
      }
      .mode-btn {
        background: var(--card-background-color);
        color: var(--primary-text-color);
        border: 1px solid var(--divider-color);
        border-radius: 18px;
        padding: 6px 10px; font-size: 0.9em; cursor: pointer;
        min-width: 36px;
      }
      .mode-btn.on {
        background: #ff8c00; color: #fff; border-color: transparent;
      }
      .tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--divider-color); margin-bottom: 8px; }
      .tab {
        flex: 1; background: none; border: none; padding: 8px 4px;
        cursor: pointer; font-size: 0.95em; color: var(--secondary-text-color);
        border-bottom: 2px solid transparent;
      }
      .tab.active { color: var(--primary-color); border-bottom-color: var(--primary-color); }
      .badge {
        background: var(--primary-color); color: #fff;
        border-radius: 10px; font-size: 0.75em; padding: 1px 6px; margin-left: 4px;
      }
      .filter-row { display: flex; gap: 6px; margin-bottom: 8px; }
      .filter-row input {
        flex: 1; padding: 6px 10px; border-radius: 16px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color);
      }
      .filter-row button {
        width: 36px; border-radius: 50%; border: 1px solid var(--divider-color);
        background: var(--card-background-color); font-size: 1.3em; cursor: pointer;
      }
      .group-header {
        font-size: 0.85em; color: var(--secondary-text-color);
        text-transform: uppercase; margin: 10px 0 4px; letter-spacing: 0.5px;
      }
      .row {
        display: flex; align-items: center; gap: 10px;
        padding: 8px 4px; border-bottom: 1px solid var(--divider-color);
      }
      .row:last-child { border-bottom: none; }
      .row.shop .chk { width: 22px; height: 22px; }
      .row-main { flex: 1; min-width: 0; }
      .row-title { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .row-sub { font-size: 0.85em; color: var(--secondary-text-color); display: flex; gap: 6px; flex-wrap: wrap; margin-top: 2px; }
      .qty { font-variant-numeric: tabular-nums; }
      .pill {
        background: var(--divider-color); border-radius: 10px;
        padding: 1px 8px; font-size: 0.78em;
      }
      .pill.muted { opacity: 0.7; }
      .pill.exp.warn { background: #fdd; color: #a33; }
      .pill.exp.expired { background: #f88; color: #fff; }
      .pill.auto { background: #ffd; color: #963; }
      .row-actions { display: flex; gap: 4px; }
      .mini {
        min-width: 32px; height: 32px; border-radius: 50%;
        border: 1px solid var(--divider-color);
        background: var(--card-background-color);
        font-size: 1.1em; cursor: pointer;
      }
      .mini.danger { color: #c33; border-color: #c33; }
      .mini.ghost { opacity: 0.7; }
      .mini.ghost:hover { opacity: 1; }
      .mini.cart { color: var(--primary-color); font-size: 0.9em; }
      .exp-picker {
        border: 1px solid var(--divider-color);
        border-radius: 4px;
        padding: 2px 4px;
        font-size: 0.8em;
        color: var(--primary-text-color);
        background: var(--card-background-color);
        flex: 1; min-width: 120px; max-width: 160px;
      }
      .empty { text-align: center; padding: 24px; color: var(--secondary-text-color); }
      .pill.multi {
        background: rgba(3,169,244,0.15);
        color: var(--primary-color);
      }
      .row.expandable .row-main { cursor: pointer; }
      .expand-caret { font-size: 0.8em; margin-left: 4px; opacity: 0.6; }
      .expanded {
        padding: 6px 12px 10px 56px;
        background: rgba(128,128,128,0.05);
        border-bottom: 1px solid var(--divider-color);
      }
      .expanded-title {
        font-size: 0.8em;
        color: var(--secondary-text-color);
        margin-bottom: 4px;
      }
      .expanded-row {
        display: flex; align-items: center; gap: 6px;
        padding: 4px 0; font-size: 0.9em;
        flex-wrap: wrap;
      }
      .barcode {
        font-family: monospace;
        font-size: 0.9em;
        background: rgba(128,128,128,0.15);
        padding: 1px 6px;
        border-radius: 4px;
      }
    `;
  }
}

// Defensive registration: if something is already registered under this name
// (e.g. a cached old version), don't throw — log and continue.
if (!customElements.get("home-inventory-card")) {
  try {
    customElements.define("home-inventory-card", HomeInventoryCard);
    console.info("[home-inventory-card] Custom element registered successfully.");
    // Tell Lovelace to rebuild any "Configuration error" stubs that rendered
    // before this script finished loading (can happen in the Companion
    // app's WebView, which sometimes renders the dashboard before
    // add_extra_js_url scripts finish loading).
    queueMicrotask(() => {
      try {
        window.dispatchEvent(new Event("ll-rebuild"));
      } catch (e) {/* ignore */}
    });
  } catch (err) {
    console.error("[home-inventory-card] Failed to register custom element:", err);
  }
} else {
  console.info("[home-inventory-card] Custom element already registered; skipping.");
}
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === "home-inventory-card")) {
  window.customCards.push({
    type: "home-inventory-card",
    name: "Home Inventory",
    description: "Barcode-scanning inventory & shopping list"
  });
}
console.info("%c Home Inventory Card %c 0.4.11 ", "background: #0a8; color: white; padding: 2px 6px; border-radius: 3px 0 0 3px;", "background: #555; color: white; padding: 2px 6px; border-radius: 0 3px 3px 0;");