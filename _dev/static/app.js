const LOW_END_MODE = detectLowEndMode();
const SEARCH_RESULT_LIMIT = LOW_END_MODE ? 18 : 28;
const SEARCH_DEBOUNCE_MS = LOW_END_MODE ? 260 : 150;
const SUGGESTION_DEBOUNCE_MS = LOW_END_MODE ? 240 : 130;
const MINI_MAP_ZOOM = 18;
const MAP_BOUNDS_FALLBACK = {
  min_lon: -4.452,
  min_lat: 48.018,
  max_lon: -4.23,
  max_lat: 48.133,
};

const state = {
  overview: null,
  stock: null,
  searchResults: [],
  selectedResident: null,
  selectedAddress: null,
  householdResidents: [],
  residentHistoryCache: new Map(),
  addressResidentsCache: new Map(),
  searchController: null,
  searchTimer: null,
  suggestionTimers: new Map(),
  suggestionControllers: new Map(),
  residentDetailVersion: 0,
  householdVersion: 0,
  map: null,
  mapBoundsFrame: null,
  mapMarker: null,
};

const elements = {
  residentCountMetric: document.getElementById("resident-count-metric"),
  addressCountMetric: document.getElementById("address-count-metric"),
  streetCountMetric: document.getElementById("street-count-metric"),
  importedCountMetric: document.getElementById("imported-count-metric"),
  searchForm: document.getElementById("search-form"),
  resetSearch: document.getElementById("reset-search"),
  searchLastName: document.getElementById("search-last-name"),
  searchFirstName: document.getElementById("search-first-name"),
  searchAddress: document.getElementById("search-address"),
  lastNameMenu: document.getElementById("last-name-menu"),
  firstNameMenu: document.getElementById("first-name-menu"),
  addressMenu: document.getElementById("address-menu"),
  detailStack: document.getElementById("detail-stack"),
  resultsView: document.getElementById("results-view"),
  resultsTitle: document.getElementById("results-title"),
  resultsCount: document.getElementById("results-count"),
  resultsCaption: document.getElementById("results-caption"),
  residentResults: document.getElementById("resident-results"),
  residentDetail: document.getElementById("resident-detail"),
  supportPanel: document.getElementById("support-panel"),
  householdCountChip: document.getElementById("household-count-chip"),
  addressContext: document.getElementById("address-context"),
  householdList: document.getElementById("household-list"),
  mapShell: document.getElementById("map-shell"),
  stockForm: document.getElementById("stock-form"),
  stockBlackCount: document.getElementById("stock-black-count"),
  stockYellowCount: document.getElementById("stock-yellow-count"),
  stockHealthChip: document.getElementById("stock-health-chip"),
  stockStatus: document.getElementById("stock-status"),
};

const FIELD_CONFIGS = {
  name: {
    input: elements.searchLastName,
    menu: elements.lastNameMenu,
    emptyMessage: "",
  },
  first_name: {
    input: elements.searchFirstName,
    menu: elements.firstNameMenu,
    emptyMessage: "",
  },
  address: {
    input: elements.searchAddress,
    menu: elements.addressMenu,
    emptyMessage: "",
  },
};

function detectLowEndMode() {
  const connection =
    navigator.connection || navigator.mozConnection || navigator.webkitConnection || null;
  const deviceMemory = Number(navigator.deviceMemory || 0);
  const hardwareConcurrency = Number(navigator.hardwareConcurrency || 0);
  return Boolean(
    connection?.saveData ||
      (deviceMemory && deviceMemory <= 4) ||
      (hardwareConcurrency && hardwareConcurrency <= 4)
  );
}

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function normalizeSuggestionText(value = "") {
  return String(value)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[-'’]+/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function normalizeSuggestionAddressText(value = "") {
  return normalizeSuggestionText(value)
    .replace(/\b29100\b/g, " ")
    .replace(/\bdouarnenez\b/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeSuggestionQuery(fieldName, value = "") {
  return fieldName === "address"
    ? normalizeSuggestionAddressText(value)
    : normalizeSuggestionText(value);
}

function normalizeExactSuggestionText(value = "") {
  return String(value)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[-'\u2019]+/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function normalizeExactSuggestionAddressText(value = "") {
  return normalizeExactSuggestionText(value)
    .replace(/\b29100\b/g, " ")
    .replace(/\bdouarnenez\b/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeExactSuggestionQuery(fieldName, value = "") {
  return fieldName === "address"
    ? normalizeExactSuggestionAddressText(value)
    : normalizeExactSuggestionText(value);
}

function formatDate(value) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(`${value}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleDateString("fr-FR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}

function formatLongDate(value) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(`${value}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  const formatted = parsed.toLocaleDateString("fr-FR", {
    day: "numeric",
    month: "long",
    year: "numeric",
  });
  return formatted.charAt(0).toUpperCase() + formatted.slice(1);
}

function renderAdaptiveDateMarkup(value) {
  const shortDate = escapeHtml(formatDate(value));
  const longDate = escapeHtml(formatLongDate(value));
  return `
    <span class="resident-kpi-date-short">${shortDate}</span>
    <span class="resident-kpi-date-long">${longDate}</span>
  `;
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("fr-FR");
}

function formatSignedNumber(value) {
  return Number(value || 0).toLocaleString("fr-FR");
}

function getMapBounds() {
  return state.overview?.map_bounds || state.overview?.city_bounds || MAP_BOUNDS_FALLBACK;
}

function getStockHealth(stock = state.stock) {
  if (!stock) {
    return { label: "Chargement", alert: false };
  }

  const black = Number(stock.black_bags_in_stock || 0);
  const yellow = Number(stock.yellow_bags_in_stock || 0);
  if (black < 0 || yellow < 0) {
    return { label: "Deficit", alert: true };
  }
  if (black === 0 || yellow === 0) {
    return { label: "A reappro.", alert: true };
  }
  return { label: "En stock", alert: false };
}

function renderStock() {
  const stock = state.stock;
  const black = Number(stock?.black_bags_in_stock || 0);
  const yellow = Number(stock?.yellow_bags_in_stock || 0);
  const health = getStockHealth(stock);

  setElementText(elements.stockBlackCount, formatSignedNumber(black));
  setElementText(elements.stockYellowCount, formatSignedNumber(yellow));
  setElementText(elements.stockHealthChip, health.label);
  elements.stockHealthChip.classList.toggle("is-alert", health.alert);
  elements.stockBlackCount.classList.toggle("is-negative", black < 0);
  elements.stockYellowCount.classList.toggle("is-negative", yellow < 0);
}

function todayDateInputValue() {
  const today = new Date();
  today.setMinutes(today.getMinutes() - today.getTimezoneOffset());
  return today.toISOString().slice(0, 10);
}

function latestReceiptLabel(resident) {
  if (!resident?.last_distribution_date) {
    return "-";
  }
  return `${formatDate(resident.last_distribution_date)} - ${resident.last_distribution_black_bags || 0} noir(s) / ${resident.last_distribution_yellow_bags || 0} jaune(s)`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Erreur reseau");
  }
  return payload;
}

function setElementText(element, value) {
  if (element) {
    element.textContent = value;
  }
}

function updateSupportVisibility() {
  const isVisible = Boolean(state.selectedResident);
  elements.detailStack.classList.toggle("is-support-hidden", !isVisible);
  elements.supportPanel.classList.toggle("is-hidden", !isVisible);
}

function showResultsView() {
  elements.resultsView.classList.remove("is-hidden");
  elements.residentDetail.classList.add("is-hidden");
  updateSupportVisibility();
}

function showResidentDetailView() {
  elements.resultsView.classList.add("is-hidden");
  elements.residentDetail.classList.remove("is-hidden");
  updateSupportVisibility();
}

function getSearchValues() {
  return {
    name: elements.searchLastName.value.trim(),
    first_name: elements.searchFirstName.value.trim(),
    address: elements.searchAddress.value.trim(),
  };
}

function hasActiveFilters(values = getSearchValues()) {
  return Boolean(values.name || values.first_name || values.address);
}

function buildResidentsSearchUrl(limit = SEARCH_RESULT_LIMIT) {
  const values = getSearchValues();
  const params = new URLSearchParams();

  if (values.name) {
    params.set("name", values.name);
  }
  if (values.first_name) {
    params.set("first_name", values.first_name);
  }
  if (values.address) {
    params.set("address", values.address);
  }
  params.set("limit", String(limit));

  return `/api/residents?${params.toString()}`;
}

function isAddressSelectionStillVisible() {
  if (!state.selectedAddress) {
    return false;
  }

  const query = elements.searchAddress.value.trim().toLowerCase();
  if (!query) {
    return Boolean(state.selectedResident);
  }

  const candidates = [
    state.selectedAddress.short_address,
    state.selectedAddress.full_address,
    state.selectedAddress.address_line,
  ]
    .filter(Boolean)
    .map((value) => String(value).trim().toLowerCase());

  return candidates.includes(query);
}

function updateOverview(overview) {
  state.overview = overview;
  setElementText(elements.residentCountMetric, formatNumber(overview.resident_count));
  setElementText(elements.addressCountMetric, formatNumber(overview.address_count));
  setElementText(elements.streetCountMetric, formatNumber(overview.street_count));
  setElementText(elements.importedCountMetric, formatNumber(overview.imported_address_count));
}

async function loadStock() {
  const stock = await fetchJson("/api/stock");
  state.stock = stock;
  renderStock();
}

function updateResultsHeader(results, values = getSearchValues(), { loading = false } = {}) {
  const count = results.length;
  const active = hasActiveFilters(values);

  setElementText(elements.resultsCount, formatNumber(count));

  if (loading) {
    setElementText(elements.resultsTitle, "Resultats");
    setElementText(elements.resultsCaption, "");
    return;
  }

  if (!active) {
    setElementText(elements.resultsTitle, "Resultats");
    setElementText(elements.resultsCaption, "");
    return;
  }

  if (count === 0) {
    setElementText(elements.resultsTitle, "Aucun resultat");
    setElementText(elements.resultsCaption, "");
    return;
  }

  if (count === 1) {
    setElementText(elements.resultsTitle, "1 resultat");
    setElementText(elements.resultsCaption, "");
    return;
  }

  setElementText(elements.resultsTitle, "Resultats");
  setElementText(elements.resultsCaption, "");
}

function renderResults() {
  const active = hasActiveFilters();

  if (!active) {
    elements.residentResults.innerHTML = "";
    return;
  }

  if (!state.searchResults.length) {
    elements.residentResults.innerHTML = `
      <div class="empty-state">Aucun resultat.</div>
    `;
    return;
  }

  elements.residentResults.innerHTML = state.searchResults
    .map((resident) => {
      const isActive = state.selectedResident?.id === resident.id;
      return `
        <button type="button" class="result-card${isActive ? " is-active" : ""}" data-resident-id="${resident.id}">
          <div class="result-card-top">
            <div class="result-card-name">
              <strong>${escapeHtml(`${resident.first_name} ${resident.last_name}`)}</strong>
              <span>${escapeHtml(resident.phone || resident.email || "")}</span>
            </div>
            <span class="result-badge">${resident.black_bags_received || 0}/${resident.yellow_bags_received || 0}</span>
          </div>
          <p class="result-address">${escapeHtml(resident.address_line || "-")}</p>
          <p class="result-meta">${escapeHtml(latestReceiptLabel(resident))}</p>
        </button>
      `;
    })
    .join("");

  elements.residentResults.querySelectorAll(".result-card").forEach((button) => {
    button.addEventListener("click", async () => {
      const residentId = Number(button.getAttribute("data-resident-id"));
      const resident = state.searchResults.find((item) => item.id === residentId);
      if (!resident) {
        return;
      }
      await chooseResident(resident);
    });
  });
}

function addressFromResident(resident) {
  if (!resident?.address_id) {
    return null;
  }

  return {
    id: resident.address_id,
    short_address: resident.address_line,
    full_address: resident.address_line,
    address_line: resident.address_line,
    lon: resident.lon,
    lat: resident.lat,
  };
}

function renderResidentPlaceholder(message) {
  showResidentDetailView();
  elements.residentDetail.innerHTML = `
    <div class="empty-state spacious">${escapeHtml(message)}</div>
  `;
}

function renderResidentLoading(resident) {
  showResidentDetailView();
  elements.residentDetail.innerHTML = `
    <div class="empty-state spacious">Chargement...</div>
  `;
}

function renderAddressContext() {
  if (!state.selectedAddress) {
    setElementText(elements.householdCountChip, "0");
    elements.addressContext.innerHTML = `
      <p class="detail-subtitle support-subtitle">Aucune adresse.</p>
    `;
    return;
  }

  const addressLabel =
    state.selectedAddress.full_address ||
    state.selectedAddress.address_line ||
    state.selectedAddress.short_address ||
    "Adresse";
  elements.addressContext.innerHTML = `
    <p class="detail-subtitle support-subtitle">${escapeHtml(addressLabel)}</p>
  `;
}

function renderHouseholdList() {
  if (!state.selectedAddress) {
    elements.householdList.innerHTML = "";
    return;
  }

  const residents = [...state.householdResidents].sort((left, right) => {
    const leftKey = `${left.last_name} ${left.first_name}`.toLowerCase();
    const rightKey = `${right.last_name} ${right.first_name}`.toLowerCase();
    return leftKey.localeCompare(rightKey, "fr");
  });

  setElementText(
    elements.householdCountChip,
    residents.length ? `${formatNumber(residents.length)}` : "0"
  );

  if (!residents.length) {
    elements.householdList.innerHTML = `
      <div class="empty-state">0 habitant.</div>
    `;
    return;
  }

  elements.householdList.innerHTML = residents
    .map((resident) => {
      const isActive = state.selectedResident?.id === resident.id;
      return `
        <button type="button" class="household-card${isActive ? " is-active" : ""}" data-household-resident-id="${resident.id}">
          <div class="household-card-top">
            <div class="household-card-name">
              <strong>${escapeHtml(`${resident.first_name} ${resident.last_name}`)}</strong>
              <span>${escapeHtml(latestReceiptLabel(resident))}</span>
            </div>
            <span class="result-badge">${resident.black_bags_received || 0}/${resident.yellow_bags_received || 0}</span>
          </div>
          <p>${escapeHtml(resident.phone || resident.email || "")}</p>
        </button>
      `;
    })
    .join("");

  elements.householdList.querySelectorAll(".household-card").forEach((button) => {
    button.addEventListener("click", async () => {
      const residentId = Number(button.getAttribute("data-household-resident-id"));
      const resident = state.householdResidents.find((item) => item.id === residentId);
      if (!resident) {
        return;
      }
      await chooseResident(resident);
    });
  });
}

function renderHouseholdLoading() {
  if (!state.selectedAddress) {
    elements.householdList.innerHTML = "";
    return;
  }

  setElementText(elements.householdCountChip, "...");
  elements.householdList.innerHTML = `
    <div class="empty-state">Chargement...</div>
  `;
}

function updateResidentCaches(updatedResident) {
  const mergeResident = (resident) =>
    resident.id === updatedResident.id ? { ...resident, ...updatedResident } : resident;

  state.searchResults = state.searchResults.map(mergeResident);
  state.householdResidents = state.householdResidents.map(mergeResident);

  if (state.selectedResident?.id === updatedResident.id) {
    state.selectedResident = { ...state.selectedResident, ...updatedResident };
  }

  state.addressResidentsCache.forEach((residents, addressId) => {
    if (residents.some((resident) => resident.id === updatedResident.id)) {
      state.addressResidentsCache.set(addressId, residents.map(mergeResident));
    }
  });

  state.residentHistoryCache.forEach((payload, residentId) => {
    if (payload?.resident?.id === updatedResident.id) {
      state.residentHistoryCache.set(residentId, {
        ...payload,
        resident: { ...payload.resident, ...updatedResident },
      });
    }
  });
}

async function submitStock(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const formData = new FormData(form);
  const blackBags = Number.parseInt(formData.get("black_bags"), 10) || 0;
  const yellowBags = Number.parseInt(formData.get("yellow_bags"), 10) || 0;
  const submitButton = form.querySelector("button[type='submit']");

  if (blackBags < 0 || yellowBags < 0) {
    elements.stockStatus.textContent = "Le stock ajoute ne peut pas etre negatif.";
    return;
  }
  if (blackBags === 0 && yellowBags === 0) {
    elements.stockStatus.textContent = "Indique au moins un sac a ajouter.";
    return;
  }

  submitButton.disabled = true;
  elements.stockStatus.textContent = "Mise a jour du stock...";

  try {
    const stock = await fetchJson("/api/stock/add", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        black_bags: blackBags,
        yellow_bags: yellowBags,
      }),
    });

    state.stock = stock;
    renderStock();
    form.reset();
    form.elements.black_bags.value = "0";
    form.elements.yellow_bags.value = "0";
    elements.stockStatus.textContent = "Stock mis a jour.";
  } catch (error) {
    elements.stockStatus.textContent = error.message || "Le stock n'a pas pu etre mis a jour.";
  } finally {
    submitButton.disabled = false;
  }
}

async function submitBagDonation(event, residentId) {
  event.preventDefault();

  const form = event.currentTarget;
  const formData = new FormData(form);
  const blackBags = Number.parseInt(formData.get("black_bags"), 10) || 0;
  const yellowBags = Number.parseInt(formData.get("yellow_bags"), 10) || 0;
  const distributionDate = String(formData.get("distribution_date") || "").trim();
  const notes = String(formData.get("notes") || "").trim();
  const submitButton = form.querySelector("button[type='submit']");
  const status = form.querySelector(".bag-donation-status");

  if (blackBags < 0 || yellowBags < 0) {
    status.textContent = "Les quantites ne peuvent pas etre negatives.";
    return;
  }
  if (blackBags === 0 && yellowBags === 0) {
    status.textContent = "Indique au moins un sac remis.";
    return;
  }

  submitButton.disabled = true;
  status.textContent = "Enregistrement de la remise...";

  try {
    const updatedResident = await fetchJson(`/api/residents/${residentId}/bags`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        distribution_date: distributionDate,
        black_bags: blackBags,
        yellow_bags: yellowBags,
        notes,
      }),
    });

    state.residentHistoryCache.delete(residentId);
    updateResidentCaches(updatedResident);
    renderResults();
    renderHouseholdList();
    let refreshWarning = false;
    try {
      await loadResidentHistory(residentId, { force: true });
    } catch {
      refreshWarning = true;
    }
    try {
      await loadStock();
    } catch {
      refreshWarning = true;
    }

    form.elements.black_bags.value = "0";
    form.elements.yellow_bags.value = "0";
    form.elements.notes.value = "";
    status.textContent = refreshWarning
      ? "Remise enregistree. Rafraichissement incomplet."
      : "Remise enregistree.";
  } catch (error) {
    status.textContent = error.message || "La remise n'a pas pu etre enregistree.";
  } finally {
    submitButton.disabled = false;
  }
}

async function deleteBagDonation(event, residentId, historyEventId) {
  const button = event.currentTarget;
  const confirmed = window.confirm(
    "Voulez-vous vraiment supprimer cette reception de sacs ?"
  );
  if (!confirmed) {
    return;
  }

  button.disabled = true;

  try {
    const payload = await fetchJson(`/api/residents/${residentId}/history/${historyEventId}`, {
      method: "DELETE",
    });

    state.residentHistoryCache.set(residentId, payload);
    updateResidentCaches(payload.resident);
    renderResults();
    renderHouseholdList();
    try {
      await loadStock();
    } catch {
      elements.stockStatus.textContent = "Le stock n'a pas pu etre rafraichi automatiquement.";
    }

    if (state.selectedResident?.id === residentId) {
      renderResidentDetail(payload);
    }
  } catch (error) {
    button.disabled = false;
    window.alert(error.message || "La reception n'a pas pu etre supprimee.");
  }
}

function renderResidentDetail(payload) {
  showResidentDetailView();
  const resident = payload.resident;
  const historyItems = payload.history || [];

  const historyMarkup = historyItems.length
    ? historyItems
        .map(
          (entry) => `
            <article class="history-entry">
              <div class="history-entry-top">
                <div class="history-entry-title">
                  <strong>${escapeHtml(formatDate(entry.distribution_date))}</strong>
                  <span>${entry.black_bags} noir(s) / ${entry.yellow_bags} jaune(s)</span>
                </div>
                <button
                  type="button"
                  class="history-delete-button"
                  data-history-event-id="${entry.id}"
                  aria-label="Supprimer cette reception"
                  title="Supprimer cette reception"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                    <path d="M9 3h6l1 2h4v2H4V5h4l1-2Zm-2 6h10l-.7 11H7.7L7 9Zm3 2v7h2v-7h-2Zm4 0v7h2v-7h-2Z" />
                  </svg>
                </button>
              </div>
              <p>${escapeHtml(entry.notes || "-")}</p>
            </article>
          `
        )
        .join("")
    : '<div class="empty-state">Aucun historique.</div>';

  elements.residentDetail.innerHTML = `
    <div class="resident-hero">
      <div class="resident-hero-text">
        <h2 class="resident-title">${escapeHtml(`${resident.first_name} ${resident.last_name}`)}</h2>
        <p class="detail-subtitle">${escapeHtml(resident.address_line || "-")}</p>
      </div>
      <div class="resident-hero-actions">
        <button type="button" class="button button-secondary button-small detail-close-button">
          Retour
        </button>
      </div>
    </div>

    <div class="resident-kpis">
      <article class="resident-kpi">
        <span>Derniere remise</span>
        <strong>${renderAdaptiveDateMarkup(resident.last_distribution_date)}</strong>
      </article>
      <article class="resident-kpi">
        <span>Derniere quantite</span>
        <strong>${resident.last_distribution_black_bags || 0} noir(s) / ${resident.last_distribution_yellow_bags || 0} jaune(s)</strong>
      </article>
    </div>

    <div class="detail-grid">
      <article class="detail-card">
        <span class="detail-label">Contact</span>
        <p>Telephone : ${escapeHtml(resident.phone || "-")}</p>
        <p>Mail : ${escapeHtml(resident.email || "-")}</p>
      </article>
      <article class="detail-card">
        <span class="detail-label">Notes</span>
        <p>${escapeHtml(resident.notes || "-")}</p>
        <p>${escapeHtml(state.selectedAddress?.full_address || resident.address_line || "-")}</p>
      </article>
    </div>

    <form class="bag-donation-form">
      <div class="bag-donation-title">
        <strong>Ajout</strong>
      </div>
      <div class="bag-donation-grid">
        <label>
          Date
          <input name="distribution_date" type="date" value="${todayDateInputValue()}" required />
        </label>
        <label>
          Sacs noirs
          <input name="black_bags" type="number" min="0" max="200" step="1" value="0" inputmode="numeric" />
        </label>
        <label>
          Sacs jaunes
          <input name="yellow_bags" type="number" min="0" max="200" step="1" value="0" inputmode="numeric" />
        </label>
      </div>
      <label class="bag-donation-note">
        Note
        <textarea name="notes" rows="2"></textarea>
      </label>
      <div class="bag-donation-actions">
        <button type="submit" class="button button-primary">Enregistrer la remise</button>
        <p class="bag-donation-status" aria-live="polite"></p>
      </div>
    </form>

    <section class="history-section">
      <div class="section-head">
        <div>
          <h3>Historique</h3>
        </div>
        <span>${formatNumber(historyItems.length)} entree(s)</span>
      </div>
      <div class="history-list">${historyMarkup}</div>
    </section>
  `;

  elements.residentDetail
    .querySelector(".detail-close-button")
    .addEventListener("click", () => {
      state.selectedResident = null;
      showResultsView();
      updateSupportVisibility();
      renderResults();
    });

  elements.residentDetail
    .querySelector(".bag-donation-form")
    .addEventListener("submit", (event) => submitBagDonation(event, resident.id));

  elements.residentDetail
    .querySelectorAll(".history-delete-button")
    .forEach((button) => {
      button.addEventListener("click", (event) => {
        deleteBagDonation(event, resident.id, Number(button.getAttribute("data-history-event-id")));
      });
    });
}

async function loadResidentHistory(residentId, { force = false } = {}) {
  const requestVersion = ++state.residentDetailVersion;

  try {
    let payload = force ? null : state.residentHistoryCache.get(residentId);
    if (!payload) {
      payload = await fetchJson(`/api/residents/${residentId}/history`);
      state.residentHistoryCache.set(residentId, payload);
    }

    if (requestVersion !== state.residentDetailVersion || state.selectedResident?.id !== residentId) {
      return;
    }

    updateResidentCaches(payload.resident);
    state.selectedResident = { ...state.selectedResident, ...payload.resident };
    renderResults();
    renderHouseholdList();
    renderResidentDetail(payload);
  } catch (error) {
    if (requestVersion !== state.residentDetailVersion) {
      return;
    }
    renderResidentPlaceholder(error.message || "Indisponible.");
  }
}

async function loadAddressResidents(addressId, { force = false } = {}) {
  const requestVersion = ++state.householdVersion;
  renderHouseholdLoading();

  try {
    let residents = force ? null : state.addressResidentsCache.get(addressId);
    if (!residents) {
      const payload = await fetchJson(`/api/address-residents?address_id=${addressId}`);
      residents = payload.items || [];
      state.addressResidentsCache.set(addressId, residents);
    }

    if (requestVersion !== state.householdVersion || state.selectedAddress?.id !== addressId) {
      return;
    }

    state.householdResidents = residents;
    renderHouseholdList();
  } catch (error) {
    if (requestVersion !== state.householdVersion) {
      return;
    }

    setElementText(elements.householdCountChip, "!");
    elements.householdList.innerHTML = `
      <div class="empty-state">${escapeHtml(error.message || "Indisponible.")}</div>
    `;
  }
}

async function chooseAddress(address, { clearResident = false } = {}) {
  state.selectedAddress = address;
  renderAddressContext();

  if (clearResident) {
    state.selectedResident = null;
    showResultsView();
    renderResults();
  }

  updateSupportVisibility();
  state.householdResidents = [];
  renderHouseholdLoading();
  updateMiniMapSelection();

  if (address?.id) {
    await loadAddressResidents(address.id);
  }
}

async function chooseResident(resident) {
  const wasSameResident = state.selectedResident?.id === resident.id;
  state.selectedResident = resident;
  updateSupportVisibility();

  const residentAddress = addressFromResident(resident);
  if (residentAddress) {
    state.selectedAddress = residentAddress;
    renderAddressContext();
    loadAddressResidents(residentAddress.id);
  } else {
    state.selectedAddress = null;
    state.householdResidents = [];
    renderAddressContext();
    renderHouseholdList();
  }

  updateMiniMapSelection();
  renderResults();

  if (!wasSameResident) {
    renderResidentLoading(resident);
  }

  const cachedPayload = state.residentHistoryCache.get(resident.id);
  if (cachedPayload) {
    state.residentHistoryCache.set(resident.id, {
      ...cachedPayload,
      resident: { ...cachedPayload.resident, ...resident },
    });
    if (wasSameResident) {
      renderResidentDetail(state.residentHistoryCache.get(resident.id));
      return;
    }
  }

  await loadResidentHistory(resident.id);
}

async function findBestAddressMatch(queryText) {
  const payload = await fetchJson(`/api/address-directory?q=${encodeURIComponent(queryText)}`);
  const items = payload.items || [];

  if (!items.length) {
    return null;
  }

  const normalizedQuery = queryText.trim().toLowerCase();
  return (
    items.find((item) =>
      [item.short_address, item.full_address]
        .filter(Boolean)
        .some((value) => String(value).trim().toLowerCase() === normalizedQuery)
    ) || items[0]
  );
}

function clearSuggestionMenu(fieldName) {
  const config = FIELD_CONFIGS[fieldName];
  const timerId = state.suggestionTimers.get(fieldName);
  if (timerId) {
    clearTimeout(timerId);
    state.suggestionTimers.delete(fieldName);
  }

  config.menu.innerHTML = "";

  const controller = state.suggestionControllers.get(fieldName);
  if (controller) {
    controller.abort();
    state.suggestionControllers.delete(fieldName);
  }
}

function clearAllSuggestionMenus({ exceptFieldName = null } = {}) {
  Object.keys(FIELD_CONFIGS).forEach((fieldName) => {
    if (fieldName === exceptFieldName) {
      return;
    }
    clearSuggestionMenu(fieldName);
  });
}

function isSuggestionMenuFocused(fieldName) {
  const config = FIELD_CONFIGS[fieldName];
  const activeElement = document.activeElement;
  return activeElement === config.input || config.menu.contains(activeElement);
}

function renderSuggestionMenu(fieldName, items) {
  const config = FIELD_CONFIGS[fieldName];
  const normalizedQuery = normalizeExactSuggestionQuery(fieldName, config.input.value.trim());
  const hasExactMatch = normalizedQuery
    ? items.some((item) =>
        [item.value || "", item.label || ""].some(
          (candidate) => normalizeExactSuggestionQuery(fieldName, candidate) === normalizedQuery
        )
      )
    : false;

  if (hasExactMatch) {
    config.menu.innerHTML = "";
    return;
  }

  const filteredItems = normalizedQuery
    ? items.filter((item) => {
        const candidates = [item.value || "", item.label || ""].filter(Boolean);
        return !candidates.some(
          (candidate) => normalizeExactSuggestionQuery(fieldName, candidate) === normalizedQuery
        );
      })
    : items;

  if (!filteredItems.length) {
    config.menu.innerHTML = config.emptyMessage
      ? `<div class="suggestion-empty">${escapeHtml(config.emptyMessage)}</div>`
      : "";
    return;
  }

  config.menu.innerHTML = filteredItems
    .map((item) => {
      const mainLine = item.value || item.label || "";
      const secondaryLine =
        item.label && item.label !== item.value ? item.label : fieldName === "address" ? item.value : "";
      return `
        <button
          type="button"
          class="suggestion-button"
          data-field-name="${fieldName}"
          data-value="${escapeHtml(item.value || "")}"
          data-label="${escapeHtml(item.label || "")}"
        >
          <span class="suggestion-text">${escapeHtml(mainLine)}</span>
          ${secondaryLine ? `<span class="suggestion-subtext">${escapeHtml(secondaryLine)}</span>` : ""}
        </button>
      `;
    })
    .join("");

  config.menu.querySelectorAll(".suggestion-button").forEach((button) => {
    button.addEventListener("click", async () => {
      const targetField = button.getAttribute("data-field-name");
      const value = button.getAttribute("data-value") || "";
      const label = button.getAttribute("data-label") || "";

      FIELD_CONFIGS[targetField].input.value = value;

      clearAllSuggestionMenus();

      if (targetField === "address") {
        try {
          const address = await findBestAddressMatch(label || value);
          if (address) {
            await chooseAddress(address, { clearResident: true });
          }
        } catch {
          state.selectedAddress = null;
          state.householdResidents = [];
          renderAddressContext();
          renderHouseholdList();
        }
      }

      scheduleLiveSearch({ immediate: true });
    });
  });
}

async function loadSuggestions(fieldName, expectedQuery) {
  const config = FIELD_CONFIGS[fieldName];
  const query = config.input.value.trim();

  if (query.length < 2 || query !== expectedQuery || document.activeElement !== config.input) {
    clearSuggestionMenu(fieldName);
    return;
  }

  clearSuggestionMenu(fieldName);

  const controller = new AbortController();
  state.suggestionControllers.set(fieldName, controller);

  const values = getSearchValues();
  const params = new URLSearchParams({
    field: fieldName,
    q: query,
  });

  if (fieldName !== "name" && values.name) {
    params.set("name", values.name);
  }
  if (fieldName !== "first_name" && values.first_name) {
    params.set("first_name", values.first_name);
  }
  if (fieldName !== "address" && values.address) {
    params.set("address", values.address);
  }

  try {
    const payload = await fetchJson(`/api/search-suggestions?${params.toString()}`, {
      signal: controller.signal,
    });

    if (config.input.value.trim() !== expectedQuery || document.activeElement !== config.input) {
      clearSuggestionMenu(fieldName);
      return;
    }

    renderSuggestionMenu(fieldName, payload.items || []);
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }

    config.menu.innerHTML = config.emptyMessage
      ? `<div class="suggestion-empty">${escapeHtml(config.emptyMessage)}</div>`
      : "";
  } finally {
    if (state.suggestionControllers.get(fieldName) === controller) {
      state.suggestionControllers.delete(fieldName);
    }
  }
}

function scheduleSuggestionsRefresh(activeFieldName = null) {
  const resolvedActiveFieldName =
    activeFieldName ||
    Object.entries(FIELD_CONFIGS).find(([, config]) => document.activeElement === config.input)?.[0] ||
    null;

  Object.entries(FIELD_CONFIGS).forEach(([fieldName, config]) => {
    const existingTimer = state.suggestionTimers.get(fieldName);
    if (existingTimer) {
      clearTimeout(existingTimer);
      state.suggestionTimers.delete(fieldName);
    }

    if (fieldName !== resolvedActiveFieldName) {
      clearSuggestionMenu(fieldName);
      return;
    }

    const query = config.input.value.trim();
    if (query.length < 2) {
      clearSuggestionMenu(fieldName);
      return;
    }

    const timerId = setTimeout(() => {
      loadSuggestions(fieldName, query);
    }, SUGGESTION_DEBOUNCE_MS);
    state.suggestionTimers.set(fieldName, timerId);
  });
}

function scheduleLiveSearch({ immediate = false } = {}) {
  if (state.searchTimer) {
    clearTimeout(state.searchTimer);
  }

  if (immediate) {
    runLiveSearch();
    return;
  }

  state.searchTimer = setTimeout(runLiveSearch, SEARCH_DEBOUNCE_MS);
}

async function runLiveSearch() {
  if (state.searchTimer) {
    clearTimeout(state.searchTimer);
    state.searchTimer = null;
  }

  const values = getSearchValues();

  if (!hasActiveFilters(values)) {
    if (state.searchController) {
      state.searchController.abort();
      state.searchController = null;
    }

    state.searchResults = [];
    updateResultsHeader([], values);

    if (!state.selectedResident) {
      showResultsView();
      renderResults();
    }
    return;
  }

  updateResultsHeader(state.searchResults, values, { loading: true });

  if (state.searchController) {
    state.searchController.abort();
  }

  const controller = new AbortController();
  state.searchController = controller;

  try {
    const payload = await fetchJson(buildResidentsSearchUrl(), {
      signal: controller.signal,
    });

    if (state.searchController !== controller) {
      return;
    }

    state.searchResults = payload.items || [];
    updateResultsHeader(state.searchResults, values);

    if (state.selectedResident) {
      const refreshedResident = state.searchResults.find(
        (resident) => resident.id === state.selectedResident.id
      );
      if (refreshedResident) {
        state.selectedResident = { ...state.selectedResident, ...refreshedResident };

        const cachedPayload = state.residentHistoryCache.get(refreshedResident.id);
        if (cachedPayload) {
          state.residentHistoryCache.set(refreshedResident.id, {
            ...cachedPayload,
            resident: { ...cachedPayload.resident, ...refreshedResident },
          });
          renderResidentDetail(state.residentHistoryCache.get(refreshedResident.id));
        }
      } else {
        state.selectedResident = null;
        updateSupportVisibility();
      }
    }

    if (state.selectedAddress && !state.selectedResident && !isAddressSelectionStillVisible()) {
      state.selectedAddress = null;
      state.householdResidents = [];
      renderAddressContext();
      renderHouseholdList();
      updateMiniMapSelection();
    }

    if (state.selectedResident) {
      return;
    }

    showResultsView();
    renderResults();
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }

    showResultsView();
    setElementText(elements.resultsTitle, "Indisponible");
    setElementText(elements.resultsCount, "0");
    setElementText(elements.resultsCaption, "");
    elements.residentResults.innerHTML = `
      <div class="empty-state">${escapeHtml(error.message || "Indisponible.")}</div>
    `;
  } finally {
    if (state.searchController === controller) {
      state.searchController = null;
    }
  }
}

function buildLatLngBounds(bounds) {
  return L.latLngBounds(
    [bounds.min_lat, bounds.min_lon],
    [bounds.max_lat, bounds.max_lon]
  );
}

function getMapTarget() {
  return state.selectedResident || state.selectedAddress;
}

function getMapTargetCoordinates() {
  const target = getMapTarget();
  if (!target) {
    return null;
  }

  const lat = Number(target.lat);
  const lon = Number(target.lon);

  if (Number.isNaN(lat) || Number.isNaN(lon)) {
    return null;
  }

  return {
    lat,
    lon,
    label:
      target.address_line ||
      target.full_address ||
      target.short_address ||
      `${target.first_name || ""} ${target.last_name || ""}`.trim() ||
      "",
  };
}

function ensureMiniMap() {
  if (state.map || typeof window.L === "undefined") {
    return;
  }

  const bounds = buildLatLngBounds(getMapBounds());
  state.map = L.map("map", {
    zoomControl: false,
    attributionControl: true,
    scrollWheelZoom: false,
    dragging: true,
    doubleClickZoom: false,
    boxZoom: false,
    keyboard: false,
    tap: false,
    zoomAnimation: false,
  });

  L.tileLayer("/tiles/ortho/{z}/{x}/{y}.jpg", {
    attribution: "IGN - GeoPlateforme",
    maxZoom: 19,
    noWrap: true,
    keepBuffer: 1,
    updateWhenIdle: true,
    updateWhenZooming: false,
  }).addTo(state.map);

  state.mapBoundsFrame = L.rectangle(bounds, {
    color: "#175d61",
    weight: 2,
    opacity: 0.78,
    fill: false,
    dashArray: "10 6",
    interactive: false,
  }).addTo(state.map);

  state.mapMarker = L.circleMarker([48.0952, -4.3316], {
    radius: 8,
    color: "#ffffff",
    weight: 3,
    fillColor: "#ba643c",
    fillOpacity: 0.92,
  }).addTo(state.map);

  state.map.fitBounds(bounds, { padding: [18, 18] });
}

function updateMiniMapSelection() {
  const coordinates = getMapTargetCoordinates();

  if (!state.map) {
    return;
  }

  if (!coordinates) {
    const bounds = buildLatLngBounds(getMapBounds());
    state.map.fitBounds(bounds, { padding: [18, 18] });
    if (state.mapMarker) {
      state.map.removeLayer(state.mapMarker);
    }
    return;
  }

  if (state.mapMarker && !state.map.hasLayer(state.mapMarker)) {
    state.mapMarker.addTo(state.map);
  }
  state.mapMarker.setLatLng([coordinates.lat, coordinates.lon]);
  state.map.setView([coordinates.lat, coordinates.lon], MINI_MAP_ZOOM);
  window.setTimeout(() => state.map.invalidateSize(), 40);
}

async function loadOverview() {
  const overview = await fetchJson("/api/overview");
  updateOverview(overview);
}

function clearSelections() {
  state.selectedResident = null;
  state.selectedAddress = null;
  state.householdResidents = [];
  renderAddressContext();
  renderHouseholdList();
  showResultsView();
  elements.residentDetail.innerHTML = "";
  updateSupportVisibility();
  updateMiniMapSelection();
}

function resetSearch() {
  Object.values(FIELD_CONFIGS).forEach(({ input }) => {
    input.value = "";
  });

  Object.keys(FIELD_CONFIGS).forEach((fieldName) => clearSuggestionMenu(fieldName));

  clearSelections();
  scheduleLiveSearch({ immediate: true });
}

function bindFieldEvents() {
  Object.entries(FIELD_CONFIGS).forEach(([fieldName, config]) => {
    config.input.addEventListener("input", () => {
      if (fieldName === "address" && !isAddressSelectionStillVisible()) {
        state.selectedAddress = state.selectedResident ? addressFromResident(state.selectedResident) : null;
        if (!state.selectedResident && !state.selectedAddress) {
          state.householdResidents = [];
          renderAddressContext();
          renderHouseholdList();
          showResultsView();
          elements.residentDetail.innerHTML = "";
          updateSupportVisibility();
        }
      }

      scheduleSuggestionsRefresh(fieldName);
      scheduleLiveSearch();
    });

    config.input.addEventListener("focus", () => {
      scheduleSuggestionsRefresh(fieldName);
    });

    config.input.addEventListener("blur", () => {
      window.setTimeout(() => {
        if (isSuggestionMenuFocused(fieldName)) {
          return;
        }
        clearSuggestionMenu(fieldName);
      }, 120);
    });
  });
}

function bindEvents() {
  bindFieldEvents();
  document.addEventListener("pointerdown", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) {
      clearAllSuggestionMenus();
      return;
    }

    if (target.closest(".search-field") || target.closest(".suggestion-menu")) {
      return;
    }

    clearAllSuggestionMenus();
  });

  elements.searchForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    if (state.searchResults[0]) {
      await chooseResident(state.searchResults[0]);
      return;
    }

    const addressQuery = elements.searchAddress.value.trim();
    if (addressQuery) {
      try {
        const address = await findBestAddressMatch(addressQuery);
        if (address) {
          await chooseAddress(address, { clearResident: true });
        }
      } catch {
        return;
      }
    }
  });

  elements.resetSearch.addEventListener("click", resetSearch);
  elements.stockForm.addEventListener("submit", submitStock);
}

async function boot() {
  try {
    renderStock();
    renderAddressContext();
    updateSupportVisibility();
    showResultsView();
    renderResults();
    await Promise.all([loadOverview(), loadStock()]);
    if (typeof window.L !== "undefined") {
      ensureMiniMap();
      updateMiniMapSelection();
    }
    bindEvents();
    await runLiveSearch();
  } catch (error) {
    elements.residentResults.innerHTML = `
      <div class="empty-state">${escapeHtml(error.message || "Indisponible.")}</div>
    `;
  }
}

boot();
