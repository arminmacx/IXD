/** Options page: reads and writes the extension's stored settings. */

const $ = (id) => document.getElementById(id);

const BOOLEAN_FIELDS = ["enabled", "interceptDownloads", "injectVideoButtons", "notifyOnAdd"];
const TEXT_FIELDS = ["preferredQuality"];
const LIST_FIELDS = ["extensions", "ignoredHosts"];
const NUMBER_FIELDS = ["minSizeBytes"];

const DEFAULTS = {
  enabled: true,
  interceptDownloads: true,
  injectVideoButtons: true,
  minSizeBytes: 0,
  extensions: [],
  ignoredHosts: ["localhost", "127.0.0.1"],
  notifyOnAdd: true,
  preferredQuality: "1080p",
};

function send(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else if (!response || response.ok !== true) {
        reject(new Error((response && response.error) || "request failed"));
      } else resolve(response.result);
    });
  });
}

function parseList(value) {
  return value
    .split(",")
    .map((part) => part.trim().replace(/^\./, "").toLowerCase())
    .filter(Boolean);
}

function apply(settings) {
  for (const field of BOOLEAN_FIELDS) $(field).checked = Boolean(settings[field]);
  for (const field of TEXT_FIELDS) $(field).value = settings[field] ?? "";
  for (const field of NUMBER_FIELDS) $(field).value = Number(settings[field] ?? 0);
  for (const field of LIST_FIELDS) $(field).value = (settings[field] || []).join(", ");
}

function collect() {
  const patch = {};
  for (const field of BOOLEAN_FIELDS) patch[field] = $(field).checked;
  for (const field of TEXT_FIELDS) patch[field] = $(field).value.trim();
  for (const field of NUMBER_FIELDS) patch[field] = Math.max(0, Number($(field).value) || 0);
  for (const field of LIST_FIELDS) patch[field] = parseList($(field).value);
  return patch;
}

function setStatus(text, isError) {
  const element = $("save-status");
  element.textContent = text;
  element.style.color = isError ? "var(--bad)" : "var(--muted)";
  if (text) setTimeout(() => { element.textContent = ""; }, 3000);
}

async function checkConnection() {
  const detail = $("conn-detail");
  detail.textContent = "Checking…";
  try {
    const result = await send({ type: "ping" });
    detail.textContent = `Connected — version ${result.version} (pid ${result.pid})`;
    detail.style.color = "var(--good)";
  } catch (error) {
    detail.textContent = `Not reachable: ${error.message}`;
    detail.style.color = "var(--bad)";
  }
}

async function boot() {
  try {
    apply(await send({ type: "getSettings" }));
  } catch (error) {
    apply(DEFAULTS);
    setStatus(error.message, true);
  }

  $("save").addEventListener("click", async () => {
    try {
      await send({ type: "saveSettings", patch: collect() });
      setStatus("Saved");
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  $("reset").addEventListener("click", async () => {
    apply(DEFAULTS);
    try {
      await send({ type: "saveSettings", patch: DEFAULTS });
      setStatus("Reset to defaults");
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  // Diagnostics live here now rather than in a popup: the toolbar button hands
  // the page to the application, and there is no second interface to put them
  // in. "No panel on this site" has three causes that look identical from
  // outside, and this is what tells them apart.
  $("diagnose").addEventListener("click", async () => {
    const panel = $("diagnostics");
    panel.hidden = false;
    panel.textContent = "Checking…";
    let tabId = -1;
    try {
      const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      const page = tabs.find((tab) => /^https?:/i.test(tab.url || ""));
      tabId = page ? page.id : -1;
    } catch (error) {
      /* the query may be unavailable; the report below still says so */
    }
    try {
      const info = await send({ type: "diagnostics", tabId });
      panel.textContent = [
        `in-page panel      : ${info.panel || "unknown"}`,
        `streams captured   : ${info.capturedStreams.length
          ? info.capturedStreams.join("\n                     ") : "none"}`,
        `server-driven page : ${info.serverDriven ? "yes" : "no"}`,
        `proof of origin    : ${info.hasToken ? info.poToken : "not captured"}`,
        `visitor identity   : ${info.visitorData || "not captured"}`,
        `player endpoint    : ${info.endpoint || "not seen"}`,
      ].join("\n");
    } catch (error) {
      panel.textContent = `could not read diagnostics: ${error.message}`;
    }
  });

  $("test").addEventListener("click", checkConnection);
  await checkConnection();
}

boot();
