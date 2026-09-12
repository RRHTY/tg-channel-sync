const { createApp } = Vue;
const {
  AppCard,
  SectionHeader,
  FormSection,
  FieldGroup,
  FieldBadge,
  ActionBar,
  ToastBanner,
  LogPanel,
  SettingSectionNav,
  SettingGroup,
  ToggleField,
  MappingOptionBadges,
  EmptyState,
  SenderIdentityOptions,
} = window.TgcsUi;
const HELP_LINK = "https://github.com/RRHTY/tg-channel-sync/issues/2";

const BotApiHint = {
  template: `<p class="text-xs text-gray-500 mt-1">可选。自行搭建 BOT API 可突破 Bot 上传 50M 限制，参考 <a :href="helpLink" target="_blank" class="text-blue-600 hover:underline">#2</a></p>`,
  data(){ return { helpLink: HELP_LINK }; }
};

const SetupWizard = {
  props:["config","saving"], components:{ AppCard, SectionHeader, FormSection, FieldGroup, ActionBar, BotApiHint },
  template:`<app-card class="max-w-3xl mx-auto"><section-header title="初始化向导" description="填写基础配置后即可开始使用，配置会保存到程序目录下的 config.json。"></section-header><form-section title="Bot 配置" title-class="text-sm font-bold text-gray-800"><div class="form-stack"><field-group label="BOT_TOKEN（必填）"><input v-model="config.telegram.bot_token" type="text" class="input-box"></field-group><field-group label="BOT_API_BASE_URL（可选，例如 http://127.0.0.1:8081）"><input v-model="config.telegram.bot_api_base_url" type="text" class="input-box"><bot-api-hint></bot-api-hint></field-group></div></form-section><form-section title="高级配置" description="仅在使用 API 复制、下载重传等模式时需要。" title-class="text-sm font-bold text-gray-800"><div class="field-grid field-grid-md-2"><field-group label="API_ID"><input v-model="config.telegram.api_id" type="number" class="input-box"></field-group><field-group label="API_HASH"><input v-model="config.telegram.api_hash" type="text" class="input-box"></field-group></div></form-section><form-section title="代理配置" title-class="text-sm font-bold text-gray-800"><div class="form-stack"><label class="flex items-center text-sm"><input v-model="config.proxy.enabled" type="checkbox" class="mr-2">启用代理</label><div class="field-grid field-grid-md-2" :class="{ 'opacity-50': !config.proxy.enabled }"><field-group label="HOST"><input v-model="config.proxy.host" type="text" class="input-box"></field-group><field-group label="PORT"><input v-model="config.proxy.port" type="number" class="input-box"></field-group><field-group label="USERNAME"><input v-model="config.proxy.username" type="text" class="input-box"></field-group><field-group label="PASSWORD"><input v-model="config.proxy.password" type="password" class="input-box"></field-group></div></div></form-section><action-bar class-name="action-bar mt-6"><button @click="$emit('save', true)" :disabled="saving" class="btn-primary">保存并重启</button><button @click="$emit('save', false)" :disabled="saving" class="btn-secondary">仅保存配置</button></action-bar></app-card>`
};

const StatusOverview = {
  props:["appInfo","status"],
  methods:{ tone(v){ if(["已连接","已登录","运行中"].includes(v)) return "status-tone-positive"; if(["初始化中","连接超时","等待验证码","等待两步验证","需要登录"].includes(v)) return "status-tone-warning"; if(["启动失败","未配置"].includes(v)) return "status-tone-negative"; return "status-tone-info"; } },
  template:`<section class="status-grid" aria-label="连接与任务状态">
    <div class="status-card" :class="tone(appInfo.bot.status)"><div class="status-label">Bot</div><div class="status-value">{{ appInfo.bot.status || '未配置' }}</div><div class="status-detail">{{ appInfo.bot.name || '未连接' }}</div></div>
    <div class="status-card" :class="tone(appInfo.user.status)"><div class="status-label">辅助账号</div><div class="status-value">{{ appInfo.user.status === '需要登录' ? '未登录' : (appInfo.user.status || '未配置') }}</div><div v-if="appInfo.user.status === '需要登录'" class="status-detail"><button @click="$emit('open-settings')" class="font-semibold underline">前往设置登录</button></div><div v-else class="status-detail">{{ appInfo.user.name || '未登录' }}</div></div>
    <div class="status-card" :class="tone(status.is_syncing ? '运行中' : '空闲')"><div class="status-label">任务状态</div><div class="status-value">{{ status.is_syncing ? '运行中' : (status.result?.label || '空闲') }}</div><div class="status-detail">{{ status.mode || '等待任务' }}</div></div>
    <div class="status-card status-tone-neutral"><div class="status-label">同步进度</div><div class="status-value">{{ status.current || 0 }} / {{ status.total || 0 }}</div><div class="status-detail">已跳过 {{ status.skipped || 0 }} 条</div></div>
  </section>`
};

const ChannelMapping = {
  props:["mappings", "saveMapping", "updateMapping"],
  components:{ AppCard, FieldGroup, MappingOptionBadges, EmptyState },
  data(){ return { source:"", target:"", realtime_sender:"bot", realtime_fallback_to_user:true, realtime_hash_perturb:false, to_saved:false, saving:false, editing:null, editForm:{}, actionKey:"" }; },
  computed:{ mappingCount(){ return this.mappings?.mappings?.length || 0; } },
  methods:{
    async saveRule(){
      if(!this.source.trim() || (!this.to_saved && !this.target.trim())){
        this.$emit("log-error", "请填写源频道和目标频道"); return;
      }
      this.saving = true;
      try {
        const saved = await this.saveMapping(this.source, this.target, {
          realtime_sender: this.to_saved ? "user" : this.realtime_sender,
          realtime_fallback_to_user: this.realtime_fallback_to_user ? "1" : "0",
          realtime_hash_perturb: this.realtime_hash_perturb ? "1" : "0",
          target_type: this.to_saved ? "saved" : "channel",
        });
        if(saved){ this.source = ""; this.target = ""; }
      } finally { this.saving = false; }
    },
    edit(item){
      this.editing = item;
      this.editForm = { source_title:item.source_title, target_title:item.target_title,
        realtime_sender:item.realtime_sender, realtime_fallback_to_user:item.realtime_fallback_to_user,
        realtime_hash_perturb:item.realtime_hash_perturb };
    },
    async saveEdit(){
      this.saving = true;
      try { if(await this.updateMapping(this.editing, this.editForm)) this.editing = null; }
      finally { this.saving = false; }
    },
    async toggle(item){
      this.actionKey = item.source_id + ":" + item.target_id;
      try { await this.updateMapping(item, { enabled: !item.enabled }); }
      finally { this.actionKey = ""; }
    },
    remove(item){
      if(window.confirm("删除这条映射？已同步消息和去重记录会保留。")) this.$emit("del", item.source_id, item.target_id);
    }
  },
  template:`
    <app-card>
      <div class="panel-heading"><div><div class="panel-kicker">Realtime</div><h2 class="panel-title">频道映射</h2></div><span class="field-badge field-badge-muted">{{ mappingCount }} 条</span></div>
      <div class="mapping-form">
        <div class="form-row">
          <field-group label="源频道"><input v-model="source" :disabled="saving" placeholder="ID、@用户名或 t.me 链接" class="input-box"></field-group>
          <field-group v-if="!to_saved" label="目标频道"><input v-if="!to_saved" v-model="target" :disabled="saving" placeholder="ID、@用户名或 t.me 链接" class="input-box"></field-group>
        </div>
        <label class="identity-option"><input type="checkbox" v-model="to_saved" :disabled="saving">发送到收藏夹</label>
        <details class="compact-details"><summary>发送策略<span>按需调整</span></summary>
          <div class="form-stack">
            <field-group label="发送身份"><select v-model="realtime_sender" :disabled="to_saved" class="input-box"><option value="bot">Bot</option><option value="user">辅助账号</option></select></field-group>
            <label class="identity-option"><input type="checkbox" v-model="realtime_fallback_to_user">允许使用辅助账号读取或回退发送</label>
            <label class="identity-option"><input type="checkbox" v-model="realtime_hash_perturb">重置图片 / 视频指纹</label>
          </div>
        </details>
        <button @click="saveRule" :disabled="saving" class="btn-primary">{{ saving ? '保存中…' : '添加映射' }}</button>
      </div>
      <div class="mapping-scroll mt-4 space-y-2">
        <article v-for="item in mappings.mappings || []" :key="item.source_id + ':' + item.target_id" class="mapping-entry" :class="{ 'mapping-paused': !item.enabled }">
          <div class="mapping-route"><strong :title="item.source_id">{{ item.source_title || item.source_id }}</strong><span aria-hidden="true">→</span><strong :title="item.target_id">{{ item.target_title || item.target_id }}</strong></div>
          <div class="mapping-meta"><span>{{ item.enabled ? '已启用' : '已暂停' }}</span><span>{{ item.source_mode === 'public_user' ? '公开频道读取' : 'Bot 监听' }}</span></div>
          <div class="inline-actions">
            <button @click="$emit('use', item)">填入历史同步</button><button @click="edit(item)">编辑</button>
            <button :disabled="!!actionKey" @click="toggle(item)">{{ item.enabled ? '暂停' : '恢复' }}</button><button @click="remove(item)" class="danger-text">删除</button>
          </div>
          <div v-if="editing && editing.source_id === item.source_id && editing.target_id === item.target_id" class="mapping-editor form-stack">
            <div class="form-row"><field-group label="来源显示名称"><input v-model="editForm.source_title" maxlength="100" class="input-box"></field-group><field-group label="目标显示名称"><input v-model="editForm.target_title" maxlength="100" class="input-box"></field-group></div>
            <field-group label="发送身份"><select v-model="editForm.realtime_sender" :disabled="item.source_mode === 'public_user' || item.target_type === 'saved'" class="input-box"><option value="bot">Bot</option><option value="user">辅助账号</option></select></field-group>
            <label class="identity-option"><input type="checkbox" v-model="editForm.realtime_fallback_to_user">发送失败时允许辅助账号回退</label>
            <label class="identity-option"><input type="checkbox" v-model="editForm.realtime_hash_perturb">重置图片 / 视频指纹</label>
            <div class="inline-actions"><button :disabled="saving" @click="saveEdit">保存修改</button><button :disabled="saving" @click="editing=null">取消</button></div>
          </div>
        </article>
        <empty-state v-if="!mappingCount" text="添加映射后自动同步新消息"></empty-state>
      </div>
      <p class="field-hint mt-3">暂停从下一条起生效。公开频道恢复后补齐积压；Bot 监听暂停期间的消息可通过历史同步补齐。</p>
    </app-card>
  `
};
const SyncPanel = {
  components:{ FieldGroup, SenderIdentityOptions },
  props:["status","form","stopping","starting","userAuth","hasLastParams"],
  computed:{
    supportsSenderOptions(){ return (this.form.mode === "json" || this.form.mode === "clone") && this.form.target_type !== "saved"; },
    supportsHashPerturb(){ return this.form.mode === "json" || this.form.mode === "clone"; },
    modeHint(){ return { api:"直接复制历史消息，适合日常补齐；需要辅助账号读取源频道。", clone:"下载后重新上传，适合迁移媒体；耗时和磁盘占用较高。", json:"导入 Telegram Desktop 导出的单个聊天 JSON；媒体文件须保留在导出目录中。" }[this.form.mode]; },
    needsLogin(){ return (this.form.mode !== "json" || this.form.sender === "user" || this.form.target_type === "saved") && this.userAuth?.status !== "authorized"; }
  },
  template:`<div class="card" id="history-sync">
    <div class="panel-heading"><div><div class="panel-kicker">History</div><h2 class="panel-title">历史同步</h2></div><span v-if="status.is_syncing" class="field-badge">运行中</span></div>
    <div class="progress-shell"><template v-if="status.is_syncing"><div class="sync-progress-meta mb-2 flex justify-between text-xs font-semibold"><span>{{ status.mode }}</span><span>{{ status.current }} / {{ status.total }}</span></div><div class="sync-progress-track mb-3"><div class="sync-progress-value" :style="{ width: (status.total > 0 ? status.current / status.total * 100 : 0) + '%' }"></div></div><p class="break-all text-xs text-slate-500">{{ status.current_text || ('已跳过 ' + status.skipped + ' 条') }}</p></template><div v-else-if="status.result" role="status" class="text-sm"><p>{{ status.result.label }} · 成功 {{ status.result.sent }} · 跳过 {{ status.result.skipped }} · 失败 {{ status.result.failed }}</p><p v-if="status.result.unmapped" class="text-amber-700">{{ status.result.unmapped }} 条发送结果待核对；重跑可能重复发送。</p><p v-if="status.result.failed_batches" class="text-amber-700">{{ status.result.failed_batches }} 批消息读取失败，详见日志。</p><p v-if="status.result.error" class="break-all text-red-600">{{ status.result.error }}</p></div><div v-else class="flex min-h-[56px] items-center text-xs text-slate-400">等待任务</div></div>
    <div v-if="hasLastParams && !status.is_syncing" class="restore-bar"><button type="button" :disabled="starting" @click="$emit('restore')" class="text-action">恢复上次参数</button><span>仅保存在此浏览器</span><button type="button" @click="$emit('forget')" class="text-action">清除</button></div>
    <fieldset class="form-surface" :disabled="status.is_syncing || starting" :class="{ 'opacity-50': status.is_syncing || starting }">
      <div class="mode-switch" aria-label="同步模式"><button type="button" @click="form.mode='json'" class="mode-button" :class="{ 'mode-button-active': form.mode === 'json' }">JSON 导入</button><button type="button" @click="form.mode='api'" class="mode-button" :class="{ 'mode-button-active': form.mode === 'api' }">API 复制</button><button type="button" @click="form.mode='clone'" class="mode-button" :class="{ 'mode-button-active': form.mode === 'clone' }">下载重传</button></div>
      <p class="field-hint">{{ modeHint }}</p>
      <p v-if="needsLogin" class="inline-warning">此模式需要辅助账号。<button type="button" class="text-action" @click="$emit('open-settings')">前往设置登录</button></p>
      <div v-if="form.mode !== 'json'" class="form-row"><field-group label="源频道"><input v-model="form.source_id" placeholder="ID 或 t.me 链接" class="input-box"></field-group><field-group v-if="form.target_type !== 'saved'" label="目标频道"><input v-model="form.target_id" placeholder="ID 或 t.me 链接" class="input-box"></field-group></div>
      <div v-else class="form-surface"><field-group v-if="form.target_type !== 'saved'" label="目标频道"><input v-model="form.target_id" placeholder="ID 或 t.me 链接" class="input-box"></field-group><field-group label="JSON 文件路径"><input v-model="form.json_path" placeholder="例如 D:/Export/result.json" class="input-box font-mono text-sm"></field-group><p class="field-hint">路径位于运行本程序的电脑上，并非浏览器上传；Docker 部署请使用容器内已挂载的路径。</p></div>
      <label class="choice-card"><input type="checkbox" v-model="form.target_type" true-value="saved" false-value="channel"><span><span class="choice-title">发送到收藏夹</span><span class="choice-description">使用当前辅助账号</span></span></label>
      <div v-if="form.mode === 'api' || form.mode === 'clone'" class="form-row"><field-group label="起始消息 ID"><input v-model="form.start_id" type="number" min="0" placeholder="留空：从头开始" class="input-box"></field-group><field-group label="结束消息 ID"><input v-model="form.end_id" type="number" min="0" placeholder="留空：直到最新" class="input-box"></field-group></div>
      <details class="compact-details"><summary>高级选项 · 延时与发送策略</summary><div class="form-surface">
        <sender-identity-options v-if="supportsSenderOptions" :sender="form.sender" :fallback-value="form.clone_fallback_to_user" :show-hash-option="supportsHashPerturb" :hash-value="form.hash_perturb" fallback-true-value="1" fallback-false-value="0" hash-true-value="1" hash-false-value="0" @update:sender="form.sender = $event" @update:fallback="form.clone_fallback_to_user = $event" @update:hash="form.hash_perturb = $event"></sender-identity-options>
        <div v-if="form.mode === 'json'" class="form-row"><field-group label="源频道用户名（可选）"><input v-model="form.json_source_username" placeholder="@username，用于链接改写" class="input-box"></field-group><field-group label="媒体组合并窗口（秒）"><input v-model="form.json_media_group_window_seconds" type="number" min="1" step="1" class="input-box"></field-group></div>
        <field-group label="单条延时（秒）"><input v-model="form.delay" type="number" step="0.5" min="0.5" class="input-box"></field-group>
        <label class="choice-card"><input type="checkbox" v-model="form.force_send" true-value="1" false-value="0"><span><span class="choice-title">强制发送</span><span class="choice-description">忽略重复和断点记录，可能产生重复消息</span></span></label>
      </div></details>
    </fieldset>
    <button v-if="!status.is_syncing" type="button" :disabled="starting" @click="$emit('start', form)" class="btn-primary mt-4">{{ starting ? '启动中…' : '启动任务' }}</button><button v-else-if="stopping" type="button" class="btn-primary mt-4 !bg-red-600">中断中<span class="dot-anim"></span></button><button v-else type="button" @click="$emit('stop')" class="btn-primary mt-4 !bg-red-600">中断任务</button>
  </div>`
};

const LogViewer = {
  components:{ LogPanel },
  props:["sysLogs","msgLogs"],
  data(){ return { activeKind:"system" }; },
  template:`<div class="log-workspace" aria-description="导出可获取当前保留的全部日志">
    <div class="log-switch" role="tablist" aria-label="日志类型"><button type="button" role="tab" :aria-selected="activeKind === 'system'" :class="{ active: activeKind === 'system' }" @click="activeKind='system'">系统日志 · {{ (sysLogs || []).length }}</button><button type="button" role="tab" :aria-selected="activeKind === 'message'" :class="{ active: activeKind === 'message' }" @click="activeKind='message'">消息日志 · {{ (msgLogs || []).length }}</button></div>
    <log-panel v-if="activeKind === 'system'" title="系统日志" description="最近记录" :logs="sysLogs" kind="system" panel-id="sys-log-panel" @clear="$emit('clear-sys-logs')" @export="$emit('export-sys-logs')"></log-panel>
    <log-panel v-else title="消息日志" description="最近记录" :logs="msgLogs" kind="message" panel-id="msg-log-panel" @clear="$emit('clear-msg-logs')" @export="$emit('export-msg-logs')"></log-panel>
  </div>`
};

const GlobalFilters = {
  props:["settings","rules","newRule"],
  data(){ return { previewText:"", previewFile:"", previewResult:null, previewBusy:false, previewVersion:0, typeLabels:{ sync_text:"文本", sync_photo:"图片", sync_video:"视频", sync_document:"文件", sync_audio:"音频", sync_voice:"语音", sync_sticker:"贴纸", sync_gif:"动图" } }; },
  computed:{ ruleCount(){ return (this.rules || []).length; } },
  watch:{
    newRule:{ deep:true, handler(){ this.invalidatePreview(); } },
    previewText(){ this.invalidatePreview(); },
    previewFile(){ this.invalidatePreview(); }
  },
  methods:{
    invalidatePreview(){ this.previewVersion++; this.previewResult = null; },
    async preview(){
      const version = this.previewVersion;
      this.previewBusy = true;
      try {
        const api = window.TgcsApi;
        const result = api.ensureSuccess(await api.postJson("/api/filter_rules/preview", { ...this.newRule, text:this.previewText, file_name:this.previewFile }), "预览失败");
        if (version === this.previewVersion) this.previewResult = result;
      } catch(error) {
        if (version === this.previewVersion) this.previewResult = {error:window.TgcsApi.getErrorMessage(error, "预览失败")};
      } finally { this.previewBusy = false; }
    }
  },
  template:`<div class="space-y-4">
    <div class="card">
      <div class="panel-heading"><div><div class="panel-kicker">Content</div><h2 class="panel-title">类型过滤</h2></div></div>
      <div class="grid grid-cols-2 gap-2 sm:grid-cols-4 mb-4"><label v-for="(label, key) in typeLabels" :key="key" class="choice-card !p-2"><input type="checkbox" v-model="settings[key]" true-value="1" false-value="0"><span class="choice-title">{{ label }}</span></label></div>
      <button @click="$emit('save-settings', settings)" class="btn-secondary">保存类型</button>
    </div>
    <div class="card">
      <div class="panel-heading"><div><div class="panel-kicker">Rules</div><h2 class="panel-title">正则过滤</h2></div><span class="field-badge field-badge-muted">{{ ruleCount }} 条</span></div>
      <p class="field-hint mb-3">按添加顺序生效；屏蔽匹配文本或文件名，媒体组任一项命中则整组跳过。</p>
      <div class="filter-form mb-4">
        <select v-model="newRule.rule_type" aria-label="规则类型" class="input-box"><option value="replace">替换文本</option><option value="drop">屏蔽消息</option></select>
        <input v-model="newRule.pattern" maxlength="1000" type="text" aria-label="正则表达式" placeholder="例如：广告|推广" class="input-box font-mono">
        <input v-if="newRule.rule_type === 'replace'" v-model="newRule.replacement" maxlength="1000" type="text" aria-label="替换内容" placeholder="替换为；留空则删除" class="input-box">
        <label class="identity-option text-xs text-slate-600"><input type="checkbox" v-model="newRule.is_case_sensitive" :true-value="1" :false-value="0">区分大小写</label>
        <button @click="$emit('add-rule', newRule)" :disabled="!newRule.pattern" class="btn-primary">添加规则</button>
      </div>
      <details class="compact-details mb-4"><summary>测试当前规则（不保存）</summary><div class="form-surface">
        <p class="field-hint">仅测试上方草稿，使用与同步相同的 Python 正则。实际输入包含 HTML 标签；结果仅显示文本，不执行 HTML。</p>
        <textarea v-model="previewText" maxlength="4000" rows="3" aria-label="预览文本" placeholder="粘贴一段示例文本…" class="input-box"></textarea>
        <input v-model="previewFile" maxlength="255" aria-label="预览文件名" placeholder="文件名（可选，例如 video.mp4）" class="input-box">
        <button type="button" @click="preview" :disabled="previewBusy || !newRule.pattern" class="btn-secondary">{{ previewBusy ? '预览中…' : '预览效果' }}</button>
        <div v-if="previewResult" class="preview-result" role="status">
          <p v-if="previewResult.error" class="text-red-600">{{ previewResult.error }}</p>
          <template v-else><p>{{ previewResult.dropped ? '命中：将屏蔽此消息（媒体组会整组跳过）' : (previewResult.matched ? '命中：替换后内容' : '未命中：内容保持不变') }}</p><pre v-if="!previewResult.dropped">{{ previewResult.text || '（空文本）' }}</pre></template>
        </div>
      </div></details>
      <ul class="rule-scroll space-y-2 text-sm border-t border-slate-100 pt-4"><li v-for="rule in rules" :key="rule.id" class="mapping-item"><span class="min-w-0 break-all font-mono text-xs"><span class="field-hint">{{ ['drop','skip_media'].includes(rule.rule_type) ? '屏蔽' : '替换' }}</span> {{ rule.pattern }} <span v-if="['replace','replace_text'].includes(rule.rule_type)" class="text-emerald-600">→ {{ rule.replacement || '(删除)' }}</span></span><button @click="$emit('del-rule', rule.id)" class="delete-action">删除</button></li><li v-if="!(rules || []).length" class="empty-state">暂无规则</li></ul>
    </div>
  </div>`
};
const UserAuthPanel = {
  props:["auth","submitting","cooldown"],
  components:{ AppCard, SectionHeader, ActionBar, FieldBadge },
  data(){ return { phoneNumber:"", phoneCode:"", password:"" }; },
  watch:{ auth:{ immediate:true, handler(v){ if(v && v.phone_number) this.phoneNumber = v.phone_number; } } },
  template:`
    <app-card id="settings-account" class="settings-section-card">
      <div class="settings-section-header">
        <div class="settings-section-title-row">
          <h2 class="settings-section-title">辅助账号</h2>
          <field-badge text="按步骤完成"></field-badge>
        </div>
      </div>

      <div class="settings-auth-shell">
        <div class="settings-auth-status">
          <div class="settings-auth-status-head">
            <div>
              <div class="settings-auth-status-title">当前状态</div>
              <div class="settings-auth-status-text mt-1">{{ auth.status_label || '未登录' }}</div>
            </div>
            <field-badge :text="auth.status === 'authorized' ? '已授权' : '待处理'" :tone="auth.status === 'authorized' ? 'muted' : ''"></field-badge>
          </div>
          <div class="settings-auth-meta">
            <div class="text-xs text-slate-500" v-if="auth.phone_number">手机号：{{ auth.phone_number }}</div>
            <div class="text-xs text-amber-700" v-if="auth.password_hint">密码提示：{{ auth.password_hint }}</div>
            <div class="text-xs text-slate-500" v-if="!auth.phone_number && !auth.password_hint">尚未绑定辅助账号，发送验证码后继续。</div>
          </div>
          <div v-if="auth.status === 'authorized'" class="pt-1">
            <button @click="$emit('switch-account')" :disabled="submitting" class="btn-secondary md:w-auto">切换账号</button>
          </div>
        </div>

        <div class="settings-auth-step">
          <div class="settings-auth-step-title">
            {{ auth.awaiting_password ? '步骤 3 · 输入两步验证密码' : (auth.awaiting_code ? '步骤 2 · 输入验证码' : '步骤 1 · 发送验证码') }}
          </div>
          <p class="settings-auth-step-description">
            {{ auth.awaiting_password
              ? '如果账号开启了两步验证，请输入密码完成授权。'
              : (auth.awaiting_code
                ? '验证码发送成功后，在这里提交收到的登录验证码。'
                : '输入辅助账号手机号，系统会向 Telegram 发送验证码。') }}
          </p>

          <div class="form-stack mt-4">
            <div v-if="!auth.awaiting_code && !auth.awaiting_password" class="flex flex-col gap-3 md:flex-row">
              <input v-model="phoneNumber" type="text" placeholder="手机号，例如 +8613712345678" class="input-box">
              <button @click="$emit('send-code', phoneNumber)" :disabled="submitting || cooldown > 0" class="btn-primary md:w-auto md:min-w-[160px]">
                {{ cooldown > 0 ? (cooldown + 's 后重试') : '发送验证码' }}
              </button>
            </div>

            <div v-if="auth.awaiting_code" class="form-stack">
              <input v-model="phoneCode" type="text" placeholder="输入验证码" class="input-box">
              <action-bar>
                <button @click="$emit('verify-code', phoneCode)" :disabled="submitting" class="btn-primary">提交验证码</button>
                <button @click="$emit('cancel-auth')" :disabled="submitting" class="btn-secondary">取消</button>
              </action-bar>
            </div>

            <div v-if="auth.awaiting_password" class="form-stack">
              <input v-model="password" type="password" placeholder="输入两步验证密码" class="input-box">
              <action-bar>
                <button @click="$emit('submit-password', password)" :disabled="submitting" class="btn-primary">提交密码</button>
                <button @click="$emit('cancel-auth')" :disabled="submitting" class="btn-secondary">取消</button>
              </action-bar>
            </div>
          </div>
        </div>
      </div>
    </app-card>
  `
};

const SettingsPanel = {
  props:["config","saving","userAuth","authSubmitting","sendCodeCooldown"],
  data(){ return { themes:[
    { id:"clover", name:"CLover", mark:"✦", colors:["#aabb22", "#fff8b2", "#52c4ee"] },
    { id:"sakura", name:"Sakura Pop", mark:"❀", colors:["#f27aa6", "#fff0b8", "#79cef2"] },
    { id:"mint", name:"Mint Melody", mark:"♫", colors:["#51cbb0", "#ffd1e7", "#6bc8ff"] },
    { id:"starlight", name:"Starlight", mark:"★", colors:["#8b7cf6", "#ffe9a8", "#61dbe9"] },
  ] }; },
  methods:{
    chooseTheme(theme){
      this.config.app.theme = theme;
      this.$emit("preview-theme", theme);
    },
    onThemeKeydown(event, index){
      const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];
      if(!keys.includes(event.key)) return;
      event.preventDefault();
      let nextIndex = index;
      if(event.key === "Home") nextIndex = 0;
      else if(event.key === "End") nextIndex = this.themes.length - 1;
      else if(event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (index + 1) % this.themes.length;
      else nextIndex = (index - 1 + this.themes.length) % this.themes.length;
      const nextTheme = this.themes[nextIndex].id;
      this.chooseTheme(nextTheme);
      this.$nextTick(() => document.querySelector(`[data-theme-option="${nextTheme}"]`)?.focus());
    },
  },
  // Legacy layout signature kept for UI regression tests:
  // components:{ AppCard, SectionHeader, FormSection, FieldGroup, ActionBar, BotApiHint, UserAuthPanel }
  components:{ AppCard, SectionHeader, FormSection, FieldGroup, ActionBar, BotApiHint, UserAuthPanel, SettingSectionNav, SettingGroup, ToggleField, FieldBadge },
  template:`
    <div class="settings-shell">
      <app-card id="settings-appearance" class="settings-section-card">
        <div class="settings-section-header">
          <div class="settings-section-title-row">
            <h2 class="settings-section-title">界面主题</h2>
            <field-badge text="即时预览"></field-badge>
          </div>
        </div>
        <div class="theme-picker" role="radiogroup" aria-label="界面主题">
          <button
            v-for="(theme, index) in themes"
            :key="theme.id"
            type="button"
            role="radio"
            :aria-checked="config.app.theme === theme.id"
            :tabindex="config.app.theme === theme.id ? 0 : -1"
            :data-theme-option="theme.id"
            class="theme-option"
            :class="{ 'theme-option-active': config.app.theme === theme.id }"
            :style="{ '--swatch-primary': theme.colors[0], '--swatch-warm': theme.colors[1], '--swatch-info': theme.colors[2] }"
            @click="chooseTheme(theme.id)"
            @keydown="onThemeKeydown($event, index)"
          >
            <span class="theme-mark" aria-hidden="true">{{ theme.mark }}</span>
            <span class="theme-option-name">{{ theme.name }}</span>
            <span class="theme-swatches" aria-hidden="true"><i></i><i></i><i></i></span>
          </button>
        </div>
      </app-card>

      <app-card id="settings-basic" class="settings-section-card">
        <div class="settings-section-header">
          <div class="settings-section-title-row">
            <h2 class="settings-section-title">基础配置</h2>
            <field-badge text="多数需重启"></field-badge>
          </div>
        </div>

        <div class="settings-grid settings-grid-12">
          <setting-group
            class="span-8 span-12"
            title="Telegram 接入"
          >
            <div class="settings-grid settings-grid-12">
              <field-group class="span-12" label="BOT_TOKEN" badge="需重启">
                <input v-model="config.telegram.bot_token" type="text" class="input-box">
              </field-group>
              <field-group class="span-12" label="BOT_API_BASE_URL" badge="需重启">
                <input v-model="config.telegram.bot_api_base_url" type="text" class="input-box">
                <bot-api-hint></bot-api-hint>
              </field-group>
              <field-group class="span-6" label="API_ID" badge="需重启">
                <input v-model="config.telegram.api_id" type="number" class="input-box">
              </field-group>
              <field-group class="span-6" label="API_HASH" badge="需重启">
                <input v-model="config.telegram.api_hash" type="text" class="input-box">
              </field-group>
            </div>
          </setting-group>

          <setting-group
            class="span-4 span-12"
            title="程序行为"
          >
            <div class="settings-grid-tight">
              <field-group label="服务端口" badge="需重启">
                <input v-model="config.server.port" type="number" class="input-box">
              </field-group>
              <field-group label="默认延时（秒）">
                <input v-model="config.sync.default_delay" type="number" step="0.5" min="0.5" class="input-box">
              </field-group>
              <toggle-field
                label="启动后自动打开浏览器"
                badge="需重启"
                :checked="config.server.auto_open_browser"
                @update:checked="config.server.auto_open_browser = $event"
              ></toggle-field>
            </div>
          </setting-group>

          <setting-group
            class="span-12"
            title="代理网络"
          >
            <div class="settings-grid-tight">
              <toggle-field
                label="启用代理"
                badge="需重启"
                :checked="config.proxy.enabled"
                @update:checked="config.proxy.enabled = $event"
              ></toggle-field>
              <div class="settings-grid settings-grid-md-2" :class="{ 'opacity-60': !config.proxy.enabled }">
                <field-group label="HOST" badge="需重启">
                  <input v-model="config.proxy.host" type="text" class="input-box">
                </field-group>
                <field-group label="PORT" badge="需重启">
                  <input v-model="config.proxy.port" type="number" class="input-box">
                </field-group>
                <field-group label="USERNAME" badge="需重启">
                  <input v-model="config.proxy.username" type="text" class="input-box">
                </field-group>
                <field-group label="PASSWORD" badge="需重启">
                  <input v-model="config.proxy.password" type="password" class="input-box">
                </field-group>
              </div>
            </div>
          </setting-group>
        </div>
      </app-card>

      <app-card id="settings-sync" class="settings-section-card">
        <div class="settings-section-header">
          <div class="settings-section-title-row">
            <h2 class="settings-section-title">同步配置</h2>
            <field-badge text="运行策略"></field-badge>
          </div>
        </div>

        <div class="settings-grid settings-grid-md-2">
          <setting-group
            title="默认行为"
          >
            <div class="toggle-grid">
              <toggle-field
                label="默认强制发送"
                :checked="config.sync.force_send"
                @update:checked="config.sync.force_send = $event"
              ></toggle-field>
              <toggle-field
                label="为外部转发/回复追加来源前缀"
                :checked="config.sync.add_external_source_header"
                @update:checked="config.sync.add_external_source_header = $event"
              ></toggle-field>
            </div>
          </setting-group>

          <setting-group
            title="上传与下载参数"
          >
            <div class="toggle-grid">
              <field-group label="未启用本地 Bot API 时的单文件上限（MB）">
                <input v-model="config.sync.bot_upload_max_mb" type="number" step="1" min="1" class="input-box">
              </field-group>
            </div>
          </setting-group>
        </div>

        <setting-group
          class="mt-4"
          title="多 Bot 上传限流轮换"
        >
          <div class="settings-grid-tight">
            <field-group label="额外 BOT_TOKEN" badge="需重启" hint="每行一个，用于上传轮换。">
              <textarea v-model="config.telegram.extra_bot_tokens" rows="4" class="input-box font-mono text-sm"></textarea>
            </field-group>
            <toggle-field
              label="启用多 Bot 上传限流轮换"
              :checked="config.sync.bot_rate_limit_enabled"
              @update:checked="config.sync.bot_rate_limit_enabled = $event"
            ></toggle-field>
            <div class="settings-grid settings-grid-md-3" :class="{ 'opacity-60': !config.sync.bot_rate_limit_enabled }">
              <field-group label="阈值（GB）">
                <input v-model="config.sync.bot_rate_limit_gb" type="number" step="0.1" min="0.1" class="input-box">
              </field-group>
              <field-group label="统计窗口（小时）">
                <input v-model="config.sync.bot_rate_limit_window_hours" type="number" step="1" min="1" class="input-box">
              </field-group>
              <field-group label="冷却时间（分钟）">
                <input v-model="config.sync.bot_rate_limit_cooldown_minutes" type="number" step="1" min="1" class="input-box">
              </field-group>
            </div>
          </div>
        </setting-group>
      </app-card>

      <app-card id="settings-logs" class="settings-section-card">
        <div class="settings-section-header">
          <div class="settings-section-title-row">
            <h2 class="settings-section-title">日志配置</h2>
            <field-badge text="保留策略" tone="muted"></field-badge>
          </div>
        </div>

        <div class="settings-grid settings-grid-md-2">
          <field-group label="系统日志最大保留条数">
            <input v-model="config.sync.system_log_retention_limit" type="number" step="1" min="100" class="input-box">
          </field-group>
          <field-group label="消息日志最大保留条数">
            <input v-model="config.sync.message_log_retention_limit" type="number" step="1" min="100" class="input-box">
          </field-group>
        </div>
        <div class="mt-4">
          <toggle-field
            label="Debug 模式：同步输出日志到终端"
            :checked="config.app.debug_terminal_logs"
            @update:checked="config.app.debug_terminal_logs = $event"
          ></toggle-field>
        </div>
      </app-card>

      <user-auth-panel
        :auth="userAuth"
        :submitting="authSubmitting"
        :cooldown="sendCodeCooldown"
        @send-code="$emit('send-code', $event)"
        @verify-code="$emit('verify-code', $event)"
        @submit-password="$emit('submit-password', $event)"
        @cancel-auth="$emit('cancel-auth')"
        @switch-account="$emit('switch-account')"
      ></user-auth-panel>

      <app-card id="settings-actions" class="settings-section-card settings-action-card">
        <div class="settings-section-header !mb-0 !border-b-0 !pb-0">
          <div class="settings-section-title-row">
            <h2 class="settings-section-title">操作</h2>
            <span class="settings-compact-note">保存配置后可按需立即重启服务</span>
          </div>
        </div>
        <action-bar class-name="action-bar mt-4">
          <button @click="$emit('save-config', false)" :disabled="saving" class="btn-primary md:w-auto md:min-w-[160px]">保存设置</button>
          <button @click="$emit('save-restart')" :disabled="saving" class="btn-secondary md:w-auto md:min-w-[160px]">保存并重启</button>
        </action-bar>
      </app-card>
    </div>
  `
};

createApp({
  components:{ SetupWizard, StatusOverview, ChannelMapping, SyncPanel, LogViewer, SettingsPanel, GlobalFilters, ToastBanner },
  data(){ return { syncStarting:false, lastSyncParams:null, currentView:"home", appInfo:{ bot:{}, user:{} }, mappings:{ mappings:[], grouped_mappings:[] }, filterRules:[], newFilter:{ rule_type:"replace", pattern:"", replacement:"", is_case_sensitive:0 }, settings:{ sync_text:"1", sync_photo:"1", sync_video:"1", sync_document:"1", sync_audio:"1", sync_voice:"1", sync_sticker:"1", sync_gif:"1" }, configForm:{ telegram:{ bot_token:"", extra_bot_tokens:"", api_id:"", api_hash:"", bot_api_base_url:"" }, proxy:{ enabled:false, host:"127.0.0.1", port:7897, username:"", password:"" }, server:{ host:"127.0.0.1", port:8011, auto_open_browser:true }, sync:{ default_delay:5, force_send:false, add_external_source_header:false, system_log_retention_limit:1000, message_log_retention_limit:5000, bot_upload_max_mb:50, bot_rate_limit_enabled:false, bot_rate_limit_gb:10, bot_rate_limit_window_hours:24, bot_rate_limit_cooldown_minutes:300, realtime_sender:"bot", realtime_fallback_to_user:true, realtime_hash_perturb:false }, app:{ portable_mode:true, log_level:"INFO", debug_terminal_logs:false, theme:"clover" } }, setupStatus:{ needs_setup:false }, syncForm:{ mode:"api", sender:"bot", source_id:"", target_id:"", start_id:"", end_id:"", json_path:"", json_source_username:"", json_media_group_window_seconds:3, delay:5, force_send:"0", hash_perturb:"0", clone_fallback_to_user:"1", target_type:"channel" }, syncStatus:{ is_syncing:false, mode:"", total:0, current:0, skipped:0 }, userAuth:{ status:"idle", status_label:"未登录", awaiting_code:false, awaiting_password:false, phone_number:"", password_hint:"", send_code_cooldown:0 }, versionInfo:{ status:"idle", current_version:"", latest_version:"", up_to_date:false, url:"https://github.com/RRHTY/tg-channel-sync" }, sendCodeCooldown:0, sendCodeTimer:null, authSubmitting:false, stopping:false, serverAction:"", restartPolling:null, sysLogs:[], msgLogs:[], sseConnection:null, configSaving:false, notice:{ message:"", type:"info" }, noticeTimer:null }; },
  async mounted(){
    this.startSendCodeTimer();
    try {
      await this.bootstrap();
      this.setupSSE();
    } catch(error) {
      this.showAppError(window.TgcsApi.getErrorMessage(error, "页面初始化失败"));
    }
    this.loadVersionInfo();
  },
  beforeUnmount(){
    if(this.sendCodeTimer) clearInterval(this.sendCodeTimer);
    if(this.restartPolling) clearInterval(this.restartPolling);
    if(this.noticeTimer) clearTimeout(this.noticeTimer);
    if(this.sseConnection) this.sseConnection.close();
  },
  methods: window.TgcsAppMethods
}).mount("#app");
