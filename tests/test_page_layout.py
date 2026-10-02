import shutil
import subprocess
import unittest
from pathlib import Path


NODE_SETUP = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
let app;
const hashWrites = [], focusCalls = [], scrollCalls = [], requestedPanels = [];
const panels = new Map();
const context = {
  Vue: {createApp(options) {app = options; return {mount() {}};}},
  window: {TgcsApi: {}, TgcsUi: {}, location: {hash: ''}},
  history: {replaceState(...args) {hashWrites.push(args);}},
  location: {pathname: '/', search: '?source=local'},
  document: {getElementById(id) {requestedPanels.push(id); return panels.get(id);}},
};
vm.runInNewContext(fs.readFileSync('static/app-methods.js', 'utf8'), context);
vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
function state(layout = 'single') {
  const value = {...app.data(), ...app.methods, $nextTick(fn) {fn();}};
  value.configForm.app.page_layout = layout;
  for (const [name, getter] of Object.entries(app.computed || {})) {
    Object.defineProperty(value, name, {get() {return getter.call(value);}});
  }
  return value;
}
function addPanel(section) {
  const heading = {focus(options) {focusCalls.push({section, options});}};
  const panel = {
    querySelector(selector) {assert.equal(selector, 'h2'); return heading;},
    scrollIntoView(options) {scrollCalls.push({section, options});},
  };
  panels.set('workspace-' + section, panel);
  return {panel, heading};
}
function plain(value) {return JSON.parse(JSON.stringify(value));}
'''


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class PageLayoutTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(
            [shutil.which('node'), '-e', NODE_SETUP + script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, encoding='utf-8', timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_and_switch_use_the_same_live_config(self):
        self.run_node(r'''
const initial = app.data();
assert.equal(initial.configForm.app.page_layout, 'single');
const value = state();
assert.equal(value.isSinglePage, true);
value.configForm.app.page_layout = 'sidebar';
assert.equal(value.isSinglePage, false);
value.configForm.app.page_layout = 'single';
assert.equal(value.isSinglePage, true);
delete value.configForm.app.page_layout;
assert.equal(value.isSinglePage, true);
''')

    def test_config_normalization_handles_old_and_invalid_layout_values(self):
        self.run_node(r'''
for (const [input, expected] of [
  [undefined, 'single'], [null, 'single'], ['', 'single'], ['unknown', 'single'],
  [1, 'single'], [false, 'single'], ['single', 'single'], ['sidebar', 'sidebar'],
  ['  SINGLE  ', 'single'], ['\tSiDeBaR\n', 'sidebar'],
]) {
  const value = state(input);
  if (input === undefined) delete value.configForm.app.page_layout;
  value.configForm.telegram.api_id = 0;
  value.configForm.telegram.extra_bot_tokens = ['first', 'second'];
  value.normalizeConfigForm();
  assert.equal(value.configForm.app.page_layout, expected);
  assert.equal(value.isSinglePage, expected === 'single');
  assert.equal(value.configForm.telegram.api_id, '');
  assert.equal(value.configForm.telegram.extra_bot_tokens, 'first\nsecond');
}
''')

    def test_single_layout_navigation_scrolls_and_focuses_target(self):
        self.run_node(r'''
context.window.matchMedia = query => {
  assert.equal(query, '(prefers-reduced-motion: reduce)');
  return {matches: false};
};
const value = state('single');
value.currentView = 'settings';
for (const section of ['history', 'automatic', 'filters', 'logs']) {
  const {heading} = addPanel(section);
  value.scrollLogsToBottom = () => {};
  value.selectWorkspace(section);
  assert.equal(value.workspaceSection, section);
  assert.equal(value.currentView, 'home');
  assert.equal(hashWrites.at(-1)[2], '/?source=local#' + section);
  assert.equal(heading.tabIndex, -1);
  assert.deepEqual(plain(focusCalls.at(-1)), {section, options: {preventScroll: true}});
  assert.deepEqual(plain(scrollCalls.at(-1)), {section, options: {block: 'start', behavior: 'smooth'}});
}
assert.equal(scrollCalls.length, 4);
''')

    def test_single_layout_respects_reduced_motion_and_missing_media_api(self):
        self.run_node(r'''
addPanel('history');
const value = state('single');
context.window.matchMedia = () => ({matches: true});
value.selectWorkspace('history');
assert.equal(scrollCalls.at(-1).options.behavior, 'auto');
delete context.window.matchMedia;
value.selectWorkspace('history');
assert.equal(scrollCalls.at(-1).options.behavior, 'smooth');
''')

    def test_sidebar_navigation_updates_hash_and_focus_without_scrolling(self):
        self.run_node(r'''
const value = state('sidebar');
value.currentView = 'settings';
const {heading} = addPanel('filters');
value.selectWorkspace('filters');
assert.equal(value.currentView, 'home');
assert.equal(value.workspaceSection, 'filters');
assert.equal(hashWrites.at(-1)[2], '/?source=local#filters');
assert.equal(heading.tabIndex, -1);
assert.deepEqual(plain(focusCalls), [{section: 'filters', options: {preventScroll: true}}]);
assert.equal(scrollCalls.length, 0);
''')

    def test_invalid_section_is_ignored_and_missing_panel_is_safe(self):
        self.run_node(r'''
const value = state('single');
value.currentView = 'settings';
const originalSection = value.workspaceSection;
for (const section of ['settings', '', null, 'unknown']) value.selectWorkspace(section);
assert.equal(value.currentView, 'settings');
assert.equal(value.workspaceSection, originalSection);
assert.equal(hashWrites.length, 0);
assert.equal(requestedPanels.length, 0);
value.selectWorkspace('automatic');
assert.equal(value.currentView, 'home');
assert.equal(value.workspaceSection, 'automatic');
assert.equal(hashWrites.at(-1)[2], '/?source=local#automatic');
assert.equal(focusCalls.length, 0);
assert.equal(scrollCalls.length, 0);
''')

    def test_layout_switch_preserves_workspace_and_log_reading_state(self):
        self.run_node(r'''
const value = state('sidebar');
addPanel('logs'); addPanel('history');
let logScrolls = 0;
value.scrollLogsToBottom = () => {logScrolls++;};
value.selectWorkspace('logs');
assert.equal(value.workspaceLogsOpened, true);
assert.equal(logScrolls, 1);
value.configForm.app.page_layout = 'single';
assert.equal(value.workspaceSection, 'logs');
assert.equal(value.workspaceLogsOpened, true);
value.selectWorkspace('history');
value.selectWorkspace('logs');
assert.equal(logScrolls, 1);
assert.deepEqual(scrollCalls.map(call => call.section), ['history', 'logs']);
''')

    def test_visible_logs_initialize_once_and_navigation_keeps_reading_position(self):
        self.run_node(r'''
const value = state('single');
addPanel('logs');
let visible = false;
const system = {scrollTop: 0, scrollHeight: 900, getClientRects: () => visible ? [{}] : []};
const message = {scrollTop: 40, scrollHeight: 700, getClientRects: () => []};
panels.set('sys-log-panel', system);
panels.set('msg-log-panel', message);

value.scrollLogsToBottom();
assert.equal(value.workspaceLogsOpened, false);
assert.equal(system.scrollTop, 0);
assert.equal(message.scrollTop, 40);

visible = true;
value.scrollLogsToBottom({sys: false, msg: false});
assert.equal(value.workspaceLogsOpened, false);
assert.equal(system.scrollTop, 0);

value.scrollLogsToBottom();
assert.equal(value.workspaceLogsOpened, true);
assert.equal(system.scrollTop, 900);
assert.equal(message.scrollTop, 40);

system.scrollTop = 120;
value.selectWorkspace('logs');
assert.equal(system.scrollTop, 120);
assert.equal(message.scrollTop, 40);
assert.equal(value.workspaceSection, 'logs');
assert.equal(scrollCalls.at(-1).section, 'logs');
''')

    def test_first_home_after_switch_to_single_initializes_logs_once(self):
        self.run_node(r'''
const value = state('sidebar');
addPanel('logs');
const windowScrolls = [];
context.window.scrollTo = options => windowScrolls.push(options);
const system = {
  scrollTop: 0, scrollHeight: 900,
  getClientRects: () => value.currentView === 'home' && value.isSinglePage ? [{}] : [],
};
panels.set('sys-log-panel', system);

// Bootstrap loads logs while the sidebar's history section is selected.
value.scrollLogsToBottom();
assert.equal(value.workspaceLogsOpened, false);
assert.equal(system.scrollTop, 0);
value.navigateTo('home');
assert.equal(value.workspaceLogsOpened, false);
assert.equal(system.scrollTop, 0);

value.navigateTo('settings');
value.configForm.app.page_layout = 'single';
value.scrollLogsToBottom();
assert.equal(value.currentView, 'settings');
assert.equal(value.workspaceLogsOpened, false);
assert.equal(system.scrollTop, 0);

value.navigateTo('home');
assert.equal(system.scrollTop, 900);
assert.equal(value.workspaceLogsOpened, true);
assert.deepEqual(plain(windowScrolls.at(-1)), {top: 0, behavior: 'smooth'});

system.scrollTop = 120;
value.selectWorkspace('logs');
assert.equal(system.scrollTop, 120);
value.navigateTo('settings');
value.navigateTo('home');
assert.equal(system.scrollTop, 120);
assert.equal(value.workspaceLogsOpened, true);
''')

    def test_setup_rebuild_initializes_new_log_panel_and_preserves_later_reading(self):
        self.run_node(r'''
(async () => {
  const value = state('single');
  context.window.scrollTo = () => {};
  const visibleRects = () => value.currentView === 'home' && value.isSinglePage ? [{}] : [];
  const initialPanel = {scrollTop: 0, scrollHeight: 900, getClientRects: visibleRects};
  panels.set('sys-log-panel', initialPanel);
  for (const method of [
    'loadConfig', 'fetchAppInfo', 'loadMappings', 'loadFilters', 'loadSettings',
    'loadUserAuthStatus', 'loadMessageLogs', 'loadLastSyncParams',
  ]) value[method] = async () => {};
  value.loadSetupStatus = async () => {value.setupStatus = {needs_setup: true};};
  let initiallyOpened = false;
  value.loadSystemLogs = async () => {
    assert.equal(value.currentView, 'home');
    value.scrollLogsToBottom();
    initiallyOpened = value.workspaceLogsOpened;
  };

  await value.bootstrap();
  assert.equal(initiallyOpened, true);
  assert.equal(initialPanel.scrollTop, 900);
  assert.equal(value.currentView, 'setup');
  assert.equal(value.workspaceLogsOpened, false);

  // Completing setup recreates the home panel, so its new scroll position starts at zero.
  const rebuiltPanel = {scrollTop: 0, scrollHeight: 900, getClientRects: visibleRects};
  panels.set('sys-log-panel', rebuiltPanel);
  addPanel('logs');
  value.configForm.telegram.bot_token = 'configured-token';
  let saved = false;
  value.saveConfig = async showToast => {
    assert.equal(showToast, true);
    saved = true;
    value.setupStatus = {needs_setup: false};
  };
  await value.saveSetup(false);
  assert.equal(saved, true);
  assert.equal(value.currentView, 'home');
  assert.equal(rebuiltPanel.scrollTop, 900);
  assert.equal(value.workspaceLogsOpened, true);

  rebuiltPanel.scrollTop = 120;
  value.selectWorkspace('logs');
  value.navigateTo('settings');
  value.navigateTo('home');
  assert.equal(rebuiltPanel.scrollTop, 120);
  assert.equal(value.workspaceLogsOpened, true);
})().catch(error => {console.error(error); process.exitCode = 1;});
''')
