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
  // Personen-Dialog: woher die Referenzaufnahme kommt ("file" oder
  // "youtube"), das geladene Video und das in der Suche gewählte Bild.
  voiceSource: "file",
  video: null,
  // Läuft der Ausschnitt gerade? Dann hält dieser Handler ihn am Ende an.
  rangeWatcher: null,
  // Steht im Referenztext das Transkript (dann darf es mitwandern) oder
  // etwas Selbstgeschriebenes (dann bleibt es stehen)?
  transcriptTaken: false,
  imageUrl: null,
  // Letztes Ergebnis: das erzeugte WAV plus die daraus schon gebauten
  // Download-Dateien je Format ({ mp3: "blob:…" }).
  result: null,
  downloadRun: 0,
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
    buildDownloadFormats(info);
    $("footer-info").textContent =
      `${info.model.model || ""} · ${info.model.device || ""} · ` +
      `${info.sampling_rate} Hz · `;
    $("library-hint").textContent =
      "Personen, Bilder, Aufnahmen und Texte bleiben beim Modellwechsel " +
      `erhalten. Berechnet wird jeweils für: ${info.voice_model_key}`;
    applySourceSupport(info);
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

// ------------------------------------------------------- Download-Format
function buildDownloadFormats(info) {
  const formats = info.audio_formats || [];
  if (!formats.length) return;
  const select = $("download-format");
  const previous = select.value;
  select.innerHTML = "";
  for (const format of formats) {
    const option = document.createElement("option");
    option.value = format.key;
    option.textContent = format.label;
    select.appendChild(option);
  }
  // MP3 gibt es nur mit ffmpeg; fehlt es, bleibt WAV als einzige Wahl.
  const wanted = formats.some((format) => format.key === previous)
    ? previous
    : info.default_download_format || formats[0].key;
  select.value = wanted;
  select.disabled = formats.length < 2;
  if (state.result) updateDownload();
}

/** Den Download-Link auf das gewählte Format setzen.
 *
 * Erzeugt wird immer WAV; ein anderes Format rechnet der Server einmal um
 * (`/api/convert`) und das Ergebnis bleibt für weitere Klicks liegen. */
async function updateDownload() {
  const link = $("download");
  const format = $("download-format").value || "wav";
  if (!state.result) return;
  const cached = state.result.urls[format];
  if (cached) {
    link.href = cached;
    link.download = `omnivoice.${format}`;
    link.removeAttribute("aria-disabled");
    link.textContent = "⬇ Herunterladen";
    return;
  }

  // Wechselt jemand schnell hin und her, gilt nur die letzte Anfrage.
  const run = ++state.downloadRun;
  link.removeAttribute("href");
  link.setAttribute("aria-disabled", "true");
  link.textContent = `⏳ ${format.toUpperCase()} wird erzeugt …`;
  try {
    const body = new FormData();
    body.set("audio", state.result.blob, "omnivoice.wav");
    body.set("format", format);
    const response = await fetch("/api/convert", { method: "POST", body });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        detail = (await response.json()).detail || detail;
      } catch (err) {
        /* keep the status code */
      }
      throw new Error(detail);
    }
    const blob = await response.blob();
    if (run !== state.downloadRun) return;
    state.result.urls[format] = URL.createObjectURL(blob);
  } catch (err) {
    if (run !== state.downloadRun) return;
    showError(`Download als ${format.toUpperCase()} nicht möglich: ${err.message}`);
    // Das WAV liegt immer vor -- damit bleibt der Knopf benutzbar.
    $("download-format").value = "wav";
  }
  if (run === state.downloadRun) updateDownload();
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
// Bei offenem Dialog liegt die Seite dahinter inert – die Meldung muss dann
// im Dialog stehen, sonst sieht sie niemand.
function libraryError(message) {
  const dialog = $("voice-dialog");
  const inside = Boolean(dialog && dialog.open);
  const node = $(inside ? "voice-dialog-error" : "library-error");
  const other = $(inside ? "library-error" : "voice-dialog-error");
  other.hidden = true;
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

// Eine Kachel zeigt nur Bild und Namen; ob die Stimme für das geladene Modell
// bereitliegt, sagt der grüne Rahmen (und -- für Vorlesegeräte -- der Titel).
function voiceTile(voice, onClick, selected = false) {
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "voice-tile";
  tile.classList.toggle("prepared", Boolean(voice.prepared));
  tile.classList.toggle("selected", selected);
  tile.title = voice.prepared
    ? `${voice.name} – für dieses Modell bereit`
    : `${voice.name} – noch nicht berechnet`;
  tile.setAttribute("aria-label", tile.title);
  tile.appendChild(avatarFor(voice));
  const name = document.createElement("span");
  name.className = "voice-tile-name";
  name.textContent = voice.name;
  tile.appendChild(name);
  tile.addEventListener("click", () => onClick(voice));
  return tile;
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
  renderDialogState();
}

function renderVoicePicker() {
  const picker = $("voice-picker");
  picker.innerHTML = "";
  for (const voice of state.voices) {
    picker.appendChild(
      voiceTile(voice, () => selectVoice(voice.id), voice.id === state.voiceId),
    );
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
    list.appendChild(voiceTile(voice, () => openVoice(voice.id)));
  }
  $("voice-list-empty").hidden = state.voices.length > 0;
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
  // Die Referenzaufnahme kommt entweder als Datei oder als Ausschnitt eines
  // YouTube-Videos; beim Bearbeiten darf beides leer bleiben.
  const audio = state.voiceSource === "file" ? $("voice-audio").files[0] : null;
  const clip = state.voiceSource === "youtube" && state.video ? currentRange() : null;
  if (clip && clip.end <= clip.start) {
    libraryError("Bitte Start- und Endzeit des Ausschnitts wählen.");
    return;
  }
  if (clip && clip.end - clip.start > maxClipSeconds()) {
    libraryError(`Der Ausschnitt darf höchstens ${maxClipSeconds()} Sekunden lang sein.`);
    return;
  }
  if (!state.editing && !audio && !clip) {
    libraryError(
      state.voiceSource === "youtube"
        ? "Bitte zuerst den YouTube-Link laden und einen Ausschnitt wählen."
        : "Bitte ein Referenz-Audio auswählen.",
    );
    return;
  }

  const form = new FormData();
  form.set("name", name);
  form.set("ref_text", $("voice-ref-text").value.trim());
  form.set("description", $("voice-description").value.trim());
  form.set("language", $("voice-language").value);
  if (audio) form.set("ref_audio", audio, audio.name);
  if (clip) {
    form.set("youtube_video_id", state.video.id);
    form.set("youtube_url", state.video.url);
    form.set("youtube_start", clip.start.toFixed(2));
    form.set("youtube_end", clip.end.toFixed(2));
  }
  const image = $("voice-image").files[0];
  if (image) form.set("image", image, image.name);
  // Ein in der Suche gewähltes Bild holt der Server selbst.
  else if (state.imageUrl) form.set("image_url", state.imageUrl);

  // Neu angelegte Personen werden gleich vorbereitet – sonst wartet der erste
  // Auftrag darauf. Beim Bearbeiten macht das die Bibliothek nicht ungefragt.
  const prepare = !state.editing && $("voice-prepare").checked;
  form.set("prepare", prepare ? "true" : "false");

  const url = state.editing ? `/api/voices/${state.editing}` : "/api/voices";
  const saved = await voiceRequest(
    url,
    { method: "POST", body: form },
    $("voice-save"),
    clip ? "Schneide & speichere …" : prepare ? "Speichere & bereite vor …" : "Speichere …",
  );
  if (!saved) return;
  closeVoiceDialog();
  await loadVoices();
  // Die Person steht, nur das Rechnen ging schief – das ist ein Hinweis,
  // kein verlorenes Formular.
  if (saved.error) {
    libraryError(
      `„${saved.name}" ist gespeichert, aber noch nicht vorbereitet: ${saved.error}`,
    );
  }
}

// -- Detailansicht einer Person ---------------------------------------------
function openVoice(id) {
  const voice = voiceById(id);
  if (!voice) return;
  resetVoiceForm();
  state.editing = id;
  $("voice-name").value = voice.name;
  $("voice-ref-text").value = voice.ref_text || "";
  $("voice-description").value = voice.description || "";
  $("voice-language").value = voice.language || "";
  const preview = $("voice-image-preview");
  preview.hidden = !voice.has_image;
  if (voice.has_image) preview.src = `/api/voices/${id}/image?v=${voice.revision}`;
  const current = $("voice-audio-current");
  $("voice-current").hidden = !voice.has_audio;
  if (voice.has_audio) current.src = `/api/voices/${id}/audio?v=${voice.revision}`;
  renderVoiceOrigin(voice.source);
  openVoiceDialog();
}

// Woher die hinterlegte Aufnahme stammt (steht nur bei YouTube-Quellen in
// voice.json) – als Link zurück auf die Stelle im Video.
function renderVoiceOrigin(source) {
  const node = $("voice-source");
  node.innerHTML = "";
  if (!source || source.kind !== "youtube") {
    node.hidden = true;
    return;
  }
  node.hidden = false;
  node.append(
    `Quelle: ${source.title || "YouTube"} · ` +
      `${clockSeconds(source.start)} – ${clockSeconds(source.end)} · `,
  );
  const link = document.createElement("a");
  const start = Math.floor(Number(source.start) || 0);
  link.href = `${source.url}${source.url.includes("?") ? "&" : "?"}t=${start}`;
  link.target = "_blank";
  link.rel = "noreferrer";
  link.textContent = "im Video ansehen";
  node.appendChild(link);
}

function newVoice() {
  resetVoiceForm();
  openVoiceDialog();
}

function openVoiceDialog() {
  const dialog = $("voice-dialog");
  renderDialogState();
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  $("voice-name").focus();
}

function closeVoiceDialog() {
  const dialog = $("voice-dialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
  $("voice-audio-current").pause();
  resetVoiceForm();
}

// Titel, Zustand und Knöpfe hängen daran, ob eine bestehende Person offen ist
// und ob ihre Stimme für die geladenen Gewichte schon berechnet wurde.
function renderDialogState() {
  const voice = state.editing ? voiceById(state.editing) : null;
  if (state.editing && !voice) {
    closeVoiceDialog();
    return;
  }
  $("voice-dialog-title").textContent = voice
    ? `„${voice.name}" bearbeiten`
    : "Neue Person anlegen";
  const badge = $("voice-dialog-state");
  badge.hidden = !voice;
  if (voice) {
    badge.className = `badge ${voice.prepared ? "ready" : "pending"}`;
    badge.textContent = voice.prepared
      ? "für dieses Modell bereit"
      : "noch nicht berechnet";
  }
  $("voice-save").textContent = voice ? "Änderungen speichern" : "Speichern";
  $("voice-prepare-box").hidden = Boolean(voice);
  $("voice-use").hidden = !voice;
  $("voice-delete").hidden = !voice;
  const prepareButton = $("voice-prepare-now");
  prepareButton.hidden = !voice;
  if (voice) {
    prepareButton.textContent = voice.prepared ? "Neu berechnen" : "Vorbereiten";
  }
}

function resetVoiceForm() {
  state.editing = null;
  $("voice-form").reset();
  $("voice-image-preview").hidden = true;
  $("voice-audio-preview").hidden = true;
  $("voice-current").hidden = true;
  $("voice-source").hidden = true;
  $("image-results").hidden = true;
  $("image-search-links").hidden = true;
  clearImageChoice();
  resetYoutube();
  setVoiceSource("file");
  libraryError("");
  renderDialogState();
}

async function deleteVoice(id) {
  const voice = voiceById(id);
  if (!voice) return;
  if (!window.confirm(`„${voice.name}" mitsamt Aufnahme und Bild löschen?`)) return;
  const done = await voiceRequest(`/api/voices/${id}`, { method: "DELETE" });
  if (!done) return;
  closeVoiceDialog();
  await loadVoices();
}

async function prepareVoice(id, node, force = false) {
  const done = await voiceRequest(
    `/api/voices/${id}/prepare${force ? "?force=true" : ""}`,
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

// ------------------------------------------ Referenz aus einem YouTube-Video
// Der Server sagt, ob er yt-dlp und ffmpeg hat und wie lang ein Ausschnitt
// höchstens sein darf; fehlt etwas, steht der Grund gleich im Formular.
function applySourceSupport(info) {
  const youtube = info.youtube || {};
  $("yt-url").disabled = !youtube.enabled;
  $("yt-load").disabled = !youtube.enabled;
  $("yt-hint").textContent = youtube.enabled
    ? "Link einfügen, laden, dann Start und Ende wählen – auch während der " +
      `Wiedergabe. Ausschnitt: höchstens ${maxClipSeconds()} Sekunden.`
    : `YouTube-Links gehen hier nicht: ${youtube.reason || "nicht verfügbar"}`;
  const search = info.image_search || {};
  $("voice-image-search").title = search.enabled
    ? "Bild zu diesem Namen im Internet suchen"
    : `Bildersuche: ${search.reason || "nicht verfügbar"} – es bleiben die ` +
      "Links zu den Suchmaschinen.";
}

function maxClipSeconds() {
  const youtube = (state.info && state.info.youtube) || {};
  return Number(youtube.max_clip_seconds) || 120;
}

// Sekunden als "1:23,4" – im Formular selbst stehen weiterhin Sekunden,
// damit sich der Ausschnitt notfalls von Hand genau eintippen lässt.
function clockSeconds(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(value / 60);
  const rest = (value % 60).toFixed(1).padStart(4, "0");
  return `${minutes}:${rest.replace(".", ",")}`;
}

function setVoiceSource(kind) {
  state.voiceSource = kind;
  for (const tab of document.querySelectorAll("[data-source]")) {
    tab.classList.toggle("active", tab.dataset.source === kind);
  }
  $("voice-source-file").hidden = kind !== "file";
  $("voice-source-youtube").hidden = kind !== "youtube";
  if (kind !== "youtube") stopRange();
}

function currentRange() {
  const duration = (state.video && state.video.duration) || 0;
  let start = Math.max(0, Number($("yt-start").value) || 0);
  let end = Math.max(0, Number($("yt-end").value) || 0);
  if (duration) {
    start = Math.min(start, duration);
    end = Math.min(end, duration);
  }
  return { start, end, duration };
}

// Das Transkript des Videos ist nach Zeitmarken sortiert; für den Ausschnitt
// zählt jeder Abschnitt, der hineinragt.
function transcriptForRange(start, end) {
  const segments = (state.video && state.video.transcript) || [];
  if (end <= start) return "";
  return segments
    .filter((segment) => segment.end > start && segment.start < end)
    .map((segment) => segment.text)
    .join(" ")
    .trim();
}

function renderRange() {
  if (!state.video) return;
  const { start, end, duration } = currentRange();
  const length = end - start;
  const limit = maxClipSeconds();
  const parts = [
    `Ausschnitt ${clockSeconds(start)} – ${clockSeconds(end)}`,
    `${length.toFixed(1)} s von ${clockSeconds(duration)}`,
  ];
  if (length <= 0) {
    parts.push("⚠ Das Ende muss hinter dem Start liegen.");
  } else if (length > limit) {
    parts.push(`⚠ Höchstens ${limit} Sekunden.`);
  } else if (length < 2) {
    parts.push("⚠ Sehr kurz – 3 bis 10 Sekunden klingen am besten.");
  }
  $("yt-range-hint").textContent = parts.join(" · ");

  const transcript = transcriptForRange(start, end);
  const box = $("yt-transcript-box");
  const hasSegments = ((state.video.transcript || []).length || 0) > 0;
  box.hidden = false;
  $("yt-transcript").textContent = hasSegments
    ? transcript || "(für diesen Bereich steht nichts im Transkript)"
    : "Zu diesem Video gibt es keine Untertitel – bitte den Referenztext " +
      "selbst eintragen.";
  $("yt-use-transcript").hidden = !transcript;
  // Solange der Referenztext aus dem Transkript stammt, wandert er mit;
  // sobald jemand selbst tippt, bleibt das Getippte stehen.
  const refText = $("voice-ref-text");
  if (transcript && (state.transcriptTaken || !refText.value.trim())) {
    refText.value = transcript;
    state.transcriptTaken = true;
  }
}

function useTranscript() {
  const { start, end } = currentRange();
  const transcript = transcriptForRange(start, end);
  if (!transcript) return;
  $("voice-ref-text").value = transcript;
  state.transcriptTaken = true;
}

async function loadYoutube(node) {
  const url = $("yt-url").value.trim();
  if (!url) {
    libraryError("Bitte einen YouTube-Link einfügen.");
    return;
  }
  $("yt-hint").textContent =
    "Tonspur und Untertitel werden geladen – bei langen Videos dauert das " +
    "einen Moment.";
  const data = await voiceRequest(
    "/api/youtube/fetch",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    },
    node,
    "Lade …",
  );
  if (!data) {
    $("yt-hint").textContent = "";
    return;
  }
  state.video = data;
  $("yt-result").hidden = false;
  $("yt-title").textContent = data.title || data.id;
  const bits = [data.uploader, `Länge ${clockSeconds(data.duration)}`];
  bits.push(
    (data.transcript || []).length
      ? `Transkript vorhanden (${data.transcript_kind === "auto" ? "automatisch" : "vom Kanal"}${
          data.transcript_language ? `, ${data.transcript_language}` : ""
        })`
      : "kein Transkript verfügbar",
  );
  $("yt-meta").textContent = bits.filter(Boolean).join(" · ");
  const thumb = $("yt-thumb");
  thumb.hidden = !data.thumbnail;
  if (data.thumbnail) thumb.src = data.thumbnail;
  $("yt-audio").src = data.audio_url;
  // Voreinstellung: die ersten Sekunden – von dort aus wird gesucht.
  $("yt-start").value = "0";
  $("yt-end").value = String(
    Math.min(10, maxClipSeconds(), Math.max(1, data.duration || 10)),
  );
  $("yt-hint").textContent =
    "Beim Abspielen mit „Start hier“ und „Ende hier“ den Ausschnitt setzen.";
  renderRange();
}

// Start/Ende aus der laufenden Wiedergabe übernehmen.
function markStart() {
  const audio = $("yt-audio");
  const { end } = currentRange();
  const start = Math.max(0, audio.currentTime || 0);
  $("yt-start").value = start.toFixed(1);
  if (end <= start) {
    const duration = (state.video && state.video.duration) || start + 10;
    $("yt-end").value = Math.min(start + 10, maxClipSeconds() + start, duration)
      .toFixed(1);
  }
  renderRange();
}

function markEnd() {
  const audio = $("yt-audio");
  $("yt-end").value = Math.max(0, audio.currentTime || 0).toFixed(1);
  renderRange();
}

function stopRange() {
  if (!state.rangeWatcher) return;
  $("yt-audio").removeEventListener("timeupdate", state.rangeWatcher);
  state.rangeWatcher = null;
}

// Den gewählten Bereich anhören: an der Endmarke hält die Wiedergabe an.
function playRange() {
  const audio = $("yt-audio");
  const { start, end } = currentRange();
  if (end <= start) {
    libraryError("Bitte erst Start und Ende setzen.");
    return;
  }
  stopRange();
  audio.currentTime = start;
  const watcher = () => {
    if (audio.currentTime >= end) {
      audio.pause();
      stopRange();
    }
  };
  audio.addEventListener("timeupdate", watcher);
  state.rangeWatcher = watcher;
  audio.play().catch(() => {});
}

function resetYoutube() {
  stopRange();
  state.video = null;
  state.transcriptTaken = false;
  $("yt-result").hidden = true;
  $("yt-audio").removeAttribute("src");
  $("yt-thumb").hidden = true;
  $("yt-range-hint").textContent = "";
  $("yt-transcript").textContent = "";
  $("yt-transcript-box").hidden = true;
  if (state.info) applySourceSupport(state.info);
}

// ------------------------------------------------------------ Bildersuche
function browserSearchLinks(query) {
  const escaped = encodeURIComponent(query);
  return [
    { label: "DuckDuckGo", url: `https://duckduckgo.com/?q=${escaped}&iax=images&ia=images` },
    { label: "Google", url: `https://www.google.com/search?q=${escaped}&tbm=isch` },
    { label: "Bing", url: `https://www.bing.com/images/search?q=${escaped}` },
  ];
}

async function searchImages(node) {
  const name = $("voice-name").value.trim();
  if (!name) {
    libraryError("Bitte zuerst den Namen eintragen – danach wird gesucht.");
    return;
  }
  renderSearchLinks(browserSearchLinks(name));
  const data = await voiceRequest(
    `/api/image-search?q=${encodeURIComponent(name)}`,
    {},
    node,
    "Suche …",
  );
  if (!data) return;
  renderImageResults(data);
}

function renderSearchLinks(links) {
  const box = $("image-search-links");
  box.innerHTML = "";
  box.hidden = false;
  box.append("Nichts Passendes dabei? Weitersuchen bei ");
  links.forEach((entry, index) => {
    const link = document.createElement("a");
    link.href = entry.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = entry.label;
    box.appendChild(link);
    if (index < links.length - 1) box.append(" · ");
  });
  box.append(" – das gefundene Bild dann als Datei auswählen.");
}

function renderImageResults(data) {
  const box = $("image-results");
  box.innerHTML = "";
  const results = data.results || [];
  box.hidden = false;
  if (!results.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = `Zu „${data.query}“ wurde nichts gefunden.`;
    box.appendChild(empty);
  }
  for (const hit of results) {
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = "image-hit";
    tile.title = [hit.title, hit.credit].filter(Boolean).join(" – ");
    const image = document.createElement("img");
    image.src = hit.thumbnail || hit.url;
    image.alt = hit.title || "";
    image.loading = "lazy";
    const caption = document.createElement("span");
    caption.textContent = hit.title || "";
    tile.append(image, caption);
    tile.addEventListener("click", () => pickImage(hit, tile));
    box.appendChild(tile);
  }
  renderSearchLinks(data.browser_search || browserSearchLinks(data.query || ""));
}

// Ausgewählt wird nur der Link; heruntergeladen wird das Bild erst beim
// Speichern der Person (der Server prüft dabei Herkunft und Größe).
function pickImage(hit, tile) {
  state.imageUrl = hit.url;
  $("voice-image").value = "";
  const preview = $("voice-image-preview");
  preview.hidden = false;
  preview.src = hit.thumbnail || hit.url;
  for (const other of document.querySelectorAll(".image-hit")) {
    other.classList.toggle("selected", other === tile);
  }
  const hint = $("voice-image-hint");
  hint.hidden = false;
  hint.textContent = `Übernommen: ${[hit.title, hit.credit].filter(Boolean).join(" – ")}`;
}

function clearImageChoice() {
  state.imageUrl = null;
  $("voice-image-hint").hidden = true;
  for (const tile of document.querySelectorAll(".image-hit")) {
    tile.classList.remove("selected");
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
    // Die Blobs des letzten Laufs werden nicht mehr gebraucht.
    if (state.result) {
      for (const old of Object.values(state.result.urls)) URL.revokeObjectURL(old);
    }
    state.result = { blob, urls: { wav: url } };
    $("output").src = url;
    $("result").hidden = false;
    updateDownload();
    const seconds = response.headers.get("X-OmniVoice-Duration-Seconds");
    const measured = Number(response.headers.get("X-OmniVoice-Generation-Seconds"));
    const elapsed = measured > 0 ? measured : (performance.now() - started) / 1000;
    const parts = [];
    if (seconds) parts.push(`${seconds} s Audio`);
    parts.push(`in ${formatSeconds(elapsed)} erzeugt`);
    if (predicted) parts.push(`Prognose war ${formatSeconds(predicted)}`);
    $("result-meta").textContent = parts.join(" · ");
    // Autoplay kann der Browser verweigern (kein Nutzerklick, stumm geschaltet);
    // die Aufnahme steht dann trotzdem im Ergebnis-Player.
    if ($("autoplay").checked) $("output").play().catch(() => {});
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
  $("download-format").addEventListener("change", updateDownload);
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
  $("voice-new").addEventListener("click", newVoice);
  $("voice-close").addEventListener("click", closeVoiceDialog);
  // Escape schließt den Dialog am Browser vorbei: Formular mit aufräumen.
  $("voice-dialog").addEventListener("close", resetVoiceForm);
  $("voice-use").addEventListener("click", () => {
    const id = state.editing;
    closeVoiceDialog();
    setMode("saved");
    if (state.voiceId !== id) selectVoice(id);
    $("text").focus();
  });
  $("voice-prepare-now").addEventListener("click", (event) => {
    const voice = voiceById(state.editing);
    if (voice) prepareVoice(voice.id, event.currentTarget, voice.prepared);
  });
  $("voice-delete").addEventListener("click", () => deleteVoice(state.editing));
  $("prepare-all").addEventListener("click", (event) =>
    prepareAllVoices(event.currentTarget),
  );
  $("voice-image").addEventListener("change", (event) => {
    const file = event.target.files[0];
    const preview = $("voice-image-preview");
    preview.hidden = !file;
    if (file) preview.src = URL.createObjectURL(file);
    // Eine eigene Datei sticht das gefundene Bild aus.
    if (file) clearImageChoice();
  });
  $("voice-audio").addEventListener("change", (event) => {
    const file = event.target.files[0];
    const preview = $("voice-audio-preview");
    preview.hidden = !file;
    if (file) preview.src = URL.createObjectURL(file);
  });
  for (const tab of document.querySelectorAll("[data-source]")) {
    tab.addEventListener("click", () => setVoiceSource(tab.dataset.source));
  }
  $("yt-load").addEventListener("click", (event) => loadYoutube(event.currentTarget));
  $("yt-url").addEventListener("keydown", (event) => {
    // Enter im Link-Feld lädt das Video, statt das Formular abzuschicken.
    if (event.key !== "Enter") return;
    event.preventDefault();
    loadYoutube($("yt-load"));
  });
  $("yt-set-start").addEventListener("click", markStart);
  $("yt-set-end").addEventListener("click", markEnd);
  $("yt-play-range").addEventListener("click", playRange);
  $("yt-use-transcript").addEventListener("click", useTranscript);
  $("yt-start").addEventListener("input", renderRange);
  $("yt-end").addEventListener("input", renderRange);
  // Von Hand geschriebener Referenztext bleibt stehen.
  $("voice-ref-text").addEventListener("input", () => {
    state.transcriptTaken = false;
  });
  $("voice-image-search").addEventListener("click", (event) =>
    searchImages(event.currentTarget),
  );
  $("generate").disabled = true;
  setMode("auto");
  pollHealth();
  fetchEstimate();
  loadVoices();
});
