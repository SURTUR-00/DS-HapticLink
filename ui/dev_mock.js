// Solo para desarrollo: simula la API de Python para ver la interfaz en un navegador (index.html?mock[&estado]).
(function () {
  const params = new URLSearchParams(location.search);
  const state = params.get("state") || "listo";
  const setup = params.has("setup");
  let s = {
    language: params.get("lang") || "es", lightbar_color: [0, 0, 128], haptics_gain: 1.0, speaker_boost: 0,
    audio_enabled: true, mic_enabled: true, start_minimized: true, notifications: true, power_warning: true, autostart: false,
  };
  const connected = ["listo", "en_juego"].includes(state);
  const un = { view: params.has("uninstall") || params.has("undone"), running: false, message: "", percent: 0,
    ok: params.has("undone") ? true : null, drivers: true, folder: "C:\\Users\\you\\Downloads\\DSHapticLink" };
  const pad = (params.get("lang") || "es") === "en" ? "Living room controller" : "Control de la sala";
  window.pywebview = {
    api: {
      get_info: async () => ({ name: "DS HapticLink", version: "0.4.9" }),
      get_settings: async () => ({ ...s }),
      set_setting: async (k, v) => { s[k] = v; return v; },
      get_status: async () => ({
        state, running: true,
        prerequisites: { usbip: !setup, task: !setup },
        setup: { running: setup && params.has("installing"), percent: params.has("installing") ? 62 : 0,
          message: params.has("installing") ? "Instalando usbip-win2 (los dispositivos USB pueden desconectarse un momento)…" : "", ok: null },
        controller: { name: connected ? pad : null, model: "DualSense", mac: "A0:B1:C2:D3:E4:F5",
          needs_cable: params.has("edge"), connected, exclusive: connected, blocked: false, battery: connected ? 75 : null,
          battery_state: "descargando", headphones: false, mic_muted: false },
        uninstall: un,
        power: params.has("saver") ? { name: "Economizador", guid: "a1841308-3541-4fab-bc81-f71556f20b4a",
          power_saver: true, reason: params.get("saver") || "plan" } : { name: "Equilibrado", power_saver: false, reason: null },
        virtual: { attached: true, mode: "normal" }, game_active: state === "en_juego",
        stats: { bt_in: 12345, out_reports: state === "en_juego" ? 4321 : 0, haptic_reports: 980, underruns: 2,
          reserve_ms: 21.3, connections: 1 },
      }),
      get_logs: async () => ["19:00:00 servicio  puente iniciado", "19:00:01 ds5       DualSense Bluetooth conectado"],
      get_controllers: async () => [
        { mac: "A0:B1:C2:D3:E4:F5", name: pad, model: "DualSense", connected,
          firmware: { firmware: "0x0110002A" }, last_seen: Date.now() / 1000, settings: { lightbar_color: [255, 0, 60] },
          has_profile: true, needs_cable: false },
        { mac: "A0:AB:51:11:22:33", name: "DualSense 2", model: "DualSense Edge", connected: false,
          firmware: null, last_seen: Date.now() / 1000 - 86400 * 3, settings: {}, has_profile: false, needs_cable: true },
      ],
      rename_controller: async () => true, set_controller_setting: async () => true, forget_controller: async () => true,
      run_setup: async () => true,
      show_uninstall: async (v) => { un.view = v; return true; },
      run_uninstall: async (d) => { Object.assign(un, { running: true, percent: 40, drivers: d, message: "Removing usbip-win2…" });
        setTimeout(() => Object.assign(un, { running: false, ok: true, percent: 100 }), 2500); return true; },
      open_app_folder: async () => {}, test_rumble: async () => true, toggle_pause: async () => false,
      open_bluetooth_settings: async () => {}, open_power_settings: async () => {}, copy_diagnostics: async () => true, quit: async () => {},
    },
  };
  window.addEventListener("load", () => window.dispatchEvent(new Event("pywebviewready")));
})();
