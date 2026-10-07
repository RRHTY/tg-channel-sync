(() => {
  const languages = ["zh", "en"];
  const normalizePreference = value => languages.includes(value) ? value : "auto";

  function resolveLanguage(preference, browserLanguages = []) {
    if (languages.includes(preference)) return preference;
    for (const value of browserLanguages) {
      const match = /^(zh|en)(?:-|$)/i.exec(String(value || "").trim().replace(/_/g, "-"));
      if (match) return match[1].toLowerCase();
    }
    return "en";
  }

  function createI18n(options = {}) {
    const browser = options.navigator ?? (typeof navigator === "undefined" ? {} : navigator);
    const page = options.document ?? (typeof document === "undefined" ? null : document);
    let storage = options.storage;
    if (storage === undefined) {
      try { storage = localStorage; } catch (_) {}
    }
    const browserLanguages = () => browser.languages?.length ? browser.languages : [browser.language];
    let preference = "auto";
    try { preference = normalizePreference(storage?.getItem("tgcs-language")); } catch (_) {}
    const reactive = options.reactive ?? (typeof Vue === "undefined" ? value => value : Vue.reactive);
    const state = reactive({ preference, language: resolveLanguage(preference, browserLanguages()) });
    const english = window.TgcsLocales?.en || {};
    const interpolate = (text, params = {}) => String(text).replace(/\{(\w+)\}/g, (match, name) =>
      Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match);

    function t(text, params) {
      if (text === null || text === undefined) return "";
      const source = String(text);
      const translated = state.language === "en" && Object.prototype.hasOwnProperty.call(english, source)
        ? english[source] : source;
      return interpolate(translated, params);
    }

    // Match only known application messages; captured names, paths and error details stay intact.
    const messagePatterns = (window.TgcsLocales?.messagePatterns || []).map(key => {
      const names = [];
      const chunks = key.split(/(\{\w+\})/g).map(chunk => {
        if (/^\{\w+\}$/.test(chunk)) { names.push(chunk.slice(1, -1)); return "(.*?)"; }
        return chunk.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      });
      return { key, names, pattern: new RegExp("^" + chunks.join("") + "$", "s") };
    });

    function translateMessage(text) {
      if (text === null || text === undefined) return "";
      const source = String(text);
      if (state.language !== "en" || Object.prototype.hasOwnProperty.call(english, source)) return t(source);
      for (const { key, names, pattern } of messagePatterns) {
        const match = pattern.exec(source);
        if (match) return t(key, Object.fromEntries(names.map((name, index) => [name, match[index + 1]])));
      }
      return source;
    }

    function setLanguage(value, remember = true) {
      state.preference = normalizePreference(value);
      state.language = resolveLanguage(state.preference, browserLanguages());
      if (remember) {
        try { storage?.setItem("tgcs-language", state.preference); } catch (_) {}
      }
      if (page?.documentElement) page.documentElement.lang = state.language === "zh" ? "zh-CN" : "en";
      if (page) page.title = t("杏铃同步台");
      return state.language;
    }

    function install(app) {
      app.mixin({
        computed: {
          $language() { return state.language; },
          $languagePreference() { return state.preference; },
        },
        methods: { $t: t, $tm: translateMessage, $setLanguage: setLanguage },
      });
    }

    setLanguage(preference, false);
    return { state, t, translateMessage, setLanguage, install };
  }

  window.TgcsI18n = { ...createI18n(), createI18n, resolveLanguage };
  window.addEventListener?.("languagechange", () => {
    if (window.TgcsI18n.state.preference === "auto") window.TgcsI18n.setLanguage("auto", false);
  });
  window.addEventListener?.("storage", event => {
    if (event.key === "tgcs-language") window.TgcsI18n.setLanguage(event.newValue, false);
  });
})();
