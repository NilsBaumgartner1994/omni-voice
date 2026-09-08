"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  mode: "auto",
  ready: false,
  // Stimm-Bibliothek: Personen samt Bild, Referenzaufnahme und Referenztext.
  voices: [],
  voiceId: null,
  editing: null,
  recorder: null,
  recordedBlob: null,
  info: null,
  // Latest answer of /api/estimate for the current settings.
  estimate: null,
  progressTimer: null,
  estimateTimer: null,
};

const MODE_HINTS = {
  auto: "Das Modell wählt selbst eine passende Stimme.",
  saved:
    "Wähle eine gespeicherte Person – ihre Stimme wird für das geladene Modell " +
    "einmal berechnet und danach wiederverwendet.",
  clone:
    "Lade ein kurzes Referenz-Audio hoch (3–10 Sekunden) – die Stimme wird geklont, " +
    "aber nicht gespeichert.",
  design: "Beschreibe die gewünschte Stimme über die Eigenschaften unten.",
};

// Der Server kennt nur auto/clone/design; "saved" ist Klonen aus der Bibliothek.
const serverMode = (mode) => (mode === "saved" ? "clone" : mode);

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
    $("library-hint").textContent =
      "Personen, Bilder, Aufnahmen und Texte bleiben beim Modellwechsel " +
      `erhalten. Berechnet wird jeweils für: ${info.voice_model_key}`;
  } catch (err) {
    /* info is cosmetic – ignore */
  }
  try {
    const langs = await (await fetch("/api/languages")).json();
    for (const select of [$("language"), $("voice-language")]) {
      for (const name of langs.languages) {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      }
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
  const changed = state.mode !== mode;
  state.mode = mode;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.mode === mode);
  }
  $("pane-saved").hidden = mode !== "saved";
  $("pane-clone").hidden = mode !== "clone";
  $("pane-design").hidden = mode !== "design";
  $("mode-hint").textContent = MODE_HINTS[mode];
  if (changed) scheduleEstimate();
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

// ------------------------------------------------------------ forecast
function formatSeconds(seconds) {
  if (!isFinite(seconds) || seconds <= 0) return "0 s";
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}:${String(rest).padStart(2, "0")} min`;
}

function estimateQuery() {
  const params = new URLSearchParams({
    text_chars: String($("text").value.trim().length),
    num_step: $("num-step").value,
    guidance_scale: $("guidance").value,
    speed: $("speed").value,
    mode: serverMode(state.mode),
  });
  const duration = $("duration").value;
  if (duration) params.set("duration", duration);
  return params;
}

// The server predicts from the runtimes it measured on this machine, so the
// answer changes with every finished job and with every settings change.
async function fetchEstimate() {
  try {
    const response = await fetch(`/api/estimate?${estimateQuery()}`);
    state.estimate = response.ok ? await response.json() : null;
  } catch (err) {
    state.estimate = null;
  }
  renderEstimate();
  return state.estimate;
}

function scheduleEstimate() {
  clearTimeout(state.estimateTimer);
  state.estimateTimer = setTimeout(fetchEstimate, 300);
}

function renderEstimate() {
  const node = $("eta");
  const data = state.estimate;
  if (!data || !$("text").value.trim()) {
    node.textContent = "";
    return;
  }
  if (!data.estimate_seconds) {
    node.textContent =
      "Voraussichtliche Dauer: noch unbekannt – sie wird aus dem ersten " +
      "Auftrag auf diesem Rechner gelernt.";
    return;
  }
  const runs =
    data.samples === 1 ? "einem früheren Lauf" : `${data.samples} früheren Läufen`;
  const low = formatSeconds(data.low_seconds);
  const high = formatSeconds(data.high_seconds);
  // A range that rounds to one and the same value only adds noise.
  const spread = low === high ? "" : `${low}–${high}, `;
  node.textContent =
    `Voraussichtliche Dauer: ca. ${formatSeconds(data.estimate_seconds)} ` +
    `(${spread}geschätzt aus ${runs} auf diesem Rechner).`;
}

// ------------------------------------------------------------ progress
function startProgress(estimateSeconds) {
  const fill = $("progress-fill");
  const started = performance.now();
  $("progress").hidden = false;
  fill.classList.remove("indeterminate");
  fill.style.width = "0%";

  const tick = () => {
    const elapsed = (performance.now() - started) / 1000;
    $("progress-elapsed").textContent = `${formatSeconds(elapsed)} vergangen`;
    if (!estimateSeconds) {
      fill.classList.add("indeterminate");
      $("progress-text").textContent =
        "Dauer wird gemessen – ab dem nächsten Auftrag gibt es eine Prognose.";
      return;
    }
    if (elapsed < estimateSeconds) {
      // Cap at 97 %: a full bar before the audio arrives looks broken.
      const ratio = Math.min(0.97, elapsed / estimateSeconds);
      fill.style.width = `${(ratio * 100).toFixed(1)}%`;
      $("progress-text").textContent =
        `noch ca. ${formatSeconds(estimateSeconds - elapsed)} ` +
        `(geschätzt: ${formatSeconds(estimateSeconds)})`;
      return;
    }
    fill.classList.add("indeterminate");
    $("progress-text").textContent =
      `dauert länger als die geschätzten ${formatSeconds(estimateSeconds)} …`;
  };

  tick();
  state.progressTimer = setInterval(tick, 250);
}

function stopProgress() {
  clearInterval(state.progressTimer);
  state.progressTimer = null;
  $("progress").hidden = true;
  $("progress-fill").classList.remove("indeterminate");
  $("progress-fill").style.width = "0%";
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

// ------------------------------------------------------ Stimm-Bibliothek
function libraryError(message) {
  const node = $("library-error");
  node.textContent = message;
  node.hidden = !message;
}

function voiceById(id) {
  return state.voices.find((voice) => voice.id === id) || null;
}

// Bild der Person – oder der Anfangsbuchstabe, wenn keines hinterlegt ist.
function avatarFor(voice) {
  if (voice.has_image) {
    const image = document.createElement("img");
    image.className = "voice-avatar";
    image.loading = "lazy";
    image.alt = "";
    // revision im Query: sonst zeigt der Browser das alte Bild weiter an.
    image.src = `/api/voices/${voice.id}/image?v=${voice.revision}`;
    return image;
  }
  const initial = document.createElement("span");
  initial.className = "voice-avatar placeholder";
  initial.textContent = (voice.name || "?").trim().charAt(0).toUpperCase();
  return initial;
}

function badgeFor(voice) {
  const badge = document.createElement("small");
  badge.className = `badge ${voice.prepared ? "ready" : "pending"}`;
  badge.textContent = voice.prepared
    ? "für dieses Modell bereit"
    : "noch nicht berechnet";
  return badge;
}

async function loadVoices() {
  try {
    const data = await (await fetch("/api/voices")).json();
    state.voices = data.voices || [];
  } catch (err) {
    state.voices = [];
  }
  if (state.voiceId && !voiceById(state.voiceId)) state.voiceId = null;
  renderVoicePicker();
  renderVoiceList();
}

function renderVoicePicker() {
  const picker = $("voice-picker");
  picker.innerHTML = "";
  for (const voice of state.voices) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "voice-card";
    if (voice.id === state.voiceId) card.classList.add("selected");
    card.appendChild(avatarFor(voice));
    const name = document.createElement("span");
    name.className = "voice-card-name";
    name.textContent = voice.name;
    card.appendChild(name);
    card.appendChild(badgeFor(voice));
    card.addEventListener("click", () => selectVoice(voice.id));
    picker.appendChild(card);
  }
  $("voice-picker-empty").hidden = state.voices.length > 0;
}

function selectVoice(id) {
  state.voiceId = state.voiceId === id ? null : id;
  const voice = voiceById(state.voiceId);
  // Die Sprache der Aufnahme ist ein guter Vorschlag – überschreibbar bleibt sie.
  if (voice && voice.language) {
    const select = $("language");
    if ([...select.options].some((option) => option.value === voice.language)) {
      select.value = voice.language;
    }
  }
  renderVoicePicker();
  showError("");
}

function renderVoiceList() {
  const list = $("voice-list");
  list.innerHTML = "";
  for (const voice of state.voices) {
    const row = document.createElement("div");
    row.className = "voice-row";
    row.appendChild(avatarFor(voice));

    const main = document.createElement("div");
    main.className = "voice-row-main";
    const title = document.createElement("strong");
    title.textContent = voice.name;
    main.appendChild(title);
    const meta = document.createElement("small");
    meta.className = "hint";
    meta.textContent = [voice.description, voice.ref_text]
      .filter(Boolean)
      .join(" · ")
      .slice(0, 120);
    main.appendChild(meta);
    main.appendChild(badgeFor(voice));
    const player = document.createElement("audio");
    player.controls = true;
    player.preload = "none";
    player.src = `/api/voices/${voice.id}/audio?v=${voice.revision}`;
    main.appendChild(player);
    row.appendChild(main);

    const actions = document.createElement("div");
    actions.className = "voice-row-actions";
    actions.appendChild(
      button("Verwenden", "secondary", () => {
        setMode("saved");
        if (state.voiceId !== voice.id) selectVoice(voice.id);
        $("text").focus();
      }),
    );
    if (!voice.prepared) {
      actions.appendChild(
        button("Vorbereiten", "secondary", (node) => prepareVoice(voice.id, node)),
      );
    }
    actions.appendChild(button("Bearbeiten", "secondary", () => editVoice(voice.id)));
    actions.appendChild(button("Löschen", "danger", () => deleteVoice(voice.id)));
    row.appendChild(actions);
    list.appendChild(row);
  }
  $("voice-list-empty").hidden = state.voices.length > 0;
}

function button(label, variant, handler) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = variant;
  node.textContent = label;
  node.addEventListener("click", () => handler(node));
  return node;
}

async function voiceRequest(url, options, busyNode, busyLabel) {
  libraryError("");
  const previous = busyNode ? busyNode.textContent : null;
  if (busyNode) {
    busyNode.disabled = true;
    busyNode.textContent = busyLabel;
  }
  try {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        detail = (await response.json()).detail || detail;
      } catch (err) {
        /* Statuscode reicht */
      }
      throw new Error(detail);
    }
    return await response.json();
  } catch (err) {
    libraryError(err.message);
    return null;
  } finally {
    if (busyNode) {
      busyNode.disabled = false;
      busyNode.textContent = previous;
    }
  }
}

async function submitVoiceForm(event) {
  event.preventDefault();
  const name = $("voice-name").value.trim();
  if (!name) {
    libraryError("Bitte einen Namen angeben.");
    return;
  }
  const audio = $("voice-audio").files[0];
  if (!state.editing && !audio) {
    libraryError("Bitte ein Referenz-Audio auswählen.");
    return;
  }

  const form = new FormData();
  form.set("name", name);
  form.set("ref_text", $("voice-ref-text").value.trim());
  form.set("description", $("voice-description").value.trim());
  form.set("language", $("voice-language").value);
  if (audio) form.set("ref_audio", audio, audio.name);
  const image = $("voice-image").files[0];
  if (image) form.set("image", image, image.name);

  const url = state.editing ? `/api/voices/${state.editing}` : "/api/voices";
  const saved = await voiceRequest(
    url,
    { method: "POST", body: form },
    $("voice-save"),
    "Speichere …",
  );
  if (!saved) return;
  resetVoiceForm();
  await loadVoices();
}

function editVoice(id) {
  const voice = voiceById(id);
  if (!voice) return;
  state.editing = id;
  $("voice-name").value = voice.name;
  $("voice-ref-text").value = voice.ref_text || "";
  $("voice-description").value = voice.description || "";
  $("voice-language").value = voice.language || "";
  $("voice-audio").value = "";
  $("voice-image").value = "";
  const preview = $("voice-image-preview");
  preview.hidden = !voice.has_image;
  if (voice.has_image) preview.src = `/api/voices/${id}/image?v=${voice.revision}`;
  $("voice-form-title").textContent = `„${voice.name}" bearbeiten`;
  $("voice-cancel").hidden = false;
  $("voice-form-box").open = true;
  $("voice-name").focus();
}

function resetVoiceForm() {
  state.editing = null;
  $("voice-form").reset();
  $("voice-image-preview").hidden = true;
  $("voice-form-title").textContent = "Neue Person anlegen";
  $("voice-cancel").hidden = true;
  $("voice-audio-preview").hidden = true;
  libraryError("");
}

async function deleteVoice(id) {
  const voice = voiceById(id);
  if (!voice) return;
  if (!window.confirm(`„${voice.name}" mitsamt Aufnahme und Bild löschen?`)) return;
  const done = await voiceRequest(`/api/voices/${id}`, { method: "DELETE" });
  if (!done) return;
  if (state.editing === id) resetVoiceForm();
  await loadVoices();
}

async function prepareVoice(id, node) {
  const done = await voiceRequest(
    `/api/voices/${id}/prepare`,
    { method: "POST" },
    node,
    "Berechne …",
  );
  if (done) await loadVoices();
}

async function prepareAllVoices(node) {
  const result = await voiceRequest(
    "/api/voices/prepare-all",
    { method: "POST" },
    node,
    "Berechne …",
  );
  if (!result) return;
  await loadVoices();
  if (result.failed) {
    const failed = result.results.filter((entry) => !entry.prepared);
    libraryError(
      `${result.failed} von ${result.count} Stimmen konnten nicht berechnet ` +
        `werden: ${failed.map((entry) => `${entry.name} (${entry.error})`).join(", ")}`,
    );
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
  form.set("mode", serverMode(state.mode));
  form.set("language", $("language").value);
  form.set("num_step", $("num-step").value);
  form.set("guidance_scale", $("guidance").value);
  form.set("speed", $("speed").value);
  form.set("duration", $("duration").value || "");
  form.set("denoise", $("denoise").checked ? "true" : "false");
  form.set("normalize_text", $("normalize").checked ? "true" : "false");

  if (state.mode === "saved") {
    if (!state.voiceId) {
      showError("Bitte eine gespeicherte Stimme auswählen.");
      return;
    }
    form.set("voice_id", state.voiceId);
  }

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

  const prediction = await fetchEstimate();
  const predicted = (prediction && prediction.estimate_seconds) || null;
  const started = performance.now();
  startProgress(predicted);

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
    const measured = Number(response.headers.get("X-OmniVoice-Generation-Seconds"));
    const elapsed = measured > 0 ? measured : (performance.now() - started) / 1000;
    const parts = [];
    if (seconds) parts.push(`${seconds} s Audio`);
    parts.push(`in ${formatSeconds(elapsed)} erzeugt`);
    if (predicted) parts.push(`Prognose war ${formatSeconds(predicted)}`);
    $("result-meta").textContent = parts.join(" · ");
    $("output").play().catch(() => {});
  } catch (err) {
    showError(err.message);
  } finally {
    stopProgress();
    button.disabled = false;
    button.textContent = "Sprache erzeugen";
    // The finished run is now part of the history: refresh the forecast.
    fetchEstimate();
    // Beim ersten Lauf wurde die Stimme berechnet – das zeigt die Liste an.
    if (state.mode === "saved") loadVoices();
  }
}

// --------------------------------------------------------------- setup
document.addEventListener("DOMContentLoaded", () => {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => setMode(tab.dataset.mode));
  }
  $("text").addEventListener("input", (event) => {
    $("charcount").textContent = `${event.target.value.length} Zeichen`;
    scheduleEstimate();
  });
  $("duration").addEventListener("input", scheduleEstimate);
  const bind = (slider, output, digits) => {
    const update = () =>
      ($(output).textContent = Number($(slider).value).toFixed(digits));
    $(slider).addEventListener("input", () => {
      update();
      scheduleEstimate();
    });
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
  $("voice-form").addEventListener("submit", submitVoiceForm);
  $("voice-cancel").addEventListener("click", resetVoiceForm);
  $("prepare-all").addEventListener("click", (event) =>
    prepareAllVoices(event.currentTarget),
  );
  $("voice-image").addEventListener("change", (event) => {
    const file = event.target.files[0];
    const preview = $("voice-image-preview");
    preview.hidden = !file;
    if (file) preview.src = URL.createObjectURL(file);
  });
  $("voice-audio").addEventListener("change", (event) => {
    const file = event.target.files[0];
    const preview = $("voice-audio-preview");
    preview.hidden = !file;
    if (file) preview.src = URL.createObjectURL(file);
  });
  $("generate").disabled = true;
  setMode("auto");
  pollHealth();
  fetchEstimate();
  loadVoices();
});
