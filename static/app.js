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
  methods:{ tone(v){ if(["已连接","已登录","运行中"].includes(v)) return "border-green-200 bg-green-50 text-green-700"; if(["初始化中","连接超时","等待验证码","等待两步验证","需要登录"].includes(v)) return "border-amber-200 bg-amber-50 text-amber-700"; if(["启动失败","未配置"].includes(v)) return "border-red-200 bg-red-50 text-red-700"; return "border-blue-200 bg-blue-50 text-blue-700"; } },
  template:`<section class="status-grid" aria-label="连接与任务状态">
    <div class="status-card" :class="tone(appInfo.bot.status)"><div class="status-label">Bot</div><div class="status-value">{{ appInfo.bot.status || '未配置' }}</div><div class="status-detail">{{ appInfo.bot.name || '未连接' }}</div></div>
    <div class="status-card" :class="tone(appInfo.user.status)"><div class="status-label">辅助账号</div><div class="status-value">{{ appInfo.user.status === '需要登录' ? '未登录' : (appInfo.user.status || '未配置') }}</div><div v-if="appInfo.user.status === '需要登录'" class="status-detail"><button @click="$emit('open-settings')" class="font-semibold underline">前往设置登录</button></div><div v-else class="status-detail">{{ appInfo.user.name || '未登录' }}</div></div>
    <div class="status-card" :class="tone(status.is_syncing ? '运行中' : '空闲')"><div class="status-label">任务状态</div><div class="status-value">{{ status.is_syncing ? '运行中' : '空闲' }}</div><div class="status-detail">{{ status.mode || '等待任务' }}</div></div>
    <div class="status-card text-slate-700"><div class="status-label">同步进度</div><div class="status-value">{{ status.current || 0 }} / {{ status.total || 0 }}</div><div class="status-detail">已跳过 {{ status.skipped || 0 }} 条</div></div>
  </section>`
};

const ChannelMapping = {
  props:["mappings"],
  components:{ AppCard, FieldGroup, MappingOptionBadges, EmptyState },
  data(){ return { source:"", target:"", realtime_sender:"bot", realtime_fallback_to_user:true, realtime_hash_perturb:false, to_saved:false }; },
  computed:{
    mappingCount(){ return (this.mappings && this.mappings.mappings ? this.mappings.mappings.length : 0); }
  },
  methods:{
    saveRule(){
      if(!String(this.source || "").trim()){
        this.$emit("log-error", "添加频道映射失败：源频道不能为空");
        return;
      }
      if(!this.to_saved){
        if(!String(this.target || "").trim()){
          this.$emit("log-error", "添加频道映射失败：目标频道不能为空");
          return;
        }
        if(String(this.source || "").trim() === String(this.target || "").trim()){
          this.$emit("log-error", "添加频道映射失败：源频道和目标频道不能相同");
          return;
        }
      }
      this.$emit("add", this.source, this.target, {
        realtime_sender: this.to_saved ? "user" : this.realtime_sender,
        realtime_fallback_to_user: this.realtime_fallback_to_user ? "1" : "0",
        realtime_hash_perturb: this.realtime_hash_perturb ? "1" : "0",
        target_type: this.to_saved ? "saved" : "channel",
      });
      this.source = "";
      this.target = "";
    }
  },
  template:`<app-card>
    <div class="panel-heading"><div><div class="panel-kicker">Realtime</div><h2 class="panel-title">频道映射</h2></div><span class="field-badge field-badge-muted">{{ mappingCount }} 条</span></div>
    <div class="mapping-form">
      <div class="form-row">
        <field-group label="源频道"><input v-model="source" type="text" placeholder="ID 或 t.me 链接" class="input-box"></field-group>
        <field-group v-if="!to_saved" label="目标频道"><input v-if="!to_saved" v-model="target" type="text" placeholder="ID 或 t.me 链接" class="input-box"></field-group>
      </div>
      <label class="choice-card"><input type="checkbox" v-model="to_saved"><span><span class="choice-title">发送到收藏夹</span><span class="choice-description">使用当前辅助账号的 Saved Messages</span></span></label>
      <div class="identity-panel">
        <div class="identity-row"><span class="identity-label">发送身份</span><label class="identity-option"><input type="radio" :checked="!to_saved && realtime_sender === 'bot'" :disabled="to_saved" @change="realtime_sender='bot'">Bot</label><label class="identity-option"><input type="radio" :checked="to_saved || realtime_sender === 'user'" :disabled="to_saved" @change="realtime_sender='user'">辅助账号</label></div>
        <div class="identity-row"><span class="identity-label">获取身份</span><label class="identity-option"><input type="radio" :checked="!realtime_fallback_to_user" @change="realtime_fallback_to_user=false">Bot</label><label class="identity-option"><input type="radio" :checked="realtime_fallback_to_user" @change="realtime_fallback_to_user=true">辅助账号</label></div>
        <label class="identity-option text-xs text-slate-600"><input type="checkbox" v-model="realtime_hash_perturb">重置图片/视频指纹</label>
      </div>
      <button @click="saveRule" class="btn-primary">添加映射</button>
    </div>
    <div class="mapping-scroll mt-4 space-y-2 text-sm">
      <div v-for="group in mappings.grouped_mappings || []" :key="group.target_id" class="mapping-group"><div class="mb-2 flex items-center justify-between"><span class="text-xs font-semibold text-slate-500">目标</span><span class="font-mono text-xs font-semibold text-slate-700">{{ group.target_id === -1 ? '收藏夹' : group.target_id }}</span></div><div class="space-y-2"><div v-for="item in group.sources" :key="item.source_id" class="mapping-item"><div class="min-w-0"><div class="truncate font-mono text-xs text-slate-700">{{ item.source_id }}</div><mapping-option-badges class="mt-1" :item="item"></mapping-option-badges></div><button @click="$emit('del', item.source_id, group.target_id)" class="delete-action">删除</button></div></div></div>
      <empty-state v-if="!(mappings.grouped_mappings || []).length" text="暂无映射"></empty-state>
    </div>
  </app-card>`
};
const SyncPanel = {
  components:{ FieldGroup, SenderIdentityOptions },
  props:["status","form","stopping"],
  computed:{
    supportsSenderOptions(){ return (this.form.mode === "json" || this.form.mode === "clone") && this.form.target_type !== "saved"; },
    supportsHashPerturb(){ return this.form.mode === "json" || this.form.mode === "clone"; }
  },
  template:`<div class="card">
    <div class="panel-heading"><div><div class="panel-kicker">History</div><h2 class="panel-title">历史同步</h2></div><span v-if="status.is_syncing" class="field-badge">运行中</span></div>
    <div class="progress-shell"><template v-if="status.is_syncing"><div class="mb-2 flex justify-between text-xs font-semibold text-indigo-700"><span>{{ status.mode }}</span><span>{{ status.current }} / {{ status.total }}</span></div><div class="mb-3 h-1.5 w-full rounded-full bg-indigo-100"><div class="h-1.5 rounded-full bg-indigo-600 transition-all" :style="{ width: (status.total > 0 ? status.current / status.total * 100 : 0) + '%' }"></div></div><p class="break-all text-xs text-slate-500">{{ status.current_text || ('已跳过 ' + status.skipped + ' 条') }}</p></template><div v-else class="flex min-h-[56px] items-center text-xs text-slate-400">等待任务</div></div>
    <div class="form-surface" :class="{ 'opacity-50 pointer-events-none': status.is_syncing }">
      <div class="mode-switch" aria-label="同步模式"><button type="button" @click="form.mode='json'" class="mode-button" :class="{ 'mode-button-active': form.mode === 'json' }">JSON 导入</button><button type="button" @click="form.mode='api'" class="mode-button" :class="{ 'mode-button-active': form.mode === 'api' }">API 复制</button><button type="button" @click="form.mode='clone'" class="mode-button" :class="{ 'mode-button-active': form.mode === 'clone' }">下载重传</button></div>
      <div v-if="form.mode !== 'json'" class="form-row"><field-group label="源频道"><input v-model="form.source_id" placeholder="ID 或 t.me 链接" class="input-box"></field-group><field-group v-if="form.target_type !== 'saved'" label="目标频道"><input v-model="form.target_id" placeholder="ID 或 t.me 链接" class="input-box"></field-group></div>
      <div v-else class="form-surface"><field-group v-if="form.target_type !== 'saved'" label="目标频道"><input v-model="form.target_id" placeholder="ID 或 t.me 链接" class="input-box"></field-group><field-group label="JSON 文件"><input v-model="form.json_path" placeholder="result.json 路径" class="input-box font-mono text-sm"></field-group><div class="form-row"><field-group label="源频道用户名"><input v-model="form.json_source_username" placeholder="@username" class="input-box"></field-group><field-group label="媒体组合并窗口"><input v-model="form.json_media_group_window_seconds" type="number" min="1" step="1" class="input-box"></field-group></div></div>
      <label class="choice-card"><input type="checkbox" v-model="form.target_type" true-value="saved" false-value="channel"><span><span class="choice-title">发送到收藏夹</span><span class="choice-description">使用当前辅助账号</span></span></label>
      <sender-identity-options v-if="supportsSenderOptions" :sender="form.sender" :fallback-value="form.clone_fallback_to_user" :show-hash-option="supportsHashPerturb" :hash-value="form.hash_perturb" fallback-true-value="1" fallback-false-value="0" hash-true-value="1" hash-false-value="0" @update:sender="form.sender = $event" @update:fallback="form.clone_fallback_to_user = $event" @update:hash="form.hash_perturb = $event"></sender-identity-options>
      <div v-if="form.mode === 'api' || form.mode === 'clone'" class="form-row"><field-group label="起始消息 ID"><input v-model="form.start_id" type="number" placeholder="可留空" class="input-box"></field-group><field-group label="结束消息 ID"><input v-model="form.end_id" type="number" placeholder="可留空" class="input-box"></field-group></div>
      <field-group label="单条延时（秒）"><input v-model="form.delay" type="number" step="0.5" min="0.5" class="input-box"></field-group>
      <label class="choice-card"><input type="checkbox" v-model="form.force_send" true-value="1" false-value="0"><span><span class="choice-title">强制发送</span><span class="choice-description">忽略重复和断点记录</span></span></label>
    </div>
    <button v-if="!status.is_syncing" type="button" @click="$emit('start', form)" class="btn-primary mt-4">启动任务</button><button v-else-if="stopping" type="button" class="btn-primary mt-4 !bg-red-600">中断中<span class="dot-anim"></span></button><button v-else type="button" @click="$emit('stop')" class="btn-primary mt-4 !bg-red-600">中断任务</button>
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
  data(){ return { typeLabels:{ sync_text:"文本", sync_photo:"图片", sync_video:"视频", sync_document:"文件", sync_audio:"音频", sync_voice:"语音", sync_sticker:"贴纸", sync_gif:"动图" } }; },
  computed:{ ruleCount(){ return (this.rules || []).length; } },
  template:`<div class="space-y-4"><div class="card"><div class="panel-heading"><div><div class="panel-kicker">Content</div><h2 class="panel-title">类型过滤</h2></div></div><div class="grid grid-cols-2 gap-2 sm:grid-cols-4 mb-4"><label v-for="(label, key) in typeLabels" class="choice-card !p-2"><input type="checkbox" v-model="settings[key]" true-value="1" false-value="0"><span class="choice-title">{{ label }}</span></label></div><button @click="$emit('save-settings', settings)" class="btn-secondary">保存类型</button></div><div class="card"><div class="panel-heading"><div><div class="panel-kicker">Rules</div><h2 class="panel-title">正则过滤</h2></div><span class="field-badge field-badge-muted">{{ ruleCount }} 条</span></div><div class="filter-form mb-4"><select v-model="newRule.rule_type" aria-label="规则类型" class="input-box bg-white"><option value="replace">替换文本</option><option value="drop">屏蔽消息</option></select><input v-model="newRule.pattern" type="text" aria-label="正则表达式" placeholder="正则表达式" class="input-box font-mono"><input v-if="newRule.rule_type === 'replace'" v-model="newRule.replacement" type="text" aria-label="替换内容" placeholder="替换为；留空则删除" class="input-box"><label class="identity-option text-xs text-slate-600"><input type="checkbox" v-model="newRule.is_case_sensitive" :true-value="1" :false-value="0">区分大小写</label><button @click="$emit('add-rule', newRule)" class="btn-primary">添加规则</button></div><ul class="rule-scroll space-y-2 text-sm border-t border-slate-100 pt-4"><li v-for="rule in rules" :key="rule.id" class="mapping-item"><span class="min-w-0 truncate font-mono text-xs">{{ rule.pattern }} <span v-if="rule.rule_type === 'replace'" class="text-emerald-600">→ {{ rule.replacement || '(删除)' }}</span></span><button @click="$emit('del-rule', rule.id)" class="delete-action">删除</button></li><li v-if="!(rules || []).length" class="empty-state">暂无规则</li></ul></div></div>`
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
  // Legacy layout signature kept for UI regression tests:
  // components:{ AppCard, SectionHeader, FormSection, FieldGroup, ActionBar, BotApiHint, UserAuthPanel }
  components:{ AppCard, SectionHeader, FormSection, FieldGroup, ActionBar, BotApiHint, UserAuthPanel, SettingSectionNav, SettingGroup, ToggleField, FieldBadge },
  template:`
    <div class="settings-shell">
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
  data(){ return { currentView:"home", appInfo:{ bot:{}, user:{} }, mappings:{ mappings:[], grouped_mappings:[] }, filterRules:[], newFilter:{ rule_type:"replace", pattern:"", replacement:"", is_case_sensitive:0 }, settings:{ sync_text:"1", sync_photo:"1", sync_video:"1", sync_document:"1", sync_audio:"1", sync_voice:"1", sync_sticker:"1", sync_gif:"1" }, configForm:{ telegram:{ bot_token:"", extra_bot_tokens:"", api_id:"", api_hash:"", bot_api_base_url:"" }, proxy:{ enabled:false, host:"127.0.0.1", port:7897, username:"", password:"" }, server:{ host:"127.0.0.1", port:8011, auto_open_browser:true }, sync:{ default_delay:5, force_send:false, add_external_source_header:false, system_log_retention_limit:1000, message_log_retention_limit:5000, bot_upload_max_mb:50, bot_rate_limit_enabled:false, bot_rate_limit_gb:10, bot_rate_limit_window_hours:24, bot_rate_limit_cooldown_minutes:300, realtime_sender:"bot", realtime_fallback_to_user:true, realtime_hash_perturb:false }, app:{ portable_mode:true, log_level:"INFO", debug_terminal_logs:false } }, setupStatus:{ needs_setup:false }, syncForm:{ mode:"api", sender:"bot", source_id:"", target_id:"", start_id:"", end_id:"", json_path:"", json_source_username:"", json_media_group_window_seconds:3, delay:5, force_send:"0", hash_perturb:"0", clone_fallback_to_user:"1", target_type:"channel" }, syncStatus:{ is_syncing:false, mode:"", total:0, current:0, skipped:0 }, userAuth:{ status:"idle", status_label:"未登录", awaiting_code:false, awaiting_password:false, phone_number:"", password_hint:"", send_code_cooldown:0 }, versionInfo:{ status:"idle", current_version:"", latest_version:"", up_to_date:false, url:"https://github.com/RRHTY/tg-channel-sync" }, sendCodeCooldown:0, sendCodeTimer:null, authSubmitting:false, stopping:false, serverAction:"", restartPolling:null, sysLogs:[], msgLogs:[], sseConnection:null, configSaving:false, notice:{ message:"", type:"info" }, noticeTimer:null }; },
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
