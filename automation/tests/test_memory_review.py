"""Offline controller regressions: temporary native histories and fake argv workers.

No provider, live config, real session, writable source Harness, or scheduler.
Subprocess timeouts below bound TEST failures, not production worker execution.
"""
import copy
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


CONTROLLER = Path(__file__).resolve().parents[1] / "memory_review.py"
HELPER = CONTROLLER.parents[1] / "helpers" / "read_conversation.py"
SCHEMA = (HELPER.parent / "tests" / "native_schema_v1.sql").read_text()
SPEC = importlib.util.spec_from_file_location("offline_memory_review", CONTROLLER)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)
NOTE = "---\ntype: fact\ntopics: [fixture]\nsummary: Offline fixture.\n---\n\n# Note\n"


WORKER = r'''
import json
import os
from pathlib import Path
import sys
import time
root = Path(__file__).parent
job = json.load(sys.stdin)
identity = job['owner']['id']
settings = json.loads((root / 'behavior.json').read_text()).get(identity, {})
fd = os.open(root / 'calls.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
os.write(fd, (json.dumps({'pid': os.getpid(), 'ppid': os.getppid(), 'job': job}) + '\n').encode())
os.close(fd)
(root / ('started-' + identity)).touch()
if settings.get('hold'):
    while not (root / ('release-' + identity)).exists():
        time.sleep(.01)
if settings.get('kind') == 'fail':
    print('PRIVATE worker stderr/proposal', file=sys.stderr)
    sys.exit(7)
if settings.get('kind') == 'bad_json':
    print('PRIVATE malformed worker output')
    sys.exit(0)
coverage = [{**source, 'processed_through_entry': source['through_entry']} for source in job['sources']]
mode = settings.get('coverage')
if mode == 'missing': coverage = []
if mode == 'duplicate': coverage += coverage[:1]
if mode == 'partial': coverage[0]['processed_through_entry'] = coverage[0]['after_entry']
if mode == 'database': coverage[0]['database'] += '.unregistered'
if mode == 'conversation': coverage[0]['conversation_id'] += 1
if mode == 'state_file': coverage[0]['state_file'] += '.unregistered'
if mode == 'after': coverage[0]['after_entry'] += 1
if mode == 'through': coverage[0]['through_entry'] += 1
if mode == 'bool': coverage[0]['after_entry'] = False
result = {'version': 1, 'status': settings.get('status', 'complete'), 'coverage': coverage,
          'edits': settings.get('edits', []), 'cross_links': settings.get('cross_links', []),
          'summary': 'PRIVATE summary that must not reach diagnostics'}
result.update(settings.get('result_overrides', {}))
(root / ('finished-' + identity)).touch()
print(json.dumps(result))
'''


HISTORY_WRAPPER = r'''
import json
from pathlib import Path
import subprocess
import sys
root = Path(__file__).parent
settings = json.loads((root / 'history-behavior.json').read_text())
with (root / 'history-calls.jsonl').open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\n')
command = sys.argv[1]
result = subprocess.run([sys.executable, '-B', REAL_HELPER, *sys.argv[1:]], capture_output=True)
if result.returncode:
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    sys.exit(result.returncode)
value = json.loads(result.stdout)
mode = settings.get('mode')
if mode == 'snapshot_database' and command == 'snapshot': value['database'] += '.other'
if mode == 'snapshot_conversation' and command == 'snapshot': value['conversation_id'] += 1
if command == 'read':
    after = int(sys.argv[sys.argv.index('--after-entry') + 1])
    if mode == 'fail_later' and after:
        print('PRIVATE history failure', file=sys.stderr)
        sys.exit(4)
    if mode == 'stuck': value['next_after_entry'] = after
    if mode == 'early_end': value['has_more'] = False
    if mode == 'bounds': value['through_entry'] += 1
    if mode == 'commit_regression': value['observed_commit_seq'] = 0
    if mode == 'bad_role' and value['messages']: value['messages'][0]['role'] = 'toolResult'
    if mode == 'truncated':
        sys.stdout.write('{"messages": ["PRIVATE truncated')
        sys.exit(0)
print(json.dumps(value))
'''


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config_file = self.root / "config.json"
        self.worker = self.root / "worker.py"
        self.worker.write_text(WORKER)
        self.behavior = {}
        (self.root / "behavior.json").write_text("{}")
        self.policy = self.root / "policy.md"
        self.policy.write_text("Offline policy, not model instructions.\n")
        self.config = {"version": 1, "interval_seconds": 900, "enabled": True,
                       "history_helper": str(HELPER), "policy_file": str(self.policy),
                       "worker_command": [sys.executable, "-B", str(self.worker)], "agents": []}
        self.add_owner("planning")
        self.save_config()

    def add_owner(self, identity, *, enabled=True, registered=True):
        workspace = self.root / identity
        (workspace / "memory").mkdir(parents=True)
        (workspace / "state").mkdir()
        (workspace / "memory" / "index.md").write_text(NOTE)
        state = self.make_source(workspace / "state" / "history.json", self.root / (identity + ".sqlite"))
        agent = {"id": identity, "enabled": enabled, "workspace": str(workspace),
                 "owned_paths": ["memory"], "indexes": ["memory/index.md"],
                 "state_files": [str(state)] if registered else [], "require_metadata": True}
        self.config["agents"].append(agent)
        return agent

    def make_source(self, state, database, *, conversation=1):
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript(SCHEMA)
            connection.execute("INSERT INTO record_ids VALUES (?, 'conversation')", (conversation,))
            connection.execute("INSERT INTO conversations VALUES (?, NULL, NULL, ?)",
                               (conversation, json.dumps({"id": conversation})))
        state.write_text(json.dumps({"version": 1, "mode": "history-reference",
                                     "source": {"database": str(database), "conversation_id": conversation},
                                     "last_saved_entry_id": 0, "last_saved_review": None,
                                     "active_review": None, "preserved_extra": "unchanged"}))
        return state

    def agent(self, identity="planning"):
        return next(agent for agent in self.config["agents"] if agent["id"] == identity)

    def state(self, identity="planning", index=0):
        return Path(self.agent(identity)["state_files"][index])

    def memory(self, relative="index.md", identity="planning"):
        return Path(self.agent(identity)["workspace"]) / "memory" / relative

    def append(self, entry, *, kind="pi.user", text="PRIVATE source text", identity="planning", index=0):
        source = json.loads(self.state(identity, index).read_text())["source"]
        conversation = source["conversation_id"]
        role = {"pi.user": "user", "pi.assistant": "assistant", "pi.system": "system",
                "pi.tool-result": "toolResult", "pi-d.subagent-completion": "user"}.get(kind)
        record = {"id": entry, "conversationId": conversation, "kind": kind}
        if role:
            record["model"] = [{"role": role, "content": [{"type": "text", "text": text}]}]
        with closing(sqlite3.connect(source["database"])) as connection, connection:
            sequence = connection.execute("SELECT next_seq FROM durable_metadata").fetchone()[0]
            connection.execute("INSERT INTO record_ids VALUES (?, 'entry')", (entry,))
            connection.execute("INSERT INTO entries VALUES (?, ?, NULL, ?, ?)",
                               (entry, conversation, sequence, json.dumps(record)))
            connection.execute("UPDATE durable_metadata SET next_seq=?, next_id=?", (sequence + 1, str(entry + 1)))

    def save_config(self):
        self.config_file.write_text(json.dumps(self.config))
        (self.root / "behavior.json").write_text(json.dumps(self.behavior))

    def wrapper(self, mode="normal"):
        path = self.root / "history_wrapper.py"
        path.write_text("REAL_HELPER = " + repr(str(HELPER)) + "\n" + HISTORY_WRAPPER)
        (self.root / "history-behavior.json").write_text(json.dumps({"mode": mode}))
        self.config["history_helper"] = str(path)
        self.save_config()

    def invoke(self, command, *extra, expected=0):
        self.save_config()
        process = subprocess.run([sys.executable, "-B", str(CONTROLLER), "--config", str(self.config_file),
                                  command, *extra], capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        self.assertEqual(process.stderr, "")
        self.assertNotIn("PRIVATE", process.stdout)
        self.assertNotIn("Traceback", process.stdout)
        return json.loads(process.stdout)

    def run_owner(self, identity="planning", *, expected=0, audit=False, recover=False):
        options = (["--audit"] if audit else []) + (["--recover"] if recover else [])
        return self.invoke("run", "--agent", identity, *options, expected=expected)["agents"][0]

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def cursor(self, identity="planning", index=0):
        return json.loads(self.state(identity, index).read_text())["last_saved_entry_id"]

    def edit(self, relative="memory/index.md", *, content=NOTE + "New supported fact.\n", expected="auto"):
        path = Path(self.agent()["workspace"]) / relative
        if expected == "auto":
            expected = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        return {"path": relative, "expected_sha256": expected, "content": content}

    def link(self, destination="projects", *, path="memory/index.md", target="planning", target_path="memory/index.md"):
        return {"destination_agent": destination, "destination_path": path,
                "target_agent": target, "target_path": target_path}

    def wait(self, condition, message="subprocess failed to progress"):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.02)
        self.fail(message)

    def start_run(self, identity="planning"):
        self.save_config()
        child = subprocess.Popen([sys.executable, "-B", str(CONTROLLER), "--config", str(self.config_file),
                                  "run", "--agent", identity], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def settle():
            (self.root / ("release-" + identity)).touch()
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
        self.addCleanup(settle)
        self.wait(lambda: (self.root / ("started-" + identity)).exists())
        return child

    def finish(self, child, identity="planning", *, expected=0):
        (self.root / ("release-" + identity)).touch()
        stdout, stderr = child.communicate(timeout=15)
        self.assertEqual(child.returncode, expected, stdout + stderr)
        self.assertEqual(stderr, "")
        self.assertNotIn("PRIVATE", stdout)
        return json.loads(stdout)["agents"][0]

    def test_empty_and_filtered_history_are_read_only_no_launch(self):
        before = self.state().read_bytes()
        for command in ("doctor", "status", "check", "tick"):
            self.invoke(command)
        self.assertEqual(self.run_owner()["status"], "unchanged")
        self.assertEqual(self.calls(), [])
        self.append(10, kind="pi.system")
        self.append(20, kind="pi.tool-result")
        self.append(30, kind="pi-d.subagent-completion")
        self.append(40, kind="pi.assistant")
        self.append(50, text=" \n\t")
        self.assertEqual(self.invoke("check")["agents"][0]["status"], "unchanged")
        self.assertEqual(self.invoke("tick")["agents"][0]["status"], "unchanged")
        self.assertEqual(self.run_owner()["status"], "unchanged")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state().read_bytes(), before)

    def test_doctor_check_status_never_create_runtime_or_change_any_file(self):
        self.append(10)
        self.wrapper()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(self.invoke("doctor")["agents"][0]["status"], "ready")
        self.assertEqual(self.invoke("check")["agents"][0]["status"], "pending")
        self.assertEqual(self.invoke("status")["agents"][0]["status"], "idle")
        # The fake helper intentionally records calls; the real controller writes nothing.
        after = {path: path.read_bytes() for path in self.root.rglob("*")
                 if path.is_file() and path.name != "history-calls.jsonl"}
        self.assertEqual(before, after)
        self.assertFalse((self.root / review.RUNTIME_NAME).exists())
        self.assertEqual(self.calls(), [])

    def test_disabled_global_disabled_owner_and_unregistered_never_launch(self):
        self.append(10)
        cases = [(False, True, True, "disabled"), (True, False, True, "disabled"),
                 (True, True, False, "unregistered")]
        original = list(self.agent()["state_files"])
        for global_enabled, owner_enabled, registered, status in cases:
            with self.subTest(status=status, global_enabled=global_enabled):
                self.config["enabled"] = global_enabled
                self.agent()["enabled"] = owner_enabled
                self.agent()["state_files"] = original if registered else []
                for command in ("doctor", "check", "tick", "status"):
                    self.assertEqual(self.invoke(command)["agents"][0]["status"], status)
                self.assertEqual(self.run_owner()["status"], status)
                self.assertEqual(self.run_owner(audit=True)["status"], status)
        self.assertEqual(self.calls(), [])

    def test_busy_owner_never_launches_any_worker(self):
        self.append(10)
        config = review.Config(self.config_file)
        config.runtime_valid(create=True)
        with review.lock(config.lock_path("planning"), create=True):
            for command in ("doctor", "status", "check", "tick"):
                self.assertEqual(self.invoke(command)["agents"][0]["status"], "busy")
            self.assertEqual(self.run_owner()["status"], "busy")
            self.assertEqual(self.run_owner(audit=True)["status"], "busy")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cursor(), 0)

    def test_busy_native_source_never_launches_worker_or_checkpoints(self):
        self.append(10)
        before = self.state().read_bytes()
        database = json.loads(before)["source"]["database"]
        connection = sqlite3.connect(database)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            self.assertEqual(self.run_owner(expected=1)["error"], "history_failed")
            self.assertEqual(self.calls(), [])
            self.assertEqual(self.state().read_bytes(), before)
        finally:
            connection.rollback()
            connection.close()
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(self.cursor(), 10)

    def test_legacy_active_review_is_preserved_and_blocks(self):
        value = json.loads(self.state().read_text())
        value["active_review"] = {"unknown_job": "legacy", "through_entry": 999}
        self.state().write_text(json.dumps(value))
        before = self.state().read_bytes()
        self.append(10)
        for command in ("doctor", "check", "tick", "status"):
            self.assertEqual(self.invoke(command, expected=1)["agents"][0]["error"], "legacy_active_review")
        self.assertEqual(self.run_owner(expected=1)["error"], "legacy_active_review")
        self.assertEqual(self.state().read_bytes(), before)
        self.assertEqual(self.calls(), [])

    def test_full_paging_and_job_is_reference_only(self):
        for entry in range(10, 1240, 10):
            self.append(entry, kind="pi.system" if entry != 1230 else "pi.user")
        self.wrapper()
        self.assertEqual(self.run_owner()["status"], "complete")
        reads = [json.loads(line) for line in (self.root / "history-calls.jsonl").read_text().splitlines()]
        reads = [args for args in reads if args[0] == "read"]
        self.assertEqual([int(args[args.index("--after-entry") + 1]) for args in reads], [0, 500, 1000])
        self.assertEqual({args[args.index("--through-entry") + 1] for args in reads}, {"1230"})
        job = self.calls()[0]["job"]
        self.assertEqual(job["mode"], "capture")
        self.assertEqual(job["sources"][0]["after_entry"], 0)
        self.assertEqual(job["sources"][0]["through_entry"], 1230)
        self.assertNotIn("PRIVATE", json.dumps(job))
        self.assertNotIn("messages", job)
        self.assertNotIn("text_blocks", json.dumps(job))
        self.assertEqual(self.cursor(), 1230)
        state = json.loads(self.state().read_text())
        self.assertEqual(state["preserved_extra"], "unchanged")
        self.assertIsNone(state["active_review"])
        self.assertTrue(state["last_saved_review"].startswith("controller-planning-"))
        self.assertEqual(self.run_owner()["status"], "unchanged")
        self.assertEqual(len(self.calls()), 1)

    def test_arrivals_after_freeze_are_left_pending(self):
        self.append(10)
        self.behavior["planning"] = {"hold": True}
        child = self.start_run()
        self.append(20, text="PRIVATE later arrival")
        self.finish(child)
        self.assertEqual(self.cursor(), 10)
        self.assertEqual(self.calls()[0]["job"]["sources"][0]["through_entry"], 10)
        self.assertEqual(self.invoke("check")["agents"][0]["status"], "pending")
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(self.cursor(), 20)
        self.assertEqual(self.calls()[1]["job"]["sources"][0]["after_entry"], 10)

    def test_manual_audit_empty_range_and_missing_coverage(self):
        self.behavior["planning"] = {"edits": [self.edit("memory/audited.md")]}
        self.assertEqual(self.run_owner()["status"], "unchanged")
        self.assertEqual(self.run_owner(audit=True)["status"], "complete")
        job = self.calls()[0]["job"]
        self.assertEqual(job["mode"], "audit")
        self.assertEqual(job["sources"][0]["after_entry"], 0)
        self.assertEqual(job["sources"][0]["through_entry"], 0)
        self.assertTrue(self.memory("audited.md").exists())
        self.assertEqual(self.cursor(), 0)
        self.behavior["planning"] = {"coverage": "missing", "edits": [self.edit()]}
        before = self.memory().read_bytes()
        self.assertEqual(self.run_owner(audit=True, expected=1)["error"], "incomplete_coverage")
        self.assertEqual(self.memory().read_bytes(), before)
        self.assertEqual(self.invoke("tick")["agents"][0]["status"], "unchanged")
        self.assertEqual(len(self.calls()), 2)

    def test_manual_audit_already_captured_positive_checkpoint(self):
        self.append(10)
        self.assertEqual(self.run_owner()["status"], "complete")
        before = self.state().read_bytes()
        self.behavior["planning"] = {"edits": [self.edit()]}
        self.assertEqual(self.run_owner(audit=True)["status"], "complete")
        job = self.calls()[-1]["job"]
        self.assertEqual(job["mode"], "audit")
        self.assertEqual(job["sources"][0]["after_entry"], 10)
        self.assertEqual(job["sources"][0]["through_entry"], 10)
        self.assertEqual(self.state().read_bytes(), before)
        self.assertEqual(self.invoke("tick")["agents"][0]["status"], "unchanged")
        self.assertEqual(len(self.calls()), 2)

    def test_atomic_replacements_preserve_modes_and_private_backups_stay_outside_memory(self):
        self.append(10)
        self.memory().chmod(0o600)
        self.state().chmod(0o640)
        self.behavior["planning"] = {"edits": [self.edit()]}
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(self.memory().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state().stat().st_mode & 0o777, 0o640)
        runtime = self.root / review.RUNTIME_NAME
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
        backups = list(runtime.rglob("*.before"))
        self.assertTrue(backups)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in backups))
        self.assertEqual([path.name for path in self.memory().parent.iterdir()], ["index.md"])

    def test_detached_failure_keeps_a_bounded_sanitized_last_result(self):
        self.append(10)
        self.behavior["planning"] = {"kind": "fail"}
        self.save_config()
        config = review.Config(self.config_file)
        result_path = config.lock_path("planning").with_suffix(".result.json")
        self.assertEqual(self.invoke("tick")["agents"][0]["status"], "dispatched")
        self.wait(result_path.is_file)
        failed = json.loads(result_path.read_text())
        self.assertEqual(failed["result"]["status"], "blocked")
        self.assertEqual(failed["result"]["error"], "worker_failed")
        self.assertNotIn("PRIVATE", result_path.read_text())
        self.assertEqual(self.cursor(), 0)
        before = result_path.read_bytes()
        status = self.invoke("status")["agents"][0]
        self.assertEqual(status["last_result"]["error"], "worker_failed")
        self.assertEqual(result_path.read_bytes(), before, "Reading status must not write it")
        self.behavior["planning"] = {}
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(json.loads(result_path.read_text())["result"]["status"], "complete")
        self.assertEqual(len(list(config.runtime.glob("*.result.json"))), 1)
        self.assertEqual(self.cursor(), 10)

    def test_real_node_pipeline_applies_faux_proposals_and_idle_tick_is_free(self):
        self.add_owner("hyatt", enabled=False, registered=False)
        for entry in range(10, 530, 10):
            self.append(entry)
        host = Path(os.environ.get("TEST_PI_D_ROOT") or CONTROLLER.parents[2] / "pi-d").resolve()
        runtime = self.root / "runtime.json"
        runtime.write_text(json.dumps({"version": 1, "pi_d_root": str(host),
            "auth_module_sha256": hashlib.sha256((host / "src/auth.ts").read_bytes()).hexdigest(),
            "model": {"provider": "faux", "modelId": "faux-1"},
            "thinking_level": "off", "allow_auth_refresh": False}))
        calls = self.root / "native-model-calls.txt"
        fixture = {"module": (CONTROLLER.parent / "run_review.mjs").as_uri(),
                   "runtime": str(runtime), "calls": str(calls),
                   "old_hash": hashlib.sha256(NOTE.encode()).hexdigest(),
                   "index": NOTE + "\n- [New fixture note](new.md)\n",
                   "new_note": NOTE + "Synthetic offline fixture only.\n"}
        worker = self.root / "faux-review.mjs"
        worker.write_text("const fixture = " + json.dumps(fixture) + ";\n" + r'''
import { readFile, appendFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const { main, verifyRuntime } = await import(fixture.module);
const runtime = JSON.parse(await readFile(fixture.runtime, 'utf8'));
const loaded = await verifyRuntime(runtime);
const { fauxProvider, createModels, fauxAssistantMessage } = await import(pathToFileURL(
  path.join(runtime.pi_d_root, 'node_modules/@earendil-works/pi-ai/dist/index.js')).href);
const provider = fauxProvider({ tokensPerSecond: 0, tokenSize: { min: 4096, max: 4096 } });
provider.setResponses([async (context) => {
  const tools = context.messages.flatMap((message) => message.role === 'system' ? message.toolsAdded ?? [] : []);
  if (JSON.stringify(tools.map((tool) => tool.name).sort()) !== '["list_notes","read_note"]') {
    throw new Error('Unexpected capability in offline fixture');
  }
  await appendFile(fixture.calls, 'called\n');
  return fauxAssistantMessage(JSON.stringify({ status: 'complete', summary: 'Offline fixture proposal.',
    edits: [{ path: 'memory/index.md', expected_sha256: fixture.old_hash, content: fixture.index },
            { path: 'memory/new.md', expected_sha256: null, content: fixture.new_note }],
    cross_links: [{ destination_agent: 'hyatt', destination_path: 'memory/index.md',
                   target_agent: 'planning', target_path: 'memory/new.md' }] }));
}]);
const models = createModels(); models.setProvider(provider.provider);
process.exitCode = await main(['--runtime', fixture.runtime], {
  loadRuntime: async () => loaded, makeModels: () => models,
});
''')
        node = shutil.which("node")
        self.assertIsNotNone(node, "The staged native worker requires Node")
        self.config["worker_command"] = [node, str(worker)]
        database = Path(json.loads(self.state().read_text())["source"]["database"])
        before_database = database.read_bytes()
        before_peer = self.memory(identity="hyatt").read_bytes()
        result = self.run_owner()
        self.assertEqual(result["status"], "complete")
        self.assertEqual((result["edits"], result["cross_links"]), (2, 1))
        self.assertEqual(self.memory().read_text(), fixture["index"])
        self.assertEqual(self.memory("new.md").read_text(), fixture["new_note"])
        self.assertEqual(self.memory(identity="hyatt").read_bytes(), before_peer +
                         b'- [Memory: planning](<../../planning/memory/new.md>)\n')
        self.assertEqual(self.cursor(), 520)
        self.assertEqual(database.read_bytes(), before_database)
        self.assertEqual(calls.read_text().splitlines(), ["called"])
        before_state = self.state().read_bytes()
        self.assertEqual(self.invoke("tick")["agents"][0]["status"], "unchanged")
        self.assertEqual(self.state().read_bytes(), before_state)
        self.assertEqual(calls.read_text().splitlines(), ["called"])

    def test_tick_launches_separate_detached_owner_processes(self):
        self.add_owner("projects")
        self.append(10)
        self.append(20, identity="projects")
        self.behavior = {"planning": {"hold": True}, "projects": {"hold": True}}
        def release_all():
            for identity in ("planning", "projects"):
                (self.root / ("release-" + identity)).touch()
        self.addCleanup(release_all)
        output = self.invoke("tick")
        self.assertEqual([item["status"] for item in output["agents"]], ["dispatched", "dispatched"])
        for identity in ("planning", "projects"):
            self.wait(lambda identity=identity: (self.root / ("started-" + identity)).exists())
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual({call["ppid"] for call in calls}, {item["pid"] for item in output["agents"]})
        for call in calls:
            identity = call["job"]["owner"]["id"]
            self.assertEqual(len(call["job"]["sources"]), 1)
            self.assertEqual(call["job"]["sources"][0]["state_file"], str(self.state(identity)))
            self.assertEqual([peer["id"] for peer in call["job"]["peers"]],
                             ["projects" if identity == "planning" else "planning"])
        release_all()
        self.wait(lambda: self.cursor() == 10 and self.cursor("projects") == 20)
        self.wait(lambda: all(item["status"] != "busy" for item in self.invoke("status")["agents"]))
        self.assertEqual(len(self.calls()), 2)

    def test_second_run_and_tick_skip_overlapping_worker(self):
        self.append(10)
        self.behavior["planning"] = {"hold": True}
        first = self.start_run()
        self.assertEqual(self.run_owner()["status"], "busy")
        self.assertEqual(self.invoke("tick")["agents"][0]["status"], "busy")
        self.assertEqual(len(self.calls()), 1)
        self.finish(first)
        self.assertEqual(self.cursor(), 10)

    def test_adapter_inherits_lock_after_supervisor_is_killed(self):
        self.append(10)
        self.behavior["planning"] = {"hold": True}
        child = self.start_run()
        child.kill()
        child.wait(timeout=5)
        self.assertEqual(self.run_owner()["status"], "busy")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.cursor(), 0)
        (self.root / "release-planning").touch()
        self.wait(lambda: (self.root / "finished-planning").exists())
        # The orphaned adapter can now exit (its stdout pipe has no supervisor).
        self.wait(lambda: self.invoke("status")["agents"][0]["status"] != "busy")
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(self.cursor(), 10)

    def test_history_source_and_page_failures_never_launch_or_checkpoint(self):
        for entry in range(10, 530, 10):
            self.append(entry)
        before = self.state().read_bytes()
        for mode in ("snapshot_database", "snapshot_conversation", "fail_later", "stuck", "early_end",
                     "bounds", "commit_regression", "bad_role", "truncated"):
            with self.subTest(mode=mode):
                self.wrapper(mode)
                self.assertEqual(self.run_owner(expected=1)["status"], "blocked")
                self.assertEqual(self.state().read_bytes(), before)
                self.assertEqual(self.calls(), [])
        self.wrapper()
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(self.cursor(), 520)

    def test_invalid_or_missing_registration_is_not_guessed(self):
        self.append(10)
        original = self.state().read_bytes()
        value = json.loads(original)
        cases = []
        for key, replacement in (("last_saved_entry_id", None), ("last_saved_entry_id", True),
                                 ("last_saved_entry_id", 999), ("version", 2), ("mode", "tail")):
            altered = copy.deepcopy(value)
            altered[key] = replacement
            cases.append(altered)
        altered = copy.deepcopy(value)
        altered["source"]["database"] = "relative.sqlite"
        cases.append(altered)
        altered = copy.deepcopy(value)
        altered["source"]["conversation_id"] = 999
        cases.append(altered)
        for altered in cases:
            with self.subTest(state=altered):
                self.state().write_text(json.dumps(altered))
                before = self.state().read_bytes()
                self.assertEqual(self.run_owner(expected=1)["status"], "blocked")
                self.assertEqual(self.state().read_bytes(), before)
        self.assertEqual(self.calls(), [])
        self.state().write_bytes(original)
        self.assertEqual(self.run_owner()["status"], "complete")

    def test_worker_failures_and_exact_coverage_are_retryable(self):
        self.append(10)
        before = self.state().read_bytes()
        settings = [{"kind": "fail"}, {"kind": "bad_json"}, {"status": "blocked"},
                    {"result_overrides": {"version": True}}, {"result_overrides": {"summary": []}}]
        settings += [{"coverage": mode} for mode in ("missing", "duplicate", "partial", "database", "conversation",
                                                    "state_file", "after", "through", "bool")]
        for setting in settings:
            with self.subTest(setting=setting):
                self.behavior["planning"] = {**setting, "edits": [self.edit()]}
                self.assertEqual(self.run_owner(expected=1)["status"], "blocked")
                self.assertEqual(self.state().read_bytes(), before)
                self.assertEqual(self.memory().read_text(), NOTE)
        self.behavior["planning"] = {"edits": [self.edit()]}
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(self.cursor(), 10)
        self.assertEqual(len(self.calls()), len(settings) + 1)

    def test_duplicate_registered_source_references_across_states_or_owners_are_rejected(self):
        self.append(10)
        self.add_owner("projects", enabled=False)
        peer_state = self.state("projects")
        peer_original = peer_state.read_bytes()
        peer_state.write_bytes(self.state().read_bytes())
        self.assertEqual(self.invoke("doctor", expected=2)["error"], "duplicate_source_reference")
        self.assertEqual(self.invoke("run", "--agent", "planning", expected=2)["error"],
                         "duplicate_source_reference")
        peer_state.write_bytes(peer_original)
        duplicate = self.state().with_name("duplicate.json")
        duplicate.write_bytes(self.state().read_bytes())
        self.agent()["state_files"].append(str(duplicate))
        self.assertEqual(self.invoke("check", expected=2)["error"], "duplicate_source_reference")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cursor(), 0)
        self.agent()["state_files"].pop()
        # Duplicate canonical source names cannot evade the guard with dot paths.
        peer_value = json.loads(peer_original)
        peer_value["source"] = json.loads(self.state().read_text())["source"]
        peer_value["source"]["database"] = str(self.root) + "/./planning.sqlite"
        peer_state.write_text(json.dumps(peer_value))
        self.assertEqual(self.invoke("check", expected=2)["error"], "duplicate_source_reference")
        peer_state.write_bytes(peer_original)
        self.assertEqual(self.run_owner()["status"], "complete")

    def test_multiple_sources_require_every_source_coverage(self):
        second = self.make_source(Path(self.agent()["workspace"]) / "state" / "other.json",
                                  self.root / "other.sqlite", conversation=7)
        self.agent()["state_files"].append(str(second))
        self.append(10)
        # The second source is empty: it STILL must appear in coverage.
        self.behavior["planning"] = {"coverage": "missing"}
        self.assertEqual(self.run_owner(expected=1)["error"], "incomplete_coverage")
        self.assertEqual(self.cursor(), 0)
        self.behavior["planning"] = {}
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(len(self.calls()[-1]["job"]["sources"]), 2)
        self.assertEqual(self.cursor(index=1), 0)

    def test_traversal_non_markdown_instructions_and_unowned_edits_rejected(self):
        self.add_owner("projects")
        self.append(10)
        outside = Path(self.agent()["workspace"]) / "outside.md"
        outside.write_text("Must not change")
        before_foreign = self.memory(identity="projects").read_bytes()
        for path in ("../projects/memory/index.md", "/absolute.md", "memory/../outside.md",
                     "memory//index.md", "memory/./index.md", "memory\\index.md", "memory/notes.json",
                     "memory/AGENTS.md", "memory/CLAUDE.md", "memory/instructions.md",
                     "state/history.json", "outside.md", "memory/new-directory/note.md"):
            with self.subTest(path=path):
                self.behavior["planning"] = {"edits": [{"path": path, "expected_sha256": None, "content": NOTE}]}
                self.assertEqual(self.run_owner(expected=1)["status"], "blocked")
                self.assertEqual(self.cursor(), 0)
                self.assertEqual(self.memory().read_text(), NOTE)
                self.assertEqual(self.memory(identity="projects").read_bytes(), before_foreign)
                self.assertEqual(outside.read_text(), "Must not change")
        extra = self.edit()
        extra["agent"] = "projects"
        self.behavior["planning"] = {"edits": [extra]}
        self.assertEqual(self.run_owner(expected=1)["error"], "invalid_edit")

    def test_file_directory_symlinks_and_hardlinks_cannot_escape(self):
        self.append(10)
        outside = self.root / "external.md"
        outside.write_text(NOTE)
        self.memory("escape.md").symlink_to(outside)
        self.memory("escape-dir").symlink_to(self.root, target_is_directory=True)
        os.link(outside, self.memory("hard.md"))
        for path in ("memory/escape.md", "memory/escape-dir/external.md", "memory/hard.md"):
            with self.subTest(path=path):
                self.behavior["planning"] = {"edits": [self.edit(path)]}
                self.assertEqual(self.run_owner(expected=1)["status"], "blocked")
                self.assertEqual(outside.read_text(), NOTE)
                self.assertEqual(self.cursor(), 0)
        self.add_owner("projects")
        self.memory("bad-link.md", "projects").symlink_to(outside)
        self.behavior["planning"] = {"cross_links": [self.link(path="memory/bad-link.md")]}
        self.assertEqual(self.run_owner(expected=1)["error"], "symlink_path")
        self.assertEqual(outside.read_text(), NOTE)

    def test_metadata_duplicate_edits_and_bad_hash_types_are_rejected(self):
        self.append(10)
        settings = [([self.edit(content="No metadata")], "missing_metadata"),
                    ([self.edit(), self.edit()], "duplicate_edit"),
                    ([self.edit(expected=True)], "invalid_base_hash"),
                    ([self.edit(content=NOTE + "\ud800")], "invalid_content")]
        for edits, code in settings:
            self.behavior["planning"] = {"edits": edits}
            self.assertEqual(self.run_owner(expected=1)["error"], code)
            self.assertEqual(self.memory().read_text(), NOTE)
            self.assertEqual(self.cursor(), 0)
        self.agent()["require_metadata"] = False
        self.behavior["planning"] = {"edits": [self.edit(content="Metadata explicitly optional\n")]}
        self.assertEqual(self.run_owner()["status"], "complete")

    def test_case_and_unicode_aliases_are_rejected_before_any_write(self):
        self.append(10)
        for first, second in (("memory/New.md", "memory/new.md"),
                              ("memory/caf\u00e9.md", "memory/cafe\u0301.md")):
            with self.subTest(paths=(first, second)):
                self.behavior["planning"] = {"edits": [
                    self.edit(first, content=NOTE + "First proposed content.\n"),
                    self.edit(second, content=NOTE + "Different proposed content.\n")]}
                self.assertEqual(self.run_owner(expected=1)["error"], "duplicate_target")
                self.assertEqual([p.name for p in self.memory().parent.iterdir()], ["index.md"])
                self.assertEqual(self.cursor(), 0)
                self.assertFalse((self.root / review.RUNTIME_NAME / "transactions").exists())

    def test_foreign_arbitrary_content_and_invalid_targets_are_rejected_before_any_write(self):
        self.add_owner("projects")
        self.append(10)
        arbitrary = self.link()
        arbitrary["content"] = "PRIVATE arbitrary foreign rewrite"
        invalid_links = [arbitrary, self.link(destination="unknown"), self.link(destination="planning"),
                         self.link(target="unknown"), self.link(target="projects"),
                         self.link(target_path="memory/missing.md"),
                         self.link(path="memory/missing.md"), self.link(target_path="memory/AGENTS.md")]
        before = self.memory(identity="projects").read_bytes()
        for link in invalid_links:
            self.behavior["planning"] = {"edits": [self.edit("memory/staged.md")], "cross_links": [link]}
            self.assertEqual(self.run_owner(expected=1)["status"], "blocked")
            self.assertFalse(self.memory("staged.md").exists())
            self.assertEqual(self.memory(identity="projects").read_bytes(), before)
            self.assertEqual(self.cursor(), 0)

    def test_cross_links_are_additive_deduplicated_and_support_own_staged_new_targets(self):
        self.add_owner("projects", enabled=False, registered=False)
        self.append(10)
        destination = self.memory(identity="projects")
        before = b"\xef\xbb\xbf---\r\nsummary: Exact original bytes\r\n---\r\nNo final newline"
        destination.write_bytes(before)
        target = "memory/new (supported) [fact].md"
        link = self.link(target_path=target)
        self.behavior["planning"] = {"edits": [self.edit(target)], "cross_links": [link, link]}
        self.assertEqual(self.run_owner()["status"], "complete")
        after = destination.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(after[len(before):],
                         b"\n- [Memory: planning](<../../planning/memory/new%20%28supported%29%20%5Bfact%5D.md>)\n")
        self.append(20)
        self.behavior["planning"] = {"cross_links": [link, link]}
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(destination.read_bytes(), after)
        self.assertEqual(self.cursor(), 20)

    def test_existing_authored_inline_and_reference_links_are_deduplicated(self):
        self.add_owner("projects")
        self.append(10)
        destination = self.memory(identity="projects")
        before = (NOTE + "\n[Already here](../../planning/memory/index.md#section)\n"
                  "[also]: <../../planning/memory/index.md>\n").encode()
        destination.write_bytes(before)
        self.behavior["planning"] = {"cross_links": [self.link()]}
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(destination.read_bytes(), before)

    def test_stale_cas_existing_and_new_file_never_overwrites(self):
        self.append(10)
        for expected in (None, "0" * 64):
            self.behavior["planning"] = {"edits": [self.edit(expected=expected)]}
            self.assertEqual(self.run_owner(expected=1)["error"], "stale_cas")
            self.assertEqual(self.cursor(), 0)
            self.assertEqual(self.memory().read_text(), NOTE)
        # Another writer changes the base while the model is running.
        self.behavior["planning"] = {"hold": True, "edits": [self.edit()]}
        child = self.start_run()
        self.memory().write_text(NOTE + "Human edit wins\n")
        self.assertEqual(self.finish(child, expected=1)["error"], "stale_cas")
        self.assertEqual(self.memory().read_text(), NOTE + "Human edit wins\n")
        self.assertEqual(self.cursor(), 0)

    def test_source_state_and_database_identity_cas(self):
        self.append(10)
        self.behavior["planning"] = {"hold": True, "edits": [self.edit()]}
        child = self.start_run()
        value = json.loads(self.state().read_text())
        value["active_review"] = {"do_not_clear": "unknown"}
        self.state().write_text(json.dumps(value))
        self.assertEqual(self.finish(child, expected=1)["error"], "source_state_changed")
        self.assertEqual(json.loads(self.state().read_text())["active_review"], {"do_not_clear": "unknown"})
        self.assertEqual(self.memory().read_text(), NOTE)
        value["active_review"] = None
        self.state().write_text(json.dumps(value))
        (self.root / "started-planning").unlink()
        (self.root / "release-planning").unlink()
        child = self.start_run()
        database = Path(value["source"]["database"])
        replacement = self.root / "replacement.sqlite"
        shutil.copyfile(database, replacement)
        os.replace(replacement, database)
        self.assertEqual(self.finish(child, expected=1)["error"], "source_database_changed")
        self.assertEqual(self.cursor(), 0)

    def test_common_commit_lock_serializes_two_foreign_link_appends(self):
        self.add_owner("projects")
        self.add_owner("hyatt", enabled=False, registered=False)
        self.append(10)
        self.append(20, identity="projects")
        self.behavior = {"planning": {"hold": True, "cross_links": [self.link(destination="hyatt")]},
                         "projects": {"hold": True, "cross_links": [self.link(destination="hyatt", target="projects")]}}
        first = self.start_run()
        second = self.start_run("projects")
        before = self.memory(identity="hyatt").read_bytes()
        (self.root / "release-planning").touch()
        (self.root / "release-projects").touch()
        self.finish(first)
        self.finish(second, "projects")
        after = self.memory(identity="hyatt").read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(after.count(b"[Memory: planning]"), 1)
        self.assertEqual(after.count(b"[Memory: projects]"), 1)
        self.assertEqual(self.cursor(), 10)
        self.assertEqual(self.cursor("projects"), 20)

    def two_sources_with_edits(self):
        second = self.make_source(Path(self.agent()["workspace"]) / "state" / "other.json", self.root / "other.sqlite")
        self.agent()["state_files"].append(str(second))
        self.append(10)
        self.append(20, index=1)
        self.behavior["planning"] = {"edits": [self.edit(), self.edit("memory/new.md")]}
        self.save_config()
        return second

    def test_partial_commit_failure_rolls_back_notes_foreign_links_and_all_checkpoints_then_retries(self):
        self.add_owner("projects")
        second = self.two_sources_with_edits()
        self.behavior["planning"]["cross_links"] = [self.link()]
        self.save_config()
        config = review.Config(self.config_file)
        first_before, second_before = self.state().read_bytes(), second.read_bytes()
        foreign_before = self.memory(identity="projects").read_bytes()
        original_replace = review.atomic_replace
        failed = False
        def fail_second_state(path, data, mode):
            nonlocal failed
            if path == second and not failed:
                failed = True
                original_replace(path, data, mode)
                raise OSError("PRIVATE injected fsync error after replacement")
            return original_replace(path, data, mode)
        with mock.patch.object(review, "atomic_replace", side_effect=fail_second_state):
            result = review.run_owner(config, "planning")
        self.assertEqual(result["error"], "commit_failed")
        self.assertTrue(failed)
        self.assertEqual(self.state().read_bytes(), first_before)
        self.assertEqual(second.read_bytes(), second_before)
        self.assertEqual(self.memory().read_text(), NOTE)
        self.assertEqual(self.memory(identity="projects").read_bytes(), foreign_before)
        self.assertFalse(self.memory("new.md").exists())
        journals = list((config.runtime / "transactions").glob("*/journal.json"))
        self.assertEqual(json.loads(journals[0].read_text())["phase"], "rolled_back")
        self.assertTrue(any(journals[0].parent.glob("*.before")))
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual(self.cursor(), 10)
        self.assertEqual(self.cursor(index=1), 20)

    def test_crash_during_checkpoint_is_recovered_before_next_review(self):
        second = self.two_sources_with_edits()
        config = review.Config(self.config_file)
        original_replace = review.atomic_replace
        class SimulatedCrash(BaseException):
            pass
        def crash(path, data, mode):
            if path == second:
                raise SimulatedCrash()
            return original_replace(path, data, mode)
        with mock.patch.object(review, "atomic_replace", side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                review.run_owner(config, "planning")
        # A crash can leave a visible first cursor, but the durable prepared journal
        # bars every new review until ALL preimages have been restored.
        self.assertEqual(self.cursor(), 10)
        self.assertEqual(self.cursor(index=1), 0)
        self.assertEqual(self.invoke("check", expected=1)["agents"][0]["error"], "recovery_required")
        self.assertEqual(self.run_owner(expected=1)["error"], "recovery_required")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.run_owner(recover=True)["status"], "recovered")
        self.assertEqual(len(self.calls()), 1, "Recovery must not launch a model")
        self.assertEqual(self.cursor(), 0)
        self.assertEqual(self.cursor(index=1), 0)
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual([source["after_entry"] for source in self.calls()[-1]["job"]["sources"]], [0, 0])
        self.assertEqual(self.cursor(), 10)
        self.assertEqual(self.cursor(index=1), 20)
        phases = [json.loads(path.read_text())["phase"] for path in (config.runtime / "transactions").glob("*/journal.json")]
        self.assertEqual(sorted(phases), ["committed", "rolled_back"])

    def test_failed_commit_marker_cannot_hide_interrupted_rollback(self):
        second = self.two_sources_with_edits()
        config = review.Config(self.config_file)
        original_replace = review.atomic_replace
        old_second = second.read_bytes()
        class SimulatedCrash(BaseException):
            pass
        def fail_marker_then_crash_rollback(path, data, mode):
            if path.name == "journal.json" and json.loads(data)["phase"] == "committed":
                original_replace(path, data, mode)
                raise OSError("PRIVATE failed directory fsync")
            if path == second and data == old_second:
                raise SimulatedCrash()
            return original_replace(path, data, mode)
        with mock.patch.object(review, "atomic_replace", side_effect=fail_marker_then_crash_rollback):
            with self.assertRaises(SimulatedCrash):
                review.run_owner(config, "planning")
        journal = next((config.runtime / "transactions").glob("*/journal.json"))
        self.assertEqual(json.loads(journal.read_text())["phase"], "prepared")
        self.assertEqual(self.invoke("check", expected=1)["agents"][0]["error"], "recovery_required")
        self.assertEqual(self.run_owner(expected=1)["error"], "recovery_required")
        self.assertEqual(self.run_owner(recover=True)["status"], "recovered")
        self.assertEqual(self.run_owner()["status"], "complete")
        self.assertEqual([source["after_entry"] for source in self.calls()[-1]["job"]["sources"]], [0, 0])

    def test_recovery_never_clobbers_an_external_edit_or_unknown_active_review(self):
        second = self.two_sources_with_edits()
        config = review.Config(self.config_file)
        original_replace = review.atomic_replace
        class SimulatedCrash(BaseException):
            pass
        def crash(path, data, mode):
            if path == second:
                raise SimulatedCrash()
            return original_replace(path, data, mode)
        with mock.patch.object(review, "atomic_replace", side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                review.run_owner(config, "planning")
        value = json.loads(second.read_text())
        value["active_review"] = {"new_external_job": "never-clear"}
        second.write_text(json.dumps(value))
        original = second.read_bytes()
        self.assertEqual(self.run_owner(expected=1)["error"], "recovery_required")
        self.assertEqual(self.run_owner(recover=True, expected=1)["error"], "recovery_conflict")
        self.assertEqual(second.read_bytes(), original)
        self.assertEqual(len(self.calls()), 1)

    def test_common_lock_waits_only_for_commit_not_worker(self):
        self.append(10)
        self.behavior["planning"] = {"hold": True}
        config = review.Config(self.config_file)
        config.runtime_valid(create=True)
        child = self.start_run()
        # Owner worker is alive, but the short common lock is available.
        with review.lock(config.runtime / "commit.lock", create=True):
            self.assertEqual(len(self.calls()), 1)
        self.finish(child)

    def test_config_validation_and_unknown_owner_fail_closed(self):
        original = copy.deepcopy(self.config)
        cases = []
        for field, value in (("version", True), ("version", 2), ("interval_seconds", 60),
                             ("enabled", "true"), ("worker_command", ["relative-node"]),
                             ("history_helper", "relative.py")):
            bad = copy.deepcopy(original)
            bad[field] = value
            cases.append(bad)
        bad = copy.deepcopy(original)
        bad["agents"].append(copy.deepcopy(bad["agents"][0]))
        cases.append(bad)
        for bad in cases:
            self.config = bad
            result = self.invoke("doctor", expected=2)
            self.assertEqual(result["status"], "blocked")
        self.config = original
        self.assertEqual(self.invoke("run", "--agent", "unknown", expected=2)["error"], "unregistered_agent")
        self.assertEqual(self.calls(), [])

    def test_runtime_in_memory_symlink_config_and_overlapping_ownership_rejected(self):
        self.append(10)
        self.add_owner("projects")
        self.agent("projects")["workspace"] = self.agent()["workspace"]
        self.assertEqual(self.invoke("doctor", expected=2)["error"], "overlapping_ownership")
        self.config["agents"].pop()
        # A symlinked allowlist can never establish write authority.
        self.memory("alias").symlink_to(self.memory(), target_is_directory=False)
        self.agent()["owned_paths"].append("memory/alias")
        self.assertEqual(self.invoke("doctor", expected=2)["error"], "symlink_path")
        self.agent()["owned_paths"].pop()
        # A config installed in memory would put backups there: explicitly refuse.
        self.config_file = self.memory("config.json")
        self.assertEqual(self.invoke("doctor", expected=2)["error"], "runtime_in_memory")


if __name__ == "__main__":
    unittest.main()
