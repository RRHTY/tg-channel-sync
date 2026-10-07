import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE_SETUP = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const context = {window: {}, navigator: {languages: ['en-US'], language: 'en-US'}};
for (const file of ['static/locales/en.js', 'static/i18n.js']) {
  assert.ok(fs.existsSync(file), 'missing language support: ' + file);
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), context, {filename: file});
}
const i18n = context.window.TgcsI18n;
function make(options = {}) {
  const values = new Map(Object.entries(options.values || {}));
  const storage = {
    getItem(key) {return values.get(key) ?? null;},
    setItem(key, value) {values.set(key, value);},
  };
  const document = {documentElement: {lang: ''}, title: ''};
  const navigator = options.navigator || {languages: ['en-US'], language: 'en-US'};
  return {value: i18n.createI18n({navigator, storage, document, reactive: value => value, ...options}), values, document};
}
'''


@unittest.skipUnless(shutil.which('node'), 'Node.js required for language behavior tests')
class I18nUiTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(
            [shutil.which('node'), '-e', NODE_SETUP + script],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_browser_language_uses_first_supported_language_and_english_fallback(self):
        self.run_node(r'''
assert.equal(i18n.resolveLanguage('auto', ['zh-CN']), 'zh');
assert.equal(i18n.resolveLanguage('auto', ['zh-Hans-CN']), 'zh');
assert.equal(i18n.resolveLanguage('auto', ['zh-TW']), 'zh');
assert.equal(i18n.resolveLanguage('auto', ['zh-HK']), 'zh');
assert.equal(i18n.resolveLanguage('auto', ['fr-FR', 'zh-CN', 'en-US']), 'zh');
assert.equal(i18n.resolveLanguage('auto', ['en-GB', 'zh-CN']), 'en');
assert.equal(i18n.resolveLanguage('auto', ['fr-FR', 'de-DE']), 'en');
assert.equal(i18n.resolveLanguage('auto', []), 'en');
assert.equal(i18n.resolveLanguage('zh', ['en-US']), 'zh');
assert.equal(i18n.resolveLanguage('en', ['zh-CN']), 'en');
''')

    def test_default_and_empty_languages_fall_back_to_browser_language(self):
        self.run_node(r'''
const {value} = make({navigator: {languages: [], language: 'zh-CN'}});
assert.equal(value.state.preference, 'auto');
assert.equal(value.state.language, 'zh');
assert.equal(value.t('自动同步'), '自动同步');
''')

    def test_saved_preference_overrides_browser_and_switches_immediately(self):
        self.run_node(r'''
const {value, values, document} = make({values: {'tgcs-language': 'en'}, navigator: {languages: ['zh-CN']}});
assert.equal(value.state.preference, 'en');
assert.equal(value.state.language, 'en');
assert.equal(value.t('自动同步'), 'Auto sync');
assert.equal(document.documentElement.lang, 'en');
assert.equal(document.title, 'Xingling Sync');
const state = value.state;
value.setLanguage('zh');
assert.equal(value.state, state, 'existing component subscriptions keep the same state');
assert.equal(value.state.language, 'zh');
assert.equal(value.t('自动同步'), '自动同步');
assert.equal(values.get('tgcs-language'), 'zh');
assert.equal(document.documentElement.lang, 'zh-CN');
assert.equal(document.title, '杏铃同步台');
value.setLanguage('auto');
assert.equal(value.state.preference, 'auto');
assert.equal(value.state.language, 'zh');
assert.equal(values.get('tgcs-language'), 'auto');
''')

    def test_invalid_saved_language_uses_browser_and_storage_failure_is_nonfatal(self):
        self.run_node(r'''
const invalid = make({values: {'tgcs-language': 'unsupported'}, navigator: {languages: ['zh-CN']}}).value;
assert.equal(invalid.state.preference, 'auto');
assert.equal(invalid.state.language, 'zh');
const blockedStorage = {getItem() {throw Error('blocked');}, setItem() {throw Error('blocked');}};
const {value} = make({storage: blockedStorage});
assert.equal(value.state.preference, 'auto');
assert.doesNotThrow(() => value.setLanguage('zh'));
assert.equal(value.state.language, 'zh');
''')

    def test_named_interpolation_and_unknown_text_preserve_user_data(self):
        self.run_node(r'''
const {value} = make();
assert.equal(value.t('已跳过 {count} 条', {count: 12}), 'Skipped 12');
assert.equal(value.t('未收录的文案 {name}', {name: '频道 $& {count}'}), '未收录的文案 频道 $& {count}');
assert.equal(value.t('My channel / 自动同步'), 'My channel / 自动同步');
assert.equal(value.t('tg://resolve?domain=中文频道'), 'tg://resolve?domain=中文频道');
value.setLanguage('zh');
assert.equal(value.t('已跳过 {count} 条', {count: 0}), '已跳过 0 条');
''')

    def test_known_backend_messages_translate_without_changing_unknown_errors(self):
        self.run_node(r'''
const {value} = make();
assert.equal(value.translateMessage('配置已保存'), 'Settings saved');
assert.equal(value.translateMessage('启动 API 任务成功'), 'API task started');
const external = 'Telegram error: MESSAGE_ID_INVALID / @中文频道';
assert.equal(value.translateMessage(external), external);
const detail = 'Telegram error: invalid 9 条';
assert.equal(value.translateMessage(detail), detail, 'UI count patterns must not rewrite raw error details');
value.setLanguage('zh');
assert.equal(value.translateMessage('启动 API 任务成功'), '启动 API 任务成功');
''')

    def test_vue_install_reads_live_language_and_keeps_raw_status_values(self):
        self.run_node(r'''
const {value} = make();
let mixin;
value.install({mixin(options) {mixin = options;}});
assert.ok(mixin, 'language helpers must be available to nested components');
const component = {...mixin.methods, appInfo: {bot: {status: '已连接'}, user: {status: '需要登录'}}};
for (const [key, getter] of Object.entries(mixin.computed || {})) {
  Object.defineProperty(component, key, {get() {return getter.call(component);}});
}
assert.equal(component.$language, 'en');
assert.equal(component.$languagePreference, 'auto');
assert.equal(component.$t(component.appInfo.bot.status), 'Connected');
assert.equal(component.$tm('配置已保存'), 'Settings saved');
assert.equal(component.appInfo.bot.status, '已连接');
assert.equal(component.appInfo.user.status, '需要登录');
component.$setLanguage('zh');
assert.equal(component.$language, 'zh');
assert.equal(component.$languagePreference, 'zh');
assert.equal(component.$t(component.appInfo.bot.status), '已连接');
''')

    def test_entry_scripts_load_language_support_before_the_app(self):
        html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
        self.assertIn('/static/locales/en.js', html)
        self.assertIn('/static/i18n.js', html)
        self.assertLess(html.index('/static/locales/en.js'), html.index('/static/i18n.js'))
        self.assertLess(html.index('/static/i18n.js'), html.index('/static/app.js'))

    def test_language_selector_is_available_before_setup_and_in_appearance_settings(self):
        self.run_node(r'''
let root;
context.Vue = {createApp(options) {root = options; return {mount() {}, mixin() {}, use(plugin) {plugin.install(this); return this;}};}};
vm.runInNewContext(fs.readFileSync('static/ui-components.js', 'utf8'), context);
vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
const select = context.window.TgcsUi.LanguageSelect;
assert.ok(select, 'a shared language selector keeps both entry points consistent');
assert.match(select.template, /<select\b/);
for (const preference of ['auto', 'zh', 'en']) {
  assert.match(select.template, new RegExp('value=["\x27]' + preference + '["\x27]'));
}
assert.ok(select.template.includes('$languagePreference'));
assert.ok(select.template.includes('$setLanguage'));
assert.equal(root.components.SetupWizard.components.LanguageSelect, select);
assert.equal(root.components.SettingsPanel.components.LanguageSelect, select);
assert.match(root.components.SetupWizard.template, /<language-select\b/);
assert.match(root.components.SettingsPanel.template, /<language-select\b/);
const tone = root.components.StatusOverview.methods.tone;
assert.equal(tone('已连接'), 'status-tone-positive');
assert.equal(tone('需要登录'), 'status-tone-warning');
assert.equal(tone('未配置'), 'status-tone-negative');
''')

    def test_current_message_preview_preserves_original_user_text(self):
        self.run_node(r'''
let root;
context.Vue = {createApp(options) {root = options; return {mount() {}, mixin() {}, use(plugin) {plugin.install(this); return this;}};}};
vm.runInNewContext(fs.readFileSync('static/ui-components.js', 'utf8'), context);
vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
const {value} = make();
const expression = [...root.components.SyncPanel.template.matchAll(/\{\{([\s\S]*?)\}\}/g)]
  .map(match => match[1]).find(text => text.includes('status.current_text'));
assert.ok(expression, 'the current message preview must remain visible');
for (const original of ['已连接', '10 条', '配置已保存', '自动同步', 'Telegram error: invalid 9 条']) {
  const displayed = vm.runInNewContext('(' + expression + ')', {
    status: {current_text: original, skipped: 0}, $t: value.t, $tm: value.translateMessage,
  });
  assert.equal(displayed, original, 'the backend preview may contain user-authored message text');
}
''')

    def test_header_has_a_shared_language_switch_next_to_github(self):
        self.run_node(r'''
let root;
context.Vue = {createApp(options) {root = options; return {mount() {}, mixin() {}, use(plugin) {plugin.install(this); return this;}};}};
vm.runInNewContext(fs.readFileSync('static/ui-components.js', 'utf8'), context);
vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
const menu = context.window.TgcsUi.LanguageMenu;
assert.ok(menu, 'the header language switch must be available on every view');
assert.equal(root.components.LanguageMenu, menu);
assert.ok(menu.template.includes('$languagePreference'));
const {value} = make();
let focused = false;
const menuState = {
  $setLanguage: value.setLanguage,
  $el: {open: true, querySelector(selector) {assert.equal(selector, 'summary'); return {focus() {focused = true;}};}},
};
menu.methods.choose.call(menuState, 'zh');
assert.equal(value.state.language, 'zh');
assert.equal(menuState.$el.open, false);
assert.equal(focused, true);
const html = fs.readFileSync('static/index.html', 'utf8');
const header = html.match(/<header\b[\s\S]*?<\/header>/)?.[0] || '';
assert.match(header, /<language-menu\b/);
assert.ok(header.includes('https://github.com/RRHTY/tg-channel-sync'));
''')

    def test_visible_component_text_and_attributes_use_language_helpers(self):
        self.run_node(r'''
let root;
context.Vue = {createApp(options) {root = options; return {mount() {}, mixin() {}, use(plugin) {plugin.install(this); return this;}};}};
vm.runInNewContext(fs.readFileSync('static/ui-components.js', 'utf8'), context);
vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
const components = {...context.window.TgcsUi, ...root.components};
const visited = new Set(), missing = [];
function check(name, component) {
  if (!component || visited.has(component)) return;
  visited.add(component);
  // Language names remain in their own language so they can always be recognized.
  if (!['LanguageSelect', 'LanguageMenu'].includes(name) && component.template) {
    const template = component.template.replace(/\{\{[\s\S]*?\}\}/g, '');
    const tags = /<(?:[^>"']|"[^"]*"|'[^']*')*>/g;
    const text = template.replace(tags, '');
    if (/[\u4e00-\u9fff]/u.test(text)) missing.push(name + ': ' + text.trim());
    const attributes = /(?<![:\w-])(?:title|label|description|hint|placeholder|aria-label)="([^"]*)"/g;
    for (const tag of template.matchAll(tags)) {
      const tagName = /^<\/?([\w-]+)/.exec(tag[0])?.[1] || '';
      // Shared components translate display props at the point of rendering.
      if (tagName.includes('-')) continue;
      for (const match of tag[0].matchAll(attributes)) {
        if (/[\u4e00-\u9fff]/u.test(match[1])) missing.push(name + ': ' + match[0]);
      }
    }
  }
  for (const [childName, child] of Object.entries(component.components || {})) check(childName, child);
}
for (const [name, component] of Object.entries(components)) check(name, component);
assert.deepEqual(missing, [], 'untranslated visible component text');
''')

    def test_english_dictionary_covers_explicit_frontend_translation_keys(self):
        self.run_node(r'''
const messages = context.window.TgcsLocales.en, missing = [];
for (const file of ['static/index.html', 'static/app.js', 'static/ui-components.js', 'static/app-methods.js']) {
  const source = fs.readFileSync(file, 'utf8');
  const keys = /\$(?:t|tm)\(\s*(['"])((?:\\.|(?!\1).)*?)\1/g;
  for (const match of source.matchAll(keys)) {
    const key = vm.runInNewContext(match[1] + match[2] + match[1]);
    if (/[\u4e00-\u9fff]/u.test(key) && !Object.hasOwn(messages, key)) missing.push(file + ': ' + key);
  }
}
assert.deepEqual([...new Set(missing)], [], 'frontend translations missing from English dictionary');
let root;
context.Vue = {createApp(options) {root = options; return {mount() {}, mixin() {}, use(plugin) {plugin.install(this); return this;}};}};
vm.runInNewContext(fs.readFileSync('static/ui-components.js', 'utf8'), context);
vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
const components = {...context.window.TgcsUi, ...root.components};
const visited = new Set();
function checkProps(name, component) {
  if (!component || visited.has(component)) return;
  visited.add(component);
  for (const match of (component.template || '').matchAll(/(?<![:\w-])(?:title|label|description|hint|badge|text)="([^"]*)"/g)) {
    if (/[\u4e00-\u9fff]/u.test(match[1]) && !Object.hasOwn(messages, match[1])) missing.push(name + ': ' + match[1]);
  }
  for (const [childName, child] of Object.entries(component.components || {})) checkProps(childName, child);
}
for (const [name, component] of Object.entries(components)) checkProps(name, component);
assert.deepEqual([...new Set(missing)], [], 'display props missing from English dictionary');
for (const [name, prop] of [['SectionHeader', 'title'], ['SectionHeader', 'description'], ['FieldGroup', 'label'], ['FieldGroup', 'hint'], ['FieldGroup', 'badge'], ['FieldBadge', 'text'], ['SettingGroup', 'title'], ['SettingGroup', 'description'], ['ToggleField', 'label'], ['ToggleField', 'description']]) {
  assert.ok(context.window.TgcsUi[name].template.includes('$t(' + prop + ')'), name + '.' + prop + ' must render translated');
}
''')


if __name__ == '__main__':
    unittest.main()
