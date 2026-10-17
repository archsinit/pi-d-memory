"""Offline fixtures only; never discover/open a real pi-d session."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "read_conversation.py"
SCHEMA = Path(__file__).with_name("native_schema_v1.sql").read_text()


def assistant(*blocks):
    return {
        "role": "assistant", "content": list(blocks), "api": "fixture",
        "provider": "fixture", "model": "fixture", "timestamp": 1,
        "stopReason": "stop", "usage": {
            "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
            "totalTokens": 0, "cost": {
                "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0,
            },
        },
    }


def text(value):
    return {"type": "text", "text": value}


def user(value):
    return {"role": "user", "content": value, "timestamp": 1}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.sqlite"
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.executescript(SCHEMA)
        self.conversation(1)
        self.conversation(2)
        self.writer.commit()

    def conversation(self, identity, **extra):
        record = {"id": identity, **extra}
        owner = extra.get("owner", {})
        self.writer.execute("INSERT INTO record_ids VALUES (?, 'conversation')", (identity,))
        self.writer.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", (
            identity, owner.get("conversationId"), owner.get("taskId"), json.dumps(record),
        ))

    def entry(self, identity, kind, message=None, conversation=1, **extra):
        sequence = self.writer.execute("SELECT next_seq FROM durable_metadata").fetchone()[0]
        record = {"id": identity, "conversationId": conversation, "kind": kind, **extra}
        if message is not None:
            record["model"] = [message]
        self.writer.execute("INSERT INTO record_ids VALUES (?, 'entry')", (identity,))
        self.writer.execute("INSERT INTO entries VALUES (?, ?, ?, ?, ?)", (
            identity, conversation, extra.get("head"), sequence, json.dumps(record),
        ))
        self.writer.execute("UPDATE durable_metadata SET next_seq = ?, next_id = ?", (
            sequence + 1, str(max(identity + 1, int(self.writer.execute(
                "SELECT next_id FROM durable_metadata").fetchone()[0]))),
        ))
        self.writer.commit()

    def run_cli(self, command="read", *, conversation=1, after=0, through=None, limit=50,
                max_bytes=None, path=None, more=()):
        arguments = [sys.executable, "-B", str(HELPER), command, "--db", str(path or self.path),
                     "--conversation", str(conversation)]
        if command == "read":
            arguments += ["--after-entry", str(after), "--through-entry", str(through),
                          "--limit", str(limit)]
            if max_bytes is not None:
                arguments += ["--max-bytes", str(max_bytes)]
        return subprocess.run(arguments + list(more), text=True, capture_output=True, timeout=10)

    def ok(self, *args, **kwargs):
        result = self.run_cli(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def bad(self, *args, **kwargs):
        result = self.run_cli(*args, **kwargs)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "Failures must not emit partial transcripts")
        self.assertNotIn("PRIVATE", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stderr

    def load_helper(self):
        spec = importlib.util.spec_from_file_location("local_history_reader", HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_snapshot_has_only_identifiers_not_transcript(self):
        self.entry(10, "pi.user", user("PRIVATE user text"))
        self.entry(20, "pi.assistant", assistant(text("PRIVATE answer")))
        self.entry(30, "pi.user", user("PRIVATE unrelated"), conversation=2)
        result = self.ok("snapshot")
        self.assertEqual(result["conversation_id"], 1)
        self.assertEqual(result["through_entry"], 20)
        self.assertEqual(result["first_entry"], 10)
        self.assertEqual(result["observed_commit_seq"], 3)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(self.ok("snapshot", conversation=2)["through_entry"], 30)

    def test_normal_turns_preserve_text_blocks_exactly(self):
        self.entry(10, "pi.user", user("  Hello\nworld 👋  "))
        self.entry(20, "pi.assistant", assistant(text("first"), text("\nsecond")))
        result = self.ok(through=20)
        self.assertEqual(result["messages"], [
            {"entry_id": 10, "role": "user", "text_blocks": ["  Hello\nworld 👋  "]},
            {"entry_id": 20, "role": "assistant", "text_blocks": ["first", "\nsecond"]},
        ])
        self.assertEqual(result["processed_entries"], 2)
        self.assertEqual(result["processed_from_entry"], 10)
        self.assertEqual(result["processed_through_entry"], 20)
        self.assertEqual(result["next_after_entry"], 20)
        self.assertEqual(result["last_user_entry"], 10)
        self.assertFalse(result["has_more"])

    def test_empty_text_blocks_and_lone_surrogates_are_preserved_as_json(self):
        self.entry(10, "pi.user", user([text(""), text("\ud800")]))
        self.entry(20, "pi.assistant", assistant(text("\udfff")))
        result = self.ok(through=20)
        self.assertEqual(result["messages"][0]["text_blocks"], ["", "\ud800"])
        self.assertEqual(result["messages"][1]["text_blocks"], ["\udfff"])

    def test_order_and_conversation_isolation(self):
        self.entry(10, "pi.user", user("first"))
        self.entry(15, "pi.user", user("PRIVATE foreign"), conversation=2)
        self.conversation(3, owner={"conversationId": 1, "taskId": 99})
        self.entry(17, "pi.user", user("PRIVATE child"), conversation=3)
        self.entry(21, "pi.assistant", assistant(text("last")))
        result = self.ok(through=21)
        self.assertEqual([m["entry_id"] for m in result["messages"]], [10, 21])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertIn("owned", self.bad("snapshot", conversation=3).lower())

    def test_paging_counts_hidden_entries_and_resumes_without_duplicates(self):
        self.entry(10, "pi.user", user("first"))
        self.entry(20, "pi.system", {"role": "system", "content": "PRIVATE", "timestamp": 1})
        self.entry(30, "pi.assistant", assistant({"type": "thinking", "thinking": "PRIVATE"}))
        self.entry(40, "pi.tool-result", {"role": "toolResult", "content": [text("PRIVATE")],
                                          "toolCallId": "x", "toolName": "x", "isError": False,
                                          "timestamp": 1})
        self.entry(50, "pi.assistant", assistant(text("last")))
        after, gathered, ranges = 0, [], []
        for _ in range(10):
            page = self.ok(after=after, through=50, limit=2)
            gathered += page["messages"]
            ranges.append(page["processed_entries"])
            self.assertGreater(page["next_after_entry"], after)
            after = page["next_after_entry"]
            if not page["has_more"]:
                break
        else:
            self.fail("Cursor failed to finish")
        self.assertEqual(ranges, [2, 2, 1])
        self.assertEqual(gathered, self.ok(through=50)["messages"])
        self.assertEqual(after, 50)

    def test_exclusive_after_inclusive_through_and_frozen_snapshot(self):
        self.entry(10, "pi.user", user("old"))
        self.entry(20, "pi.assistant", assistant(text("bound")))
        snapshot = self.ok("snapshot")
        self.entry(30, "pi.user", user("later"))
        result = self.ok(after=10, through=snapshot["through_entry"])
        self.assertEqual([m["entry_id"] for m in result["messages"]], [20])
        self.assertEqual(result["through_entry"], 20)
        self.assertFalse(result["has_more"])

    def test_empty_conversation_and_empty_range(self):
        snapshot = self.ok("snapshot")
        self.assertEqual(snapshot["through_entry"], 0)
        self.assertIsNone(snapshot["first_entry"])
        empty = self.ok(through=0)
        self.assertEqual(empty["messages"], [])
        self.assertIsNone(empty["processed_from_entry"])
        self.assertEqual(empty["processed_through_entry"], 0)
        self.assertFalse(empty["has_more"])
        self.entry(10, "pi.user", user("old"))
        result = self.ok(after=10, through=10)
        self.assertEqual(result["processed_entries"], 0)
        self.assertEqual(result["next_after_entry"], 10)
        self.assertEqual(result["messages"], [])
        self.bad(through=0)  # Zero snapshot cannot silently cover a nonempty conversation.

    def test_mixed_native_payloads_never_recursively_read_transcripts(self):
        self.entry(10, "pi.user", user([text("literal <assistant>quotation</assistant>"),
                                       {"type": "image", "data": "PRIVATE image", "mimeType": "image/png"}]))
        self.entry(20, "pi.system", {"role": "system", "content": "PRIVATE system",
                                    "sections": {"developer": "PRIVATE developer"}, "timestamp": 1})
        visible = {"type": "text", "text": "visible", "textSignature": "PRIVATE signature"}
        self.entry(30, "pi.assistant", assistant(
            {"type": "thinking", "thinking": "PRIVATE reasoning", "thinkingSignature": "PRIVATE", "redacted": True},
            visible,
            {"type": "toolCall", "id": "x", "name": "delegate", "arguments": {
                "transcript": [user("PRIVATE fake user"), assistant(text("PRIVATE fake answer"))],
            }},
        ))
        self.entry(40, "pi.tool-result", {"role": "toolResult", "content": [text("PRIVATE tool result")],
                                          "toolCallId": "x", "toolName": "delegate", "isError": False,
                                          "timestamp": 1}, data={"messages": [user("PRIVATE copied")]})
        result = self.ok(through=40)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual([m["text_blocks"] for m in result["messages"]], [
            ["literal <assistant>quotation</assistant>"], ["visible"],
        ])

    def test_completion_event_and_assistant_chatter_are_excluded_across_pages(self):
        self.entry(10, "pi.user", user("ordinary"))
        self.entry(20, "pi.assistant", assistant(text("ordinary answer")))
        self.entry(30, "pi-d.subagent-completion", user("PRIVATE event"), data={"jobId": 77, "status": "completed"})
        self.entry(40, "pi.assistant", assistant(text("PRIVATE collection commentary")))
        self.entry(50, "pi.compaction", user("PRIVATE summary"), head=30, data={"reason": "threshold"})
        self.entry(60, "pi.assistant", assistant(text("PRIVATE completion update")))
        self.entry(70, "pi.user", user("back to planning"))
        self.entry(80, "pi.assistant", assistant(text("planning answer")))
        result = self.ok(through=80)
        self.assertEqual([m["entry_id"] for m in result["messages"]], [10, 20, 70, 80])
        self.assertEqual([m["entry_id"] for m in self.ok(after=30, through=80)["messages"]], [70, 80])
        self.assertEqual(self.ok(after=40, through=60)["messages"], [])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_retained_history_survives_compaction_reset_and_context_edits(self):
        self.entry(10, "pi.user", user("original before compaction"))
        self.entry(20, "pi.assistant", assistant(text("original answer")))
        self.entry(30, "pi.compaction", user("PRIVATE synthetic summary"), head=20,
                   data={"reason": "threshold"}, edits=[{"target": 10, "action": "omit"}])
        self.entry(40, "pi.reset", user("PRIVATE handoff"), head=40)
        self.entry(50, "pi.assistant", assistant(text("PRIVATE handoff chatter")))
        self.entry(60, "pi.user", user("after reset"))
        self.entry(70, "pi.assistant", assistant(text("new answer")), edits=[
            {"target": 20, "action": "replace", "messages": [user("PRIVATE context replacement")]},
        ])
        result = self.ok(through=70)
        self.assertEqual([m["entry_id"] for m in result["messages"]], [10, 20, 60, 70])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(self.ok(after=40, through=70)["messages"], result["messages"][2:])

    def test_forks_are_explicitly_rejected_instead_of_hiding_inherited_entries(self):
        self.entry(10, "pi.user", user("parent at fork"))
        self.conversation(3, parent={"conversationId": 1, "at": 10})
        self.entry(20, "pi.user", user("PRIVATE fork"), conversation=3)
        self.assertIn("fork", self.bad("snapshot", conversation=3).lower())
        self.assertIn("fork", self.bad(conversation=3, through=20).lower())
        self.assertEqual([m["entry_id"] for m in self.ok(through=10)["messages"]], [10])

    def test_invalid_identifiers_and_page_sizes(self):
        self.entry(10, "pi.user", user("PRIVATE"))
        for conversation in [0, -1, "1.0", "no", 2**53, 999]:
            with self.subTest(conversation=conversation):
                self.bad("snapshot", conversation=conversation)
        for after, through, limit in [(-1, 10, 1), (0, -1, 1), (0, 10, 0),
                                      (0, 10, 501), ("1.1", 10, 1), (0, 2**53, 1)]:
            with self.subTest(after=after, through=through, limit=limit):
                self.bad(after=after, through=through, limit=limit)
        self.bad(through=10, max_bytes=1)

    def test_missing_foreign_or_reversed_checkpoints_fail_closed(self):
        self.entry(10, "pi.user", user("PRIVATE"))
        self.entry(20, "pi.user", user("PRIVATE foreign"), conversation=2)
        self.entry(30, "pi.assistant", assistant(text("PRIVATE")))
        for after, through in [(20, 30), (0, 20), (11, 30), (0, 31), (30, 10), (30, 0)]:
            with self.subTest(after=after, through=through):
                self.bad(after=after, through=through)

    def test_paths_are_explicit_and_missing_db_is_not_created(self):
        missing = Path(self.temp.name) / "missing.sqlite"
        self.bad("snapshot", path=missing)
        self.assertFalse(missing.exists())
        self.bad("snapshot", path="relative.sqlite")
        self.bad("snapshot", path=self.temp.name)
        result = subprocess.run([sys.executable, "-B", str(HELPER), "snapshot"],
                                text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_uri_special_characters_are_escaped_as_filename_not_options(self):
        self.writer.close()
        odd_path = Path(self.temp.name) / "fixture ?mode=rw#%20.sqlite"
        self.path.rename(odd_path)
        result = self.ok("snapshot", path=odd_path)
        self.assertEqual(result["database"], str(odd_path.resolve()))

    def test_schema_version_and_shape_changes_are_rejected(self):
        for statement in ["UPDATE durable_schema SET version = 2",
                          "UPDATE durable_schema SET version = 0",
                          "ALTER TABLE entries ADD COLUMN unrecognized TEXT",
                          "DROP TABLE durable_schema",
                          "DROP INDEX entries_by_conversation",
                          "CREATE VIEW unknown_view AS SELECT 1 AS id",
                          "CREATE TRIGGER unknown_trigger AFTER INSERT ON entries BEGIN SELECT 1; END"]:
            with self.subTest(statement=statement):
                self.writer.execute(statement)
                self.writer.commit()
                self.bad("snapshot")
                # Recreate only our disposable synthetic fixture.
                self.writer.close()
                self.path.unlink()
                self.writer = sqlite3.connect(self.path)
                self.addCleanup(self.writer.close)
                self.writer.executescript(SCHEMA)
                self.conversation(1)
                self.conversation(2)
                self.writer.commit()

    def test_unknown_serialization_and_mismatched_identity_fail_without_leak(self):
        self.entry(10, "pi.user", user("PRIVATE"))
        originals = self.writer.execute("SELECT record FROM entries WHERE id = 10").fetchone()[0]
        variants = [
            {"id": 999, "conversationId": 1, "kind": "pi.user", "model": [user("PRIVATE")]},
            {"id": 10, "conversationId": 2, "kind": "pi.user", "model": [user("PRIVATE")]},
            {"id": 10, "conversationId": 1, "kind": "pi.user", "model": [assistant(text("PRIVATE"))]},
            {"id": 10, "conversationId": 1, "kind": "custom.input", "model": [user("PRIVATE")]},
            {"id": 10, "conversationId": 1, "kind": "pi.user", "model": [user([{"type": "unknown", "text": "PRIVATE"}])]},
            {"id": 10, "conversationId": 1, "kind": "pi.user", "model": [user([{"type": ["text"], "text": "PRIVATE"}])]},
            {"id": 10, "conversationId": 1, "kind": "pi.user", "model": [user("PRIVATE")], "futureField": True},
            [],
        ]
        for record in variants:
            with self.subTest(record_type=type(record).__name__):
                self.writer.execute("UPDATE entries SET record = ? WHERE id = 10", (json.dumps(record),))
                self.writer.commit()
                self.bad(through=10)
        self.writer.execute("UPDATE entries SET record = ? WHERE id = 10", (originals,))
        self.writer.commit()
        self.entry(20, "custom.display", data={"transcript": [user("PRIVATE fake")]})
        self.assertEqual([m["entry_id"] for m in self.ok(through=20)["messages"]], [10])

    def test_unknown_modeled_entry_mid_page_never_emits_a_partial_transcript(self):
        self.entry(10, "pi.user", user("PRIVATE earlier in page"))
        self.entry(20, "custom.input", user("PRIVATE unsupported input"))
        self.entry(30, "pi.assistant", assistant(text("PRIVATE later")))
        self.bad(through=30)
        # Resuming after the unsupported input must not misattribute its assistant reply.
        self.bad(after=20, through=30)

    def test_malformed_conversation_and_index_record_disagreement_are_rejected(self):
        self.entry(10, "pi.user", user("PRIVATE"))
        for record in [{"id": 2}, {"id": 1, "futureField": True}, {"id": 1, "parent": None}]:
            self.writer.execute("UPDATE conversations SET record = ? WHERE id = 1", (json.dumps(record),))
            self.writer.commit()
            self.bad("snapshot")
        self.writer.execute("UPDATE conversations SET record = ? WHERE id = 1", (json.dumps({"id": 1}),))
        self.writer.execute("UPDATE entries SET head = 10 WHERE id = 10")
        self.writer.commit()
        self.bad(through=10)

    def test_read_only_reads_leave_database_bytes_rows_and_directory_unchanged(self):
        self.entry(10, "pi.user", user("fixture text"))
        self.entry(20, "pi.assistant", assistant(text("answer")))
        before_bytes = self.path.read_bytes()
        before_dump = list(self.writer.iterdump())
        before_files = sorted(p.name for p in self.path.parent.iterdir())
        self.ok("snapshot")
        self.ok(through=20, limit=1)
        self.ok(after=10, through=20)
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(list(self.writer.iterdump()), before_dump)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), before_files)

    def test_connection_denies_writes_attach_and_mutating_pragmas(self):
        module = self.load_helper()
        attached = Path(self.temp.name) / "must-not-exist.sqlite"
        with module.open_readonly(self.path) as connection:
            for sql in ["DELETE FROM conversations", "CREATE TABLE dangerous (id)",
                        "PRAGMA journal_mode=DELETE", "PRAGMA user_version=10",
                        "ATTACH DATABASE '%s' AS dangerous" % attached]:
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(sql)
        self.assertFalse(attached.exists())

    def test_wal_committed_history_is_read_without_checkpoint_or_data_writes(self):
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.entry(10, "pi.user", user("in WAL"))
        self.entry(20, "pi.assistant", assistant(text("WAL answer")))
        paths = [self.path, Path(str(self.path) + "-wal")]
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        files = sorted(p.name for p in self.path.parent.iterdir())
        snapshot = self.ok("snapshot")
        self.assertEqual(snapshot["through_entry"], 20)
        self.writer.execute("UPDATE durable_metadata SET next_seq = next_seq + 1")  # Uncommitted fixture writer.
        result = self.ok(through=20)
        self.assertEqual([m["entry_id"] for m in result["messages"]], [10, 20])
        self.assertEqual(result["observed_commit_seq"], snapshot["observed_commit_seq"])
        self.assertEqual({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}, before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), files)
        self.writer.rollback()

    def test_wal_without_existing_sidecars_is_rejected_without_creating_them(self):
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.entry(10, "pi.user", user("PRIVATE"))
        self.assertEqual(self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
        self.writer.close()
        # Some SQLite builds retain empty sidecars after close. This synthetic fixture
        # explicitly models a checkpointed, closed store with no companion files.
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        files = sorted(p.name for p in self.path.parent.iterdir())
        before = self.path.read_bytes()
        self.assertIn("sidecar", self.bad("snapshot").lower())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), files)

    def test_sqlite_lock_is_not_bypassed_or_recovered(self):
        self.entry(10, "pi.user", user("PRIVATE"))
        before = self.path.read_bytes()
        self.writer.execute("BEGIN EXCLUSIVE")
        self.assertIn("SQLite", self.bad("snapshot"))
        self.assertTrue(self.writer.in_transaction)
        self.assertEqual(self.path.read_bytes(), before)
        self.writer.rollback()

    def test_byte_budget_pages_whole_entries_and_fails_closed_on_oversized_entry(self):
        self.entry(10, "pi.user", user("small"))
        self.entry(20, "pi.assistant", assistant(text("x" * 4000)))
        page = self.ok(through=20, max_bytes=2048)
        self.assertEqual([m["entry_id"] for m in page["messages"]], [10])
        self.assertEqual(page["next_after_entry"], 10)
        self.assertTrue(page["has_more"])
        self.assertIn("max-bytes", self.bad(after=10, through=20, max_bytes=2048))
        larger = self.ok(after=10, through=20, max_bytes=8192)
        self.assertEqual(larger["messages"][0]["text_blocks"], ["x" * 4000])

    def test_native_index_is_used_for_page_range_not_all_history(self):
        module = self.load_helper()
        with module.open_readonly(self.path) as connection:
            plan = connection.execute("EXPLAIN QUERY PLAN " + module.PAGE_SQL, (1, 10, 20, 3)).fetchall()
        description = " ".join(str(tuple(row)) for row in plan)
        self.assertIn("entries_by_conversation", description)
        self.assertNotIn("SCAN e", description)


if __name__ == "__main__":
    unittest.main()
