"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  mode: "auto",
  ready: false,
  recorder: null,
  recordedBlob: null,
  info: null,
};

const MODE_HINTS = {
  auto: "Das Modell wählt selbst eine passende Stimme.",
  clone:
    "Lade ein kurzes Referenz-Audio hoch (3–10 Sekunden) – die Stimme wird geklont.",
  design: "Beschreibe die gewünschte Stimme über die Eigenschaften unten.",
};

// ---------------------------------------------------------------- status
async function pollHealth() {
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    const model = data.model || {};
    const pill = $("status");
    const text = $("status-text");
    pill.classList.remove("ready", "error", "loading");

    if (model.state === "ready") {
      state.ready = true;
      pill.classList.add("ready");
      const bits = [model.device, model.dtype].filter(Boolean).join(" · ");
      text.textContent = bits ? `bereit (${bits})` : "bereit";
      $("generate").disabled = false;
      if (!state.info) loadInfo();
      return;
    }

    state.ready = false;
    $("generate").disabled = true;
    if (model.state === "error") {
      pill.classList.add("error");
      text.textContent = "Fehler beim Laden";
      showError(model.detail || "Das Modell konnte nicht geladen werden.");
    } else {
      pill.classList.add("loading");
      text.textContent = model.detail || "Modell wird geladen …";
    }
  } catch (err) {
    $("status").classList.remove("ready");
    $("status").classList.add("error");
    $("status-text").textContent = "Server nicht erreichbar";
    $("generate").disabled = true;
  }
  setTimeout(pollHealth, 2000);
}

async function loadInfo() {
  try {
    const info = await (await fetch("/api/info")).json();
    state.info = info;
    buildDesignControls(info.voice_design || []);
    $("asr-hint").textContent = info.asr_enabled
      ? "Ohne Referenztext wird das Audio automatisch transkribiert (Whisper)."
      : "Referenztext bitte eintragen – die automatische Transkription (Whisper) ist deaktiviert.";
    $("text").maxLength = info.limits.max_text_chars;
    $("footer-info").textContent =
      `${info.model.model || ""} · ${info.model.device || ""} · ` +
      `${info.sampling_rate} Hz · `;
  } catch (err) {
    /* info is cosmetic – ignore */
  }
  try {
    const langs = await (await fetch("/api/languages")).json();
    const select = $("language");
    for (const name of langs.languages) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    }
  } catch (err) {
    /* ignore */
  }
}

// ------------------------------------------------------------------ UI
function buildDesignControls(categories) {
  const grid = $("design-grid");
  grid.innerHTML = "";
  for (const category of categories) {
    const wrapper = document.createElement("div");
    const label = document.createElement("label");
    label.textContent = category.label;
    label.htmlFor = `design-${category.key}`;
    const select = document.createElement("select");
    select.id = `design-${category.key}`;
    select.dataset.design = "1";
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "– egal –";
    select.appendChild(empty);
    for (const option of category.options) {
      const node = document.createElement("option");
      node.value = option.value;
      node.textContent = option.label;
      select.appendChild(node);
    }
    wrapper.appendChild(label);
    wrapper.appendChild(select);
    if (category.hint) {
      const hint = document.createElement("small");
      hint.className = "hint";
      hint.textContent = category.hint;
      wrapper.appendChild(hint);
    }
    grid.appendChild(wrapper);
  }
}

function setMode(mode) {
  state.mode = mode;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.mode === mode);
  }
  $("pane-clone").hidden = mode !== "clone";
  $("pane-design").hidden = mode !== "design";
  $("mode-hint").textContent = MODE_HINTS[mode];
}

function collectInstruct() {
  const parts = [];
  for (const select of document.querySelectorAll("[data-design]")) {
    if (select.value) parts.push(select.value);
  }
  return parts.join(", ");
}

function showError(message) {
  const node = $("error");
  node.textContent = message;
  node.hidden = !message;
}

// ----------------------------------------------------------- recording
async function toggleRecording() {
  const button = $("record");
  if (state.recorder && state.recorder.state === "recording") {
    state.recorder.stop();
    return;
  }
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    $("record-hint").textContent =
      "Aufnahme wird von diesem Browser/Kontext nicht unterstützt – bitte Datei wählen.";
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    const recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (event) => chunks.push(event.data);
    recorder.onstop = () => {
      stream.getTracks().forEach((track) => track.stop());
      state.recordedBlob = new Blob(chunks, { type: recorder.mimeType });
      const preview = $("ref-preview");
      preview.src = URL.createObjectURL(state.recordedBlob);
      preview.hidden = false;
      button.textContent = "⏺ Aufnehmen";
      $("record-hint").textContent = "Aufnahme gespeichert.";
    };
    recorder.start();
    state.recorder = recorder;
    button.textContent = "⏹ Stopp";
    $("record-hint").textContent = "Aufnahme läuft …";
  } catch (err) {
    $("record-hint").textContent = `Mikrofon nicht verfügbar: ${err.message}`;
  }
}

// ---------------------------------------------------------- generation
async function generate() {
  showError("");
  const text = $("text").value.trim();
  if (!text) {
    showError("Bitte einen Text eingeben.");
    return;
  }

  const form = new FormData();
  form.set("text", text);
  form.set("mode", state.mode);
  form.set("language", $("language").value);
  form.set("num_step", $("num-step").value);
  form.set("guidance_scale", $("guidance").value);
  form.set("speed", $("speed").value);
  form.set("duration", $("duration").value || "");
  form.set("denoise", $("denoise").checked ? "true" : "false");
  form.set("normalize_text", $("normalize").checked ? "true" : "false");

  if (state.mode === "design") {
    const instruct = collectInstruct();
    if (!instruct) {
      showError("Bitte mindestens eine Stimm-Eigenschaft auswählen.");
      return;
    }
    form.set("instruct", instruct);
  }

  if (state.mode === "clone") {
    const file = $("ref-audio").files[0];
    if (file) {
      form.set("ref_audio", file, file.name);
    } else if (state.recordedBlob) {
      form.set("ref_audio", state.recordedBlob, "aufnahme.webm");
    } else {
      showError("Bitte ein Referenz-Audio hochladen oder aufnehmen.");
      return;
    }
    const refText = $("ref-text").value.trim();
    if (refText) form.set("ref_text", refText);
  }

  const button = $("generate");
  button.disabled = true;
  button.textContent = "Erzeuge Sprache …";
  const started = performance.now();

  try {
    const response = await fetch("/api/tts", { method: "POST", body: form });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        detail = data.detail || detail;
      } catch (err) {
        /* keep the status code */
      }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    $("output").src = url;
    $("download").href = url;
    $("result").hidden = false;
    const seconds = response.headers.get("X-OmniVoice-Duration-Seconds");
    const elapsed = ((performance.now() - started) / 1000).toFixed(1);
    $("result-meta").textContent =
      `${seconds ? seconds + " s Audio · " : ""}in ${elapsed} s erzeugt`;
    $("output").play().catch(() => {});
  } catch (err) {
    showError(err.message);
  } finally {
    button.disabled = false;
    button.textContent = "Sprache erzeugen";
  }
}

// --------------------------------------------------------------- setup
document.addEventListener("DOMContentLoaded", () => {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => setMode(tab.dataset.mode));
  }
  $("text").addEventListener("input", (event) => {
    $("charcount").textContent = `${event.target.value.length} Zeichen`;
  });
  const bind = (slider, output, digits) => {
    const update = () =>
      ($(output).textContent = Number($(slider).value).toFixed(digits));
    $(slider).addEventListener("input", update);
    update();
  };
  bind("speed", "speed-value", 2);
  bind("num-step", "num-step-value", 0);
  bind("guidance", "guidance-value", 1);
  $("record").addEventListener("click", toggleRecording);
  $("ref-audio").addEventListener("change", () => {
    state.recordedBlob = null;
    $("ref-preview").hidden = true;
  });
  $("generate").addEventListener("click", generate);
  $("generate").disabled = true;
  setMode("auto");
  pollHealth();
});
