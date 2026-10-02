import shutil
import subprocess
import unittest
from pathlib import Path


NODE_SETUP = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
let app;
const context = {
  Vue: {createApp(options) {app = options; return {mount() {}};}},
  window: {TgcsApi: {}, TgcsUi: {}},
};
vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), context);
function component(name, extra = {}) {
  const definition = app.components[name];
  return {...definition.data?.(), ...definition.methods, ...extra};
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
'''


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class FrontendInteractionTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run(
            [shutil.which('node'), '-e', NODE_SETUP + script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_filter_submission_is_single_and_failure_keeps_draft(self):
        self.run_node(r'''
(async () => {
  const response = deferred(); let requests = 0, submitted, errors = 0;
  context.window.TgcsApi = {
    buildFormData: value => value,
    postForm: (url, value) => {requests++; submitted = value; return response.promise;},
    ensureSuccess: value => value,
  };
  vm.runInNewContext(fs.readFileSync('static/app-methods.js', 'utf8'), context);
  const draft = {rule_type:'drop', pattern:'ad', replacement:'', is_case_sensitive:1};
  const state = {...context.window.TgcsAppMethods, filterSaving:false, newFilter:draft,
    handleApiError() {errors++;}, showToast() {}, loadFilters:async () => {}};
  const first = state.addFilter(draft);
  assert.equal(state.filterSaving, true);
  await state.addFilter(draft);
  assert.equal(requests, 1);
  assert.notEqual(submitted, draft);
  response.reject(new Error('offline')); await first;
  assert.equal(errors, 1);
  assert.equal(state.filterSaving, false);
  assert.equal(state.newFilter, draft);
  assert.equal(state.newFilter.pattern, 'ad');
  context.window.TgcsApi.postForm = async () => {requests++; return {status:'success'};};
  await state.addFilter(draft);
  assert.equal(requests, 2);
  assert.equal(state.filterSaving, false);
  assert.equal(state.newFilter.pattern, '');
  assert.equal(state.newFilter.rule_type, 'drop');
  assert.equal(state.newFilter.is_case_sensitive, 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
''')

    def test_mapping_save_preserves_submitted_item_and_snapshot(self):
        self.run_node(r'''
(async () => {
  const response = deferred(); let requests = 0, submittedItem, submittedChanges;
  const state = component('ChannelMapping', {
    updateMapping(item, changes) {requests++; submittedItem = item; submittedChanges = changes; return response.promise;},
  });
  const firstItem = {source_id:1, target_id:2, source_title:'original'};
  const secondItem = {source_id:3, target_id:4, source_title:'other'};
  state.edit(firstItem); const save = state.saveEdit();
  state.edit(secondItem); await state.saveEdit(); state.toggleAdd();
  assert.equal(requests, 1);
  assert.equal(state.editing, firstItem);
  assert.equal(state.adding, false);
  assert.equal(submittedItem, firstItem);
  state.editForm.source_title = 'later typing';
  assert.equal(submittedChanges.source_title, 'original');
  // A response may only close the editor that initiated that request.
  state.editing = secondItem;
  response.resolve(true); await save;
  assert.equal(state.editing, secondItem);
  assert.equal(state.saving, false);
})().catch(error => {console.error(error); process.exitCode = 1;});
''')

    def test_log_switch_shows_latest_once_and_preserves_reading_positions(self):
        self.run_node(r'''
const state = component('LogViewer', {$nextTick:fn => fn()});
const system = {scrollTop:240, scrollHeight:1000, getClientRects:() => state.activeKind === 'system' ? [{}] : []};
const message = {scrollTop:0, scrollHeight:800, getClientRects:() => state.activeKind === 'message' ? [{}] : []};
context.document = {getElementById:id => id === 'sys-log-panel' ? system : message};
const template = app.components.LogViewer.template;
assert.ok(template.includes('<log-panel v-show="activeKind === \'system\'"'));
assert.ok(template.includes('<log-panel v-show="activeKind === \'message\'"'));
state.selectKind('message'); assert.equal(message.scrollTop, 800);
message.scrollTop = 190;
state.selectKind('system'); assert.equal(system.scrollTop, 240);
system.scrollTop = 160;
state.selectKind('message'); assert.equal(message.scrollTop, 190);
state.selectKind('system'); assert.equal(system.scrollTop, 160);
''')
