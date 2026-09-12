import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class HashPerturbUiTests(unittest.TestCase):
    def test_clone_and_json_default_hash_perturb_on_independently(self):
        script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let root;
const context={window:{TgcsUi:{}},Vue:{createApp:x=>{root=x;return {mount(){}};}}};
vm.runInNewContext(fs.readFileSync('static/app.js','utf8'),context);
const panel=root.components.SyncPanel;
const state={form:{mode:'api',hash_perturb:'0'},...panel.data()};
state.selectMode=panel.methods.selectMode;

state.selectMode('clone');
assert.equal(state.form.hash_perturb,'1');
state.form.hash_perturb='0';
state.selectMode('json');
assert.equal(state.form.hash_perturb,'1');
state.form.hash_perturb='1';
state.selectMode('clone');
assert.equal(state.form.hash_perturb,'0');
state.form.hash_perturb='1';
state.selectMode('json');
assert.equal(state.form.hash_perturb,'1');
state.form.hash_perturb='0';
state.selectMode('clone');
assert.equal(state.form.hash_perturb,'1');
state.selectMode('json');
assert.equal(state.form.hash_perturb,'0');
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
