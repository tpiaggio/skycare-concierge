// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

(() => {
  const LANG_STORAGE_KEY = "skycare_lang";

  function resolveInitialLang() {
    const queryLang = new URLSearchParams(window.location.search).get("lang");
    if (queryLang === "es" || queryLang === "en") return queryLang;
    try {
      const saved = window.localStorage.getItem(LANG_STORAGE_KEY);
      if (saved === "es" || saved === "en") return saved;
    } catch {
      // Ignore storage restrictions.
    }
    return "en";
  }

  function saveLang(lang) {
    const normalized = lang === "es" ? "es" : "en";
    try {
      window.localStorage.setItem(LANG_STORAGE_KEY, normalized);
    } catch {
      // Ignore storage restrictions.
    }
    return normalized;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function formatUsd(amount, locale = "en-US") {
    const num = Number(amount);
    if (!Number.isFinite(num)) return String(amount);
    try {
      return new Intl.NumberFormat(locale, {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: num % 1 === 0 ? 0 : 2,
        maximumFractionDigits: 2,
      }).format(num);
    } catch {
      return `$${num}`;
    }
  }

  function formatTimestamp(isoText, locale = "en-US") {
    if (!isoText) return "";
    const parsed = new Date(isoText);
    if (Number.isNaN(parsed.getTime())) return "";
    try {
      return new Intl.DateTimeFormat(locale, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(parsed);
    } catch {
      return isoText;
    }
  }

  function applyI18nAttributes(dict) {
    for (const node of document.querySelectorAll("[data-i18n]")) {
      const key = node.getAttribute("data-i18n");
      if (key && typeof dict[key] === "string") {
        node.textContent = dict[key];
      }
    }
    for (const node of document.querySelectorAll("[data-i18n-title]")) {
      const key = node.getAttribute("data-i18n-title");
      if (key && typeof dict[key] === "string") {
        node.title = dict[key];
      }
    }
    for (const node of document.querySelectorAll("[data-i18n-placeholder]")) {
      const key = node.getAttribute("data-i18n-placeholder");
      if (key && typeof dict[key] === "string") {
        node.placeholder = dict[key];
      }
    }
  }

  window.SkyCareCommon = {
    LANG_STORAGE_KEY,
    resolveInitialLang,
    saveLang,
    el,
    formatUsd,
    formatTimestamp,
    applyI18nAttributes,
  };
})();
