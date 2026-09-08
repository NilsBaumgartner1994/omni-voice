"""Übersetzungen für die Weboberfläche (Vorgabe: Deutsch).

Hier liegen alle übersetzten Texte an einer Stelle: die Beschriftungen der
Klangeffekte (``[laughter]`` und Verwandte) und die wenigen UI-Texte, die
dazugehören. Eine weitere Sprache ist ein zusätzlicher Eintrag in
:data:`TRANSLATIONS` – fehlt dort ein Schlüssel, greift Deutsch.
"""

from __future__ import annotations

import unicodedata
from typing import Any

DEFAULT_LOCALE = "de"

# Steuerzeichen, die das Modell versteht. Die Reihenfolge hier ist nur die
# Quelle der Wahrheit; ausgeliefert wird alphabetisch nach Übersetzung.
SOUND_TAGS: tuple[str, ...] = (
    "laughter",
    "sigh",
    "confirmation-en",
    "question-en",
    "question-ah",
    "question-oh",
    "question-ei",
    "question-yi",
    "surprise-ah",
    "surprise-oh",
    "surprise-wa",
    "surprise-yo",
    "dissatisfaction-hnn",
)

TRANSLATIONS: dict[str, dict[str, str]] = {
    "de": {
        "sound_tags.label": "Klangeffekt einfügen",
        "sound_tags.placeholder": "– Klangeffekt einfügen –",
        "sound_tags.hint": (
            "Der gewählte Effekt wird an der Cursorposition in den Text "
            "geschrieben. Steuerzeichen wie [laughter] dürfen auch von Hand "
            "getippt werden."
        ),
        "sound_tags.too_long": (
            "Der Text ist am Zeichenlimit – der Klangeffekt passt nicht mehr hinein."
        ),
        "tag.laughter": "Lachen",
        "tag.sigh": "Seufzen",
        "tag.confirmation-en": "Zustimmung „mhm“",
        "tag.question-en": "Nachfrage „hm?“",
        "tag.question-ah": "Nachfrage „ah?“",
        "tag.question-oh": "Nachfrage „oh?“",
        "tag.question-ei": "Nachfrage „ei?“",
        "tag.question-yi": "Nachfrage „yi?“",
        "tag.surprise-ah": "Überraschung „ah!“",
        "tag.surprise-oh": "Überraschung „oh!“",
        "tag.surprise-wa": "Überraschung „wa!“",
        "tag.surprise-yo": "Überraschung „yo!“",
        "tag.dissatisfaction-hnn": "Unmut „hnn“",
    },
    "en": {
        "sound_tags.label": "Insert sound effect",
        "sound_tags.placeholder": "– insert sound effect –",
        "sound_tags.hint": (
            "The chosen effect is written into the text at the cursor. "
            "Control tokens such as [laughter] may also be typed by hand."
        ),
        "sound_tags.too_long": (
            "The text is at the character limit – the sound effect does not "
            "fit any more."
        ),
        "tag.laughter": "Laughter",
        "tag.sigh": "Sigh",
        "tag.confirmation-en": "Confirmation “mhm”",
        "tag.question-en": "Question “hm?”",
        "tag.question-ah": "Question “ah?”",
        "tag.question-oh": "Question “oh?”",
        "tag.question-ei": "Question “ei?”",
        "tag.question-yi": "Question “yi?”",
        "tag.surprise-ah": "Surprise “ah!”",
        "tag.surprise-oh": "Surprise “oh!”",
        "tag.surprise-wa": "Surprise “wa!”",
        "tag.surprise-yo": "Surprise “yo!”",
        "tag.dissatisfaction-hnn": "Dissatisfaction “hnn”",
    },
}

LOCALES: tuple[str, ...] = tuple(TRANSLATIONS)


def sort_key(text: str) -> tuple[str, str]:
    """Sortierschlüssel für Sprachen mit lateinischer Schrift.

    Ohne ICU sortiert Python „Überraschung“ hinter „Zustimmung“. Deshalb
    fallen Akzente und Umlaute weg (ä → a, Ü → U), bevor verglichen wird;
    Anführungszeichen und Ähnliches stören dabei nicht, weil der Vergleich
    erst danach zeichenweise weiterläuft. Der Originaltext hängt als
    zweiter Teil dran, damit die Reihenfolge eindeutig bleibt.
    """

    folded = unicodedata.normalize("NFD", text.casefold())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return (stripped, text)


def resolve_locale(
    requested: str | None = None, accept_language: str | None = None
) -> str:
    """Beste unterstützte Sprache für Wunsch bzw. ``Accept-Language``.

    Geprüft wird zuerst der ausdrückliche Wunsch (``?locale=en-GB``), dann
    der Header des Browsers. Bleibt nichts übrig, gilt Deutsch.
    """

    for candidate in _candidates(requested, accept_language):
        if candidate in TRANSLATIONS:
            return candidate
        # "en-GB" zählt als "en".
        base = candidate.split("-", 1)[0]
        if base in TRANSLATIONS:
            return base
    return DEFAULT_LOCALE


def _candidates(requested: str | None, accept_language: str | None) -> list[str]:
    wanted: list[str] = []
    if requested:
        wanted.append(requested.strip().replace("_", "-").lower())
    for part in (accept_language or "").split(","):
        # "de-DE;q=0.9" -> "de-de"
        tag = part.split(";", 1)[0].strip().replace("_", "-").lower()
        if tag and tag != "*":
            wanted.append(tag)
    return wanted


def translate(key: str, locale: str = DEFAULT_LOCALE) -> str:
    """Übersetzung für ``key``; fehlt sie, greift Deutsch, sonst der Schlüssel."""

    table = TRANSLATIONS.get(locale, {})
    fallback = TRANSLATIONS[DEFAULT_LOCALE]
    return table.get(key) or fallback.get(key) or key


def messages(locale: str = DEFAULT_LOCALE) -> dict[str, str]:
    """Alle Texte dieser Sprache, mit Deutsch als Lückenfüller."""

    merged = dict(TRANSLATIONS[DEFAULT_LOCALE])
    merged.update(TRANSLATIONS.get(locale, {}))
    return merged


def sound_tags(locale: str = DEFAULT_LOCALE) -> list[dict[str, str]]:
    """Klangeffekte, alphabetisch nach der Übersetzung dieser Sprache."""

    entries = [
        {
            "key": key,
            "tag": f"[{key}]",
            "label": translate(f"tag.{key}", locale),
        }
        for key in SOUND_TAGS
    ]
    entries.sort(key=lambda entry: sort_key(entry["label"]))
    return entries


def catalogue(locale: str = DEFAULT_LOCALE) -> dict[str, Any]:
    """Was die Oberfläche für eine Sprache braucht, in einer Antwort."""

    return {
        "locale": locale,
        "default_locale": DEFAULT_LOCALE,
        "locales": list(LOCALES),
        "messages": messages(locale),
        "sound_tags": sound_tags(locale),
    }
