const { createApp } = Vue;

const ChannelMapping = {
    props: ['mappings'],
    data() { return { source: '', target: '' } },
    template: `
        <div class="card">
            <h2 class="text-lg font-semibold mb-1">频道映射</h2>
            <p class="text-xs text-gray-500 mb-4">实时同步源频道消息到目标频道，支持多组映射</p>
            <div class="space-y-3">
                <div class="flex gap-2">
                    <input v-model="source" type="text" placeholder="源频道ID/URL" class="input-box">
                    <input v-model="target" type="text" placeholder="目标频道ID/URL" class="input-box">
                </div>
                <button @click="$emit('add', source, target); source=''; target='';" class="btn-primary">💾 保存规则</button>
            </div>
            <ul class="mt-4 space-y-2 text-sm">
                <li v-for="map in mappings" :key="map.source_id" class="flex justify-between p-2 bg-gray-50 rounded group">
                    <div class="flex-1 flex justify-between px-2 font-mono"><span>{{ map.source_id }}</span><span class="text-gray-400">→</span><span>{{ map.target_id }}</span></div>
                    <button @click="$emit('del', map.source_id)" class="text-red-500 opacity-0 group-hover:opacity-100 px-2">删除</button>
                </li>
            </ul>
        </div>
    `
};

const GlobalFilters = {
    props: ['settings', 'rules', 'newRule'],
    data() {
        return {
            typeLabels: {'sync_text':'📝 文本','sync_photo':'🖼️ 图片','sync_video':'🎬 视频','sync_document':'📁 文件','sync_audio':'🎵 音乐','sync_voice':'🎤 语音','sync_sticker':'🏷️ 贴纸','sync_gif':'🎞️ 动图'}
        }
    },
    template: `
        <div class="card">
            <h2 class="text-lg font-semibold mb-1">全局过滤</h2>
            <p class="text-xs text-gray-500 mb-4">控制同步消息类型及内容过滤规则</p>
            <div class="bg-white p-4 rounded-lg mb-6 border border-gray-200 shadow-sm">
                <h3 class="text-sm font-bold text-gray-800 mb-4">消息类型</h3>
                <div class="grid grid-cols-4 gap-y-4 gap-x-2 mb-5">
                    <label v-for="(label, key) in typeLabels" class="flex items-center text-sm cursor-pointer hover:text-blue-600">
                        <input type="checkbox" v-model="settings[key]" true-value="1" false-value="0" class="mr-1.5 w-4 h-4"> {{ label }}
                    </label>
                </div>
                <button @click="$emit('save-settings', settings)" class="btn-primary">💾 保存类型配置</button>
            </div>

            <div class="bg-white p-4 rounded-lg border border-gray-200 shadow-sm">
                <h3 class="text-sm font-bold text-gray-800 mb-4">正则过滤</h3>
                <div class="space-y-3 mb-5">
                    <select v-model="newRule.rule_type" class="input-box bg-white">
                        <option value="replace">仅替换文本（命中后替换内容）</option>
                        <option value="drop">屏蔽整条消息（命中后丢弃整条，含媒体）</option>
                    </select>
                    <input v-model="newRule.pattern" type="text" placeholder="正则表达式，如：广告.*推广" class="input-box font-mono">
                    <input v-if="newRule.rule_type === 'replace'" v-model="newRule.replacement" type="text" placeholder="替换为（留空则删除匹配文本）" class="input-box">
                    <label class="flex items-center text-sm"><input type="checkbox" v-model="newRule.is_case_sensitive" :true-value="1" :false-value="0" class="mr-2">区分大小写</label>
                    <button @click="$emit('add-rule', newRule)" class="btn-primary">➕ 添加规则</button>
                </div>
                <ul class="space-y-2 text-sm border-t pt-4">
                    <li v-for="r in rules" :key="r.id" class="flex justify-between p-2 bg-gray-50 rounded group">
                        <span class="truncate font-mono">{{ r.pattern }} <span v-if="r.rule_type==='replace'" class="text-green-600">→ {{r.replacement||'(抹除)'}}</span></span>
                        <button @click="$emit('del-rule', r.id)" class="text-red-500 opacity-0 group-hover:opacity-100">删除</button>
                    </li>
                </ul>
            </div>
        </div>
    `
};

const SyncPanel = {
    props: ['status', 'form', 'stopping'],
    template: `
        <div class="card">
            <h2 class="text-lg font-semibold mb-1">历史同步</h2>
            <p class="text-xs text-gray-500 mb-4">批量同步指定ID范围内的历史消息到目标频道</p>
            <div v-if="status.is_syncing" class="mb-6 p-4 bg-white rounded shadow-sm">
                <div class="flex justify-between text-sm mb-1"><span class="font-medium text-blue-700">运行中: {{status.mode}}</span><span>{{status.current}} / {{status.total}}</span></div>
                <div class="w-full bg-gray-200 rounded-full h-2 mb-3"><div class="bg-blue-600 h-2 rounded-full transition-all" :style="{width: (status.total>0?status.current/status.total*100:0)+'%'}"></div></div>
                <div class="text-xs text-gray-500"><p>跳过: {{status.skipped}}</p><p class="truncate text-blue-500 font-bold mt-1">{{status.current_text}}</p></div>
            </div>
            <div class="space-y-4" :class="{'opacity-50 pointer-events-none': status.is_syncing}">
                <div class="flex gap-2"><input v-model="form.source_id" placeholder="源频道ID/URL" class="input-box"><input v-model="form.target_id" placeholder="目标频道ID/URL" class="input-box"></div>
                <div class="flex bg-white rounded-lg border p-1">
                    <button @click="form.mode='json'" :class="form.mode==='json'?'bg-blue-100 text-blue-700 font-semibold':'text-gray-500'" class="flex-1 py-1 text-sm rounded">JSON导入</button>
                    <button @click="form.mode='api'" :class="form.mode==='api'?'bg-purple-100 text-purple-700 font-semibold':'text-gray-500'" class="flex-1 py-1 text-sm rounded">API复制</button>
                    <button @click="form.mode='clone'" :class="form.mode==='clone'?'bg-emerald-100 text-emerald-700 font-semibold':'text-gray-500'" class="flex-1 py-1 text-sm rounded">下载重传</button>
                </div>
                <div class="text-xs text-gray-500 -mt-2 px-1">
                    <span v-if="form.mode==='json'">根据导出文件自动复制上传到目标频道</span>
                    <span v-else-if="form.mode==='api'">通过API无引用转发，速度快</span>
                    <span v-else-if="form.mode==='clone'">通过API下载再重新上传</span>
                </div>
                <div v-if="form.mode==='clone'" class="bg-white p-3 rounded border text-sm">
                    <b>发送身份:</b>
                    <label><input type="radio" v-model="form.sender" value="bot" class="ml-2 mr-1">机器人</label>
                    <label><input type="radio" v-model="form.sender" value="user" class="ml-4 mr-1">辅助账号</label>
                </div>
                <div v-if="form.mode==='api'||form.mode==='clone'" class="flex gap-2"><input v-model="form.start_id" type="number" placeholder="起始ID" class="input-box"><input v-model="form.end_id" type="number" placeholder="结束ID" class="input-box"></div>
                <div v-if="form.mode==='json'"><input v-model="form.json_path" placeholder="JSON文件路径" class="input-box"></div>
                <div><label class="text-xs">单条处理延时(秒): <span class="text-gray-400">最少0.5</span></label><input v-model="form.delay" type="number" step="0.5" min="0.5" class="input-box"></div>
            </div>
            <button v-if="!status.is_syncing" @click="$emit('start', form)" class="btn-primary mt-4 bg-gray-800 hover:bg-gray-900">启动 {{form.mode.toUpperCase()}} 任务</button>
            <button v-else-if="stopping" class="btn-primary mt-4 bg-red-600 cursor-not-allowed">中断中<span class="dot-anim"></span></button>
            <button v-else @click="$emit('stop')" class="btn-primary mt-4 bg-red-600 hover:bg-red-700">中断任务</button>
        </div>
    `
};

const LogViewer = {
    props: ['sysLogs', 'msgLogs'],
    template: `
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-2">
            <div class="card">
                <h2 class="text-lg font-semibold mb-1">系统日志</h2>
                <p class="text-xs text-gray-500 mb-4">记录系统运行状态及错误信息</p>
                <div class="bg-slate-900 p-3 rounded h-80 overflow-y-auto text-xs font-mono space-y-1">
                    <div v-for="l in sysLogs" :key="l.id" :class="{'text-red-400':l.level==='ERROR','text-yellow-400':l.level==='WARNING','text-green-400':l.level==='SUCCESS','text-gray-300':l.level==='INFO'}" class="border-b border-slate-800 pb-1">
                        <span class="text-slate-500">[{{l.time}}]</span> [{{l.level}}] {{l.msg}}
                    </div>
                </div>
            </div>
            <div class="card">
                <h2 class="text-lg font-semibold mb-1">消息日志</h2>
                <p class="text-xs text-gray-500 mb-4">记录每条消息的同步状态</p>
                <div class="bg-slate-900 p-3 rounded h-80 overflow-y-auto text-xs font-mono space-y-1">
                    <div v-for="l in msgLogs" :key="l.id" :class="{'text-red-400':l.action.includes('DROP')||l.action.includes('ERROR'),'text-green-400':l.action.includes('SEND'),'text-gray-300':l.action.includes('RECV')}" class="border-b border-slate-800 pb-1">
                        <span class="text-slate-500">[{{l.time}}]</span> [{{l.action}}] {{l.detail}}
                    </div>
                </div>
            </div>
        </div>
    `
};

const app = createApp({
    data() {
        return {
            appInfo: { bot:{}, user:{} }, mappings: [], filterRules: [],
            newFilter: { rule_type: 'replace', pattern: '', replacement: '', is_case_sensitive: 0 },
            settings: { sync_text: '1', sync_photo: '1', sync_video: '1', sync_document: '1', sync_audio: '1', sync_voice: '1', sync_sticker: '1', sync_gif: '1' },
            syncForm: { mode: 'api', sender: 'bot', source_id: '', target_id: '', start_id: '', end_id: '', json_path: '', delay: 5 },
            syncStatus: { is_syncing: false, mode: '', total: 0, current: 0 },
            stopping: false,
            sysLogs: [], msgLogs: [], sseConnection: null
        }
    },
    mounted() {
        this.fetchAppInfo(); this.loadMappings(); this.loadFilters(); this.loadSettings();
        this.setupSSE();
    },
    methods: {
        showToast(msg) { alert(msg); },
        setupSSE() {
            this.sseConnection = new EventSource('/api/stream');
            this.sseConnection.onmessage = (e) => {
                const data = JSON.parse(e.data);
                if (data.status) {
                    if (this.stopping && !data.status.is_syncing) {
                        this.stopping = false;
                    }
                    this.syncStatus = data.status;
                }
                if(data.sys_logs) this.sysLogs = [...data.sys_logs, ...this.sysLogs].slice(0, 200);
                if(data.msg_logs) this.msgLogs = [...data.msg_logs, ...this.msgLogs].slice(0, 200);
            };
        },
        async fetchAppInfo() { this.appInfo = await (await fetch('/api/app_info')).json(); },
        async loadMappings() { this.mappings = await (await fetch('/api/mappings')).json(); },
        async loadFilters() { this.filterRules = await (await fetch('/api/filter_rules')).json(); },
        async loadSettings() { const r = await (await fetch('/api/global_settings')).json(); Object.keys(this.settings).forEach(k => {if(r[k]!==undefined) this.settings[k]=r[k]}); },
        async saveSettings(silent = false) {
            const fd = new FormData(); Object.keys(this.settings).forEach(k => fd.append(k, this.settings[k]));
            const res = await (await fetch('/api/global_settings', { method: 'POST', body: fd })).json();
            if (!silent) this.showToast(res.message);
        },
        async addMapping(s, t) {
            const fd = new FormData(); fd.append('source_id', s); fd.append('target_id', t);
            await fetch('/api/mappings', { method: 'POST', body: fd }); this.loadMappings();
        },
        async deleteMapping(id) { await fetch(`/api/mappings/${id}`, { method: 'DELETE' }); this.loadMappings(); },
        async addFilter(rule) {
            const fd = new FormData(); Object.keys(rule).forEach(k => fd.append(k, rule[k]));
            await fetch('/api/filter_rules', { method: 'POST', body: fd }); this.loadFilters();
            this.newFilter = { rule_type: 'replace', pattern: '', replacement: '', is_case_sensitive: 0 };
        },
        async deleteFilter(id) { await fetch(`/api/filter_rules/${id}`, { method: 'DELETE' }); this.loadFilters(); },
        async startSync(form) {
            await this.saveSettings(true);
            const fd = new FormData(); Object.keys(form).forEach(k => fd.append(k, form[k]||(k.includes('id')?'0':'')));
            const res = await (await fetch('/api/start_sync', { method: 'POST', body: fd })).json();
            if (res.status === 'error') this.showToast(res.message);
        },
        async stopSync() {
            this.stopping = true;
            await fetch('/api/stop_sync', { method: 'POST' });
        }
    }
});

app.component('channel-mapping', ChannelMapping);
app.component('global-filters', GlobalFilters);
app.component('sync-panel', SyncPanel);
app.component('log-viewer', LogViewer);
app.mount('#app');
