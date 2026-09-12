import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class ConnectionUiTests(unittest.TestCase):
    def test_login_hint_distinguishes_connecting_from_logged_out(self):
        script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let root;
const context={window:{TgcsUi:{}},Vue:{createApp:x=>{root=x;return {mount(){}};}}};
vm.runInNewContext(fs.readFileSync('static/app.js','utf8'),context);
const computed=root.components.SyncPanel.computed;
function panel(mode,status,sender='bot',target_type='channel'){
  const state={form:{mode,sender,target_type},userAuth:{status}};
  for(const [name,fn] of Object.entries(computed)) Object.defineProperty(state,name,{get:()=>fn.call(state)});
  return state;
}
assert.equal(panel('api','initializing').needsLogin,false);
assert.equal(panel('api','initializing').accountInitializing,true);
assert.equal(panel('clone','authorized').needsLogin,false);
assert.equal(panel('api','idle').needsLogin,true);
assert.equal(panel('json','idle').needsLogin,false);
assert.equal(panel('json','idle','user').needsLogin,true);
assert.equal(panel('json','idle','bot','saved').needsLogin,true);
assert.equal(panel('json','initializing').accountInitializing,false);
'''
        result = subprocess.run([shutil.which('node'), '-e', script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disconnect_reconnect_dedup_and_stale_source(self):
        script = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
class Source { close(){this.closed=true;} }
let calls=0, complete, watchdog;
const context = {window:{TgcsApi:{getJson:()=>new Promise(r=>complete=r),ensureSuccess:x=>x}}, EventSource:Source, document:{getElementById:()=>null}, setInterval:fn=>{watchdog=fn;return 1;}, clearInterval(){}};
vm.runInNewContext(fs.readFileSync('static/app-methods.js','utf8'),context);
const m=context.window.TgcsAppMethods;
const state={...m,bootstrapReady:true,connectionState:'connecting',syncStatus:{is_syncing:false},sysLogs:[{id:1}],msgLogs:[],showToast(){},handleApiError(){},$nextTick:fn=>fn(),scrollLogsToBottom(){}};
(async()=>{
state.setupSSE(); const first=state.sseConnection;
first.onmessage({data:JSON.stringify({status:{is_syncing:false},sys_logs:[{id:1},{id:2}]})});
assert.equal(state.connectionState,'connected');
assert.equal(state.sysLogs.length,2);
state.userAuth={status:'初始化中'};
const pendingAuth=state.loadUserAuthStatus();
first.onmessage({data:JSON.stringify({app_info:{user:{status:'已登录'}},user_auth:{status:'authorized',send_code_cooldown:0}})});
assert.equal(state.userAuth.status,'authorized','live auth must follow completed startup');
complete({status:'idle'}); await pendingAuth;
assert.equal(state.userAuth.status,'authorized','older HTTP response must not overwrite live auth');
first.onmessage({data:JSON.stringify({user_auth:{status:'idle',send_code_cooldown:0}})});
assert.equal(state.userAuth.status,'idle','account switch must restore the login prompt');
first.onerror(); assert.equal(state.connectionState,'reconnecting');
await state.startSync({}); assert.equal(state.syncStarting,undefined);
state.setupSSE(); const second=state.sseConnection;
first.onmessage({data:JSON.stringify({status:{is_syncing:true}})});
assert.equal(state.syncStatus.is_syncing,false);
second.onmessage({data:JSON.stringify({status:{is_syncing:false}})});
assert.equal(state.connectionState,'connected');
const refresh=state.loadSystemLogs();
second.onmessage({data:JSON.stringify({sys_logs:[{id:3}]})});
complete([{id:1},{id:2},{id:3}]); await refresh;
assert.equal(state.sysLogs.length,3);
assert.equal(state.sysLogs[2].id,3);
state.sysLogs=Array.from({length:100},(_,i)=>({id:i+1}));
const aheadRefresh=state.loadSystemLogs();
second.onmessage({data:JSON.stringify({sys_logs:Array.from({length:100},(_,i)=>({id:i+51}))})});
complete(Array.from({length:100},(_,i)=>({id:i+151}))); await aheadRefresh;
assert.equal(state.sysLogs.length,100);
assert.equal(state.sysLogs[0].id,151); assert.equal(state.sysLogs[99].id,250);
second.onmessage({data:JSON.stringify({sys_logs:[{id:'local-1',msg:'notice'},{id:249}]})});
assert.equal(state.sysLogs[98].id,250); assert.equal(state.sysLogs[99].id,'local-1');
second.onmessage({data:'invalid'}); assert.equal(state.connectionState,'reconnecting');
state.bootstrapReady=false;
state.bootstrap=async()=>{calls++;state.bootstrapReady=true;};
state.setupSSE(); await state.sseConnection.onopen();
assert.equal(calls,1); assert.equal(state.connectionState,'connecting');
state.sseConnection.onmessage({data:JSON.stringify({status:{is_syncing:false}})});
assert.equal(state.connectionState,'connected');
let refreshed=0;
state.loadSystemLogs=state.loadMessageLogs=state.loadUserAuthStatus=async()=>{refreshed++;};
await state.sseConnection.onopen(); assert.equal(refreshed,3);
const closed=state.sseConnection; closed.readyState=2; watchdog();
assert.notEqual(state.sseConnection,closed);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
