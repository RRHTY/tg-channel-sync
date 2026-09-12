(() => {
  const api = window.TgcsApi;
  const syncParamKeys = ['mode', 'sender', 'source_id', 'target_id', 'start_id', 'end_id', 'json_path', 'json_source_username', 'json_media_group_window_seconds', 'delay', 'force_send', 'hash_perturb', 'clone_fallback_to_user', 'target_type'];
  function cleanSyncParams(value) {
    if (!value || !['api', 'json', 'clone'].includes(value.mode)) return null;
    return Object.fromEntries(syncParamKeys.filter(key => typeof value[key] === 'string' || (typeof value[key] === 'number' && Number.isFinite(value[key]))).map(key => [key, value[key]]));
  }
  function mergeLogs(...groups) {
    const unique = [...new Map(groups.flat().map(item => [item.id, item])).values()];
    const server = unique.filter(item => typeof item.id === 'number').sort((a, b) => a.id - b.id);
    // Local notices have no server sequence; keep a small separate tail.
    const local = unique.filter(item => typeof item.id !== 'number').slice(-20);
    return [...server, ...local].slice(-100);
  }

  const uiMethods = {
    syncModeFromStatus(status) {
      const rawMode = String(status?.mode || "").trim().toLowerCase();
      if (rawMode === "api" || rawMode === "json" || rawMode === "clone") {
        this.syncForm.mode = rawMode;
      }
    },
    async bootstrap() {
      await Promise.all([
        this.loadConfig(),
        this.loadSetupStatus(),
        this.fetchAppInfo(),
        this.loadMappings(),
        this.loadFilters(),
        this.loadSettings(),
        this.loadUserAuthStatus(),
        this.loadSystemLogs(),
        this.loadMessageLogs(),
      ]);
      this.syncForm.delay = this.configForm.sync.default_delay || 5;
      this.syncForm.force_send = this.configForm.sync.force_send ? "1" : "0";
      this.syncForm.json_media_group_window_seconds = Number(this.syncForm.json_media_group_window_seconds || 3);
      if (this.syncStatus?.is_syncing) this.syncModeFromStatus(this.syncStatus);
      this.currentView = this.setupStatus.needs_setup ? "setup" : "home";
      this.loadLastSyncParams();
      this.bootstrapReady = true;
    },
    navButtonClass(view) {
      return this.currentView === view ? "nav-active" : "nav-idle";
    },
    applyTheme(theme, remember = false) {
      const supported = ["clover", "sakura", "mint", "starlight"];
      const normalized = supported.includes(String(theme || "").toLowerCase()) ? String(theme).toLowerCase() : "clover";
      document.documentElement.dataset.theme = normalized;
      if (remember) {
        try { localStorage.setItem("tgcs-theme", normalized); } catch (_) {}
      }
      return normalized;
    },
    navigateTo(view) {
      this.currentView = view;
      this.$nextTick(() => window.scrollTo({ top: 0, behavior: "smooth" }));
    },
    showToast(msg, type = "info") {
      if (!msg) return;
      this.notice = { message: msg, type };
      if (this.noticeTimer) clearTimeout(this.noticeTimer);
      this.noticeTimer = setTimeout(() => {
        this.notice = { message: "", type: "info" };
        this.noticeTimer = null;
      }, 4000);
    },
    pushSystemNotice(message, level = "WARNING") {
      const time = new Date().toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
      this.sysLogs = [...this.sysLogs, { id: `local-${Date.now()}`, time, level, msg: message }].slice(-100);
      this.$nextTick(() => this.scrollLogsToBottom({ sys: true, msg: false }));
    },
    showAppError(message) {
      this.showToast(message, "error");
      this.pushSystemNotice(message, "ERROR");
    },
    handleApiError(error, fallbackMessage) {
      const message = api.getErrorMessage(error, fallbackMessage);
      this.showAppError(message);
      return message;
    },
    openSettings() {
      this.navigateTo("settings");
    },
    normalizeConfigForm() {
      if (!this.configForm.telegram.api_id) this.configForm.telegram.api_id = "";
      this.configForm.telegram.extra_bot_tokens = Array.isArray(this.configForm.telegram.extra_bot_tokens)
        ? this.configForm.telegram.extra_bot_tokens.join("\n")
        : (this.configForm.telegram.extra_bot_tokens || "");
    },
    updateUserAuthLabel() {
      const map = { initializing: "正在连接", idle: "未登录", awaiting_code: "等待验证码", awaiting_password: "等待两步验证", authorized: "已登录" };
      this.userAuth.status_label = map[this.userAuth.status] || this.userAuth.status || "未登录";
      this.sendCodeCooldown = Math.max(this.sendCodeCooldown || 0, this.userAuth.send_code_cooldown || 0);
    },
    startSendCodeTimer() {
      if (this.sendCodeTimer) clearInterval(this.sendCodeTimer);
      this.sendCodeTimer = setInterval(() => {
        if (this.sendCodeCooldown > 0) this.sendCodeCooldown -= 1;
      }, 1000);
    },
  };

  const configMethods = {
    async loadSetupStatus() {
      this.setupStatus = api.ensureSuccess(await api.getJson("/api/setup/status"), "加载初始化状态失败");
    },
    async loadConfig() {
      this.configForm = api.ensureSuccess(await api.getJson("/api/config"), "加载配置失败");
      this.normalizeConfigForm();
      this.configForm.app.theme = this.applyTheme(this.configForm.app.theme, true);
    },
    async loadUserAuthStatus() {
      const revision = this.userAuthRevision || 0;
      const auth = api.ensureSuccess(await api.getJson("/api/user_auth/status"), "无法读取 Telegram 账号的登录状态");
      // A live update received while fetching is newer than this snapshot.
      if (revision !== (this.userAuthRevision || 0)) return;
      this.userAuth = auth;
      this.sendCodeCooldown = this.userAuth.send_code_cooldown || 0;
      this.updateUserAuthLabel();
    },
    async loadVersionInfo() {
      try {
        this.versionInfo = api.ensureSuccess(await api.getJson("/api/version", { cache: "no-store" }), "加载版本信息失败");
      } catch (_) {
        this.versionInfo = { status: "error", current_version: "unknown", latest_version: "", up_to_date: false, url: "https://github.com/RRHTY/tg-channel-sync" };
      }
    },
    async saveConfig(showToast = true) {
      this.configSaving = true;
      try {
        const payload = JSON.parse(JSON.stringify(this.configForm));
        payload.telegram.api_id = payload.telegram.api_id ? Number(payload.telegram.api_id) : 0;
        payload.telegram.extra_bot_tokens = (payload.telegram.extra_bot_tokens || "").split(/\r?\n|,/).map((v) => v.trim()).filter(Boolean);
        const res = api.ensureSuccess(await api.postJson("/api/config", payload), "保存配置失败");
        if (showToast) this.showToast(res.message);
        this.configForm = res.config;
        this.normalizeConfigForm();
        this.configForm.app.theme = this.applyTheme(this.configForm.app.theme, true);
        await this.loadSetupStatus();
        await this.loadUserAuthStatus();
        return res;
      } catch (error) {
        this.handleApiError(error, "保存配置失败");
        throw error;
      } finally {
        this.configSaving = false;
      }
    },
    async saveSetup(shouldRestart) {
      if (!this.configForm.telegram.bot_token) {
        this.showToast("BOT_TOKEN 为必填项");
        return;
      }
      await this.saveConfig(!shouldRestart);
      this.navigateTo("home");
      if (shouldRestart) await this.restartServer();
    },
    async saveSettingsPage(shouldRestart) {
      await this.saveConfig(true);
      if (shouldRestart) await this.restartServer();
    },
    async saveAndRestart() {
      await this.saveConfig(false);
      await this.restartServer();
    },
  };

  const serverAndLogMethods = {
    waitForServerReady() {
      if (this.restartPolling) clearInterval(this.restartPolling);
      this.restartPolling = setInterval(async () => {
        try {
          const res = await api.requestRaw("/api/app_info", { cache: "no-store" });
          if (!res.ok) return;
          clearInterval(this.restartPolling);
          this.restartPolling = null;
          window.location.reload();
        } catch (_) {}
      }, 1000);
    },
    isPanelNearBottom(panel, threshold = 24) {
      if (!panel) return true;
      return panel.scrollHeight - panel.scrollTop - panel.clientHeight <= threshold;
    },
    scrollLogsToBottom(options = {}) {
      const { sys = true, msg = true } = options;
      const sysPanel = document.getElementById("sys-log-panel");
      const msgPanel = document.getElementById("msg-log-panel");
      if (sys && sysPanel) sysPanel.scrollTop = sysPanel.scrollHeight;
      if (msg && msgPanel) msgPanel.scrollTop = msgPanel.scrollHeight;
    },
    setupSSE() {
      this.serverAction = "";
      if (this.sseConnection) this.sseConnection.close();
      this.connectionState = "connecting";
      const source = new EventSource("/api/stream");
      this.sseConnection = source;
      let lastEventAt = Date.now();
      source.onerror = () => {
        if (this.sseConnection === source) this.connectionState = "reconnecting";
      };
      source.onopen = async () => {
        if (this.sseConnection !== source) return;
        this.connectionState = "connecting";
        try {
          if (!this.bootstrapReady) await this.bootstrap();
          else await Promise.all([this.loadSystemLogs(), this.loadMessageLogs(), this.loadUserAuthStatus()]);
        } catch (error) {
          if (this.sseConnection !== source) return;
          this.connectionState = "reconnecting";
          source.close();
          this.handleApiError(error, "重连后加载页面数据失败");
        }
      };
      source.onmessage = (event) => {
        if (this.sseConnection !== source) return;
        let data;
        try {
          data = JSON.parse(event.data);
          if (!data || typeof data !== "object") throw new Error("Invalid event");
        }
        catch (_) { this.connectionState = "reconnecting"; source.close(); return; }
        lastEventAt = Date.now();
        const sysPanel = document.getElementById("sys-log-panel");
        const msgPanel = document.getElementById("msg-log-panel");
        const shouldFollowSys = this.isPanelNearBottom(sysPanel);
        const shouldFollowMsg = this.isPanelNearBottom(msgPanel);
        if (data.status) {
          this.lastStatusAt = Date.now();
          if (this.bootstrapReady) this.connectionState = "connected";
          if (this.stopping && !data.status.is_syncing) this.stopping = false;
          this.syncStatus = data.status;
          if (data.status.is_syncing) this.syncModeFromStatus(data.status);
        }
        if (data.app_info) this.appInfo = data.app_info;
        if (data.user_auth) {
          this.userAuthRevision = (this.userAuthRevision || 0) + 1;
          this.userAuth = data.user_auth;
          this.sendCodeCooldown = this.userAuth.send_code_cooldown || 0;
          this.updateUserAuthLabel();
        }
        if (data.sys_logs) this.sysLogs = mergeLogs(this.sysLogs, data.sys_logs);
        if (data.msg_logs) this.msgLogs = mergeLogs(this.msgLogs, data.msg_logs);
        this.$nextTick(() => this.scrollLogsToBottom({ sys: shouldFollowSys, msg: shouldFollowMsg }));
      };
      if (this.connectionTimer) clearInterval(this.connectionTimer);
      this.connectionTimer = setInterval(() => {
        if (this.serverAction || this.sseConnection !== source) return;
        if (source.readyState === 2 || Date.now() - lastEventAt > 15000) this.setupSSE();
      }, 5000);
    },
    async fetchAppInfo() {
      this.appInfo = api.ensureSuccess(await api.getJson("/api/app_info"), "加载应用状态失败");
    },
    async loadSystemLogs() {
      const request = this.sysLogRequest = (this.sysLogRequest || 0) + 1;
      const before = new Set(this.sysLogs.map(item => item.id));
      const rows = api.ensureSuccess(await api.getJson("/api/logs/system"), "加载系统日志失败");
      if (request !== this.sysLogRequest) return;
      this.sysLogs = mergeLogs(rows, this.sysLogs.filter(item => !before.has(item.id)));
      this.$nextTick(() => this.scrollLogsToBottom({ sys: true, msg: false }));
    },
    async loadMessageLogs() {
      const request = this.msgLogRequest = (this.msgLogRequest || 0) + 1;
      const before = new Set(this.msgLogs.map(item => item.id));
      const rows = api.ensureSuccess(await api.getJson("/api/logs/message"), "加载消息日志失败");
      if (request !== this.msgLogRequest) return;
      this.msgLogs = mergeLogs(rows, this.msgLogs.filter(item => !before.has(item.id)));
      this.$nextTick(() => this.scrollLogsToBottom({ sys: false, msg: true }));
    },
    exportSystemLogs() {
      window.open("/api/logs/system/export", "_blank", "noopener");
    },
    exportMessageLogs() {
      window.open("/api/logs/message/export", "_blank", "noopener");
    },
    async clearSystemLogs() {
      if (!window.confirm("确认清理系统日志吗？")) return;
      try {
        const res = api.ensureSuccess(await api.deleteJson("/api/logs/system"), "清理系统日志失败");
        this.sysLogs = [];
        this.sysLogRequest = (this.sysLogRequest || 0) + 1;
        this.showToast(res.message);
        this.$nextTick(() => this.scrollLogsToBottom());
      } catch (error) {
        this.handleApiError(error, "清理系统日志失败");
      }
    },
    async clearMessageLogs() {
      if (!window.confirm("确认清理消息日志吗？")) return;
      try {
        const res = api.ensureSuccess(await api.deleteJson("/api/logs/message"), "清理消息日志失败");
        this.msgLogs = [];
        this.msgLogRequest = (this.msgLogRequest || 0) + 1;
        this.showToast(res.message);
        this.$nextTick(() => this.scrollLogsToBottom());
      } catch (error) {
        this.handleApiError(error, "清理消息日志失败");
      }
    },
    async restartServer() {
      if (this.serverAction) return;
      if (!window.confirm("确认重启服务吗？")) return;
      this.serverAction = "restart";
      try {
        const res = api.ensureSuccess(await api.postJson("/api/server/restart", {}), "重启服务失败");
        this.showToast(res.message);
        if (this.sseConnection) this.sseConnection.close();
        this.connectionState = "reconnecting";
        this.sseConnection = null;
        this.waitForServerReady();
      } catch (error) {
        this.serverAction = "";
        this.handleApiError(error, "重启服务失败");
      }
    },
    async stopServer() {
      if (this.serverAction) return;
      if (!window.confirm("确认关闭服务吗？")) return;
      this.serverAction = "stop";
      try {
        const res = api.ensureSuccess(await api.postJson("/api/server/stop", {}), "关闭服务失败");
        this.showToast(res.message);
        if (this.sseConnection) this.sseConnection.close();
        this.connectionState = "offline";
        this.sseConnection = null;
      } catch (error) {
        this.serverAction = "";
        this.handleApiError(error, "关闭服务失败");
      }
    },
  };

  const ruleAndMappingMethods = {
    async loadMappings() {
      const result = api.ensureSuccess(await api.getJson("/api/mappings"), "加载自动同步失败");
      this.mappings = { mappings: result.mappings || [], grouped_mappings: result.grouped_mappings || [] };
    },
    async loadFilters() {
      this.filterRules = api.ensureSuccess(await api.getJson("/api/filter_rules"), "加载过滤规则失败");
    },
    async loadSettings() {
      const result = api.ensureSuccess(await api.getJson("/api/global_settings"), "加载类型配置失败");
      Object.keys(this.settings).forEach((key) => {
        if (result[key] !== undefined) this.settings[key] = result[key];
      });
    },
    async saveGlobalSettings() {
      try {
        const form = api.buildFormData(this.settings);
        const res = api.ensureSuccess(await api.postForm("/api/global_settings", form), "保存选择失败");
        this.showToast(res.message);
      } catch (error) {
        this.handleApiError(error, "保存选择失败");
      }
    },
    appendLocalSystemLog(message, level = "WARNING") {
      this.pushSystemNotice(message, level);
    },
    async addMapping(source, target, options = {}) {
      try {
        const form = api.buildFormData({ source_id: source, target_id: target, ...options });
        const res = api.ensureSuccess(await api.postForm("/api/mappings", form), "添加自动同步失败");
        if (res.message) this.showToast(res.message);
        await this.loadMappings();
        return true;
      } catch (exc) {
        this.handleApiError(exc, "添加自动同步失败");
        return false;
      }
    },
    async updateMapping(item, changes) {
      try {
        api.ensureSuccess(await api.requestJson(`/api/mappings/${item.source_id}/${item.target_id}`, { method: "PATCH", json: changes }), "保存映射失败");
        await this.loadMappings();
        return true;
      } catch (error) {
        this.handleApiError(error, "保存映射失败");
        return false;
      }
    },
    useMapping(item) {
      if (this.syncStatus.is_syncing || this.syncStarting) return this.showToast("请等待当前任务结束");
      Object.assign(this.syncForm, { mode: "api", source_id: String(item.source_id), target_id: String(item.target_id), target_type: item.target_type, force_send: "0" });
      document.getElementById("history-sync")?.scrollIntoView({ behavior: "smooth", block: "start" });
      this.showToast("已填入源和目标，请检查范围后启动");
    },
    async deleteMapping(sourceId, targetId) {
      try {
        const suffix = targetId !== undefined ? `?target_id=${encodeURIComponent(targetId)}` : "";
        const res = api.ensureSuccess(await api.deleteJson(`/api/mappings/${sourceId}${suffix}`), "删除自动同步失败");
        if (res.message) this.showToast(res.message);
        await this.loadMappings();
      } catch (exc) {
        this.handleApiError(exc, "删除自动同步失败");
      }
    },
    async addFilter(rule) {
      try {
        const form = api.buildFormData(rule);
        const res = api.ensureSuccess(await api.postForm("/api/filter_rules", form), "添加过滤规则失败");
        if (res.message) this.showToast(res.message);
        await this.loadFilters();
        this.newFilter = {
          rule_type: rule.rule_type,
          pattern: "",
          replacement: "",
          is_case_sensitive: rule.is_case_sensitive,
        };
      } catch (error) {
        this.handleApiError(error, "添加过滤规则失败");
      }
    },
    async deleteFilter(id) {
      try {
        api.ensureSuccess(await api.deleteJson(`/api/filter_rules/${id}`), "删除过滤规则失败");
        await this.loadFilters();
      } catch (error) {
        this.handleApiError(error, "删除过滤规则失败");
      }
    },
  };

  const syncMethods = {
    loadLastSyncParams() {
      try { this.lastSyncParams = cleanSyncParams(JSON.parse(localStorage.getItem('tgcs-last-sync-v1'))); }
      catch (_) { this.lastSyncParams = null; }
    },
    rememberSyncParams(form) {
      this.lastSyncParams = cleanSyncParams(form);
      try { localStorage.setItem('tgcs-last-sync-v1', JSON.stringify(this.lastSyncParams)); } catch (_) {}
    },
    restoreLastSyncParams() {
      if (!this.lastSyncParams || this.syncStarting || this.syncStatus.is_syncing) return;
      Object.assign(this.syncForm, this.lastSyncParams, { force_send: '0' });
      this.showToast('已填入上次的内容；默认跳过已同步的消息，请核对后开始');
    },
    clearLastSyncParams() {
      this.lastSyncParams = null;
      try { localStorage.removeItem('tgcs-last-sync-v1'); } catch (_) {}
    },
    async startSync(form) {
      if (this.connectionState !== "connected") return this.showToast("连接尚未恢复，请等待状态更新后再启动");
      if (this.syncStarting || this.syncStatus.is_syncing) return;
      this.syncStarting = true;
      try {
        const submitted = { ...form };
        const payload = api.buildFormData(submitted, {
          valueTransform(value, key) {
            return value || (key.includes("id") ? "0" : "");
          },
        });
        const res = api.ensureSuccess(await api.postForm("/api/start_sync", payload), "开始同步失败");
        this.syncStatus = { ...this.syncStatus, is_syncing: true, starting: true };
        this.rememberSyncParams(submitted);
        if (res.message) this.showToast(res.message);
      } catch (error) {
        this.handleApiError(error, "开始同步失败");
      } finally {
        this.syncStarting = false;
      }
    },
    async stopSync() {
      if (this.connectionState !== "connected") return this.showToast("当前连接已中断，无法确认任务状态，请先恢复连接");
      this.stopping = true;
      try {
        api.ensureSuccess(await api.postJson("/api/stop_sync", {}), "停止同步失败");
      } catch (error) {
        this.stopping = false;
        this.handleApiError(error, "停止同步失败");
      }
    },
  };

  const authMethods = {
    async sendUserCode(phoneNumber) {
      if (this.sendCodeCooldown > 0 || this.authSubmitting) return;
      const normalizedPhone = String(phoneNumber || "").trim();
      if (!normalizedPhone) {
        this.showAppError("发送验证码失败：手机号不能为空");
        return;
      }
      const previousCooldown = this.sendCodeCooldown || 0;
      this.userAuth = {
        ...this.userAuth,
        status: "awaiting_code",
        awaiting_code: true,
        phone_number: normalizedPhone,
      };
      this.updateUserAuthLabel();
      this.sendCodeCooldown = Math.max(previousCooldown, 30);
      this.authSubmitting = true;
      try {
        const res = api.ensureSuccess(await api.postJson("/api/user_auth/send_code", { phone_number: normalizedPhone }), "发送验证码失败");
        this.showToast(res.message || "已发送请求");
        if (res.send_code_cooldown) {
          this.sendCodeCooldown = Math.max(this.sendCodeCooldown, Number(res.send_code_cooldown) || 0);
        }
        await this.loadUserAuthStatus();
        await this.fetchAppInfo();
      } catch (exc) {
        this.sendCodeCooldown = previousCooldown;
        this.handleApiError(exc, "发送验证码失败");
        try {
          await this.loadUserAuthStatus();
          await this.fetchAppInfo();
        } catch (_) {}
      } finally {
        this.authSubmitting = false;
      }
    },
    async verifyUserCode(phoneCode) {
      this.authSubmitting = true;
      try {
        const res = api.ensureSuccess(await api.postJson("/api/user_auth/sign_in", { phone_code: phoneCode }), "登录失败");
        this.showToast(res.message || "验证码已提交");
        await this.loadUserAuthStatus();
        await this.fetchAppInfo();
      } catch (error) {
        this.handleApiError(error, "登录失败");
      } finally {
        this.authSubmitting = false;
      }
    },
    async submitUserPassword(password) {
      this.authSubmitting = true;
      try {
        const res = api.ensureSuccess(await api.postJson("/api/user_auth/check_password", { password }), "登录失败");
        this.showToast(res.message || "密码已提交");
        await this.loadUserAuthStatus();
        await this.fetchAppInfo();
      } catch (error) {
        this.handleApiError(error, "登录失败");
      } finally {
        this.authSubmitting = false;
      }
    },
    async cancelUserAuth() {
      this.authSubmitting = true;
      try {
        const res = api.ensureSuccess(await api.postJson("/api/user_auth/cancel", {}), "取消登录失败");
        this.showToast(res.message || "已取消登录");
        await this.loadUserAuthStatus();
        await this.fetchAppInfo();
      } catch (error) {
        this.handleApiError(error, "取消登录失败");
      } finally {
        this.authSubmitting = false;
      }
    },
    async switchUserAccount() {
      if (!window.confirm("要切换 Telegram 账号吗？\n当前账号会退出登录，你需要重新登录另一个账号。")) return;
      this.authSubmitting = true;
      try {
        const res = api.ensureSuccess(await api.postJson("/api/user_auth/switch_account", {}), "切换账号失败");
        this.showToast(res.message || "已切换账号");
        await this.loadUserAuthStatus();
        await this.fetchAppInfo();
      } catch (error) {
        this.handleApiError(error, "切换账号失败");
      } finally {
        this.authSubmitting = false;
      }
    },
  };

  window.TgcsAppMethods = {
    ...uiMethods,
    ...configMethods,
    ...serverAndLogMethods,
    ...ruleAndMappingMethods,
    ...syncMethods,
    ...authMethods,
  };
})();
