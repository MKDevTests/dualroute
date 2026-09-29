const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const appState = {
  page: "dashboard",
  snapshot: { interfaces: [], apps: [], rules: [], metrics: {}, settings: {}, warnings: [] },
  history: [],
  events: [],
  flows: [],
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
  balance: "Équilibrage",
  qos: "Priorité QoS",
};
const directionLabels = { in: "Entrant", out: "Sortant", both: "Les deux" };

function esc(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function formatRate(value) {
  const bps = Number(value || 0);
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gb/s`;
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mb/s`;
  if (bps >= 1e3) return `${(bps / 1e3).toFixed(1)} kb/s`;
  return `${Math.round(bps)} b/s`;
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

async function refreshSnapshot(forceRender = false) {
  try {
    appState.snapshotPolls += 1;
    const includeAppStats = forceRender || appState.snapshotPolls % 4 === 0;
    const previousStats = appState.snapshot.app_stats || {};
    const nextSnapshot = await api(`/api/snapshot?include_app_stats=${includeAppStats}`);
    if (!includeAppStats) nextSnapshot.app_stats = previousStats;
    appState.snapshot = nextSnapshot;
    $("#side-count").textContent = `${appState.snapshot.apps.length} applications • ${appState.snapshot.interfaces.length} interfaces`;
    $("#last-update").textContent = `Actualisé à ${new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    if (forceRender || !$("#rule-dialog").open) renderPage();
  } catch (error) {
    $("#last-update").textContent = "API indisponible";
    toast(error.message, true);
  }
}

async function ensureHistory(hours = 1, force = false) {
  if (!force && Date.now() - appState.lastHistoryFetch < 9000) return;
  appState.history = await api(`/api/traffic/history?hours=${hours}`);
  appState.lastHistoryFetch = Date.now();
}

function interfaceCard(item, index) {
  const metric = appState.snapshot.metrics[item.name] || {};
  return `<article class="interface-card" style="--accent:${interfaceColor(index)}">
    <div class="card-head">
      <span class="nic-icon">↔</span><h3>${esc(item.name.toUpperCase())}</h3>
      <span class="badge ${item.up ? "green" : "red"}"><span class="status-dot"></span>${item.up ? "Opérationnel" : "Hors ligne"}</span>
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
  const { interfaces, apps, rules, warnings } = appState.snapshot;
  const cards = interfaces.slice(0, 3).map(interfaceCard).join("");
  const rows = apps.map(app => {
    const rule = rules.find(item => item.app_id === app.id);
    return `<tr>
      <td><div class="app-cell"><span class="app-icon">${app.type === "docker" ? "◇" : "▣"}</span>${esc(app.name)}</div></td>
      <td>${rule ? `<span class="badge blue">${esc(strategyLabels[rule.strategy])}</span>` : `<span class="muted">Automatique</span>`}</td>
      <td>${rule ? interfaceBadge(rule.primary_interface) : "—"}</td>
      <td>${formatBytes((appState.snapshot.app_stats[app.id] || {}).rx_bytes)}</td>
      <td>${formatBytes((appState.snapshot.app_stats[app.id] || {}).tx_bytes)}</td>
      <td><span class="badge green"><span class="status-dot"></span>${esc(app.status)}</span></td>
    </tr>`;
  }).join("");
  $("#content").innerHTML = `
    ${warnings.length ? `<div class="recommendation warning"><h3>Configuration à compléter</h3><p>${warnings.map(esc).join(" ")}</p></div>` : ""}
    <div class="cards">${cards || `<div class="recommendation warning"><h3>Aucune interface détectée</h3><p>La découverte réseau nécessite le conteneur en mode réseau hôte.</p></div>`}</div>
    <section class="panel"><div class="panel-head"><div><h2>Trafic en temps réel</h2><p>Débits descendants et montants par interface</p></div><div class="legend">${interfaces.slice(0,3).map((item, index) => `<span style="--legend:${interfaceColor(index)}">${esc(item.name)}</span>`).join("")}</div></div><div class="chart-wrap"><canvas id="traffic-chart"></canvas></div></section>
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
      <td><div class="row-actions"><button data-rule-app="${esc(app.id)}">Créer une règle</button></div></td>
    </tr>`;
  }).join("");
  $("#content").innerHTML = `<div class="toolbar"><div><h2>Applications découvertes</h2><p>Les adresses Docker sont actualisées automatiquement après chaque redémarrage de conteneur.</p></div><button class="secondary" id="refresh-apps">↻ Actualiser</button></div>
    <section class="panel"><div class="table-scroll tall"><table class="data-table"><thead><tr><th style="width:18%">Application</th><th>Type</th><th style="width:20%">Image</th><th>Adresses IP</th><th>Réseaux</th><th>Règle</th><th>Statut</th><th>Actions</th></tr></thead><tbody>${rows || tableEmpty(8, "Aucun conteneur Docker détecté")}</tbody></table></div><div class="table-footer"><span>${apps.length} services</span><span>Socket Docker monté en lecture seule</span></div></section>`;
  $("#refresh-apps").onclick = () => refreshSnapshot(true);
  $$('[data-rule-app]').forEach(button => button.onclick = () => openRuleDialog(null, button.dataset.ruleApp));
}

function renderRules() {
  const rules = appState.snapshot.rules;
  const rows = rules.map((rule, index) => `<tr>
    <td>${index + 1}</td><td><div class="app-cell"><span class="app-icon">${rule.app_id === "service:smb" ? "▣" : "◇"}</span>${esc(rule.app_name)}</div></td>
    <td>${esc(directionLabels[rule.direction])}</td><td><span class="badge blue">${esc(strategyLabels[rule.strategy])}</span></td>
    <td>${interfaceBadge(rule.primary_interface)}</td><td>${rule.fallback_interface ? interfaceBadge(rule.fallback_interface) : "—"}</td>
    <td>${rule.qos}/5</td><td><span class="badge ${rule.enabled ? "green" : "amber"}"><span class="status-dot"></span>${rule.enabled ? "Activée" : "Suspendue"}</span></td>
    <td><div class="row-actions"><button data-edit-rule="${rule.id}">Modifier</button><button data-delete-rule="${rule.id}">Supprimer</button></div></td>
  </tr>`).join("");
  $("#content").innerHTML = `<div class="toolbar"><div><h2>Règles de routage</h2><p>Ordre, stratégie, bascule et priorité de chaque service.</p></div><div class="toolbar-actions"><button class="secondary" id="preview-rules">Prévisualiser</button><button class="primary" id="new-rule">＋ Nouvelle règle</button></div></div>
    <section class="panel"><div class="table-scroll tall"><table class="data-table"><thead><tr><th style="width:45px">#</th><th style="width:20%">Application ou service</th><th>Sens</th><th style="width:18%">Stratégie</th><th>Interface principale</th><th>Repli</th><th>QoS</th><th>État</th><th style="width:150px">Actions</th></tr></thead><tbody>${rows || tableEmpty(9, "Aucune règle. Créez la première règle pour une application.")}</tbody></table></div><div class="table-footer"><span>${rules.length} règles</span><span>Les modifications sont appliquées uniquement sur demande.</span></div></section>`;
  $("#new-rule").onclick = () => openRuleDialog();
  $("#preview-rules").onclick = previewRouting;
  $$('[data-edit-rule]').forEach(button => button.onclick = () => openRuleDialog(Number(button.dataset.editRule)));
  $$('[data-delete-rule]').forEach(button => button.onclick = () => removeRule(Number(button.dataset.deleteRule)));
}

function renderTraffic() {
  const interfaces = appState.snapshot.interfaces.slice(0, 3);
  const totalRx = interfaces.reduce((sum, item) => sum + Number((appState.snapshot.metrics[item.name] || {}).rx_bps || 0), 0);
  const totalTx = interfaces.reduce((sum, item) => sum + Number((appState.snapshot.metrics[item.name] || {}).tx_bps || 0), 0);
  const recent = appState.history.slice(-120).reverse();
  const rows = recent.map(sample => `<tr><td>${new Date(sample.ts).toLocaleTimeString("fr-FR")}</td><td>${interfaceBadge(sample.interface)}</td><td>Entrant</td><td>${formatRate(sample.rx_bps)}</td><td>Sortant</td><td>${formatRate(sample.tx_bps)}</td><td>${formatBytes(sample.rx_bytes)}</td><td>${formatBytes(sample.tx_bytes)}</td></tr>`).join("");
  $("#content").innerHTML = `<div class="kpi-grid">
    <div class="kpi" style="--accent:var(--teal)"><span class="kpi-icon">▥</span><div><small>Débit total</small><b>${formatRate(totalRx + totalTx)}</b></div></div>
    ${interfaces.map((item,index) => `<div class="kpi" style="--accent:${interfaceColor(index)}"><span class="kpi-icon">↔</span><div><small>${esc(item.name.toUpperCase())}</small><b>${formatRate(((appState.snapshot.metrics[item.name] || {}).rx_bps || 0) + ((appState.snapshot.metrics[item.name] || {}).tx_bps || 0))}</b></div></div>`).join("")}
    <div class="kpi" style="--accent:var(--green)"><span class="kpi-icon">◎</span><div><small>Applications</small><b>${appState.snapshot.apps.length}</b></div></div>
  </div>
  <section class="panel"><div class="panel-head"><div><h2>Trafic réseau</h2><p>Historique reçu et envoyé pour chaque interface</p></div><div class="filters"><button class="chip-button active" data-hours="1">1 h</button><button class="chip-button" data-hours="24">24 h</button><button class="chip-button" data-hours="168">7 j</button></div></div><div class="chart-wrap large"><canvas id="traffic-chart"></canvas></div></section>
  <section class="panel"><div class="panel-head"><div><h2>Échantillons récents</h2><p>Valeurs brutes conservées pour l’analyse</p></div><span class="muted">Réception ${formatRate(totalRx)} • Envoi ${formatRate(totalTx)}</span></div><div class="table-scroll"><table class="data-table"><thead><tr><th>Heure</th><th>Interface</th><th>Sens</th><th>Débit</th><th>Sens</th><th>Débit</th><th>Total reçu</th><th>Total envoyé</th></tr></thead><tbody>${rows || tableEmpty(8, "Collecte des premières mesures…")}</tbody></table></div></section>`;
  drawTrafficChart($("#traffic-chart"));
  $$('[data-hours]').forEach(button => button.onclick = async () => {
    $$('[data-hours]').forEach(item => item.classList.toggle("active", item === button));
    await ensureHistory(Number(button.dataset.hours), true);
    renderTraffic();
  });
}

function renderNetwork() {
  const interfaces = appState.snapshot.interfaces;
  const physical = interfaces.filter(item => ["eth0", "eth1"].includes(item.name.toLowerCase()));
  const configured = physical.find(item => item.name.toLowerCase() === "eth1") || {};
  const configuredNetworks = physical.filter(item => item.network && item.address).map(item => item.network);
  const hasNetworkConflict = configuredNetworks.length >= 2 && new Set(configuredNetworks).size < configuredNetworks.length;
  const proposedAddress = hasNetworkConflict ? "192.168.2.131" : (configured.address || "192.168.2.131");
  const proposedGateway = hasNetworkConflict ? "192.168.2.1" : (configured.gateway || "192.168.2.1");
  const cards = interfaces.map((item, index) => `<article class="network-card" style="--accent:${interfaceColor(index)}"><div class="network-card-head"><span class="nic-icon">↔</span><h3>${esc(item.name.toUpperCase())}</h3><span class="badge ${item.up ? "green" : "red"}">${item.up ? "Connectée" : "Hors ligne"}</span></div><div class="network-details"><div class="detail"><small>Adresse IP</small><b>${esc(item.address || "Non configurée")}${item.prefix ? `/${item.prefix}` : ""}</b></div><div class="detail"><small>Passerelle</small><b>${esc(item.gateway || "—")}</b></div><div class="detail"><small>Sous-réseau</small><b>${esc(item.network || "—")}</b></div><div class="detail"><small>Vitesse</small><b>${item.speed_mbps || "—"} Mb/s</b></div><div class="detail"><small>MTU</small><b>${item.mtu || 1500}</b></div></div></article>`).join("");
  const distinct = configuredNetworks.length >= 2 && !hasNetworkConflict;
  $("#content").innerHTML = `<div class="split"><div><div class="toolbar"><div><h2>Interfaces détectées</h2><p>ETH0, ETH1 et Tailscale uniquement.</p></div><button class="secondary" id="rediscover">↻ Actualiser</button></div><div class="network-stack">${cards || `<div class="recommendation warning"><h3>Aucune interface</h3><p>Vérifiez network_mode: host dans Compose.</p></div>`}</div><div class="recommendation ${distinct ? "" : "warning"}"><h3>${distinct ? "Sous-réseaux distincts détectés" : "Sous-réseaux distincts recommandés"}</h3><p>ETH0 utilise normalement 192.168.1.0/24. Configurez ETH1 sur 192.168.2.0/24 avec une passerelle 192.168.2.1 pour éviter les conflits de routage et les réponses asymétriques.</p></div></div>
    <section class="panel" style="margin-top:0"><div class="panel-head"><div><h2>Configuration ETH1</h2><p>Tester avant d’appliquer</p></div></div><form id="network-form" class="config-form"><label>Interface<select name="interface"><option value="eth1">ETH1</option></select></label><div class="form-grid"><label>Adresse IP<input name="address" value="${esc(proposedAddress)}" required></label><label>Préfixe<input name="prefix" type="number" value="${configured.prefix || 24}" min="1" max="32"></label></div><label>Passerelle<input name="gateway" value="${esc(proposedGateway)}" required></label><div class="form-grid"><label>DNS<input name="dns" value="${esc(proposedGateway)}, 1.1.1.1"></label><label>MTU<input name="mtu" type="number" value="${configured.mtu || 1500}"></label></div><div class="form-actions"><button type="button" class="secondary" id="test-network">▷ Tester</button><button type="submit" class="primary">Appliquer</button></div><pre id="network-result" class="code-preview">Aucun test lancé.</pre></form></section></div>
    <section class="panel"><div class="panel-head"><div><h2>Journal de détection et de configuration</h2><p>Les événements réseau apparaissent ici après chaque test ou application.</p></div></div><div class="table-scroll"><table class="data-table"><thead><tr><th>Interface</th><th>État</th><th>Adresse</th><th>Passerelle</th><th>Sous-réseau</th><th>Lien</th></tr></thead><tbody>${interfaces.map(item => `<tr><td>${esc(item.name)}</td><td><span class="badge ${item.up ? "green" : "red"}">${item.up ? "Active" : "Inactive"}</span></td><td>${esc(item.address || "—")}</td><td>${esc(item.gateway || "—")}</td><td>${esc(item.network || "—")}</td><td>${item.speed_mbps || 0} Mb/s</td></tr>`).join("") || tableEmpty(6, "Aucune donnée")}</tbody></table></div></section>`;
  $("#rediscover").onclick = () => refreshSnapshot(true);
  $("#test-network").onclick = () => testNetwork(false);
  $("#network-form").onsubmit = event => { event.preventDefault(); testNetwork(true); };
}

function renderEvents() {
  const rows = appState.events.map(event => `<tr><td>${new Date(event.ts).toLocaleString("fr-FR")}</td><td><span class="badge ${event.level === "error" ? "red" : event.level === "warning" ? "amber" : "green"}">${esc(event.level)}</span></td><td>${esc(event.source)}</td><td>${esc(event.message)}</td><td>${esc(JSON.stringify(event.details || {}))}</td></tr>`).join("");
  $("#content").innerHTML = `<div class="toolbar"><div><h2>Journal système</h2><p>Modifications de règles, configuration réseau et erreurs d’application.</p></div><button class="secondary" id="refresh-events">↻ Actualiser</button></div><section class="panel"><div class="table-scroll tall"><table class="data-table"><thead><tr><th style="width:180px">Date</th><th style="width:100px">Niveau</th><th style="width:120px">Source</th><th style="width:32%">Message</th><th>Détails</th></tr></thead><tbody>${rows || tableEmpty(5, "Aucun événement")}</tbody></table></div></section>`;
  $("#refresh-events").onclick = loadEvents;
}

function renderSettings() {
  const settings = appState.snapshot.settings;
  $("#content").innerHTML = `<div class="settings-grid"><section class="setting-card"><h3>Application des règles</h3><p>Le mode actif autorise DualRoute à modifier nftables et les tables de routage Linux. Commencez par une prévisualisation.</p><label class="toggle-row"><span><b>Mode actif</b><small>${settings.enforcement_enabled ? "Les règles peuvent être appliquées" : "Prévisualisation uniquement"}</small></span><input id="enforcement" type="checkbox" ${settings.enforcement_enabled ? "checked" : ""}></label><div class="form-actions" style="margin-top:14px"><button class="secondary" id="preview-settings">Prévisualiser</button><button class="primary" id="apply-settings">Appliquer les règles</button></div></section>
    <section class="setting-card"><h3>Conservation des mesures</h3><p>Le volume SQLite augmente avec la fréquence et la durée de conservation.</p><div class="form-grid"><label>Intervalle (secondes)<input id="sample-interval" type="number" min="1" max="60" value="${settings.sample_interval_seconds || 2}"></label><label>Conservation (jours)<input id="retention-days" type="number" min="1" max="365" value="${settings.retention_days || 30}"></label></div><button class="primary" id="save-settings" style="margin-top:14px">Enregistrer</button></section>
    <section class="setting-card" style="grid-column:1/-1"><h3>Prévisualisation technique</h3><p>Cette zone affiche les commandes et la table nftables générées sans modifier le NAS.</p><pre id="routing-preview" class="code-preview">Cliquez sur Prévisualiser pour générer le plan.</pre></section></div>`;
  $("#save-settings").onclick = saveSettings;
  $("#preview-settings").onclick = previewRouting;
  $("#apply-settings").onclick = applyRouting;
}

function renderPage() {
  const [title, subtitle] = pageMeta[appState.page];
  $("#page-title").textContent = title;
  $("#page-subtitle").textContent = subtitle;
  $$("#nav button").forEach(button => button.classList.toggle("active", button.dataset.page === appState.page));
  const renderers = { dashboard: renderDashboard, apps: renderApps, rules: renderRules, traffic: renderTraffic, network: renderNetwork, events: renderEvents, settings: renderSettings };
  renderers[appState.page]();
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
  const interfaces = appState.snapshot.interfaces.slice(0, 3);
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
  else if (page === "traffic") ensureHistory(1, true).then(renderPage);
  else renderPage();
}

function populateRuleForm(rule, appId) {
  const form = $("#rule-form");
  const interfaces = appState.snapshot.interfaces.slice(0, 2);
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

function openRuleDialog(ruleId = null, appId = null) {
  const rule = appState.snapshot.rules.find(item => item.id === ruleId);
  populateRuleForm(rule, appId);
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
  try { appState.events = await api("/api/events?limit=500"); if (appState.page === "events") renderEvents(); }
  catch (error) { toast(error.message, true); }
}

$$("#nav button").forEach(button => button.onclick = () => setPage(button.dataset.page));
$$('[data-close]').forEach(button => button.onclick = () => $("#rule-dialog").close());
$("#rule-form").addEventListener("submit", saveRule);
$("#rule-form").addEventListener("input", updateRoutePreview);
window.addEventListener("resize", () => { if (appState.page === "dashboard" || appState.page === "traffic") drawTrafficChart($("#traffic-chart")); });

refreshSnapshot(true).then(() => ensureHistory(1, true)).then(() => renderPage());
setInterval(() => refreshSnapshot(false), 2500);
