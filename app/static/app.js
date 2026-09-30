const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const appState = {
  page: "dashboard",
  snapshot: { interfaces: [], apps: [], rules: [], metrics: {}, settings: {}, warnings: [] },
  history: [],
  events: [],
  flows: [],
  flowStatus: { available: false, message: "Chargement des connexions…" },
  flowPrevious: {},
  tableViews: {},
  trafficHours: 1,
  liveTraffic: true,
  refreshing: false,
  pendingForceRender: false,
  resizingTable: false,
  lastHistoryFetch: 0,
  snapshotPolls: 0,
};

const pageMeta = {
  dashboard: ["Tableau de bord", "Vue d’ensemble du réseau, des applications et du trafic"],
  apps: ["Applications", "Conteneurs Docker et services système détectés"],
  rules: ["Règles de routage", "Contrôlez la sortie réseau de chaque application"],
  traffic: ["Analyse du trafic", "Surveillance par interface, application et client"],
  network: ["Configuration réseau", "Détection et validation des interfaces"],
  events: ["Événements", "Journal des changements et diagnostics"],
  settings: ["Paramètres", "Activation, conservation et diagnostic"],
};

const strategyLabels = {
  force: "Forcer l’interface",
  prefer: "Préférence avec bascule",
  balance: "Répartition ETH0 / ETH1",
  qos: "Priorité QoS",
};
const strategyDescriptions = {
  force: "Tout le trafic de cette application sort par l’interface choisie. Il n’y a pas de bascule automatique.",
  prefer: "L’interface principale est utilisée normalement. DualRoute passe sur l’interface de repli si la passerelle principale ne répond plus.",
  balance: "Chaque nouvelle connexion est envoyée sur ETH0 ou ETH1. Une même connexion reste sur la même interface.",
  qos: "Le trafic utilise l’interface principale et reçoit une marque DSCP : 1 est la priorité minimale, 5 la priorité maximale.",
};
const directionLabels = { in: "Entrant", out: "Sortant", both: "Les deux" };
const qosLabels = { 1: "minimale", 2: "basse", 3: "normale", 4: "élevée", 5: "maximale" };

function managedInterfaces() {
  return appState.snapshot.interfaces.filter(item => item.managed !== false);
}

function dashboardInterfaces() {
  return appState.snapshot.settings.dashboard_managed_only === false
    ? appState.snapshot.interfaces
    : managedInterfaces();
}

function esc(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function formatRate(value) {
  const bps = Number(value || 0);
  const useBytes = appState.snapshot.settings.display_rate_unit === "MBps";
  const amount = useBytes ? bps / 8e6 : bps / 1e6;
  const digits = amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${useBytes ? "Mo/s" : "Mb/s"}`;
}

function formatBytes(value) {
  let bytes = Number(value || 0);
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index += 1; }
  return `${bytes.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function ago(date) {
  if (!date) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(date).getTime()) / 1000);
  if (seconds < 60) return `il y a ${Math.round(seconds)} s`;
  if (seconds < 3600) return `il y a ${Math.round(seconds / 60)} min`;
  return new Date(date).toLocaleString("fr-FR");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = "toast"; }, 3500);
}

function interfaceColor(index) { return index === 0 ? "var(--blue)" : index === 1 ? "var(--teal)" : "#9b7cff"; }
function interfaceBadge(name) {
  const index = appState.snapshot.interfaces.findIndex(item => item.name === name);
  return `<span class="badge ${index === 1 ? "teal" : index === 2 ? "purple" : "blue"}">${esc(name || "—")}</span>`;
}

function tableEmpty(columns, message) {
  return `<tr><td colspan="${columns}" class="table-empty">${esc(message)}</td></tr>`;
}

function captureScrollState() {
  return {
    content: $("#content")?.scrollTop || 0,
    tables: Object.fromEntries($$(".table-scroll").map((element, index) => [element.dataset.scrollId || index, {top: element.scrollTop, left: element.scrollLeft}])),
  };
}

function restoreScrollState(state) {
  if (!state) return;
  $("#content").scrollTop = state.content;
  $$(".table-scroll").forEach((element, index) => {
    const position = state.tables[element.dataset.scrollId || index];
    element.scrollTop = position?.top || 0;
    element.scrollLeft = position?.left || 0;
  });
}

function compareTableValues(left, right) {
  return String(left).localeCompare(String(right), "fr", { numeric: true, sensitivity: "base" });
}

function applyTableView(table, state) {
  const rows = [...table.tBodies[0].rows].filter(row => !row.querySelector(".table-empty"));
  rows.forEach(row => {
    const visible = state.filters.every((filter, index) => {
      if (!filter) return true;
      return (row.cells[index]?.textContent || "").toLocaleLowerCase("fr").includes(filter.toLocaleLowerCase("fr"));
    });
    row.hidden = !visible;
  });
  if (state.sortIndex !== null) {
    rows.sort((left, right) => {
      const leftCell = left.cells[state.sortIndex];
      const rightCell = right.cells[state.sortIndex];
      const result = leftCell?.dataset.sort !== undefined && rightCell?.dataset.sort !== undefined
        ? Number(leftCell.dataset.sort) - Number(rightCell.dataset.sort)
        : compareTableValues(leftCell?.textContent ?? "", rightCell?.textContent ?? "");
      return state.sortDirection === "asc" ? result : -result;
    }).forEach(row => table.tBodies[0].appendChild(row));
  }
}

function enhanceTables() {
  $$("table.data-table").forEach((table, tableIndex) => {
    const id = `${appState.page}-${tableIndex}`;
    table.dataset.tableId = id;
    table.closest(".table-scroll").dataset.scrollId = id;
    const headers = [...table.tHead.rows[0].cells];
    const state = appState.tableViews[id] || { filters: Array(headers.length).fill(""), sortIndex: null, sortDirection: "asc" };
    while (state.filters.length < headers.length) state.filters.push("");
    appState.tableViews[id] = state;
    headers.forEach((header, index) => {
      const label = header.dataset.label || header.textContent.trim();
      header.dataset.label = label;
      header.classList.add("sortable");
      header.title = `Trier par ${label}`;
      header.setAttribute("aria-sort", state.sortIndex === index ? (state.sortDirection === "asc" ? "ascending" : "descending") : "none");
      header.innerHTML = `<span>${esc(label)}</span><span class="sort-indicator">${state.sortIndex === index ? (state.sortDirection === "asc" ? "▲" : "▼") : "↕"}</span><span class="column-resizer" aria-label="Redimensionner ${esc(label)}"></span>`;
      header.tabIndex = 0;
      header.onkeydown = event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); header.click(); } };
      if (state.widths?.[index]) header.style.width = `${state.widths[index]}px`;
      $(".column-resizer", header).onpointerdown = event => {
        event.preventDefault(); event.stopPropagation();
        appState.resizingTable = true;
        const start = event.clientX;
        const width = header.getBoundingClientRect().width;
        const totalWidth = table.getBoundingClientRect().width;
        const widths = headers.map(item => item.getBoundingClientRect().width);
        const move = pointer => {
          const nextWidth = Math.max(70, width + pointer.clientX - start);
          widths[index] = nextWidth;
          headers.forEach((item, i) => { item.style.width = `${widths[i]}px`; });
          table.style.width = `${totalWidth + nextWidth - width}px`;
          state.widths = widths;
        };
        const stop = () => { appState.resizingTable = false; document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", stop); document.removeEventListener("pointercancel", stop); };
        document.addEventListener("pointermove", move);
        document.addEventListener("pointerup", stop);
        document.addEventListener("pointercancel", stop);
      };
      header.onclick = event => {
        if (event.target.closest("input, .column-resizer")) return;
        if (state.sortIndex === index) state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
        else { state.sortIndex = index; state.sortDirection = "asc"; }
        enhanceTables();
      };
    });
    if (state.widths) table.style.width = `${state.widths.reduce((total, width) => total + width, 0)}px`;
    if (table.tHead.rows.length === 1) {
      const filterRow = table.tHead.insertRow();
      filterRow.className = "filter-row";
      headers.forEach((header, index) => {
        const cell = document.createElement("th");
        const input = document.createElement("input");
        input.className = "table-filter";
        input.type = "search";
        input.placeholder = "Filtrer…";
        input.setAttribute("aria-label", `Filtrer ${header.dataset.label}`);
        input.value = state.filters[index] || "";
        input.onclick = event => event.stopPropagation();
        input.oninput = () => {
          state.filters[index] = input.value;
          applyTableView(table, state);
        };
        cell.appendChild(input);
        filterRow.appendChild(cell);
      });
    }
    applyTableView(table, state);
  });
}

async function refreshSnapshot(forceRender = false) {
  if (appState.refreshing) {
    appState.pendingForceRender ||= forceRender;
    return;
  }
  appState.refreshing = true;
  try {
    appState.snapshotPolls += 1;
    const includeAppStats = forceRender || appState.snapshotPolls % 4 === 0;
    const previousStats = appState.snapshot.app_stats || {};
    const nextSnapshot = await api(`/api/snapshot?include_app_stats=${includeAppStats}`);
    if (!includeAppStats) nextSnapshot.app_stats = previousStats;
    appState.snapshot = nextSnapshot;
    const managedCount = managedInterfaces().length;
    const otherCount = appState.snapshot.interfaces.length - managedCount;
    $("#side-count").textContent = `${appState.snapshot.apps.length} applications • ${managedCount} gérées${otherCount ? ` • ${otherCount} autres` : ""}`;
    $("#last-update").textContent = `Actualisé à ${new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    if (appState.page === "traffic" && appState.liveTraffic) {
      await Promise.all([refreshFlows(), ensureHistory(appState.trafficHours)]);
    }
    const activeEditor = document.activeElement?.matches("input, select, textarea");
    const livePage = appState.page === "dashboard" || (appState.page === "traffic" && appState.liveTraffic);
    if (forceRender || (livePage && !$("#rule-dialog").open && !activeEditor && !appState.resizingTable)) renderPage(true);
  } catch (error) {
    $("#last-update").textContent = "API indisponible";
    toast(error.message, true);
  } finally {
    appState.refreshing = false;
    if (appState.pendingForceRender) {
      appState.pendingForceRender = false;
      await refreshSnapshot(true);
    }
  }
}

async function ensureHistory(hours = 1, force = false) {
  if (!force && Date.now() - appState.lastHistoryFetch < 9000) return;
  appState.history = await api(`/api/traffic/history?hours=${hours}`);
  appState.lastHistoryFetch = Date.now();
}

async function refreshFlows() {
  const result = await api("/api/traffic/flows?limit=1000");
  const now = Date.now();
  const previous = appState.flowPrevious;
  const next = {};
  appState.flows = result.items.map(flow => {
    const old = previous[flow.id];
    const elapsed = old ? Math.max((now - old.ts) / 1000, 0.001) : 0;
    const measured = flow.accounting && old?.accounting && elapsed;
    const rxBps = measured ? Math.max(0, flow.rx_bytes - old.rx_bytes) * 8 / elapsed : null;
    const txBps = measured ? Math.max(0, flow.tx_bytes - old.tx_bytes) * 8 / elapsed : null;
    next[flow.id] = { ts: now, rx_bytes: flow.rx_bytes, tx_bytes: flow.tx_bytes, accounting: flow.accounting };
    return { ...flow, rx_bps: rxBps, tx_bps: txBps };
  });
  appState.flowPrevious = next;
  appState.flowStatus = result;
}

function flowRate(value) { return value === null ? "—" : formatRate(value); }

function interfaceCard(item, index) {
  const metric = appState.snapshot.metrics[item.name] || {};
  return `<article class="interface-card" style="--accent:${interfaceColor(index)}">
    <div class="card-head">
      <span class="nic-icon">↔</span><h3>${esc(item.name.toUpperCase())}</h3>
      <span class="badge ${item.up ? "green" : "red"}"><span class="status-dot"></span>${item.up ? "Opérationnel" : "Hors ligne"}</span>
      ${item.managed === false ? `<span class="badge amber">Observation seule</span>` : ""}
      <div class="card-address"><small>Adresse IP</small><b>${esc(item.address || "Non configurée")}${item.prefix ? `/${item.prefix}` : ""}</b></div>
    </div>
    <div class="card-stats">
      <div class="stat"><small>Débit descendant</small><b style="color:${interfaceColor(index)}">${formatRate(metric.rx_bps)}</b></div>
      <div class="stat"><small>Débit montant</small><b>${formatRate(metric.tx_bps)}</b></div>
      <div class="stat"><small>Passerelle</small><b>${esc(item.gateway || "—")}</b></div>
      <div class="stat"><small>Lien</small><b>${item.speed_mbps ? `${item.speed_mbps} Mb/s` : "Inconnu"}</b></div>
    </div>
  </article>`;
}

function renderDashboard() {
  const { apps, rules, warnings } = appState.snapshot;
  const interfaces = dashboardInterfaces();
  const cards = interfaces.map(interfaceCard).join("");
  const rows = apps.map(app => {
    const rule = rules.find(item => item.app_id === app.id);
    return `<tr>
      <td><div class="app-cell"><span class="app-icon">${app.type === "docker" ? "◇" : "▣"}</span>${esc(app.name)}</div></td>
      <td>${rule ? `<span class="badge blue">${esc(strategyLabels[rule.strategy])}</span>` : `<span class="muted">Automatique</span>`}</td>
      <td>${rule ? interfaceBadge(rule.primary_interface) : "—"}</td>
      <td data-sort="${(appState.snapshot.app_stats[app.id] || {}).rx_bytes || 0}">${formatBytes((appState.snapshot.app_stats[app.id] || {}).rx_bytes)}</td>
      <td data-sort="${(appState.snapshot.app_stats[app.id] || {}).tx_bytes || 0}">${formatBytes((appState.snapshot.app_stats[app.id] || {}).tx_bytes)}</td>
      <td><span class="badge green"><span class="status-dot"></span>${esc(app.status)}</span></td>
    </tr>`;
  }).join("");
  $("#content").innerHTML = `
    ${warnings.length ? `<div class="recommendation warning"><h3>Configuration à compléter</h3><p>${warnings.map(esc).join(" ")}</p></div>` : ""}
    <div class="cards">${cards || `<div class="recommendation warning"><h3>Aucune interface détectée</h3><p>La découverte réseau nécessite le conteneur en mode réseau hôte.</p></div>`}</div>
    <section class="panel"><div class="panel-head"><div><h2>Trafic en temps réel</h2><p>Débits descendants et montants par interface • ${appState.snapshot.settings.display_rate_unit === "MBps" ? "Mo/s" : "Mb/s"}</p></div><div class="legend">${interfaces.map((item, index) => `<span style="--legend:${interfaceColor(index)}">${esc(item.name)}</span>`).join("")}</div></div><div class="chart-wrap"><canvas id="traffic-chart"></canvas></div></section>
    <section class="panel"><div class="panel-head"><div><h2>Applications</h2><p>Activité et règle actuellement associée</p></div><button class="secondary" data-page-link="rules">Gérer les règles</button></div>
      <div class="table-scroll"><table class="data-table"><thead><tr><th style="width:24%">Application</th><th style="width:24%">Mode</th><th>Interface</th><th>Reçu</th><th>Envoyé</th><th>Statut</th></tr></thead><tbody>${rows || tableEmpty(6, "Aucune application détectée")}</tbody></table></div>
      <div class="table-footer"><span>${apps.length} applications détectées</span><span>Actualisation toutes les ${appState.snapshot.settings.sample_interval_seconds || 2} s</span></div>
    </section>`;
  bindPageLinks();
  ensureHistory(1).then(() => drawTrafficChart($("#traffic-chart"))).catch(error => toast(error.message, true));
}

function renderApps() {
  const { apps, rules } = appState.snapshot;
  const rows = apps.map(app => {
    const rule = rules.find(item => item.app_id === app.id);
    return `<tr>
      <td><div class="app-cell"><span class="app-icon">${app.type === "docker" ? "◇" : "▣"}</span>${esc(app.name)}</div></td>
      <td><span class="badge ${app.type === "docker" ? "blue" : "teal"}">${esc(app.type === "docker" ? "Docker" : "Système")}</span></td>
      <td>${esc(app.image)}</td><td>${esc(app.ips.join(", ") || "Hôte")}</td><td>${esc(app.networks.join(", "))}</td>
      <td>${rule ? interfaceBadge(rule.primary_interface) : `<span class="muted">Aucune</span>`}</td>
      <td><span class="badge green"><span class="status-dot"></span>${esc(app.status)}</span></td>
      <td><div class="row-actions"><button data-rule-app="${esc(app.id)}">${rule ? "Modifier la règle" : "Créer une règle"}</button></div></td>
    </tr>`;
  }).join("");
  $("#content").innerHTML = `<div class="toolbar"><div><h2>Applications découvertes</h2><p>Les adresses Docker sont actualisées automatiquement après chaque redémarrage de conteneur.</p></div><button class="secondary" id="refresh-apps">↻ Actualiser</button></div>
    <section class="panel"><div class="table-scroll tall"><table class="data-table"><thead><tr><th style="width:18%">Application</th><th>Type</th><th style="width:20%">Image</th><th>Adresses IP</th><th>Réseaux</th><th>Règle</th><th>Statut</th><th>Actions</th></tr></thead><tbody>${rows || tableEmpty(8, "Aucun conteneur Docker détecté")}</tbody></table></div><div class="table-footer"><span>${apps.length} services</span><span>Socket Docker monté en lecture seule</span></div></section>`;
  $("#refresh-apps").onclick = () => refreshSnapshot(true);
  $$('[data-rule-app]').forEach(button => button.onclick = () => {
    const rule = rules.find(item => item.app_id === button.dataset.ruleApp);
    openRuleDialog(rule?.id || null, button.dataset.ruleApp);
  });
}

function renderRules() {
  const rules = appState.snapshot.rules;
  const rows = rules.map((rule, index) => `<tr>
    <td>${index + 1}</td><td><div class="app-cell"><span class="app-icon">${rule.app_id === "service:smb" ? "▣" : "◇"}</span>${esc(rule.app_name)}</div></td>
    <td>${esc(directionLabels[rule.direction])}</td><td><span class="badge blue">${esc(strategyLabels[rule.strategy])}</span></td>
    <td>${interfaceBadge(rule.primary_interface)}</td><td>${rule.fallback_interface ? interfaceBadge(rule.fallback_interface) : "—"}</td>
    <td>${rule.download_limit_mbps ? `↓ ${rule.download_limit_mbps}` : "↓ ∞"} / ${rule.upload_limit_mbps ? `↑ ${rule.upload_limit_mbps}` : "↑ ∞"} Mb/s</td>
    <td data-sort="${rule.qos}"><span title="1 = priorité minimale, 5 = priorité maximale">${rule.qos}/5 — ${qosLabels[rule.qos] || "normale"}</span></td><td><span class="badge ${rule.enabled ? "green" : "amber"}"><span class="status-dot"></span>${rule.enabled ? "Activée" : "Suspendue"}</span></td>
    <td><div class="row-actions"><button data-edit-rule="${rule.id}">Modifier</button><button data-delete-rule="${rule.id}">Supprimer</button></div></td>
  </tr>`).join("");
  $("#content").innerHTML = `<div class="toolbar"><div><h2>Une règle par application</h2><p>Choisissez précisément comment chaque application utilise ETH0 et ETH1.</p></div><div class="toolbar-actions"><button class="secondary" id="preview-rules">Prévisualiser</button><button class="primary" id="new-rule">＋ Nouvelle règle</button></div></div>
    <div class="capability-grid">
      <button class="capability-card" data-new-strategy="force"><b>① Forcer une interface</b><span>Utiliser uniquement ETH0 ou uniquement ETH1.</span></button>
      <button class="capability-card" data-new-strategy="prefer"><b>② Préférer + basculer</b><span>Utiliser une interface, puis l’autre si elle tombe.</span></button>
      <button class="capability-card" data-new-strategy="balance"><b>③ Répartir le trafic</b><span>Distribuer les nouvelles connexions entre les deux sorties.</span></button>
      <button class="capability-card" data-new-strategy="qos"><b>④ Limiter ou prioriser</b><span>Définir les limites et la priorité QoS : 1 minimale, 5 maximale.</span></button>
    </div>
    <section class="panel"><div class="table-scroll tall"><table class="data-table"><thead><tr><th style="width:45px">#</th><th style="width:18%">Application ou service</th><th>Sens</th><th style="width:17%">Mode</th><th>Interface principale</th><th>Repli</th><th>Limites ↓ / ↑</th><th>QoS</th><th>État</th><th style="width:150px">Actions</th></tr></thead><tbody>${rows || tableEmpty(10, "Aucune règle. Choisissez l’un des quatre modes ci-dessus pour commencer.")}</tbody></table></div><div class="table-footer"><span>${rules.length} règles</span><span>Les modifications sont appliquées uniquement sur demande.</span></div></section>`;
  $("#new-rule").onclick = () => openRuleDialog();
  $("#preview-rules").onclick = previewRouting;
  $$('[data-new-strategy]').forEach(button => button.onclick = () => openRuleDialog(null, null, button.dataset.newStrategy));
  $$('[data-edit-rule]').forEach(button => button.onclick = () => openRuleDialog(Number(button.dataset.editRule)));
  $$('[data-delete-rule]').forEach(button => button.onclick = () => removeRule(Number(button.dataset.deleteRule)));
}

function renderTraffic() {
  const interfaces = managedInterfaces();
  const totalRx = interfaces.reduce((sum, item) => sum + Number((appState.snapshot.metrics[item.name] || {}).rx_bps || 0), 0);
  const totalTx = interfaces.reduce((sum, item) => sum + Number((appState.snapshot.metrics[item.name] || {}).tx_bps || 0), 0);
  const groups = new Map();
  appState.flows.forEach(flow => {
    const group = groups.get(flow.app_id) || {
      app_id: flow.app_id, application: flow.application, container_id: flow.container_id,
      app_type: flow.app_type, connections: 0, interfaces: new Set(), rx_bps: 0, tx_bps: 0,
      rx_bytes: 0, tx_bytes: 0, measured: true, accounting: true,
    };
    group.connections += 1;
    group.measured = group.measured && flow.rx_bps !== null;
    group.accounting = group.accounting && flow.accounting;
    group.interfaces.add(flow.interface || "Route système");
    group.rx_bps += flow.rx_bps;
    group.tx_bps += flow.tx_bps;
    group.rx_bytes += flow.rx_bytes;
    group.tx_bytes += flow.tx_bytes;
    groups.set(flow.app_id, group);
  });
  const appRows = [...groups.values()].sort((left, right) => (right.rx_bps + right.tx_bps) - (left.rx_bps + left.tx_bps)).map(group => {
    const rule = appState.snapshot.rules.find(item => item.app_id === group.app_id);
    return `<tr>
      <td><div class="app-cell"><span class="app-icon">${group.app_type === "docker" ? "◇" : group.app_type === "system" ? "▣" : "⌂"}</span><span>${esc(group.application)}${group.container_id ? `<small>${esc(group.container_id)}</small>` : ""}</span></div></td>
      <td data-sort="${group.connections}">${group.connections}</td>
      <td>${[...group.interfaces].map(interfaceBadge).join(" ")}</td>
      <td>${rule ? esc(strategyLabels[rule.strategy]) : "Route système"}</td>
      <td data-sort="${group.measured ? group.rx_bps : -1}">${group.measured ? formatRate(group.rx_bps) : "—"}</td>
      <td data-sort="${group.measured ? group.tx_bps : -1}">${group.measured ? formatRate(group.tx_bps) : "—"}</td>
      <td data-sort="${group.accounting ? group.rx_bytes : -1}">${group.accounting ? formatBytes(group.rx_bytes) : "—"}</td>
      <td data-sort="${group.accounting ? group.tx_bytes : -1}">${group.accounting ? formatBytes(group.tx_bytes) : "—"}</td>
    </tr>`;
  }).join("");
  const connectionRows = appState.flows.map(flow => `<tr>
    <td><div class="app-cell"><span class="app-icon">${flow.app_type === "docker" ? "◇" : flow.app_type === "system" ? "▣" : "⌂"}</span><span>${esc(flow.application)}${flow.container_id ? `<small>${esc(flow.container_id)}</small>` : ""}</span></div></td>
    <td><span class="badge ${flow.direction === "entrant" ? "teal" : "blue"}">${esc(flow.direction)}</span></td>
    <td>${esc(flow.protocol)}</td><td>${esc(flow.state)}</td>
    <td title="${esc(flow.local_endpoint)}">${esc(flow.local_endpoint)}</td>
    <td title="${esc(flow.remote_endpoint)}">${esc(flow.remote_endpoint)}</td>
    <td>${esc(flow.service)}</td>
    <td title="${esc(flow.interface_source || 'Interface non identifiée')}">${flow.interface ? interfaceBadge(flow.interface) : `<span class="muted">Non identifiée</span>`}</td>
    <td data-sort="${flow.rx_bps ?? -1}">${flowRate(flow.rx_bps)}</td>
    <td data-sort="${flow.tx_bps ?? -1}">${flowRate(flow.tx_bps)}</td>
    <td data-sort="${flow.accounting ? flow.rx_bytes + flow.tx_bytes : -1}">${flow.accounting ? formatBytes(flow.rx_bytes + flow.tx_bytes) : "—"}</td>
    <td data-sort="${flow.timeout_seconds}">${flow.timeout_seconds} s</td>
  </tr>`).join("");
  const managedNames = new Set(interfaces.map(item => item.name));
  const recent = appState.history.filter(sample => managedNames.has(sample.interface)).slice(-240).reverse();
  const rows = recent.map(sample => `<tr><td data-sort="${new Date(sample.ts).getTime()}">${new Date(sample.ts).toLocaleTimeString("fr-FR")}</td><td>${interfaceBadge(sample.interface)}</td><td>Entrant</td><td data-sort="${sample.rx_bps}">${formatRate(sample.rx_bps)}</td><td>Sortant</td><td data-sort="${sample.tx_bps}">${formatRate(sample.tx_bps)}</td><td data-sort="${sample.rx_bytes}">${formatBytes(sample.rx_bytes)}</td><td data-sort="${sample.tx_bytes}">${formatBytes(sample.tx_bytes)}</td></tr>`).join("");
  $("#content").innerHTML = `<div class="kpi-grid">
    <div class="kpi" style="--accent:var(--teal)"><span class="kpi-icon">▥</span><div><small>Débit total</small><b>${formatRate(totalRx + totalTx)}</b></div></div>
    ${interfaces.map((item,index) => `<div class="kpi" style="--accent:${interfaceColor(index)}"><span class="kpi-icon">↔</span><div><small>${esc(item.name.toUpperCase())}</small><b>${formatRate(((appState.snapshot.metrics[item.name] || {}).rx_bps || 0) + ((appState.snapshot.metrics[item.name] || {}).tx_bps || 0))}</b></div></div>`).join("")}
    <div class="kpi" style="--accent:var(--green)"><span class="kpi-icon">◎</span><div><small>Connexions actives</small><b>${appState.flows.length}</b></div></div>
  </div>
  <div class="traffic-status ${appState.flowStatus.available ? "" : "warning"}"><span>${esc(appState.flowStatus.message)}</span><button class="chip-button ${appState.liveTraffic ? "active" : ""}" id="toggle-live">${appState.liveTraffic ? "● Actualisation automatique" : "Ⅱ Actualisation en pause"}</button></div>
  ${appState.flowStatus.available && !appState.flowStatus.accounting_enabled ? `<div class="recommendation warning"><h3>Activer les débits par connexion</h3><p>Les compteurs Linux sont désactivés. Depuis SSH sur le NAS, exécutez <code>sudo sysctl -w net.netfilter.nf_conntrack_acct=1</code>. Les compteurs seront disponibles sur les nouvelles connexions. Les valeurs absentes sont affichées « — ».</p></div>` : ""}
  ${appState.flowStatus.accounting_enabled && appState.flowStatus.accounting_missing ? `<p class="traffic-note">${appState.flowStatus.accounting_missing} connexions créées sans compteurs : débit indisponible jusqu’à leur renouvellement.</p>` : ""}
  ${appState.flowStatus.truncated ? `<p class="traffic-note">Affichage limité à ${appState.flows.length} connexions sur ${appState.flowStatus.total}. Les synthèses portent sur les flux affichés.</p>` : ""}
  <section class="panel"><div class="panel-head"><div><h2>Trafic actuel par application</h2><p>Débits des connexions suivies • volumes cumulés des connexions encore présentes</p></div><span class="muted">Cliquez sur une colonne pour trier • filtres sous les titres</span></div><div class="table-scroll"><table class="data-table"><thead><tr><th style="width:22%">Application / conteneur</th><th>Connexions</th><th>Interface actuelle</th><th>Règle</th><th>Reçu/s</th><th>Envoyé/s</th><th>Total reçu</th><th>Total envoyé</th></tr></thead><tbody>${appRows || tableEmpty(8, appState.flowStatus.available ? "Aucune connexion active attribuable" : appState.flowStatus.message)}</tbody></table></div></section>
  <section class="panel"><div class="panel-head"><div><h2>Connexions actives</h2><p>Application, endpoints, protocole et route de chaque flux IPv4 suivi par conntrack</p></div><span class="muted">${appState.flows.length} flux</span></div><div class="table-scroll tall"><table class="data-table wide"><thead><tr><th style="width:210px">Application / conteneur</th><th>Sens</th><th>Protocole</th><th>État</th><th style="width:155px">Endpoint local</th><th style="width:175px">Destination / client</th><th>Service</th><th>Interface</th><th>Reçu/s</th><th>Envoyé/s</th><th>Volume</th><th>Expiration</th></tr></thead><tbody>${connectionRows || tableEmpty(12, appState.flowStatus.available ? "Aucune connexion active" : appState.flowStatus.message)}</tbody></table></div></section>
  <section class="panel"><div class="panel-head"><div><h2>Historique par interface</h2><p>Trafic reçu et envoyé pour chaque interface gérée</p></div><div class="filters"><button class="chip-button ${appState.trafficHours === 1 ? "active" : ""}" data-hours="1">1 h</button><button class="chip-button ${appState.trafficHours === 24 ? "active" : ""}" data-hours="24">24 h</button><button class="chip-button ${appState.trafficHours === 168 ? "active" : ""}" data-hours="168">7 j</button></div></div><div class="chart-wrap large"><canvas id="traffic-chart"></canvas></div></section>
  <section class="panel"><div class="panel-head"><div><h2>Échantillons historiques</h2><p>Valeurs conservées pour l’analyse par interface</p></div><span class="muted">Réception ${formatRate(totalRx)} • Envoi ${formatRate(totalTx)}</span></div><div class="table-scroll"><table class="data-table"><thead><tr><th>Heure</th><th>Interface</th><th>Sens</th><th>Débit</th><th>Sens</th><th>Débit</th><th>Total reçu</th><th>Total envoyé</th></tr></thead><tbody>${rows || tableEmpty(8, "Collecte des premières mesures…")}</tbody></table></div></section>`;
  drawTrafficChart($("#traffic-chart"));
  $("#toggle-live").onclick = async () => {
    appState.liveTraffic = !appState.liveTraffic;
    if (appState.liveTraffic) await refreshSnapshot(true);
    else renderPage(true);
  };
  $$('[data-hours]').forEach(button => button.onclick = async () => {
    appState.trafficHours = Number(button.dataset.hours);
    await ensureHistory(appState.trafficHours, true);
    renderPage(true);
  });
}

function renderNetwork() {
  const interfaces = appState.snapshot.interfaces;
  const managed = managedInterfaces();
  const detectedOnly = interfaces.filter(item => item.managed === false);
  const physical = managed.filter(item => ["eth0", "eth1"].includes(item.name.toLowerCase()));
  const configured = physical.find(item => item.name.toLowerCase() === "eth1") || {};
  const addressedNetworks = physical.filter(item => item.network && item.address).map(item => item.network);
  const configuredNetworks = physical.filter(item => item.up && item.network && item.address && item.gateway).map(item => item.network);
  const hasNetworkConflict = addressedNetworks.length >= 2 && new Set(addressedNetworks).size < addressedNetworks.length;
  const proposedAddress = hasNetworkConflict ? "192.168.2.131" : (configured.address || "192.168.2.131");
  const proposedGateway = hasNetworkConflict ? "192.168.2.1" : (configured.gateway || "192.168.2.1");
  const cards = managed.map((item, index) => `<article class="network-card" style="--accent:${interfaceColor(index)}"><div class="network-card-head"><span class="nic-icon">↔</span><h3>${esc(item.name.toUpperCase())}</h3><span class="badge blue">Gérée</span><span class="badge ${item.up ? "green" : "red"}">${item.up ? "Connectée" : "Hors ligne"}</span></div><div class="network-details"><div class="detail"><small>Adresse IP</small><b>${esc(item.address || "Non configurée")}${item.prefix ? `/${item.prefix}` : ""}</b></div><div class="detail"><small>Passerelle</small><b>${esc(item.gateway || "—")}</b></div><div class="detail"><small>Sous-réseau</small><b>${esc(item.network || "—")}</b></div><div class="detail"><small>Vitesse</small><b>${item.speed_mbps || "—"} Mb/s</b></div><div class="detail"><small>MTU</small><b>${item.mtu || 1500}</b></div></div></article>`).join("");
  const distinct = configuredNetworks.length >= 2 && !hasNetworkConflict;
  const eth0 = physical.find(item => item.name.toLowerCase() === "eth0");
  const eth1 = physical.find(item => item.name.toLowerCase() === "eth1");
  let networkNotice;
  if (distinct) {
    networkNotice = `<div class="recommendation"><h3>Configuration correcte : sous-réseaux distincts</h3><p>ETH0 utilise <b>${esc(eth0.network)}</b> et ETH1 utilise <b>${esc(eth1.network)}</b>. Les deux routes peuvent être sélectionnées séparément ; aucune modification n’est demandée.</p></div>`;
  } else if (hasNetworkConflict) {
    networkNotice = `<div class="recommendation warning"><h3>Conflit détecté : même sous-réseau</h3><p>ETH0 et ETH1 utilisent tous les deux <b>${esc(addressedNetworks[0])}</b>. Placez ETH1 sur un autre réseau, par exemple <b>192.168.2.131/24</b> avec la passerelle <b>192.168.2.1</b>.</p></div>`;
  } else {
    networkNotice = `<div class="recommendation warning"><h3>Deuxième sortie incomplète</h3><p>${eth1 ? "ETH1 n’a pas encore une adresse et une passerelle utilisables." : "ETH1 n’a pas été détectée."} La configuration proposée est <b>192.168.2.131/24</b> avec une passerelle <b>192.168.2.1</b>.</p></div>`;
  }
  $("#content").innerHTML = `<div class="split"><div><div class="toolbar"><div><h2>Interfaces gérées</h2><p>Seules ETH0, ETH1 et Tailscale peuvent être utilisées par DualRoute.</p></div><button class="secondary" id="rediscover">↻ Actualiser</button></div><div class="network-stack">${cards || `<div class="recommendation warning"><h3>Aucune interface gérée</h3><p>Vérifiez network_mode: host dans Compose.</p></div>`}</div>${networkNotice}</div>
    <section class="panel" style="margin-top:0"><div class="panel-head"><div><h2>Configuration ETH1</h2><p>Tester avant d’appliquer</p></div></div><form id="network-form" class="config-form"><label>Interface<select name="interface"><option value="eth1">ETH1</option></select></label><div class="form-grid"><label>Adresse IP<input name="address" value="${esc(proposedAddress)}" required></label><label>Préfixe<input name="prefix" type="number" value="${configured.prefix || 24}" min="1" max="32"></label></div><label>Passerelle<input name="gateway" value="${esc(proposedGateway)}" required></label><div class="form-grid"><label>DNS<input name="dns" value="${esc(proposedGateway)}, 1.1.1.1"></label><label>MTU<input name="mtu" type="number" value="${configured.mtu || 1500}"></label></div><div class="form-actions"><button type="button" class="secondary" id="test-network">▷ Tester</button><button type="submit" class="primary">Appliquer</button></div><pre id="network-result" class="code-preview">Aucun test lancé.</pre></form></section></div>
    <section class="panel"><div class="panel-head"><div><h2>Toutes les interfaces détectées</h2><p>${managed.length} gérées • ${detectedOnly.length} observées seulement. Les interfaces Docker, bridges et virtuelles ne sont jamais modifiées.</p></div></div><div class="table-scroll"><table class="data-table"><thead><tr><th>Interface</th><th>Périmètre</th><th>Type</th><th>État</th><th>Adresse</th><th>Passerelle</th><th>Sous-réseau</th><th>Lien</th></tr></thead><tbody>${interfaces.map(item => `<tr><td><b>${esc(item.name)}</b></td><td><span class="badge ${item.managed ? "blue" : "amber"}">${item.managed ? "Gérée" : "Observation seule"}</span></td><td>${esc(item.kind || "—")}</td><td><span class="badge ${item.up ? "green" : "red"}">${item.up ? "Active" : "Inactive"}</span></td><td>${esc(item.address || "—")}</td><td>${esc(item.gateway || "—")}</td><td>${esc(item.network || "—")}</td><td data-sort="${item.speed_mbps || 0}">${item.speed_mbps || 0} Mb/s</td></tr>`).join("") || tableEmpty(8, "Aucune donnée")}</tbody></table></div></section>`;
  $("#rediscover").onclick = () => refreshSnapshot(true);
  $("#test-network").onclick = () => testNetwork(false);
  $("#network-form").onsubmit = event => { event.preventDefault(); testNetwork(true); };
}

function renderEvents() {
  const rows = appState.events.map(event => `<tr><td data-sort="${new Date(event.ts).getTime()}">${new Date(event.ts).toLocaleString("fr-FR")}</td><td><span class="badge ${event.level === "error" ? "red" : event.level === "warning" ? "amber" : "green"}">${esc(event.level)}</span></td><td>${esc(event.source)}</td><td>${esc(event.message)}</td><td>${esc(JSON.stringify(event.details || {}))}</td></tr>`).join("");
  $("#content").innerHTML = `<div class="toolbar"><div><h2>Journal système</h2><p>Modifications de règles, configuration réseau et erreurs d’application.</p></div><button class="secondary" id="refresh-events">↻ Actualiser</button></div><section class="panel"><div class="table-scroll tall"><table class="data-table"><thead><tr><th style="width:180px">Date</th><th style="width:100px">Niveau</th><th style="width:120px">Source</th><th style="width:32%">Message</th><th>Détails</th></tr></thead><tbody>${rows || tableEmpty(5, "Aucun événement")}</tbody></table></div></section>`;
  $("#refresh-events").onclick = loadEvents;
}

function renderSettings() {
  const settings = appState.snapshot.settings;
  $("#content").innerHTML = `<div class="settings-grid"><section class="setting-card"><h3>Application des règles</h3><p>Le mode actif autorise DualRoute à modifier nftables et les tables de routage Linux. Commencez par une prévisualisation.</p><label class="toggle-row"><span><b>Mode actif</b><small>${settings.enforcement_enabled ? "Les règles peuvent être appliquées" : "Prévisualisation uniquement"}</small></span><input id="enforcement" type="checkbox" ${settings.enforcement_enabled ? "checked" : ""}></label><div class="form-actions" style="margin-top:14px"><button class="secondary" id="preview-settings">Prévisualiser</button><button class="primary" id="apply-settings">Appliquer les règles</button></div></section>
    <section class="setting-card"><h3>Affichage du trafic</h3><p>Choisissez l’unité utilisée pour tous les débits. 8 Mb/s correspondent à 1 Mo/s.</p><div class="form-grid"><label>Unité des débits<select id="display-rate-unit"><option value="mbps" ${settings.display_rate_unit !== "MBps" ? "selected" : ""}>Mb/s — mégabits par seconde</option><option value="MBps" ${settings.display_rate_unit === "MBps" ? "selected" : ""}>Mo/s — mégaoctets par seconde</option></select></label><label class="toggle-row compact"><span><b>Tableau de bord limité aux interfaces gérées</b><small>Afficher uniquement ETH0, ETH1 et Tailscale</small></span><input id="dashboard-managed-only" type="checkbox" ${settings.dashboard_managed_only !== false ? "checked" : ""}></label></div></section>
    <section class="setting-card"><h3>Conservation des mesures</h3><p>Le volume SQLite augmente avec la fréquence et la durée de conservation.</p><div class="form-grid"><label>Intervalle (secondes)<input id="sample-interval" type="number" min="1" max="60" value="${settings.sample_interval_seconds || 2}"></label><label>Conservation (jours)<input id="retention-days" type="number" min="1" max="365" value="${settings.retention_days || 30}"></label></div><button class="primary" id="save-settings" style="margin-top:14px">Enregistrer tous les paramètres</button></section>
    <section class="setting-card" style="grid-column:1/-1"><h3>Prévisualisation technique</h3><p>Cette zone affiche les commandes et la table nftables générées sans modifier le NAS.</p><pre id="routing-preview" class="code-preview">Cliquez sur Prévisualiser pour générer le plan.</pre></section></div>`;
  $("#save-settings").onclick = saveSettings;
  $("#preview-settings").onclick = previewRouting;
  $("#apply-settings").onclick = applyRouting;
}

function renderPage(preserveScroll = false) {
  const scrollState = preserveScroll ? captureScrollState() : null;
  const [title, subtitle] = pageMeta[appState.page];
  $("#page-title").textContent = title;
  $("#page-subtitle").textContent = subtitle;
  $$("#nav button").forEach(button => button.classList.toggle("active", button.dataset.page === appState.page));
  const renderers = { dashboard: renderDashboard, apps: renderApps, rules: renderRules, traffic: renderTraffic, network: renderNetwork, events: renderEvents, settings: renderSettings };
  renderers[appState.page]();
  enhanceTables();
  restoreScrollState(scrollState);
}

function drawTrafficChart(canvas) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, rect.width * ratio);
  canvas.height = Math.max(1, rect.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const width = rect.width; const height = rect.height;
  const pad = { left: 48, right: 12, top: 12, bottom: 25 };
  const innerW = width - pad.left - pad.right; const innerH = height - pad.top - pad.bottom;
  const interfaces = appState.page === "dashboard" ? dashboardInterfaces() : managedInterfaces();
  const series = interfaces.map(item => appState.history.filter(row => row.interface === item.name).slice(-240));
  const values = series.flatMap(rows => rows.flatMap(row => [Number(row.rx_bps), Number(row.tx_bps)]));
  const max = Math.max(...values, 1) * 1.12;
  ctx.font = "10px system-ui"; ctx.fillStyle = "#7f94ae"; ctx.strokeStyle = "#263a53"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + innerH * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.fillText(formatRate(max * (1 - i / 4)), 0, y + 3);
  }
  interfaces.forEach((item, index) => {
    const rows = series[index];
    [["rx_bps", interfaceColor(index), 2], ["tx_bps", index === 0 ? "#63b0ff" : index === 1 ? "#7cebe6" : "#c0afff", 1]].forEach(([key, color, lineWidth]) => {
      ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = lineWidth;
      rows.forEach((row, rowIndex) => {
        const x = pad.left + (rows.length <= 1 ? 0 : rowIndex / (rows.length - 1)) * innerW;
        const y = pad.top + innerH - (Number(row[key]) / max) * innerH;
        if (rowIndex === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
  });
  if (!values.length) { ctx.fillStyle = "#91a5bf"; ctx.fillText("Collecte des premières mesures…", pad.left + 20, pad.top + innerH / 2); }
}

function bindPageLinks() {
  $$('[data-page-link]').forEach(button => button.onclick = () => setPage(button.dataset.pageLink));
}

function setPage(page) {
  appState.page = page;
  if (page === "events") loadEvents();
  else if (page === "traffic") Promise.all([ensureHistory(appState.trafficHours, true), refreshFlows()]).then(() => { if (appState.page === "traffic") renderPage(); }).catch(error => toast(error.message, true));
  else renderPage();
}

function populateRuleForm(rule, appId) {
  const form = $("#rule-form");
  const interfaces = managedInterfaces().filter(item => ["eth0", "eth1"].includes(item.name.toLowerCase()));
  form.elements.app_id.innerHTML = appState.snapshot.apps.map(app => `<option value="${esc(app.id)}">${esc(app.name)}</option>`).join("");
  const interfaceOptions = interfaces.map(item => `<option value="${esc(item.name)}">${esc(item.name.toUpperCase())}</option>`).join("");
  form.elements.primary_interface.innerHTML = interfaceOptions;
  form.elements.fallback_interface.innerHTML = `<option value="">Aucune</option>${interfaceOptions}`;
  form.reset();
  form.elements.id.value = rule?.id || "";
  form.elements.app_id.value = rule?.app_id || appId || appState.snapshot.apps[0]?.id || "";
  form.elements.direction.value = rule?.direction || "both";
  form.elements.strategy.value = rule?.strategy || "prefer";
  form.elements.primary_interface.value = rule?.primary_interface || interfaces[0]?.name || "";
  form.elements.fallback_interface.value = rule?.fallback_interface || interfaces[1]?.name || "";
  form.elements.qos.value = rule?.qos || 3;
  form.elements.download_limit_mbps.value = rule?.download_limit_mbps || "";
  form.elements.upload_limit_mbps.value = rule?.upload_limit_mbps || "";
  form.elements.ports.value = (rule?.ports || []).join(", ");
  form.elements.enabled.checked = rule?.enabled ?? true;
  $("#rule-title").textContent = rule ? "Modifier la règle" : "Nouvelle règle";
  updateRoutePreview();
}

function openRuleDialog(ruleId = null, appId = null, strategy = null) {
  const rule = appState.snapshot.rules.find(item => item.id === ruleId);
  populateRuleForm(rule, appId);
  if (strategy && !rule) {
    $("#rule-form").elements.strategy.value = strategy;
    updateRoutePreview();
  }
  $("#rule-dialog").showModal();
}

function updateRoutePreview() {
  const form = $("#rule-form");
  const primary = form.elements.primary_interface.value || "Interface principale";
  const fallback = form.elements.fallback_interface.value;
  const strategy = form.elements.strategy.value;
  let value = strategyLabels[strategy];
  if (strategy === "prefer" && fallback) value = `${primary.toUpperCase()} → ${fallback.toUpperCase()} en cas de panne`;
  if (strategy === "balance" && fallback) value = `${primary.toUpperCase()} ⇄ ${fallback.toUpperCase()} • répartition 50/50`;
  if (strategy === "force") value = `Tout le trafic utilisera ${primary.toUpperCase()}`;
  $("#route-preview").textContent = value;
  $("#strategy-help").textContent = strategyDescriptions[strategy] || "";
  form.elements.fallback_interface.closest("label").classList.toggle("field-disabled", !["prefer", "balance"].includes(strategy));
}

async function saveRule(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const app = appState.snapshot.apps.find(item => item.id === form.elements.app_id.value);
  const id = form.elements.id.value;
  const numberOrNull = value => value ? Number(value) : null;
  const payload = {
    app_id: form.elements.app_id.value,
    app_name: app?.name || form.elements.app_id.value,
    direction: form.elements.direction.value,
    strategy: form.elements.strategy.value,
    primary_interface: form.elements.primary_interface.value,
    fallback_interface: form.elements.fallback_interface.value || null,
    qos: Number(form.elements.qos.value), enabled: form.elements.enabled.checked,
    download_limit_mbps: numberOrNull(form.elements.download_limit_mbps.value),
    upload_limit_mbps: numberOrNull(form.elements.upload_limit_mbps.value),
    ports: form.elements.ports.value.split(",").map(value => Number(value.trim())).filter(Boolean),
    weight_primary: 50,
  };
  try {
    await api(id ? `/api/rules/${id}` : "/api/rules", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    $("#rule-dialog").close(); toast("Règle enregistrée"); await refreshSnapshot(true);
  } catch (error) { toast(error.message, true); }
}

async function removeRule(id) {
  if (!window.confirm("Supprimer cette règle ?")) return;
  try { await api(`/api/rules/${id}`, { method: "DELETE" }); toast("Règle supprimée"); await refreshSnapshot(true); }
  catch (error) { toast(error.message, true); }
}

function networkPayload() {
  const form = $("#network-form");
  return {
    interface: form.elements.interface.value, address: form.elements.address.value.trim(),
    prefix: Number(form.elements.prefix.value), gateway: form.elements.gateway.value.trim(),
    dns: form.elements.dns.value.split(",").map(value => value.trim()).filter(Boolean),
    mtu: Number(form.elements.mtu.value),
  };
}

async function testNetwork(apply) {
  const output = $("#network-result");
  const payload = networkPayload();
  output.textContent = "Test en cours…";
  try {
    if (!apply) {
      const result = await api("/api/network/test", { method: "POST", body: JSON.stringify(payload) });
      output.textContent = JSON.stringify(result, null, 2);
      toast(result.ok ? "Passerelle joignable" : result.message, !result.ok);
      return;
    }
    const preview = await api("/api/network/configure", { method: "POST", body: JSON.stringify({ config: payload, dry_run: true, confirm: "" }) });
    if (!window.confirm(`Appliquer ${payload.address}/${payload.prefix} à ${payload.interface} ? Cette action modifie le réseau du NAS.`)) return;
    const result = await api("/api/network/configure", {
      method: "POST",
      body: JSON.stringify({ config: payload, dry_run: false, confirm: `APPLY ${payload.interface}` }),
    });
    output.textContent = JSON.stringify(result, null, 2);
    toast("Configuration réseau appliquée");
    await refreshSnapshot(true);
  } catch (error) {
    output.textContent = error.message; toast(error.message, true);
  }
}

async function previewRouting() {
  try {
    const result = await api("/api/routing/apply", { method: "POST", body: JSON.stringify({ dry_run: true, confirm: "" }) });
    const text = `${result.plan.join("\n")}\n\n${result.nft_script || ""}`;
    if (appState.page !== "settings") { setPage("settings"); await new Promise(resolve => setTimeout(resolve, 0)); }
    $("#routing-preview").textContent = text;
    toast("Plan généré sans modifier le NAS");
  } catch (error) { toast(error.message, true); }
}

async function saveSettings() {
  const payload = {
    enforcement_enabled: $("#enforcement").checked,
    sample_interval_seconds: Number($("#sample-interval").value),
    retention_days: Number($("#retention-days").value),
    display_rate_unit: $("#display-rate-unit").value,
    dashboard_managed_only: $("#dashboard-managed-only").checked,
  };
  try { await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) }); toast("Paramètres enregistrés"); await refreshSnapshot(true); }
  catch (error) { toast(error.message, true); }
}

async function applyRouting() {
  if (!$("#enforcement").checked) { toast("Activez et enregistrez d’abord le mode actif", true); return; }
  const confirm = window.prompt("Pour appliquer les règles au NAS, tapez APPLIQUER");
  if (confirm !== "APPLIQUER") return;
  try {
    const result = await api("/api/routing/apply", { method: "POST", body: JSON.stringify({ dry_run: false, confirm }) });
    $("#routing-preview").textContent = JSON.stringify(result.results, null, 2); toast("Règles appliquées");
  } catch (error) { toast(error.message, true); }
}

async function loadEvents() {
  try { appState.events = await api("/api/events?limit=500"); if (appState.page === "events") renderPage(true); }
  catch (error) { toast(error.message, true); }
}

$$("#nav button").forEach(button => button.onclick = () => setPage(button.dataset.page));
$$('[data-close]').forEach(button => button.onclick = () => $("#rule-dialog").close());
$("#rule-form").addEventListener("submit", saveRule);
$("#rule-form").addEventListener("input", updateRoutePreview);
window.addEventListener("resize", () => { if (appState.page === "dashboard" || appState.page === "traffic") drawTrafficChart($("#traffic-chart")); });

refreshSnapshot(true).then(() => ensureHistory(1, true)).then(() => renderPage());
setInterval(() => refreshSnapshot(false), 2500);
