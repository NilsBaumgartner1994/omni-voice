# OmniVoice im Docker-Container

[OmniVoice](https://github.com/k2-fsa/OmniVoice) ist ein mehrsprachiges
Zero-Shot-TTS-Modell (600+ Sprachen, Stimmklonen, Stimm-Design). Dieses
Repository verpackt es so, dass es **mit einem Befehl als Docker-Container auf
dem eigenen Laptop läuft** – inklusive kleiner Weboberfläche und REST-API.

* Ein Befehl zum Starten, alles läuft lokal.
* **Kein Hugging-Face-API-Key nötig** ([warum](#brauche-ich-einen-hugging-face-token)).
* CPU-Standard (läuft auf jedem Laptop), NVIDIA-GPU per Override-Datei.
* Modellgewichte liegen in einem Docker-Volume und werden nur einmal geladen.
* Smoke-Test-Modus, mit dem sich der Container ohne Modell-Download prüfen lässt.

| Hell | Dunkel |
| --- | --- |
| ![Weboberfläche](docs/screenshot-light.png) | ![Weboberfläche im Dunkelmodus](docs/screenshot-dark.png) |

> Die Screenshots stammen aus dem Smoke-Test-Modus (`OMNIVOICE_ENGINE=dummy`),
> deshalb steht in der Fußzeile `dummy`.

---

## Schnellstart

Voraussetzung: Docker Desktop bzw. Docker Engine mit Compose v2.

```bash
git clone https://github.com/NilsBaumgartner1994/omni-voice.git
cd omni-voice
docker compose up -d --build
```

Danach im Browser öffnen: **<http://localhost:7860>**

Beim **ersten** Start lädt der Container die Modellgewichte von Hugging Face
(mehrere GB). Solange zeigt die Oberfläche „Modell wird geladen …“. Fortschritt:

```bash
docker compose logs -f
```

Alternativ die Gewichte vorher gezielt laden:

```bash
docker compose run --rm omnivoice prefetch
```

Wer `make` hat, kann alles darüber steuern – `make help` zeigt die Befehle.

---

## Brauche ich einen Hugging-Face-Token?

**Nein.** Alle benötigten Repositories sind öffentlich und werden anonym
heruntergeladen:

| Repository | Wofür | Zugriff |
| --- | --- | --- |
| `k2-fsa/OmniVoice` | TTS-Modell | öffentlich |
| `eustlb/higgs-audio-v2-tokenizer` | Audio-Tokenizer (nur falls nicht im Hauptmodell enthalten) | öffentlich |
| `openai/whisper-large-v3-turbo` | **optional**, nur für automatische Transkription der Referenzstimme | öffentlich |

Es wird nirgends ein Token gesetzt oder gelesen. Falls `huggingface.co` in
deinem Netz nicht erreichbar ist, hilft ein Spiegel statt eines Tokens:

```bash
echo 'HF_ENDPOINT=https://hf-mirror.com' >> .env
docker compose up -d
```

Für den vollständig abgeschotteten Betrieb: einmal `prefetch` laufen lassen und
danach `HF_HUB_OFFLINE=1` in der `.env` setzen – dann geht der Container gar
nicht mehr ins Netz.

---

## Die Weboberfläche

Drei Modi, wie im Original-Modell:

1. **Automatisch** – nur Text eingeben, das Modell wählt eine Stimme.
2. **Stimme klonen** – Referenz-Audio (3–10 s) hochladen oder direkt im Browser
   aufnehmen. Der Referenztext ist optional, siehe Hinweis unten.
3. **Stimme entwerfen** – Stimme über Eigenschaften beschreiben (Geschlecht,
   Alter, Tonhöhe, Flüstern, englischer Akzent, chinesischer Dialekt).

Unter „Erweiterte Einstellungen“ lassen sich Tempo, Diffusionsschritte,
Guidance-Scale und eine feste Audiolänge einstellen. Steuerzeichen aus OmniVoice
wie `[laughter]` oder `[B EY1 S]` funktionieren direkt im Text.

![Stimme klonen](docs/screenshot-clone.png)

**Hinweis zum Referenztext:** Ohne Referenztext transkribiert OmniVoice das
Referenz-Audio automatisch mit Whisper. Dieses Modell ist ein zusätzlicher
Download von mehreren GB und deshalb standardmäßig **aus**. Entweder den
Referenztext eintippen – oder in der `.env`:

```env
OMNIVOICE_LOAD_ASR=true
# für Laptops sparsamer:
OMNIVOICE_ASR_MODEL=openai/whisper-small
```

### Die originale Gradio-Oberfläche

Die Oberfläche aus dem Upstream-Projekt ist ebenfalls eingebaut:

```bash
docker compose down          # Port freigeben
docker compose run --rm --service-ports omnivoice gradio
```

Sie wird über einen eigenen Starter geladen, weil `omnivoice-demo` das dtype
fest auf `float16` setzt – auf einer CPU ist das unbrauchbar langsam bzw. für
einige Operationen gar nicht implementiert. Der Starter wählt Gerät und dtype
passend zur Maschine und benutzt ansonsten exakt die Upstream-Oberfläche.

---

## REST-API

| Methode | Pfad | Zweck |
| --- | --- | --- |
| `GET` | `/api/health` | Ladezustand (`200` = bereit, `503` = lädt noch) |
| `GET` | `/api/info` | Gerät, dtype, Limits, Stimm-Eigenschaften |
| `GET` | `/api/languages` | Liste der unterstützten Sprachen |
| `POST` | `/api/tts` | Synthese, Antwort ist eine WAV-Datei |
| `GET` | `/docs` | interaktive OpenAPI-Dokumentation |

`/api/tts` akzeptiert JSON oder `multipart/form-data` (für das Referenz-Audio):

```bash
# Einfachster Fall
curl -X POST http://localhost:7860/api/tts \
  -H 'content-type: application/json' \
  -d '{"text": "Hallo, das läuft komplett auf meinem Laptop."}' \
  -o hallo.wav

# Stimme entwerfen
curl -X POST http://localhost:7860/api/tts \
  -H 'content-type: application/json' \
  -d '{"text": "Guten Morgen!", "mode": "design",
       "instruct": "female, low pitch", "language": "German"}' \
  -o design.wav

# Stimme klonen
curl -X POST http://localhost:7860/api/tts \
  -F text="Das ist meine geklonte Stimme." \
  -F mode=clone \
  -F ref_audio=@referenz.wav \
  -F ref_text="Transkript der Referenzaufnahme." \
  -o klon.wav
```

Felder: `text` (Pflicht), `mode` (`auto` | `clone` | `design`), `language`,
`instruct`, `ref_audio`, `ref_text`, `num_step`, `guidance_scale`, `speed`,
`duration`, `denoise`, `normalize_text`.

---

## Konfiguration

Alles über Umgebungsvariablen, am einfachsten per `.env` (`cp .env.example .env`):

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `OMNIVOICE_PORT` | `7860` | Port auf dem Host |
| `OMNIVOICE_MODEL` | `k2-fsa/OmniVoice` | Modell (HF-Repo oder Pfad im Container) |
| `OMNIVOICE_DEVICE` | `cpu` | `cpu`, `cuda`, `mps`, `xpu`; leer = automatisch |
| `OMNIVOICE_DTYPE` | leer | leer = `float32` auf CPU, `float16` auf GPU |
| `OMNIVOICE_LOAD_ASR` | `false` | Whisper für automatische Transkription laden |
| `OMNIVOICE_ASR_MODEL` | `openai/whisper-large-v3-turbo` | verwendetes Whisper-Modell |
| `OMNIVOICE_MAX_TEXT_CHARS` | `2000` | Längenlimit pro Anfrage |
| `OMNIVOICE_ENGINE` | `omnivoice` | `dummy` = Testton ohne Modell |
| `OMP_NUM_THREADS` | leer | CPU-Threads begrenzen |
| `HF_ENDPOINT` | leer | Spiegelserver für Hugging Face |
| `HF_HUB_OFFLINE` | leer | `1` = keine Netzwerkzugriffe mehr |

Die Modellgewichte liegen im Volume `omnivoice-models` (im Container unter
`/models`). `docker compose down` lässt sie unangetastet;
`docker compose down -v` bzw. `make clean-models` löscht sie.

---

## GPU, Apple Silicon und Geschwindigkeit

**NVIDIA-GPU** (mit installiertem NVIDIA Container Toolkit):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Damit wird dasselbe Image mit CUDA-PyTorch gebaut, die GPU durchgereicht und
`float16` verwendet. Eine andere CUDA-Version lässt sich über
`TORCH_INDEX_URL_CUDA` in der `.env` wählen.

**Apple Silicon (M1–M4):** Docker Desktop kann die Apple-GPU nicht an Container
weiterreichen; im Container läuft OmniVoice deshalb auf der CPU. Wer die
`mps`-Beschleunigung will, muss OmniVoice nativ auf dem Mac installieren
(siehe Upstream-README) – der Container bleibt trotzdem der einfachste Weg zum
Ausprobieren.

**Tempo:** Auf einer Laptop-CPU ist die Synthese deutlich langsamer als in
Echtzeit; wie viel genau, hängt stark von CPU und Kernanzahl ab. Was hilft:

* „Diffusionsschritte“ von 32 auf 16 senken (schneller, minimal weniger Qualität),
* kurze Texte statt langer Absätze,
* `OMP_NUM_THREADS` auf die Anzahl der physischen Kerne setzen,
* `OMNIVOICE_LOAD_ASR=false` lassen (spart RAM und Download).

Der Container braucht mehrere GB RAM. Unter Docker Desktop ggf. das
Speicherlimit erhöhen (Settings → Resources), sonst wird der Prozess beim Laden
des Modells mit Exit-Code 137 (OOM) beendet.

---

## Ohne Docker entwickeln

```bash
pip install -r requirements-dev.txt
OMNIVOICE_ENGINE=dummy python -m omnivoice_server --port 7860   # nur UI/API
```

Mit echtem Modell zusätzlich `pip install omnivoice torch torchaudio` und
`OMNIVOICE_ENGINE=omnivoice`.

## Tests

```bash
make test     # pytest, ohne Docker und ohne Modell-Download
make smoke    # baut den Container und prüft ihn mit der Dummy-Engine
```

Der Smoke-Test startet den Container mit `OMNIVOICE_ENGINE=dummy`, wartet auf
`/api/health`, ruft die Oberfläche ab und erzeugt eine WAV-Datei. Damit lässt
sich Port-, Volume- und Proxy-Konfiguration in Sekunden prüfen, ohne erst
mehrere GB herunterzuladen.

---

## Troubleshooting

| Symptom | Ursache / Lösung |
| --- | --- |
| Oberfläche zeigt dauerhaft „Modell wird geladen …“ | Erster Download läuft; `docker compose logs -f` zeigt den Fortschritt |
| Container wird mit Exit 137 beendet | Zu wenig RAM für Docker – Limit in Docker Desktop erhöhen |
| „Ohne Referenztext wird ein Whisper-ASR-Modell benötigt …“ | Referenztext eintragen oder `OMNIVOICE_LOAD_ASR=true` setzen |
| Download bricht ab / hängt | `HF_ENDPOINT=https://hf-mirror.com` in die `.env` |
| „Fehler beim Laden“ + `UnsupportedProtocol: Request URL is missing an 'http://' …` | `HF_ENDPOINT` ist leer gesetzt. Zeile aus der `.env` entfernen oder auf eine vollständige URL setzen, danach `docker compose up -d` |
| Port 7860 belegt | `OMNIVOICE_PORT=8080` in die `.env` |
| Healthcheck bleibt „starting“ | Normal, solange Gewichte geladen werden (Startphase: 30 Minuten) |

---

## Aufbau des Repositories

```
Dockerfile               CPU-Image (Build-Args für CUDA)
docker-compose.yml       Standarddienst (CPU) + Volume für die Gewichte
docker-compose.gpu.yml   Override für NVIDIA-GPUs
docker/entrypoint.sh     serve | gradio | prefetch | infer | shell
omnivoice_server/        FastAPI-Server, Engine-Wrapper, Weboberfläche
scripts/                 Modell-Prefetch und Smoke-Test
tests/                   Tests ohne Modellgewichte
```

Upstream-Code ist bewusst **nicht** eingecheckt: das Image installiert das
veröffentlichte Paket `omnivoice` von PyPI (Version über den Build-Arg
`OMNIVOICE_VERSION` steuerbar).

---

## Lizenz und Hinweise

Dieses Docker-Setup steht unter der Apache-Lizenz 2.0 (siehe `LICENSE` und
`NOTICE`). OmniVoice selbst stammt von Xiaomi Corp. / Next-gen Kaldi und steht
ebenfalls unter Apache 2.0.

Stimmklonen darf nur mit Einwilligung der betroffenen Person eingesetzt werden.
Der Upstream-Disclaimer gilt unverändert: keine unautorisierte Imitation,
kein Betrug, keine illegalen oder unethischen Anwendungen.
