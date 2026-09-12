import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js required for UI behavior test")
class StartSyncUiTests(unittest.TestCase):
    def test_double_click_and_rejected_request(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let calls = 0, complete;
const context = {window: {TgcsApi: {
  buildFormData: x => x,
  postForm: () => { calls++; return new Promise(resolve => { complete = resolve; }); },
  ensureSuccess: value => { if (value.status === 'error') throw Error('failed'); return value; }
}}};
vm.runInNewContext(fs.readFileSync('static/app-methods.js', 'utf8'), context);
const start = context.window.TgcsAppMethods.startSync;
const state = {syncStarting: false, syncStatus: {is_syncing: false}, rememberSyncParams() {}, showToast() {}, handleApiError() {}};
(async () => {
  const first = start.call(state, {});
  assert.equal(state.syncStarting, true);
  await start.call(state, {});
  assert.equal(calls, 1);
  complete({status: 'success'});
  await first;
  assert.equal(state.syncStarting, false);
  assert.equal(state.syncStatus.is_syncing, true);
  await start.call(state, {});
  assert.equal(calls, 1);
  state.syncStatus.is_syncing = false;
  const rejected = start.call(state, {});
  complete({status: 'error'});
  await rejected;
  assert.equal(state.syncStarting, false);
  const retry = start.call(state, {});
  assert.equal(calls, 3);
  complete({status: 'success'});
  await retry;
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([shutil.which("node"), "-e", script],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
