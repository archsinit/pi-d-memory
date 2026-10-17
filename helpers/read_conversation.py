#!/usr/bin/env python3
"""Read retained pi-durable 1.2.0 / SQLite schema-1 conversation text. No host imports."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import sys


MAX_ID = 2**53 - 1
FORMAT = "pi-d-history-v1"
COMPLETION_KIND = "pi-d.subagent-completion"
# name, declared type, NOT NULL, primary-key position; native STRICT tables.
COLUMNS = {
    "durable_schema": [("singleton", "INTEGER", 0, 1), ("version", "INTEGER", 1, 0)],
    "durable_metadata": [("singleton", "INTEGER", 0, 1), ("next_id", "TEXT", 1, 0), ("next_seq", "INTEGER", 1, 0)],
    "record_ids": [("id", "INTEGER", 0, 1), ("record_type", "TEXT", 1, 0)],
    "conversations": [("id", "INTEGER", 0, 1), ("owner_conversation_id", "INTEGER", 0, 0),
                      ("owner_task_id", "INTEGER", 0, 0), ("record", "TEXT", 1, 0)],
    "entries": [("id", "INTEGER", 0, 1), ("conversation_id", "INTEGER", 1, 0),
                ("head", "INTEGER", 0, 0), ("commit_seq", "INTEGER", 1, 0), ("record", "TEXT", 1, 0)],
    "tasks": [("id", "INTEGER", 0, 1), ("conversation_id", "INTEGER", 1, 0), ("kind", "TEXT", 1, 0),
              ("status", "TEXT", 1, 0), ("abort_requested", "INTEGER", 1, 0),
              ("background", "INTEGER", 1, 0), ("record", "TEXT", 1, 0)],
    "submissions": [("id", "INTEGER", 0, 1), ("conversation_id", "INTEGER", 1, 0),
                    ("request_id", "TEXT", 0, 0), ("status", "TEXT", 1, 0), ("record", "TEXT", 1, 0)],
    "documents": [("id", "INTEGER", 0, 1), ("kind", "TEXT", 1, 0), ("family", "INTEGER", 1, 0),
                  ("key_value", "TEXT", 1, 0), ("scope_kind", "TEXT", 1, 0), ("owner_id", "INTEGER", 1, 0),
                  ("created_at", "INTEGER", 1, 0), ("retired_at", "INTEGER", 0, 0), ("record", "TEXT", 1, 0)],
    "document_revisions": [("document_id", "INTEGER", 1, 1), ("seq", "INTEGER", 1, 2),
                           ("kind", "TEXT", 1, 0), ("version", "INTEGER", 1, 0), ("content", "TEXT", 1, 0)],
}
ENTRY_KEYS = {"id", "conversationId", "kind", "model", "data", "head", "edits", "byTaskId"}
ROLES = {"pi.user": "user", "pi.assistant": "assistant", "pi.system": "system",
         "pi.tool-result": "toolResult", "pi.compaction": "user", "pi.reset": "user",
         COMPLETION_KIND: "user"}
PAGE_SQL = """SELECT e.*, r.record_type FROM entries AS e
    LEFT JOIN record_ids AS r ON r.id = e.id
    WHERE e.conversation_id = ? AND e.id > ? AND e.id <= ?
    ORDER BY e.id ASC LIMIT ?"""
# A reset/handoff or application input is an input boundary, but a compaction summary is not.
# Index bounds restrict this lookback to the selected conversation before the checkpoint.
STATE_SQL = """SELECT e.*, r.record_type FROM entries AS e
    LEFT JOIN record_ids AS r ON r.id = e.id
    WHERE e.conversation_id = ? AND e.id <= ? AND (
        json_extract(e.record, '$.kind') = 'pi.reset' OR (
            json_extract(e.record, '$.model[0].role') = 'user'
            AND json_extract(e.record, '$.kind') != 'pi.compaction'))
    ORDER BY e.id DESC LIMIT 1"""


class HistoryError(Exception):
    """Safe diagnostic: never include serialized records or their text."""


def valid_id(value, minimum=1):
    return type(value) is int and minimum <= value <= MAX_ID


def _authorize(action, arg1, arg2, _database, _source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and arg2 == "json_extract":
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_TRANSACTION and arg1 in ("BEGIN", "ROLLBACK"):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        allowed = ((arg1 == "table_list" and arg2 is None)
                   or (arg1 == "table_xinfo" and arg2 in COLUMNS)
                   or (arg1 == "index_list" and arg2 == "entries")
                   or (arg1 == "index_xinfo" and arg2 == "entries_by_conversation"))
        if allowed:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@contextmanager
def open_readonly(database):
    """Normal SQLite locking/WAL visibility, mode=ro, one read transaction; no recovery."""
    path = Path(database)
    if not path.is_absolute():
        raise HistoryError("--db must be an explicit absolute filesystem path")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise HistoryError("--db must name an existing regular file")
    with path.open("rb") as source:
        header = source.read(20)
    if len(header) != 20 or header[:16] != b"SQLite format 3\x00":
        raise HistoryError("Unsupported database format (expected native SQLite)")
    # mode=ro SQLite can create missing WAL sidecars. Refuse that case rather than
    # using immutable/nolock (which would be unsafe/stale on a changing live store).
    if header[18:20] == b"\x02\x02":
        if not all(Path(str(path) + suffix).is_file() for suffix in ("-wal", "-shm")):
            raise HistoryError("WAL sidecars must already exist; refusing to create sidecar files")
    elif header[18:20] != b"\x01\x01":
        raise HistoryError("Unsupported SQLite journaling format")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                 isolation_level=None, timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.set_authorizer(_authorize)
    try:
        connection.execute("BEGIN")
        yield connection
    finally:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()


def validate_schema(connection):
    tables = {row["name"]: row for row in connection.execute("PRAGMA table_list")
              if row["schema"] == "main" and not row["name"].startswith("sqlite_")}
    if set(tables) != set(COLUMNS):
        raise HistoryError("Unsupported native schema: table set differs from schema 1")
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE type = 'trigger' LIMIT 1").fetchone():
        raise HistoryError("Unsupported native schema: unexpected triggers")
    for name, expected in COLUMNS.items():
        table = tables[name]
        if table["type"] != "table" or table["strict"] != 1 or table["wr"] != 0:
            raise HistoryError("Unsupported native schema: expected STRICT rowid tables")
        actual = connection.execute('PRAGMA table_xinfo("%s")' % name).fetchall()
        columns = [(row["name"], row["type"], row["notnull"], row["pk"]) for row in actual]
        if columns != expected or any(row["hidden"] or row["dflt_value"] is not None for row in actual):
            raise HistoryError("Unsupported native schema: column layout differs from schema 1")
    version = connection.execute("SELECT singleton, version FROM durable_schema LIMIT 2").fetchall()
    if [tuple(row) for row in version] != [(1, 1)]:
        raise HistoryError("Unsupported native schema version (only version 1 is supported)")
    index = [row for row in connection.execute("PRAGMA index_list(entries)")
             if row["name"] == "entries_by_conversation"]
    if len(index) != 1 or index[0]["unique"] or index[0]["partial"] or index[0]["origin"] != "c":
        raise HistoryError("Unsupported native schema: missing conversation-range index")
    keys = [(row["name"], row["desc"], row["coll"]) for row in
            connection.execute("PRAGMA index_xinfo(entries_by_conversation)") if row["key"]]
    if keys != [("conversation_id", 0, "BINARY"), ("id", 1, "BINARY")]:
        raise HistoryError("Unsupported native schema: unexpected conversation-range index")
    metadata = connection.execute("SELECT * FROM durable_metadata LIMIT 2").fetchall()
    if len(metadata) != 1 or metadata[0]["singleton"] != 1 or not valid_id(metadata[0]["next_seq"]):
        raise HistoryError("Invalid native commit metadata")
    next_id = metadata[0]["next_id"]
    if not re.fullmatch(r"[0-9]+", next_id) or not 2 <= int(next_id) <= MAX_ID + 1:
        raise HistoryError("Invalid native allocation metadata")
    return metadata[0]["next_seq"] - 1


def _object(record):
    try:
        value = json.loads(record)
    except (ValueError, TypeError, RecursionError):
        raise HistoryError("Unsupported native JSON serialization") from None
    if not isinstance(value, dict):
        raise HistoryError("Unsupported native record serialization")
    return value


def validate_conversation(connection, identity):
    row = connection.execute("""SELECT c.*, r.record_type FROM conversations AS c
        LEFT JOIN record_ids AS r ON r.id = c.id WHERE c.id = ?""", (identity,)).fetchone()
    if row is None:
        raise HistoryError("Unknown conversation identifier")
    record = _object(row["record"])
    if not valid_id(record.get("id")) or record["id"] != identity or row["record_type"] != "conversation":
        raise HistoryError("Invalid conversation identity")
    if "parent" in record:
        raise HistoryError("Forked conversations are unsupported; inherited visibility must not be guessed")
    if "owner" in record or row["owner_conversation_id"] is not None or row["owner_task_id"] is not None:
        raise HistoryError("Task-owned/worker conversations are unsupported; select the ownerless parent")
    if set(record) != {"id"}:
        raise HistoryError("Unsupported conversation record format")


def _content(message):
    """Validate native block types; extract ONLY immediate text blocks, never nested values."""
    role, content = message.get("role"), message.get("content")
    if isinstance(content, str) and role in ("user", "system"):
        return [content]
    allowed = {"user": {"text", "image"}, "assistant": {"text", "thinking", "toolCall"},
               "system": {"text"}, "toolResult": {"text", "image"}}
    if role not in allowed or not isinstance(content, list):
        raise HistoryError("Unsupported native message content")
    blocks = []
    for block in content:
        if (not isinstance(block, dict) or not isinstance(block.get("type"), str)
                or block["type"] not in allowed[role]):
            raise HistoryError("Unsupported native content block")
        if block["type"] == "text":
            if not isinstance(block.get("text"), str):
                raise HistoryError("Invalid native text block")
            blocks.append(block["text"])
    return blocks


def decode_entry(row, conversation, commit_seq):
    record = _object(row["record"])
    if (set(record) - ENTRY_KEYS or not valid_id(record.get("id"))
            or record["id"] != row["id"] or record.get("conversationId") != conversation
            or not valid_id(record.get("conversationId")) or row["conversation_id"] != conversation
            or row["record_type"] != "entry" or not valid_id(row["commit_seq"])
            or row["commit_seq"] > commit_seq):
        raise HistoryError("Invalid or unsupported entry identity/format")
    if record.get("head") != row["head"] or ("head" in record and not valid_id(record["head"])):
        raise HistoryError("Invalid native entry head")
    if "byTaskId" in record and not valid_id(record["byTaskId"]):
        raise HistoryError("Invalid native task attribution")
    if "edits" in record and not isinstance(record["edits"], list):
        raise HistoryError("Unsupported native context edits")
    kind = record.get("kind")
    if not isinstance(kind, str) or not kind:
        raise HistoryError("Invalid native entry kind")
    model = record.get("model")
    if "model" not in record and (kind == "pi.reset" or kind not in ROLES):
        return record, []  # Reset or application display/bookkeeping, not a message.
    if kind not in ROLES:
        if model == []:
            return record, []
        raise HistoryError("Unsupported application entry with model messages")
    if (not isinstance(model, list) or len(model) != 1 or not isinstance(model[0], dict)
            or model[0].get("role") != ROLES[kind]):
        raise HistoryError("Unsupported native entry/message serialization")
    return record, _content(model[0])


def _checkpoint(connection, identity, conversation, commit_seq):
    row = connection.execute("""SELECT e.*, r.record_type FROM entries AS e
        LEFT JOIN record_ids AS r ON r.id = e.id WHERE e.id = ? AND e.conversation_id = ?""",
                             (identity, conversation)).fetchone()
    if row is None:
        raise HistoryError("Checkpoint/boundary is not an entry in the selected conversation")
    decode_entry(row, conversation, commit_seq)


def _encode(value):
    # ASCII JSON also preserves native lone UTF-16 surrogates without encoding errors.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def read_page(connection, common, after, through, limit, max_bytes):
    conversation, commit_seq = common["conversation_id"], common["observed_commit_seq"]
    if after > through:
        raise HistoryError("--after-entry must not exceed --through-entry")
    for checkpoint in {after, through} - {0}:
        _checkpoint(connection, checkpoint, conversation, commit_seq)
    if through == 0 and connection.execute(
            "SELECT id FROM entries WHERE conversation_id = ? LIMIT 1", (conversation,)).fetchone():
        raise HistoryError("A zero snapshot boundary is valid only for an empty conversation")
    rows = connection.execute(PAGE_SQL, (conversation, after, through, limit + 1)).fetchall()
    allow_assistant = False
    if rows and after:
        previous = connection.execute(STATE_SQL, (conversation, after)).fetchone()
        if previous is not None:
            record, _ = decode_entry(previous, conversation, commit_seq)
            allow_assistant = record["kind"] == "pi.user"
    page = {**common, "through_entry": through, "after_entry": after,
            "processed_from_entry": None, "processed_through_entry": after,
            "processed_entries": 0, "next_after_entry": after, "has_more": bool(rows),
            "last_user_entry": None, "messages": []}
    for row in rows[:limit]:
        record, blocks = decode_entry(row, conversation, commit_seq)
        kind = record["kind"]
        if kind == "pi.user":
            allow_assistant = True
        elif kind in (COMPLETION_KIND, "pi.reset"):
            allow_assistant = False
        messages = page["messages"]
        if blocks and (kind == "pi.user" or (kind == "pi.assistant" and allow_assistant)):
            messages = messages + [{"entry_id": row["id"], "role": ROLES[kind], "text_blocks": blocks}]
        count = page["processed_entries"] + 1
        candidate = {**page, "messages": messages, "processed_entries": count,
                     "processed_from_entry": page["processed_from_entry"] or row["id"],
                     "processed_through_entry": row["id"], "next_after_entry": row["id"],
                     "has_more": count < len(rows),
                     "last_user_entry": row["id"] if kind == "pi.user" else page["last_user_entry"]}
        if len(_encode(candidate)) + 1 > max_bytes:
            if not page["processed_entries"]:
                raise HistoryError("Entry exceeds --max-bytes; increase budget only if the worker can receive it whole")
            break  # Return a complete prefix; the oversized entry has NOT been processed.
        page = candidate
    if len(_encode(page)) + 1 > max_bytes:
        raise HistoryError("Page metadata exceeds --max-bytes")
    return page


def _number(value):
    if not re.fullmatch(r"[0-9]+", value) or not valid_id(int(value), 0):
        raise argparse.ArgumentTypeError("expected an integer between 0 and 9007199254740991")
    return int(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("snapshot", "read"):
        child = commands.add_parser(command, help="metadata only" if command == "snapshot" else "one bounded text page")
        child.add_argument("--db", required=True, help="existing absolute native SQLite path; never discovered")
        child.add_argument("--conversation", required=True, type=_number, help="explicit ownerless, nonfork conversation ID")
        if command == "read":
            child.add_argument("--after-entry", required=True, type=_number, help="exclusive; 0 explicitly starts at the beginning")
            child.add_argument("--through-entry", required=True, type=_number, help="inclusive; retain the same boundary across pages")
            child.add_argument("--limit", type=_number, default=50, help="raw entries per page, 1..500 (including hidden entries)")
            child.add_argument("--max-bytes", type=_number, default=32000, help="whole JSON page budget, 2048..1000000; never truncates text")
    args = parser.parse_args(argv)
    try:
        if not valid_id(args.conversation):
            raise HistoryError("--conversation must be a positive safe integer")
        if args.command == "read" and (not 1 <= args.limit <= 500 or not 2048 <= args.max_bytes <= 1_000_000):
            raise HistoryError("Require --limit 1..500 and --max-bytes 2048..1000000")
        with open_readonly(args.db) as connection:
            commit_seq = validate_schema(connection)
            validate_conversation(connection, args.conversation)
            common = {"format": FORMAT, "database": str(Path(args.db).resolve()),
                      "conversation_id": args.conversation, "schema_version": 1,
                      "observed_commit_seq": commit_seq}
            if args.command == "snapshot":
                first = connection.execute("SELECT id FROM entries WHERE conversation_id = ? ORDER BY id ASC LIMIT 1",
                                           (args.conversation,)).fetchone()
                last = connection.execute("SELECT id FROM entries WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                                          (args.conversation,)).fetchone()
                result = {**common, "first_entry": first["id"] if first else None,
                          "through_entry": last["id"] if last else 0}
            else:
                result = read_page(connection, common, args.after_entry, args.through_entry, args.limit, args.max_bytes)
        # Only emit after the entire page validates and the connection closes successfully.
        print(_encode(result))
        return 0
    except HistoryError as error:
        print("error: " + str(error), file=sys.stderr)
    except sqlite3.Error:
        print("error: SQLite read failed (locked, unavailable, corrupt, or unsupported); no recovery attempted", file=sys.stderr)
    except (OSError, ValueError, RecursionError):
        print("error: Database path or native serialization is unavailable/unsupported", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
