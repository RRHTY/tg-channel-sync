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

    def test_mapping_opens_history_without_overwriting_running_task(self):
        script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const element = {querySelector:()=>({focus(){}}), scrollIntoView(){}};
const context = {window:{TgcsApi:{}}, document:{getElementById:()=>element, querySelector:()=>({focus(){}})}, history:{replaceState(){}}, location:{pathname:'/',search:''}};
vm.runInNewContext(fs.readFileSync('static/app-methods.js','utf8'), context);
const state = {...context.window.TgcsAppMethods, syncForm:{force_send:'1'}, syncStatus:{is_syncing:false}, syncStarting:false, workspaceSection:'automatic', currentView:'home', $nextTick:fn=>fn(), showToast(){}};
state.useMapping({source_id:-123, target_id:-456, target_type:'saved'});
assert.equal(state.workspaceSection, 'history');
assert.equal(state.syncForm.source_id, '-123');
assert.equal(state.syncForm.target_type, 'saved');
assert.equal(state.syncForm.force_send, '0');
state.syncStarting = true; state.workspaceSection = 'automatic';
state.useMapping({source_id:-999, target_id:-888, target_type:'channel'});
assert.equal(state.workspaceSection, 'automatic');
assert.equal(state.syncForm.source_id, '-123');
'''
        result = subprocess.run([shutil.which('node'), '-e', script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hidden_log_panel_keeps_its_reading_position(self):
        script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
let visible = false;
const panel = {scrollTop:300, scrollHeight:1200, clientHeight:320, getClientRects:()=>visible ? [{}] : []};
const context = {window:{TgcsApi:{}}, document:{getElementById:id=>id==='sys-log-panel' ? panel : null}};
vm.runInNewContext(fs.readFileSync('static/app-methods.js','utf8'), context);
const state = {...context.window.TgcsAppMethods};
state.scrollLogsToBottom(); assert.equal(panel.scrollTop, 300);
panel.scrollHeight = 0; panel.clientHeight = 0; panel.scrollTop = 0;
assert.equal(state.isPanelNearBottom(panel), false);
visible = true; panel.scrollHeight = 1200; panel.clientHeight = 320;
state.scrollLogsToBottom(); assert.equal(panel.scrollTop, 1200);
'''
        result = subprocess.run([shutil.which('node'), '-e', script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
