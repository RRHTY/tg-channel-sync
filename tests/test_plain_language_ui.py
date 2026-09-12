import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PlainLanguageUiTests(unittest.TestCase):
    def test_labels_do_not_need_redundant_explanations(self):
        app = (ROOT / 'static/app.js').read_text(encoding='utf-8')
        shared = (ROOT / 'static/ui-components.js').read_text(encoding='utf-8')
        for wording in ('使用当前辅助账号', '发送身份', '按需调整', '运行策略', '保留策略', '多数需重启'):
            self.assertNotIn(wording, app + shared)
        self.assertIn('保存到我的 Telegram 收藏夹', app)
        self.assertIn('重新发送已同步的消息', app)
        self.assertIn('会产生重复消息', app)
        self.assertNotIn('class="panel-kicker"', app + shared)

    def test_settings_use_named_fields_instead_of_environment_variables(self):
        app = (ROOT / 'static/app.js').read_text(encoding='utf-8')
        for label in ('代理地址', '代理端口', '机器人 Token', '上传量上限（GB）'):
            self.assertIn(label, app)
        for label in ('label="HOST"', 'label="PORT"', 'label="USERNAME"', 'label="PASSWORD"'):
            self.assertNotIn(label, app)
