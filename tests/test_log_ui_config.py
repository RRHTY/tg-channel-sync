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
        self.assertIn("接入库日志", content)
        self.assertIn("导出可获取当前保留的全部日志", content)

    def test_home_page_binds_log_export_actions(self):
        content = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('@export-sys-logs="exportSystemLogs"', content)
        self.assertIn('@export-msg-logs="exportMessageLogs"', content)

    def test_saved_messages_target_hides_channel_inputs(self):
        content = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('v-if="!to_saved" v-model="target"', content)
        self.assertEqual(content.count('v-if="form.target_type !== \'saved\'"'), 2)
