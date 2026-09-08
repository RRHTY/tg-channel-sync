(() => {
  const api = window.TgcsApi;

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
    },
    navButtonClass(view) {
      return this.currentView === view ? "nav-active" : "nav-idle";
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
      const map = { idle: "未登录", awaiting_code: "等待验证码", awaiting_password: "等待两步验证", authorized: "已登录" };
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
    },
    async loadUserAuthStatus() {
      this.userAuth = api.ensureSuccess(await api.getJson("/api/user_auth/status"), "加载辅助账号状态失败");
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
      this.sseConnection = new EventSource("/api/stream");
      this.sseConnection.onmessage = (event) => {
        const data = JSON.parse(event.data);
        const sysPanel = document.getElementById("sys-log-panel");
        const msgPanel = document.getElementById("msg-log-panel");
        const shouldFollowSys = this.isPanelNearBottom(sysPanel);
        const shouldFollowMsg = this.isPanelNearBottom(msgPanel);
        if (data.status) {
          if (this.stopping && !data.status.is_syncing) this.stopping = false;
          this.syncStatus = data.status;
          if (data.status.is_syncing) this.syncModeFromStatus(data.status);
        }
        if (data.app_info) this.appInfo = data.app_info;
        if (data.sys_logs) this.sysLogs = [...this.sysLogs, ...data.sys_logs].slice(-100);
        if (data.msg_logs) this.msgLogs = [...this.msgLogs, ...data.msg_logs].slice(-100);
        this.$nextTick(() => this.scrollLogsToBottom({ sys: shouldFollowSys, msg: shouldFollowMsg }));
      };
    },
    async fetchAppInfo() {
      this.appInfo = api.ensureSuccess(await api.getJson("/api/app_info"), "加载应用状态失败");
    },
    async loadSystemLogs() {
      this.sysLogs = api.ensureSuccess(await api.getJson("/api/logs/system"), "加载系统日志失败");
      this.$nextTick(() => this.scrollLogsToBottom({ sys: true, msg: false }));
    },
    async loadMessageLogs() {
      this.msgLogs = api.ensureSuccess(await api.getJson("/api/logs/message"), "加载消息日志失败");
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
      } catch (error) {
        this.serverAction = "";
        this.handleApiError(error, "关闭服务失败");
      }
    },
  };

  const ruleAndMappingMethods = {
    async loadMappings() {
      const result = api.ensureSuccess(await api.getJson("/api/mappings"), "加载频道映射失败");
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
        const res = api.ensureSuccess(await api.postForm("/api/global_settings", form), "保存类型配置失败");
        this.showToast(res.message);
      } catch (error) {
        this.handleApiError(error, "保存类型配置失败");
      }
    },
    appendLocalSystemLog(message, level = "WARNING") {
      this.pushSystemNotice(message, level);
    },
    async addMapping(source, target, options = {}) {
      try {
        const form = api.buildFormData({ source_id: source, target_id: target, ...options });
        const res = api.ensureSuccess(await api.postForm("/api/mappings", form), "添加频道映射失败");
        if (res.message) this.showToast(res.message);
        await this.loadMappings();
      } catch (exc) {
        this.handleApiError(exc, "添加频道映射失败");
      }
    },
    async deleteMapping(sourceId, targetId) {
      try {
        const suffix = targetId !== undefined ? `?target_id=${encodeURIComponent(targetId)}` : "";
        const res = api.ensureSuccess(await api.deleteJson(`/api/mappings/${sourceId}${suffix}`), "删除频道映射失败");
        if (res.message) this.showToast(res.message);
        await this.loadMappings();
      } catch (exc) {
        this.handleApiError(exc, "删除频道映射失败");
      }
    },
    async addFilter(rule) {
      try {
        const form = api.buildFormData(rule);
        const res = api.ensureSuccess(await api.postForm("/api/filter_rules", form), "添加过滤规则失败");
        if (res.message) this.showToast(res.message);
        await this.loadFilters();
        this.newFilter = { rule_type: "replace", pattern: "", replacement: "", is_case_sensitive: 0 };
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
    async startSync(form) {
      try {
        const payload = api.buildFormData(form, {
          valueTransform(value, key) {
            return value || (key.includes("id") ? "0" : "");
          },
        });
        const res = api.ensureSuccess(await api.postForm("/api/start_sync", payload), "启动任务失败");
        if (res.message) this.showToast(res.message);
      } catch (error) {
        this.handleApiError(error, "启动任务失败");
      }
    },
    async stopSync() {
      this.stopping = true;
      try {
        api.ensureSuccess(await api.postJson("/api/stop_sync", {}), "中断任务失败");
      } catch (error) {
        this.stopping = false;
        this.handleApiError(error, "中断任务失败");
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
        const res = api.ensureSuccess(await api.postJson("/api/user_auth/sign_in", { phone_code: phoneCode }), "提交验证码失败");
        this.showToast(res.message || "已提交验证码");
        await this.loadUserAuthStatus();
        await this.fetchAppInfo();
      } catch (error) {
        this.handleApiError(error, "提交验证码失败");
      } finally {
        this.authSubmitting = false;
      }
    },
    async submitUserPassword(password) {
      this.authSubmitting = true;
      try {
        const res = api.ensureSuccess(await api.postJson("/api/user_auth/check_password", { password }), "提交密码失败");
        this.showToast(res.message || "已提交密码");
        await this.loadUserAuthStatus();
        await this.fetchAppInfo();
      } catch (error) {
        this.handleApiError(error, "提交密码失败");
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
      if (!window.confirm("确认切换辅助账号吗？\n当前账号会退出登录，并清除本地会话。")) return;
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
