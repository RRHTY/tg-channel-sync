(() => {
const AppCard = {
  template: `<section class="card"><slot></slot></section>`,
};

const SectionHeader = {
  props: ["title", "description", "titleClass", "descriptionClass"],
  template: `<div class="section-header">
    <h2 :class="titleClass || 'section-title'">{{ title }}</h2>
    <p v-if="description" :class="descriptionClass || 'section-description'">{{ description }}</p>
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

const FieldGroup = {
  props: ["label", "hint", "labelClass", "hintClass", "wrapperClass", "badge", "badgeClass"],
  template: `<div :class="wrapperClass || 'field-group'">
    <div v-if="label || badge" class="field-label-row">
      <label v-if="label" :class="labelClass || 'field-label'">{{ label }}</label>
      <span
        v-if="badge"
        :class="badgeClass || 'field-badge'"
      >{{ badge }}</span>
    </div>
    <slot></slot>
    <p v-if="hint" :class="hintClass || 'field-hint'">{{ hint }}</p>
  </div>`,
};

const FieldBadge = {
  props: ["text", "tone"],
  template: `<span
    class="field-badge"
    :class="tone === 'muted' ? 'field-badge-muted' : ''"
  >{{ text }}</span>`,
};

const ActionBar = {
  props: ["className"],
  template: `<div :class="className || 'action-bar'"><slot></slot></div>`,
};

const ToastBanner = {
  props: ["notice"],
  template: `<div v-if="notice && notice.message" role="status" aria-live="polite" class="mb-4 rounded-xl border px-4 py-3 text-sm shadow-sm"
    :class="notice.type === 'error' ? 'border-red-200 bg-red-50 text-red-700' : 'border-slate-200 bg-slate-50 text-slate-700'">
    {{ notice.message }}
  </div>`,
};

const EmptyState = {
  props: ["text"],
  template: `<div class="empty-state">{{ text || '暂无数据' }}</div>`,
};

const SettingSectionNav = {
  props: ["items"],
  template: `<nav class="settings-section-nav" aria-label="设置分区导航">
    <a
      v-for="item in items"
      :key="item.id"
      :href="'#' + item.id"
      class="settings-section-link"
    >{{ item.label }}</a>
  </nav>`,
};

const SettingGroup = {
  props: ["title", "description", "badge", "bodyClass"],
  components: { FieldBadge },
  template: `<section class="settings-group">
    <div class="settings-group-header">
      <div class="settings-group-copy">
        <div class="settings-group-title-row">
          <h3 class="settings-group-title">{{ title }}</h3>
          <field-badge v-if="badge" :text="badge" tone="muted"></field-badge>
        </div>
        <p v-if="description" class="settings-group-description">{{ description }}</p>
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
        <span class="toggle-field-title">{{ label }}</span>
        <field-badge v-if="badge" :text="badge" tone="muted"></field-badge>
      </span>
      <span v-if="description" class="toggle-field-description">{{ description }}</span>
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
      const values = [`发送:${item.realtime_sender === "user" ? "辅助账号" : "Bot"}`];
      if (item.source_mode === "public_user") {
        values.push("获取:辅助账号");
      } else {
        values.push(`获取:${item.realtime_fallback_to_user ? "Bot/辅助账号" : "Bot"}`);
      }
      if (item.realtime_hash_perturb) values.push("重置指纹");
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
      <span class="identity-label">发送身份</span>
      <label class="identity-option"><input type="radio" :checked="sender === 'bot'" value="bot" @change="onSenderChange">Bot</label>
      <label class="identity-option"><input type="radio" :checked="sender === 'user'" value="user" @change="onSenderChange">辅助账号</label>
    </div>
    <label v-if="sender === 'bot'" class="identity-option text-xs text-slate-600">
      <input type="checkbox" :checked="fallbackChecked" @change="onFallbackChange">Bot 发送失败时改用辅助账号继续发送
    </label>
    <label v-if="showHashOption" class="identity-option text-xs text-slate-600">
      <input type="checkbox" :checked="hashChecked" @change="onHashChange">重置图片/视频指纹
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
      if (text === "ERROR" || text.includes("ERROR") || text.includes("DROP")) return "text-red-300 bg-red-950/60 border-red-900/70";
      if (text === "WARNING" || text.includes("WARN") || text.includes("FALLBACK")) return "text-amber-300 bg-amber-950/50 border-amber-900/70";
      if (text === "SUCCESS" || text.includes("SEND") || text.includes("MAP")) return "text-emerald-300 bg-emerald-950/50 border-emerald-900/70";
      if (text === "HASH_PERTURB_SKIP") return "text-sky-200 bg-slate-800/70 border-slate-700";
      if (text.includes("SKIP")) return "text-yellow-200 bg-yellow-950/40 border-yellow-900/60";
      if (text.includes("REWRITE")) return "text-cyan-300 bg-cyan-950/40 border-cyan-900/60";
      return "text-sky-200 bg-slate-800/70 border-slate-700";
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
        <div class="panel-kicker">运行记录</div>
        <h2 class="panel-title">{{ title }}</h2>
        <p class="panel-description">{{ description }}</p>
      </div>
      <div class="log-actions">
        <button @click="$emit('export')" class="btn-secondary btn-inline !px-3 !py-1 text-xs">导出</button>
        <button @click="scrollToBottom" class="btn-secondary btn-inline !px-3 !py-1 text-xs">跳至底部</button>
        <button @click="$emit('clear')" class="delete-action">清理</button>
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
      <div v-if="!(logs || []).length" class="text-slate-500">暂无{{ title }}</div>
    </div>
  </app-card>`,
};

window.TgcsUi = {
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
