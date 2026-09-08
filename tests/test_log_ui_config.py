import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LogUiConfigTests(unittest.TestCase):
    def test_shared_form_layout_components_exist(self):
        content = (ROOT / "static" / "ui-components.js").read_text(encoding="utf-8")

        self.assertIn("const SectionHeader =", content)
        self.assertIn("const FormSection =", content)
        self.assertIn("const FieldGroup =", content)
        self.assertIn("const ActionBar =", content)

    def test_setup_and_settings_reuse_form_layout_components(self):
        content = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("components:{ AppCard, SectionHeader, FormSection, FieldGroup, ActionBar, BotApiHint }", content)
        self.assertIn("components:{ AppCard, SectionHeader, FormSection, FieldGroup, ActionBar, BotApiHint, UserAuthPanel, SettingSectionNav, SettingGroup, ToggleField, FieldBadge }", content)
        self.assertIn("<form-section", content)
        self.assertIn("<field-group", content)
        self.assertIn("<action-bar", content)

    def test_settings_panel_contains_log_retention_fields(self):
        content = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("系统日志最大保留条数", content)
        self.assertIn("消息日志最大保留条数", content)
        self.assertIn("Debug 模式：同步输出日志到终端", content)
        self.assertIn("导出可获取当前保留的全部日志", content)

    def test_home_page_binds_log_export_actions(self):
        content = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('@export-sys-logs="exportSystemLogs"', content)
        self.assertIn('@export-msg-logs="exportMessageLogs"', content)

    def test_saved_messages_target_hides_channel_inputs(self):
        content = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('v-if="!to_saved" v-model="target"', content)
        self.assertEqual(content.count('v-if="form.target_type !== \'saved\'"'), 2)

    def test_application_shell_prioritizes_navigation_over_server_actions(self):
        content = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('class="app-header"', content)
        self.assertIn('class="brand-mark"', content)
        self.assertIn('class="service-menu"', content)

    def test_home_controls_have_accessible_structure(self):
        content = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('class="status-grid"', content)
        self.assertIn('class="mode-switch"', content)
        self.assertIn('label="源频道"', content)
        self.assertIn('label="目标频道"', content)
        self.assertIn('class="delete-action"', content)

    def test_log_viewer_uses_one_switchable_panel(self):
        content = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('activeKind:"system"', content)
        self.assertIn('class="log-switch"', content)
        self.assertIn('v-if="activeKind === \'system\'"', content)

    def test_log_actions_share_the_same_header_row(self):
        content = (ROOT / "static" / "ui-components.js").read_text(encoding="utf-8")

        action_group = content.index('class="log-actions"')
        export_button = content.index("$emit('export')", action_group)
        bottom_button = content.index("scrollToBottom", export_button)
        clear_button = content.index("$emit('clear')", bottom_button)
        heading_end = content.index("</div>", clear_button)
        self.assertLess(export_button, bottom_button)
        self.assertLess(bottom_button, clear_button)
        self.assertLess(clear_button, heading_end)

    def test_visual_system_has_keyboard_and_motion_accessibility(self):
        content = (ROOT / "static" / "app.css").read_text(encoding="utf-8")

        self.assertIn(":focus-visible", content)
        self.assertIn("prefers-reduced-motion: reduce", content)
        self.assertIn("@media (max-width: 640px)", content)

    def test_visual_theme_uses_project_palette(self):
        content = (ROOT / "static" / "app.css").read_text(encoding="utf-8").lower()

        self.assertIn("--primary: #aabb22", content)
        self.assertIn("--warm: #fff8b2", content)
        self.assertIn("--info: #52c4ee", content)
        self.assertIn(".status-tone-positive", content)
        self.assertIn(".version-badge-update", content)

    def test_templates_use_theme_classes_instead_of_old_primary_colors(self):
        app_content = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        methods_content = (ROOT / "static" / "app-methods.js").read_text(encoding="utf-8")

        self.assertNotIn("text-indigo-700", app_content)
        self.assertNotIn("bg-indigo-600", app_content)
        self.assertIn('"nav-active"', methods_content)

    def test_settings_exposes_live_theme_selection(self):
        app_content = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        methods_content = (ROOT / "static" / "app-methods.js").read_text(encoding="utf-8")
        index_content = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        for name in ("CLover", "Sakura Pop", "Mint Melody", "Starlight"):
            self.assertIn(name, app_content)
        self.assertIn('@preview-theme="applyTheme"', index_content)
        self.assertIn("applyTheme(theme, remember = false)", methods_content)
        self.assertIn("document.documentElement.dataset.theme", methods_content)
        self.assertIn("localStorage.setItem", methods_content)
        self.assertEqual(methods_content.count("this.applyTheme(this.configForm.app.theme, true)"), 2)
        self.assertIn("onThemeKeydown(event, index)", app_content)
        self.assertIn(":tabindex=\"config.app.theme === theme.id ? 0 : -1\"", app_content)

    def test_styles_define_every_supported_theme(self):
        content = (ROOT / "static" / "app.css").read_text(encoding="utf-8")

        for theme in ("clover", "sakura", "mint", "starlight"):
            self.assertIn(f':root[data-theme="{theme}"]', content)
        self.assertIn(".theme-picker", content)
        self.assertIn(".theme-option-active", content)
        self.assertIn("--primary-ink:", content)
        self.assertIn("--info-ink:", content)

    def test_saved_theme_is_restored_before_styles_load(self):
        content = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        restore_script = content.index('localStorage.getItem("tgcs-theme")')
        stylesheet = content.index('rel="stylesheet"')
        self.assertLess(restore_script, stylesheet)

    def test_view_navigation_returns_to_page_top(self):
        content = (ROOT / "static" / "app-methods.js").read_text(encoding="utf-8")

        self.assertIn("navigateTo(view)", content)
        self.assertIn('window.scrollTo({ top: 0, behavior: "smooth" })', content)
