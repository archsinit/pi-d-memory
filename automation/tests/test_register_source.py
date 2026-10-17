"""Offline source registration: never discover a store or choose a cursor implicitly."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "register_source.py"
_spec = importlib.util.spec_from_file_location("registration_controller", MODULE.with_name("memory_review.py"))
controller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(controller)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "owner"
        self.workspace.mkdir()
        self.database = self.root / "verified.sqlite"
        self.database.write_bytes(b"test fixture, not a live database")
        self.helper = self.root / "reader.py"
        self.helper.write_text('''import argparse, json
p=argparse.ArgumentParser();p.add_argument('command');p.add_argument('--db');p.add_argument('--conversation',type=int);p.add_argument('--after-entry',type=int,default=0);p.add_argument('--through-entry',type=int,default=0);p.add_argument('--limit');p.add_argument('--max-bytes');a=p.parse_args()
r={'format':'pi-d-history-v1','database':a.db,'conversation_id':a.conversation,'schema_version':1,'through_entry':9}
if a.command=='read':r.update(after_entry=a.after_entry,through_entry=a.through_entry,processed_through_entry=a.through_entry,next_after_entry=a.through_entry,has_more=False,messages=[])
print(json.dumps(r))
''', encoding="utf-8")
        self.config = self.root / "agents.json"
        self.policy = self.root / "policy.md"
        self.policy.write_text("Synthetic fixture policy.\n")
        (self.workspace / "memory").mkdir()
        (self.workspace / "memory/index.md").write_text("# Synthetic fixture index\n")
        self.config.write_text(json.dumps({"version": 1, "enabled": False, "interval_seconds": 900,
            "history_helper": str(self.helper), "policy_file": str(self.policy),
            "worker_command": [sys.executable], "agents": [{"id": "projects", "enabled": False,
            "workspace": str(self.workspace), "state_files": [], "owned_paths": ["memory"],
            "indexes": ["memory/index.md"], "require_metadata": False}]}), encoding="utf-8")
        self.state = self.workspace / "state" / "memory-review.json"

    def call(self, *extra):
        return subprocess.run([sys.executable, "-B", str(MODULE), "--config", str(self.config),
            "--agent", "projects", "--workspace", str(self.workspace), "--database", str(self.database),
            "--conversation", "1", "--state-file", str(self.state), *extra],
            capture_output=True, text=True)

    def test_registration_respects_controller_owner_lifetime_lock(self):
        config = controller.Config(self.config)
        config.runtime_valid(create=True)
        before = self.config.read_bytes()
        with controller.lock(config.lock_path("projects"), create=True):
            result = self.call("--replay-from-start")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_registration_respects_common_commit_lock(self):
        config = controller.Config(self.config)
        config.runtime_valid(create=True)
        before = self.config.read_bytes()
        with controller.lock(config.runtime / "commit.lock", create=True):
            result = self.call("--replay-from-start")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_registration_refuses_pending_recovery(self):
        config = controller.Config(self.config)
        config.runtime_valid(create=True)
        transaction = config.runtime / "transactions" / ("1" * 32)
        transaction.mkdir(parents=True)
        (transaction / "journal.json").write_text(json.dumps(
            {"version": 1, "owner": "projects", "phase": "prepared"}))
        before = self.config.read_bytes()
        result = self.call("--replay-from-start")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("recovery", result.stdout)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_requires_explicit_checkpoint_or_replay(self):
        before = self.config.read_bytes()
        self.assertNotEqual(self.call().returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_explicit_replay_registers_without_enabling(self):
        result = self.call("--replay-from-start")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads(self.state.read_text())
        config = json.loads(self.config.read_text())
        self.assertEqual(state["source"], {"database": str(self.database), "conversation_id": 1})
        self.assertEqual(state["last_saved_entry_id"], 0)
        self.assertIsNone(state["active_review"])
        self.assertEqual(config["agents"][0]["state_files"], [str(self.state)])
        self.assertFalse(config["enabled"])
        self.assertFalse(config["agents"][0]["enabled"])

    def use_real_nonempty_source(self):
        self.database.unlink()
        schema = MODULE.parents[1] / "helpers" / "tests" / "native_schema_v1.sql"
        with closing(sqlite3.connect(self.database)) as writer, writer:
            writer.executescript(schema.read_text())
            writer.execute("INSERT INTO record_ids VALUES (1, 'conversation')")
            writer.execute("INSERT INTO conversations VALUES (1, NULL, NULL, ?)", (json.dumps({"id": 1}),))
            record = {"id": 10, "conversationId": 1, "kind": "pi.user", "model": [
                {"role": "user", "content": "private fixture text", "timestamp": 1}]}
            writer.execute("INSERT INTO record_ids VALUES (10, 'entry')")
            writer.execute("INSERT INTO entries VALUES (10, 1, NULL, 1, ?)", (json.dumps(record),))
            writer.execute("UPDATE durable_metadata SET next_id='11', next_seq=2")
        config = json.loads(self.config.read_text())
        config["history_helper"] = str(MODULE.parents[1] / "helpers" / "read_conversation.py")
        self.config.write_text(json.dumps(config))

    def test_replay_zero_uses_the_real_reader_on_a_nonempty_source(self):
        self.use_real_nonempty_source()
        result = self.call("--replay-from-start")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["last_saved_entry_id"], 0)
        self.assertNotIn("private fixture text", result.stdout + result.stderr)

    def test_positive_checkpoint_uses_real_native_membership(self):
        self.use_real_nonempty_source()
        result = self.call("--after-entry", "10")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["last_saved_entry_id"], 10)

    def test_duplicate_source_cannot_get_a_second_cursor(self):
        other = self.root / "other-state.json"
        other.write_text(json.dumps({"source": {"database": str(self.database), "conversation_id": 1}}))
        config = json.loads(self.config.read_text())
        config["agents"].append({"id": "hyatt", "enabled": False, "state_files": [str(other)]})
        self.config.write_text(json.dumps(config))
        result = self.call("--replay-from-start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already registered", result.stdout)
        self.assertFalse(self.state.exists())

    def test_explicit_checkpoint_is_not_initialized_to_latest(self):
        result = self.call("--after-entry", "4")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(self.state.read_text())["last_saved_entry_id"], 4)

    def test_future_checkpoint_is_rejected(self):
        self.assertNotEqual(self.call("--after-entry", "10").returncode, 0)
        self.assertFalse(self.state.exists())

    def test_does_not_overwrite_an_existing_state(self):
        self.state.parent.mkdir()
        self.state.write_text('{"preserve":"existing review state"}\n')
        before = self.state.read_bytes()
        self.assertNotEqual(self.call("--replay-from-start").returncode, 0)
        self.assertEqual(self.state.read_bytes(), before)

    def test_requires_scheduler_and_owner_disabled(self):
        config = json.loads(self.config.read_text())
        config["enabled"] = True
        self.config.write_text(json.dumps(config))
        self.assertNotEqual(self.call("--replay-from-start").returncode, 0)
        self.assertFalse(self.state.exists())

    def test_rejects_mismatched_source_identity(self):
        self.helper.write_text("import json; print(json.dumps({'format':'pi-d-history-v1','database':'/wrong.sqlite','conversation_id':1,'through_entry':9}))")
        self.assertNotEqual(self.call("--replay-from-start").returncode, 0)
        self.assertFalse(self.state.exists())

    def test_rejects_symlinked_state_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "state").symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.call("--replay-from-start").returncode, 0)
        self.assertFalse((outside / "memory-review.json").exists())


if __name__ == "__main__":
    unittest.main()
