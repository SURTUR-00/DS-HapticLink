// Lógica de la interfaz. Habla con Python por window.pywebview.api (ver app/main.py, clase Api).
const STATE_COLORS = {
  listo: "#3b82f6", en_juego: "#22c55e", esperando_control: "#f59e0b", cable: "#f59e0b",
  iniciando: "#94a3b8", detenido: "#64748b", falta_configurar: "#ef4444", problema: "#ef4444",
};
const LIGHT_PRESETS = [
  [0, 0, 128], [0, 90, 255], [0, 200, 255], [0, 200, 80], [255, 200, 0],
  [255, 90, 0], [255, 0, 60], [200, 0, 255], [255, 255, 255],
];

const $ = (id) => document.getElementById(id);
let api = null;
let settings = {};
let lastStatus = null;
let logVisible = false;

function hex([r, g, b]) {
  return "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
}
function rgb(hexStr) {
  const n = parseInt(hexStr.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// ------------------------------------------------------------------ ajustes
function renderSettings() {
  $("haptics-gain").value = Math.round(settings.haptics_gain * 100);
  $("haptics-gain-value").textContent = Math.round(settings.haptics_gain * 100) + "%";
  $("speaker-boost").value = settings.speaker_boost;
  $("speaker-boost-value").textContent = "+" + settings.speaker_boost;
  $("audio-enabled").checked = settings.audio_enabled;
  $("mic-enabled").checked = settings.mic_enabled;
  $("autostart").checked = settings.autostart;
  $("start-minimized").checked = settings.start_minimized;
  $("notifications").checked = settings.notifications;
  $("language").value = settings.language;
  renderSwatches();
}

function renderSwatches() {
  const box = $("swatches");
  box.innerHTML = "";
  const current = hex(settings.lightbar_color);
  let matched = false;
  for (const color of LIGHT_PRESETS) {
    const el = document.createElement("div");
    el.className = "swatch";
    el.style.background = hex(color);
    if (hex(color) === current) { el.classList.add("selected"); matched = true; }
    el.onclick = () => setSetting("lightbar_color", color);
    box.appendChild(el);
  }
  const custom = document.createElement("label");
  custom.className = "swatch custom" + (matched ? "" : " selected");
  const input = document.createElement("input");
  input.type = "color";
  input.value = current;
  input.onchange = () => setSetting("lightbar_color", rgb(input.value));
  custom.appendChild(input);
  box.appendChild(custom);
}

async function setSetting(key, value) {
  settings[key] = await api.set_setting(key, value);
  if (key === "language") { LANG = settings.language; applyI18n(); }
  renderSettings();
  if (lastStatus) renderStatus(lastStatus);
}

function bindSettings() {
  $("haptics-gain").oninput = (e) => { $("haptics-gain-value").textContent = e.target.value + "%"; };
  $("haptics-gain").onchange = (e) => setSetting("haptics_gain", e.target.value / 100);
  $("speaker-boost").oninput = (e) => { $("speaker-boost-value").textContent = "+" + e.target.value; };
  $("speaker-boost").onchange = (e) => setSetting("speaker_boost", Number(e.target.value));
  $("audio-enabled").onchange = (e) => setSetting("audio_enabled", e.target.checked);
  $("mic-enabled").onchange = (e) => setSetting("mic_enabled", e.target.checked);
  $("autostart").onchange = (e) => setSetting("autostart", e.target.checked);
  $("start-minimized").onchange = (e) => setSetting("start_minimized", e.target.checked);
  $("notifications").onchange = (e) => setSetting("notifications", e.target.checked);
  $("language").onchange = (e) => setSetting("language", e.target.value);
}

// ------------------------------------------------------------------ estado
function renderStatus(s) {
  lastStatus = s;
  const state = s.state;
  document.documentElement.style.setProperty("--state", STATE_COLORS[state] || STATE_COLORS.problema);
  $("state-text").textContent = tr("state." + state);

  const un = s.uninstall || {};
  const needsSetup = !un.view && (!s.prerequisites.usbip || !s.prerequisites.task || s.setup.running);
  $("view-uninstall").hidden = !un.view;
  $("view-setup").hidden = !needsSetup;
  $("view-main").hidden = needsSetup || !!un.view;
  if (un.view) renderUninstall(un);
  if (needsSetup) renderSetup(s);

  const c = s.controller || {};
  // Batería
  const showBattery = c.connected && c.battery != null && !un.view;
  $("battery").hidden = !showBattery;
  if (showBattery) {
    $("battery-fill").style.width = c.battery + "%";
    $("battery-fill").style.background = c.battery <= 15 ? "var(--bad)" : c.battery <= 35 ? "var(--warn)" : "var(--ok)";
    const extra = c.battery_state === "cargando" ? " ⚡" : "";
    $("battery-text").textContent = c.battery + "%" + extra;
  }

  // Tarjeta principal
  let title, help = "";
  if (state === "cable") { title = tr("state.cable"); help = tr("hero.cable.help"); }
  else if (c.connected) {
    title = c.name ? `${c.name}` : tr("hero.connected");
    if (c.needs_cable) help = tr("hero.needs_cable", { model: c.model || "DualSense" });
  }
  else if (c.blocked) { title = tr("hero.blocked"); help = tr("hero.blocked.help"); }
  else if (c.held) { title = tr("hero.held"); help = tr("hero.held.help"); }
  else if (state === "detenido") { title = tr("state.detenido"); }
  else { title = tr("hero.waiting"); help = tr("hero.waiting.help"); }
  // Resultado de una captura por cable reciente (10 s)
  const cap = s.capture || {};
  if (cap.result && Date.now() / 1000 - cap.time < 10) {
    help = (help ? help + " " : "") + tr(cap.result === "ok" ? "capture.ok" : "capture.error");
    if (cap.result === "ok") controllersKey = "";
  }
  $("hero-title").textContent = title;
  $("hero-help").textContent = help;
  $("btn-bt").hidden = !!c.connected || state === "detenido";
  $("btn-test").disabled = !c.connected;
  $("btn-pause").textContent = state === "detenido" ? tr("hero.resume") : tr("hero.pause");

  const badges = [];
  if (c.connected) {
    if (c.exclusive) badges.push([tr("badge.exclusive"), true]);
    if (s.game_active) badges.push([tr("badge.game"), true]);
    badges.push([c.headphones ? tr("badge.headphones") : tr("badge.speaker"), false]);
    if (c.mic_muted) badges.push([tr("badge.muted"), false]);
    if (c.battery_state === "cargando") badges.push([tr("badge.charging"), false]);
    if (c.battery_state === "cargado") badges.push([tr("badge.full"), false]);
  }
  $("badges").innerHTML = "";
  for (const [text, on] of badges) {
    const el = document.createElement("span");
    el.className = "badge" + (on ? " on" : "");
    el.textContent = text;
    $("badges").appendChild(el);
  }

  const st = s.stats || {};
  $("stats").textContent = s.stats ? tr("stats", {
    bt: st.bt_in, out: st.out_reports, hap: st.haptic_reports, und: st.underruns,
    res: st.reserve_ms, con: st.connections,
  }) : "";
}

function renderSetup(s) {
  const chip = (el, ok) => {
    el.className = "chip " + (ok ? "ok" : "missing");
    el.textContent = tr(ok ? "setup.installed" : "setup.missing");
  };
  chip($("chip-usbip"), s.prerequisites.usbip);
  chip($("chip-task"), s.prerequisites.task);
  const setup = s.setup;
  $("btn-install").disabled = setup.running;
  $("btn-install").textContent = tr(setup.running ? "setup.working" : "setup.install");
  $("setup-progress").hidden = !setup.running && !setup.percent;
  $("setup-fill").style.width = (setup.percent || 0) + "%";
  let msg = setup.message || "";
  if (!setup.running && setup.ok === false) msg = setup.message === "uac_denied" ? tr("setup.uac_denied") : (msg || tr("setup.failed"));
  if (!setup.running && setup.ok === true) msg = tr("setup.done");
  $("setup-message").textContent = msg;
  // Instalación a medias: también se puede desinstalar desde aquí
  $("link-uninstall-setup").hidden = setup.running || !(s.prerequisites.usbip || s.prerequisites.task);
}

function renderUninstall(un) {
  const done = un.ok === true;
  $("un-confirm").hidden = done;
  $("un-done").hidden = !done;
  $("un-drivers").disabled = un.running;
  $("btn-un-start").hidden = done;
  $("btn-un-start").disabled = un.running;
  $("btn-un-start").textContent = tr(un.running ? "un.working" : un.ok === false ? "un.retry" : "un.start");
  $("btn-un-cancel").hidden = done || un.running;
  $("btn-un-folder").hidden = !done;
  $("btn-un-quit").hidden = !done;
  $("un-progress").hidden = done || (!un.running && !un.percent);
  $("un-fill").style.width = (un.percent || 0) + "%";
  let msg = un.running ? un.message || "" : "";
  if (!un.running && un.ok === false) msg = un.message === "uac_denied" ? tr("setup.uac_denied") : (un.message || tr("un.failed"));
  $("un-message").textContent = msg;
  if (done) {
    $("un-done-text").textContent = tr(un.drivers ? "un.done.text_restart" : "un.done.text");
    $("un-folder").textContent = un.folder || "";
  }
}

// ------------------------------------------------------------------ Mis controles
const HAPTIC_CHOICES = [null, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0];
let controllersKey = "";
let pollCount = 0;

function timeAgo(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(LANG === "es" ? "es" : "en", { dateStyle: "medium", timeStyle: "short" });
}

function renderControllers(items) {
  const editing = document.activeElement && document.activeElement.closest && document.activeElement.closest("#controllers");
  const key = JSON.stringify(items) + LANG;
  if (editing || key === controllersKey) return;  // no redibujar mientras se edita un nombre
  controllersKey = key;
  const box = $("controllers");
  box.innerHTML = "";
  $("controllers-empty").hidden = items.length > 0;
  for (const c of items) {
    const row = document.createElement("div");
    row.className = "controller" + (c.connected ? " connected" : "");

    const name = document.createElement("input");
    name.className = "controller-name";
    name.value = c.name || "";
    name.maxLength = 40;
    name.onchange = () => api.rename_controller(c.mac, name.value);
    name.onkeydown = (e) => { if (e.key === "Enter") name.blur(); };

    const meta = document.createElement("div");
    meta.className = "muted small";
    const fw = c.firmware && c.firmware.firmware ? ` · FW ${c.firmware.firmware}` : "";
    const when = c.connected ? tr("controllers.connected") : tr("controllers.last_seen", { when: timeAgo(c.last_seen) });
    meta.textContent = `${c.model || "DualSense"}${fw} · ${c.mac} · ${when}`;

    const badges = document.createElement("div");
    badges.className = "badges";
    if (c.has_profile) badges.innerHTML += `<span class="badge on">${tr("controllers.profile_ok")}</span>`;
    if (c.needs_cable) badges.innerHTML += `<span class="badge warn">${tr("controllers.needs_cable")}</span>`;

    const info = document.createElement("div");
    info.className = "controller-info";
    info.append(name, meta, badges);

    // Ajustes propios
    const color = document.createElement("select");
    color.innerHTML = `<option value="">${tr("controllers.general")}</option>` +
      LIGHT_PRESETS.map((rgbv) => `<option value="${rgbv.join(",")}">● ${hex(rgbv)}</option>`).join("");
    const own = c.settings.lightbar_color;
    color.value = own ? own.join(",") : "";
    if (own && !color.value) {  // color propio que no está entre los predefinidos
      color.insertAdjacentHTML("beforeend", `<option value="${own.join(",")}">● ${hex(own)}</option>`);
      color.value = own.join(",");
    }
    color.style.color = own ? hex(own) : "";
    color.onchange = () => api.set_controller_setting(c.mac, "lightbar_color", color.value ? color.value.split(",").map(Number) : null);

    const haptics = document.createElement("select");
    haptics.innerHTML = HAPTIC_CHOICES.map((v) => `<option value="${v ?? ""}">${v == null ? tr("controllers.general") : Math.round(v * 100) + "%"}</option>`).join("");
    haptics.value = c.settings.haptics_gain ?? "";
    haptics.onchange = () => api.set_controller_setting(c.mac, "haptics_gain", haptics.value ? Number(haptics.value) : null);

    const forget = document.createElement("button");
    forget.className = "ghost danger";
    forget.textContent = tr("controllers.forget");
    forget.onclick = async () => {
      if (confirm(tr("controllers.forget_confirm"))) { await api.forget_controller(c.mac); controllersKey = ""; }
    };

    const settingsBox = document.createElement("div");
    settingsBox.className = "controller-settings";
    const label = (text, el) => { const l = document.createElement("label"); l.className = "small muted"; l.append(text, el); return l; };
    settingsBox.append(label(tr("controllers.color"), color), label(tr("controllers.haptics"), haptics), forget);

    row.append(info, settingsBox);
    box.appendChild(row);
  }
}

async function poll(once = false) {
  try {
    renderStatus(await api.get_status());
    if (!once && pollCount++ % 3 === 0) renderControllers(await api.get_controllers());
    if (!once && logVisible) $("log").textContent = (await api.get_logs()).join("\n");
  } catch (e) {
    console.error(e);
  }
  if (!once) setTimeout(poll, 1000);
}

// ------------------------------------------------------------------ inicio
async function init() {
  api = window.pywebview.api;
  const info = await api.get_info();
  $("version").textContent = `${info.name} ${info.version}`;
  settings = await api.get_settings();
  LANG = settings.language;
  applyI18n();
  renderSettings();
  bindSettings();

  $("btn-install").onclick = () => api.run_setup();
  $("btn-test").onclick = () => api.test_rumble();
  $("btn-bt").onclick = () => api.open_bluetooth_settings();
  $("btn-pause").onclick = async () => { await api.toggle_pause(); };
  $("btn-quit").onclick = () => api.quit();
  const openUninstall = async (e) => { if (e) e.preventDefault(); await api.show_uninstall(true); poll(true); };
  $("btn-uninstall").onclick = openUninstall;
  $("link-uninstall-setup").onclick = openUninstall;
  $("btn-un-cancel").onclick = async () => { await api.show_uninstall(false); poll(true); };
  $("btn-un-start").onclick = async () => { await api.run_uninstall($("un-drivers").checked); poll(true); };
  $("btn-un-folder").onclick = () => api.open_app_folder();
  $("btn-un-quit").onclick = () => api.quit();
  $("btn-diag").onclick = async () => {
    const ok = await api.copy_diagnostics();
    $("diag-feedback").textContent = ok ? tr("diag.copied") : "";
    setTimeout(() => { $("diag-feedback").textContent = ""; }, 2500);
  };
  $("btn-log").textContent = tr("diag.show_log");
  $("btn-log").onclick = () => {
    logVisible = !logVisible;
    $("log").hidden = !logVisible;
    $("btn-log").textContent = tr(logVisible ? "diag.hide_log" : "diag.show_log");
  };
  poll();
}

window.addEventListener("pywebviewready", init);
