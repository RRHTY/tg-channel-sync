(() => {
const AppCard = {
  template: `<section class="card"><slot></slot></section>`,
};

const SectionHeader = {
  props: ["title", "description", "titleClass", "descriptionClass"],
  template: `<div class="section-header">
    <h2 :class="titleClass || 'section-title'">{{ $t(title) }}</h2>
    <p v-if="description" :class="descriptionClass || 'section-description'">{{ $t(description) }}</p>
  </div>`,
};

const FormSection = {
  props: ["title", "description", "titleClass", "descriptionClass", "bodyClass"],
  components: { SectionHeader },
  template: `<section class="form-section">
    <section-header
      :title="title"
      :description="description"
      :title-class="titleClass"
      :description-class="descriptionClass"
    ></section-header>
    <div :class="bodyClass || 'form-section-body'"><slot></slot></div>
  </section>`,
};

let fieldGroupSequence = 0;
const FieldGroup = {
  props: ["label", "hint", "labelClass", "hintClass", "wrapperClass", "badge", "badgeClass"],
  data() { return { fieldId: "tgcs-field-" + (++fieldGroupSequence) }; },
  mounted() { this.connectLabel(); },
  updated() { this.connectLabel(); },
  methods: {
    connectLabel() {
      const label = this.$el.querySelector(".field-label-row label");
      const control = this.$el.querySelector('input:not([type="checkbox"]):not([type="radio"]), select, textarea');
      if (!label) return;
      if (!control) { label.removeAttribute("for"); return; }
      if (!control.id) control.id = this.fieldId;
      label.htmlFor = control.id;
      if (this.hint) {
        const ids = new Set((control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
        ids.add(this.fieldId + "-hint");
        control.setAttribute("aria-describedby", [...ids].join(" "));
      }
    },
  },
  template: `<div :class="wrapperClass || 'field-group'">
    <div v-if="label || badge" class="field-label-row">
      <label v-if="label" :class="labelClass || 'field-label'">{{ $t(label) }}</label>
      <span
        v-if="badge"
        :class="badgeClass || 'field-badge'"
      >{{ $t(badge) }}</span>
    </div>
    <slot></slot>
    <p v-if="hint" :id="fieldId + '-hint'" :class="hintClass || 'field-hint'">{{ $t(hint) }}</p>
  </div>`,
};

const FieldBadge = {
  props: ["text", "tone"],
  template: `<span
    class="field-badge"
    :class="tone === 'muted' ? 'field-badge-muted' : ''"
  >{{ $t(text) }}</span>`,
};

const ActionBar = {
  props: ["className"],
  template: `<div :class="className || 'action-bar'"><slot></slot></div>`,
};

const ToastBanner = {
  props: ["notice"],
  template: `<div v-if="notice && notice.message" role="status" aria-live="polite" class="mb-4 rounded-xl border px-4 py-3 text-sm shadow-sm"
    :class="notice.type === 'error' ? 'border-red-200 bg-red-50 text-red-700' : 'border-slate-200 bg-slate-50 text-slate-700'">
    {{ $tm(notice.message) }}
  </div>`,
};

const EmptyState = {
  props: ["text"],
  template: `<div class="empty-state">{{ $t(text || '暂无数据') }}</div>`,
};

const SettingSectionNav = {
  props: ["items"],
  template: `<nav class="settings-section-nav" :aria-label="$t('设置分区导航')">
    <a
      v-for="item in items"
      :key="item.id"
      :href="'#' + item.id"
      class="settings-section-link"
    >{{ $t(item.label) }}</a>
  </nav>`,
};

const SettingGroup = {
  props: ["title", "description", "badge", "bodyClass"],
  components: { FieldBadge },
  template: `<section class="settings-group">
    <div class="settings-group-header">
      <div class="settings-group-copy">
        <div class="settings-group-title-row">
          <h3 class="settings-group-title">{{ $t(title) }}</h3>
          <field-badge v-if="badge" :text="badge" tone="muted"></field-badge>
        </div>
        <p v-if="description" class="settings-group-description">{{ $t(description) }}</p>
      </div>
    </div>
    <div :class="bodyClass || 'settings-group-body'"><slot></slot></div>
  </section>`,
};

const ToggleField = {
  props: ["label", "description", "checked", "disabled", "badge"],
  emits: ["update:checked"],
  components: { FieldBadge },
  methods: {
    onChange(event) {
      this.$emit("update:checked", event.target.checked);
    },
  },
  template: `<label
    class="toggle-field"
    :class="{ 'toggle-field-disabled': disabled }"
  >
    <span class="toggle-field-copy">
      <span class="toggle-field-title-row">
        <span class="toggle-field-title">{{ $t(label) }}</span>
        <field-badge v-if="badge" :text="badge" tone="muted"></field-badge>
      </span>
      <span v-if="description" class="toggle-field-description">{{ $t(description) }}</span>
    </span>
    <input
      type="checkbox"
      class="toggle-field-checkbox"
      :checked="checked"
      :disabled="disabled"
      @change="onChange"
    >
  </label>`,
};

const MappingOptionBadges = {
  props: ["item"],
  computed: {
    badges() {
      const item = this.item || {};
      const values = [this.$t("发送：{sender}", { sender: this.$t(item.realtime_sender === "user" ? "Telegram 账号" : "机器人") })];
      if (item.source_mode === "public_user") {
        values.push(this.$t("读取：{reader}", { reader: this.$t("Telegram 账号") }));
      } else {
        values.push(this.$t("读取：{reader}", { reader: item.realtime_fallback_to_user ? "Bot / " + this.$t("Telegram 账号") : "Bot" }));
      }
      if (item.realtime_hash_perturb) values.push(this.$t("修改文件哈希"));
      return values;
    },
  },
  template: `<div class="flex flex-wrap gap-1.5">
    <span v-for="badge in badges" :key="badge" class="mapping-badge">{{ badge }}</span>
  </div>`,
};

const SenderIdentityOptions = {
  props: [
    "sender",
    "fallbackValue",
    "hashValue",
    "showHashOption",
    "fallbackTrueValue",
    "fallbackFalseValue",
    "hashTrueValue",
    "hashFalseValue",
  ],
  emits: ["update:sender", "update:fallback", "update:hash"],
  computed: {
    fallbackChecked() {
      return String(this.fallbackValue) === String(this.fallbackTrueValue ?? true);
    },
    hashChecked() {
      return String(this.hashValue) === String(this.hashTrueValue ?? true);
    },
  },
  methods: {
    onSenderChange(event) {
      this.$emit("update:sender", event.target.value);
    },
    onFallbackChange(event) {
      this.$emit("update:fallback", event.target.checked ? (this.fallbackTrueValue ?? true) : (this.fallbackFalseValue ?? false));
    },
    onHashChange(event) {
      this.$emit("update:hash", event.target.checked ? (this.hashTrueValue ?? true) : (this.hashFalseValue ?? false));
    },
  },
  template: `<div class="identity-panel">
    <div class="identity-row">
      <span class="identity-label">{{ $t('用谁发送') }}</span>
      <label class="identity-option"><input type="radio" :checked="sender === 'bot'" value="bot" @change="onSenderChange">{{ $t('机器人') }}</label>
      <label class="identity-option"><input type="radio" :checked="sender === 'user'" value="user" @change="onSenderChange">{{ $t('Telegram 账号') }}</label>
    </div>
    <label v-if="sender === 'bot'" class="identity-option text-xs text-slate-600">
      <input type="checkbox" :checked="fallbackChecked" @change="onFallbackChange">{{ $t('机器人发送失败时，改用 Telegram 账号') }}
    </label>
    <label v-if="showHashOption" class="identity-option text-xs text-slate-600">
      <input type="checkbox" :checked="hashChecked" @change="onHashChange">{{ $t('修改图片和视频的哈希（画面不变）') }}
    </label>
  </div>`,
};

const LogPanel = {
  components: { AppCard },
  props: ["title", "description", "logs", "kind", "panelId"],
  emits: ["clear", "export"],
  methods: {
    levelClass(value) {
      const text = String(value || "");
      if (text === "ERROR" || text.includes("ERROR") || text.includes("DROP")) return "log-level-error";
      if (text === "WARNING" || text.includes("WARN") || text.includes("FALLBACK")) return "log-level-warning";
      if (text === "SUCCESS" || text.includes("SEND") || text.includes("MAP")) return "log-level-success";
      if (text === "HASH_PERTURB_SKIP") return "log-level-info";
      if (text.includes("SKIP")) return "log-level-skip";
      if (text.includes("REWRITE")) return "log-level-rewrite";
      return "log-level-info";
    },
    label(log) {
      return this.kind === "message" ? log.action : log.level;
    },
    body(log) {
      return this.kind === "message" ? log.detail : log.msg;
    },
    scrollToBottom() {
      const panel = document.getElementById(this.panelId);
      if (!panel) return;
      panel.scrollTop = panel.scrollHeight;
    },
  },
  template: `<app-card>
    <div class="panel-heading">
      <div>

        <h2 class="panel-title">{{ $t(title) }}</h2>

      </div>
      <div class="log-actions">
        <button @click="$emit('export')" class="btn-secondary btn-inline !px-3 !py-1 text-xs">{{ $t('导出') }}</button>
        <button @click="scrollToBottom" class="btn-secondary btn-inline !px-3 !py-1 text-xs">{{ $t('跳至底部') }}</button>
        <button @click="$emit('clear')" class="delete-action">{{ $t('清理') }}</button>
      </div>
    </div>
    <div :id="panelId" class="log-panel">
      <div v-for="log in logs" :key="log.id" class="border-b border-slate-800 pb-2 text-slate-200">
        <div class="grid grid-cols-[150px_minmax(0,1fr)] items-start gap-3">
          <div class="shrink-0 space-y-1">
            <div class="text-[11px] leading-tight text-slate-500">[{{ log.time }}]</div>
            <div :class="levelClass(label(log))" class="inline-block rounded border px-1.5 py-0.5 text-[10px] leading-none font-semibold">[{{ label(log) }}]</div>
          </div>
          <div class="min-w-0 break-all text-slate-200">{{ body(log) }}</div>
        </div>
      </div>
      <div v-if="!(logs || []).length" class="text-slate-500">{{ $t('暂无日志') }}</div>
    </div>
  </app-card>`,
};

const LanguageSelect = {
  components: { FieldGroup },
  template: `<field-group label="语言 / Language" wrapper-class="field-group language-select">
    <select class="input-box" :value="$languagePreference" @change="$setLanguage($event.target.value)">
      <option value="auto">{{ $t('跟随浏览器') }}</option>
      <option value="zh" lang="zh-CN">简体中文</option>
      <option value="en" lang="en">English</option>
    </select>
  </field-group>`,
};

const LanguageMenu = {
  methods: {
    choose(value) {
      this.$setLanguage(value);
      this.$el.open = false;
      this.$el.querySelector('summary')?.focus();
    },
  },
  template: `<details class="language-menu" @keydown.esc="$el.open = false; $el.querySelector('summary').focus()">
    <summary :aria-label="$t('切换语言')" :title="$t('切换语言')">
      <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
        <circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z"/>
      </svg>
    </summary>
    <div class="language-menu-popover" role="group" :aria-label="$t('语言 / Language')">
      <button type="button" :aria-pressed="$languagePreference === 'auto'" @click="choose('auto')">{{ $t('跟随浏览器') }}</button>
      <button type="button" lang="zh-CN" :aria-pressed="$languagePreference === 'zh'" @click="choose('zh')">简体中文</button>
      <button type="button" lang="en" :aria-pressed="$languagePreference === 'en'" @click="choose('en')">English</button>
    </div>
  </details>`,
};

window.TgcsUi = {
  LanguageSelect,
  LanguageMenu,
  AppCard,
  SectionHeader,
  FormSection,
  FieldGroup,
  FieldBadge,
  ActionBar,
  ToastBanner,
  EmptyState,
  SettingSectionNav,
  SettingGroup,
  ToggleField,
  MappingOptionBadges,
  SenderIdentityOptions,
  LogPanel,
};
})();
