"use strict";

// Oberflächensprache: Der Server hält die Übersetzungen (siehe
// omnivoice_server/i18n.py), hier landet nur, was der Browser davon braucht.
// Ohne Antwort bleibt Deutsch stehen – so wie es im HTML steht.
const I18N = {
  locale: "de",
  defaultLocale: "de",
  locales: ["de"],
  messages: {},
  soundTags: [],

  // Wunschsprache des Browsers: "de-DE" reicht, der Server macht daraus "de".
  preferred() {
    const wanted = navigator.languages && navigator.languages.length
      ? navigator.languages[0]
      : navigator.language;
    return wanted || "";
  },

  t(key, fallback = "") {
    return this.messages[key] || fallback || key;
  },

  async load() {
    const query = this.preferred() ? `?locale=${encodeURIComponent(this.preferred())}` : "";
    const data = await (await fetch(`/api/i18n${query}`)).json();
    this.locale = data.locale || this.locale;
    this.defaultLocale = data.default_locale || this.defaultLocale;
    this.locales = data.locales || this.locales;
    this.messages = data.messages || {};
    this.soundTags = data.sound_tags || [];
    document.documentElement.lang = this.locale;
    this.apply();
    return this;
  },

  // Alles mit data-i18n="schlüssel" bekommt seinen übersetzten Text;
  // data-i18n-aria füllt zusätzlich aria-label.
  apply(root = document) {
    for (const node of root.querySelectorAll("[data-i18n]")) {
      const text = this.messages[node.dataset.i18n];
      if (text) node.textContent = text;
    }
    for (const node of root.querySelectorAll("[data-i18n-aria]")) {
      const text = this.messages[node.dataset.i18nAria];
      if (text) node.setAttribute("aria-label", text);
    }
  },
};
