import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class SyncRestoreUiTests(unittest.TestCase):
    def test_restore_is_explicit_safe_and_tolerates_bad_storage(self):
        script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
let stored = JSON.stringify({mode:'clone', source_id:'123', force_send:'1', bot_token:'secret'});
const context = { window:{TgcsApi:{}}, localStorage:{getItem:()=>stored, setItem:(k,v)=>stored=v, removeItem:()=>stored=null} };
vm.runInNewContext(fs.readFileSync('static/app-methods.js','utf8'), context);
const state = { ...context.window.TgcsAppMethods, syncForm:{mode:'api',source_id:'',force_send:'0'}, syncStatus:{is_syncing:false}, showToast(){}, lastSyncParams:null };
state.loadLastSyncParams();
assert.equal(state.syncForm.mode, 'api');
state.restoreLastSyncParams();
assert.equal(state.syncForm.source_id, '123');
assert.equal(state.syncForm.force_send, '0');
assert.equal(state.syncForm.bot_token, undefined);
state.syncStatus.is_syncing = true;
state.syncForm.source_id = 'busy';
state.restoreLastSyncParams();
assert.equal(state.syncForm.source_id, 'busy');
stored = '{broken'; state.loadLastSyncParams(); assert.equal(state.lastSyncParams, null);
state.rememberSyncParams({mode:'api',source_id:'456',force_send:'1',bot_token:'secret'});
assert.equal(JSON.parse(stored).bot_token, undefined);
state.clearLastSyncParams(); assert.equal(stored, null); assert.equal(state.lastSyncParams, null);
'''
        result = subprocess.run([shutil.which('node'), '-e', script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
