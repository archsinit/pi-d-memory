#!/usr/bin/env python3
"""Isolated, stdlib-only memory-review controller (POSIX, Python 3.9+).

Interface: --config PATH {doctor,check,tick,run --agent ID [--audit|--recover],status}. Config v1
requires interval_seconds=900; this program does NOT install or run a timer.
All output is compact JSON diagnostics, never worker output or history text.

The trusted argv adapter reads one JSON job from stdin and returns one JSON
proposal on stdout. It must fetch every frozen source page with history_helper,
follow policy_file, and have NO direct knowledge/checkpoint-write side effects.
Explicitly authorized shared-store OAuth refresh is a separate runtime policy.
This is a proposal validator, not an OS sandbox for an arbitrary executable. No worker time/tool
budgets are imposed. The helper is invoked with Python -B, limit=50 and its
normal 32000-byte whole-page limit; unsupported/oversized pages fail closed.
Only non-whitespace user/assistant immediate text emitted by the helper triggers
a worker. Hidden-only ranges stay pending (no silent checkpoint advancement).
New entries repeating older text still count as new evidence. Explicit manual
run --audit may review registered owner memory even without eligible new text;
it still freezes all sources and requires full coverage. Tick never audits.

Runtime files live in config.parent/.memory-review-runtime, NEVER in an owned
memory tree. Configurations controlling overlapping stores must share that
parent directory (the lock/recovery namespace). Owner locks are keyed by
canonical workspace, held for the entire run, and inherited by the adapter so a
controller crash does not normally orphan an unlocked adapter. Adapters must
not close that inherited descriptor while working. Tick starts a detached
controller process per pending owner, not a combined model call. Run rechecks
everything under its lock; simultaneous ticks can dispatch a redundant process,
but only one can launch an owner worker.

The common commit flock covers validation, CAS, foreign-link appends and state
replacement, not the model call. Existing directories are never created in
workspaces. Symlink paths and hard-linked writable files are refused. Atomic
same-directory replacements preserve file modes. A durable prepared journal
and pre/post images outside memory allow rollback of incomplete multi-file
commits, INCLUDING partially replaced checkpoints. Prepared journals block all
ordinary runs/checks. After inspection, explicit run --agent ID --recover for
that journal's owner restores verified preimages WITHOUT launching a worker;
a subsequent normal run can retry the old history. Recovery refuses externally
changed files rather than clobbering them; manual resolution is then necessary. Completed/rolled-back
backups are retained without automatic pruning. External writers that ignore
these locks can still race filesystem checks; this is not hostile-filesystem
isolation. A successful committed journal is the transaction boundary.

require_metadata checks a deliberately small frontmatter envelope: nonempty
single-line type/summary and nonempty inline or block-list topics. It is not a
full YAML parser. Foreign links use a fixed label and escaped relative URL;
existing destination bytes are an unchanged prefix, and ordinary inline and
reference Markdown link targets are deduplicated (not a full Markdown parser).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import quote, unquote, urlsplit
import uuid

MAX_ID = 2**53 - 1
PAGE_LIMIT = 50
PAGE_BYTES = 32000
RUNTIME_NAME = ".memory-review-runtime"
ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
RESERVED_DIRS = {"state", "prompts", "instructions", ".git", ".github", RUNTIME_NAME}
RESERVED_NAMES = {"agents.md", "claude.md", "gemini.md", "instructions.md",
                  "copilot-instructions.md", "system.md", "policy.md"}


class ReviewError(Exception):
    """Only fixed diagnostic codes belong here; never interpolate external text."""


def need(condition, code):
    if not condition:
        raise ReviewError(code)


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_ID


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def decode_json(raw, code):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, code)
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ReviewError(code)))
    except (ValueError, UnicodeError, RecursionError):
        raise ReviewError(code) from None


def object_keys(value, keys, code):
    need(isinstance(value, dict) and set(value) == set(keys), code)


def inside(path, root):
    return path == root or root in path.parents


def checked_absolute(value, *, exists=True, directory=False):
    need(isinstance(value, str) and value and "\x00" not in value, "invalid_path")
    path = Path(value)
    need(path.is_absolute() and ".." not in path.parts and "." not in path.parts, "invalid_path")
    # Walk components rather than trusting resolve() to hide a symlink escape.
    for component in reversed((path, *path.parents)):
        need(not component.is_symlink(), "symlink_path")
    if exists:
        need(path.is_dir() if directory else path.is_file(), "missing_path")
    return path


def relative_path(value):
    need(isinstance(value, str) and value and "\\" not in value and "\x00" not in value,
         "invalid_relative_path")
    parts = value.split("/")
    need(all(part not in ("", ".", "..") for part in parts)
         and not PurePosixPath(value).is_absolute()
         and not any(ord(char) < 32 or ord(char) == 127 for char in value), "invalid_relative_path")
    return Path(*parts)


def regular_bytes(path):
    checked_absolute(str(path))
    need(path.stat().st_nlink == 1, "hardlinked_file")
    return path.read_bytes()


class Config:
    def __init__(self, filename):
        self.path = checked_absolute(str(Path(filename).absolute()))
        data = decode_json(self.path.read_bytes(), "invalid_config")
        object_keys(data, {"version", "interval_seconds", "enabled", "history_helper", "policy_file",
                           "worker_command", "agents"}, "invalid_config")
        need(type(data["version"]) is int and data["version"] == 1
             and type(data["interval_seconds"]) is int and data["interval_seconds"] == 900
             and type(data["enabled"]) is bool, "invalid_config")
        self.data = data
        self.helper = checked_absolute(data["history_helper"])
        self.policy = checked_absolute(data["policy_file"])
        command = data["worker_command"]
        need(isinstance(command, list) and command and all(isinstance(s, str) and s and "\x00" not in s
                                                         for s in command), "invalid_worker_command")
        # Executable may itself be a normal OS symlink (e.g. /usr/bin/python3).
        need(Path(command[0]).is_absolute() and Path(command[0]).is_file()
             and os.access(command[0], os.X_OK), "invalid_worker_command")
        need(isinstance(data["agents"], list), "invalid_config")
        self.agents = {}
        self.roots = {}
        self.allow = {}
        self.states = set()
        self.runtime = self.path.parent / RUNTIME_NAME
        for agent in data["agents"]:
            object_keys(agent, {"id", "enabled", "workspace", "owned_paths", "indexes", "state_files",
                                "require_metadata"}, "invalid_agent")
            identity = agent["id"]
            need(isinstance(identity, str) and ID_RE.fullmatch(identity)
                 and identity not in self.agents and type(agent["enabled"]) is bool
                 and type(agent["require_metadata"]) is bool, "invalid_agent")
            root = checked_absolute(agent["workspace"], directory=True)
            self.agents[identity], self.roots[identity] = agent, root
            need(isinstance(agent["owned_paths"], list) and agent["owned_paths"], "invalid_allowlist")
            allowed = []
            for item in agent["owned_paths"]:
                path = root / relative_path(item)
                checked_absolute(str(path), exists=False)
                need(path.is_file() or path.is_dir(), "missing_owned_path")
                need(not inside(self.runtime, path) and not inside(path, self.runtime), "runtime_in_memory")
                allowed.append(path)
            need(len(set(allowed)) == len(allowed), "invalid_allowlist")
            self.allow[identity] = allowed
            need(isinstance(agent["state_files"], list), "invalid_state_files")
            for item in agent["state_files"]:
                path = checked_absolute(item)
                need(path not in self.states, "duplicate_state_file")
                need(not inside(path, self.runtime), "invalid_state_files")
                self.states.add(path)
            need(isinstance(agent["indexes"], list) and agent["indexes"], "invalid_indexes")
        # A path may have exactly one owner, even with nested workspaces.
        entries = [(identity, path) for identity, paths in self.allow.items() for path in paths]
        for i, (owner, path) in enumerate(entries):
            for other, peer in entries[i + 1:]:
                need(owner == other or not (inside(path, peer) or inside(peer, path)), "overlapping_ownership")
        for identity, agent in self.agents.items():
            for index in agent["indexes"]:
                self.memory_path(identity, index, existing=True)
        # Independent cursors must never compete for the same explicitly
        # registered source, including registrations on disabled owners. Invalid
        # references are still rejected by that owner's readiness/run check;
        # resolving an explicit path here is NOT source discovery or replay.
        registered = set()
        for filename in self.states:
            value = decode_json(regular_bytes(filename), "invalid_source_state")
            reference = value.get("source") if isinstance(value, dict) else None
            if not isinstance(reference, dict):
                continue
            database, conversation = reference.get("database"), reference.get("conversation_id")
            if isinstance(database, str) and Path(database).is_absolute() and integer(conversation, 1):
                try:
                    key = (str(Path(database).resolve()), conversation)
                except (OSError, ValueError, RuntimeError):
                    raise ReviewError("invalid_source_state") from None
                need(key not in registered, "duplicate_source_reference")
                registered.add(key)
        self.runtime_valid(create=False)

    def runtime_valid(self, *, create):
        checked_absolute(str(self.runtime), exists=False)
        if create and not self.runtime.exists():
            self.runtime.mkdir(mode=0o700, exist_ok=True)
            fsync_directory(self.runtime.parent)
        if self.runtime.exists():
            info = self.runtime.stat()
            need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) & 0o077 == 0, "unsafe_runtime")
        return self.runtime

    def memory_path(self, identity, value, *, existing=False):
        need(identity in self.agents, "unregistered_agent")
        relative = relative_path(value)
        lower = [part.casefold() for part in relative.parts]
        need(relative.suffix == ".md" and not any(p in RESERVED_DIRS for p in lower)
             and lower[-1] not in RESERVED_NAMES and not lower[-1].startswith("agents."), "forbidden_edit_path")
        path = self.roots[identity] / relative
        checked_absolute(str(path), exists=False)
        need(any(path == root if root.is_file() else inside(path, root)
                 for root in self.allow[identity]), "unowned_path")
        need(path not in self.states and path not in {self.path, self.policy, self.helper}, "forbidden_edit_path")
        # Preserve layouts: new notes are fine, new directory trees are not.
        need(path.parent.is_dir(), "missing_parent")
        if path.exists():
            need(path.is_file() and path.stat().st_nlink == 1, "unsafe_memory_file")
        else:
            need(not existing, "missing_memory_file")
        return path

    def lock_path(self, identity):
        key = digest(str(self.roots[identity]).encode("utf-8"))
        return self.runtime / ("owner-" + key + ".lock")


@contextmanager
def lock(path, *, create=False, blocking=False):
    """Close rather than LOCK_UN: a surviving adapter may share this flock."""
    fd = None
    try:
        checked_absolute(str(path), exists=False)
        if not create and not path.exists():
            yield None
            return
        flags = os.O_RDWR if create else os.O_RDONLY
        flags |= os.O_NOFOLLOW | os.O_CLOEXEC
        if create:
            flags |= os.O_CREAT
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(), "unsafe_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ReviewError("busy") from None
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def load_source(filename):
    path = checked_absolute(filename)
    raw = regular_bytes(path)
    value = decode_json(raw, "invalid_source_state")
    need(isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 1
         and value.get("mode") == "history-reference"
         and {"source", "last_saved_entry_id", "last_saved_review", "active_review"} <= set(value),
         "invalid_source_state")
    reference = value["source"]
    need(isinstance(reference, dict) and {"database", "conversation_id"} <= set(reference)
         and integer(reference["conversation_id"], 1) and integer(value["last_saved_entry_id"])
         and (value["last_saved_review"] is None or isinstance(value["last_saved_review"], str)),
         "invalid_source_state")
    need(value["active_review"] is None, "legacy_active_review")
    database = checked_absolute(reference["database"])
    info = database.stat()
    return {"state_file": str(path), "database": str(database),
            "conversation_id": reference["conversation_id"], "after_entry": value["last_saved_entry_id"],
            "raw": raw, "state": value, "database_identity": (info.st_dev, info.st_ino)}


def helper_call(config, source, command, *extra):
    try:
        result = subprocess.run([sys.executable, "-B", str(config.helper), command,
                                 "--db", source["database"], "--conversation", str(source["conversation_id"]),
                                 *extra], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError:
        raise ReviewError("history_unavailable") from None
    need(result.returncode == 0, "history_failed")
    value = decode_json(result.stdout, "invalid_history_output")
    need(isinstance(value, dict) and value.get("format") == "pi-d-history-v1"
         and value.get("database") == source["database"]
         and type(value.get("conversation_id")) is int and value["conversation_id"] == source["conversation_id"]
         and type(value.get("schema_version")) is int and value["schema_version"] == 1
         and integer(value.get("observed_commit_seq")), "history_identity_mismatch")
    return value


def freeze_sources(config, identity, *, pages):
    sources, eligible = [], False
    for filename in config.agents[identity]["state_files"]:
        source = load_source(filename)
        snap = helper_call(config, source, "snapshot")
        through, first = snap.get("through_entry"), snap.get("first_entry")
        need(integer(through) and (first is None if through == 0 else integer(first, 1) and first <= through),
             "invalid_history_snapshot")
        need(source["after_entry"] <= through, "checkpoint_ahead_of_history")
        source["through_entry"] = through
        source["observed_commit_seq"] = snap["observed_commit_seq"]
        cursor = source["after_entry"]
        if pages:
            while cursor < through:
                page = helper_call(config, source, "read", "--after-entry", str(cursor),
                                   "--through-entry", str(through), "--limit", str(PAGE_LIMIT),
                                   "--max-bytes", str(PAGE_BYTES))
                need(type(page.get("after_entry")) is int and page["after_entry"] == cursor
                     and type(page.get("through_entry")) is int and page["through_entry"] == through
                     and page["observed_commit_seq"] >= source["observed_commit_seq"], "history_bounds_mismatch")
                end, count = page.get("processed_through_entry"), page.get("processed_entries")
                start, more = page.get("processed_from_entry"), page.get("has_more")
                need(integer(end, 1) and cursor < end <= through
                     and integer(count, 1) and count <= PAGE_LIMIT
                     and integer(start, 1) and cursor < start <= end and count <= end - cursor
                     and (start == end if count == 1 else start < end)
                     and type(page.get("next_after_entry")) is int and page["next_after_entry"] == end
                     and type(more) is bool and more == (end < through), "invalid_history_page")
                last_user = page.get("last_user_entry")
                need(last_user is None or integer(last_user, 1) and start <= last_user <= end,
                     "invalid_history_page")
                messages = page.get("messages")
                need(isinstance(messages, list) and len(messages) <= count, "invalid_history_messages")
                previous = cursor
                for message in messages:
                    object_keys(message, {"entry_id", "role", "text_blocks"}, "invalid_history_messages")
                    mid, blocks = message["entry_id"], message["text_blocks"]
                    need(integer(mid, 1) and previous < mid <= end and mid >= start
                         and message["role"] in ("user", "assistant") and isinstance(blocks, list)
                         and all(isinstance(block, str) for block in blocks), "invalid_history_messages")
                    previous = mid
                    eligible = eligible or any(block.strip() for block in blocks)
                cursor = end
        sources.append(source)
    return sources, eligible


def public_source(source):
    return {key: source[key] for key in ("state_file", "database", "conversation_id", "after_entry", "through_entry")}


def valid_metadata(content):
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return False
    try:
        end = lines.index("---", 1)
    except ValueError:
        return False
    fields = {}
    for i, line in enumerate(lines[1:end], 1):
        match = re.fullmatch(r"(type|topics|summary):[ \t]*(.*)", line)
        if match:
            key, value = match.groups()
            if key in fields:
                return False
            if key == "topics" and not value:
                value = " ".join(item.strip()[2:].strip() for item in lines[i + 1:end]
                                 if re.match(r"\s+-\s+\S", item))
            elif key == "topics":
                if not (value.startswith("[") and value.endswith("]")):
                    return False
                value = value[1:-1].strip()
            fields[key] = value
    return set(fields) == {"type", "topics", "summary"} and all(
        value.strip() not in ("", "null", "~", "''", '\"\"', "[]", "{}") for value in fields.values())


def validate_result(result, sources):
    object_keys(result, {"version", "status", "coverage", "edits", "cross_links", "summary"}, "invalid_worker_result")
    need(type(result["version"]) is int and result["version"] == 1
         and result["status"] in ("complete", "blocked") and isinstance(result["summary"], str)
         and all(isinstance(result[k], list) for k in ("coverage", "edits", "cross_links")), "invalid_worker_result")
    need(result["status"] == "complete", "worker_blocked")
    expected = {source["state_file"]: public_source(source) for source in sources}
    seen = set()
    for coverage in result["coverage"]:
        object_keys(coverage, {"state_file", "database", "conversation_id", "after_entry", "through_entry",
                               "processed_through_entry"}, "invalid_coverage")
        filename = coverage["state_file"]
        need(isinstance(filename, str) and filename in expected and filename not in seen, "invalid_coverage")
        need(all(type(coverage[k]) is type(value) and coverage[k] == value
                 for k, value in expected[filename].items())
             and type(coverage["processed_through_entry"]) is int
             and coverage["processed_through_entry"] == expected[filename]["through_entry"], "incomplete_coverage")
        seen.add(filename)
    need(seen == set(expected), "incomplete_coverage")


def existing_link_targets(data, parent):
    text = data.decode("utf-8")
    links = re.finditer(r"\[[^\]\r\n]*\]\(\s*(?:<([^>\r\n]+)>|([^\s)]+))"
                        r"|^\s{0,3}\[[^\]\r\n]+\]:\s*(?:<([^>\r\n]+)>|(\S+))", text, re.MULTILINE)
    targets = set()
    for match in links:
        url = next(group for group in match.groups() if group is not None)
        try:
            parsed = urlsplit(url)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            path = Path(unquote(parsed.path))
            targets.add(Path(os.path.abspath(parent / path)))
        except (ValueError, OSError):
            continue
    return targets


def change(path, content, *, owner, relative=None, kind="memory", expected=None):
    before = regular_bytes(path) if path.exists() else None
    need((digest(before) if before is not None else None) == expected, "stale_cas")
    return {"path": path, "before": before, "after": content, "owner": owner,
            "relative": relative, "kind": kind,
            "mode": stat.S_IMODE(path.stat().st_mode) & 0o777 if before is not None else 0o644}


def plan_changes(config, identity, sources, result):
    changes, staged = [], {}
    for edit in result["edits"]:
        object_keys(edit, {"path", "expected_sha256", "content"}, "invalid_edit")
        path = config.memory_path(identity, edit["path"])
        need(path not in staged, "duplicate_edit")
        expected, content = edit["expected_sha256"], edit["content"]
        need(expected is None or isinstance(expected, str) and HASH_RE.fullmatch(expected), "invalid_base_hash")
        need(isinstance(content, str) and "\x00" not in content, "invalid_content")
        if config.agents[identity]["require_metadata"]:
            need(valid_metadata(content), "missing_metadata")
        try:
            encoded = content.encode("utf-8")
        except UnicodeError:
            raise ReviewError("invalid_content") from None
        item = change(path, encoded, owner=identity, relative=edit["path"], expected=expected)
        changes.append(item)
        staged[path] = item
    foreign = {}
    for link in result["cross_links"]:
        object_keys(link, {"destination_agent", "destination_path", "target_agent", "target_path"}, "invalid_cross_link")
        destination, target = link["destination_agent"], link["target_agent"]
        need(isinstance(destination, str) and destination in config.agents and destination != identity
             and isinstance(target, str) and target in config.agents and target != destination, "invalid_cross_link")
        dest = config.memory_path(destination, link["destination_path"], existing=True)
        target_path = config.memory_path(target, link["target_path"])
        need(target_path.is_file() or target == identity and target_path in staged, "missing_link_target")
        if dest not in foreign:
            before = regular_bytes(dest)
            item = change(dest, before, owner=destination, relative=link["destination_path"], expected=digest(before))
            foreign[dest] = (item, existing_link_targets(before, dest.parent))
        item, targets = foreign[dest]
        if target_path not in targets:
            url = quote(os.path.relpath(target_path, dest.parent).replace(os.sep, "/"), safe="/.-_~")
            prefix = b"" if not item["after"] or item["after"].endswith(b"\n") else b"\n"
            item["after"] += prefix + ("- [Memory: " + target + "](<" + url + ">)\n").encode("ascii")
            targets.add(target_path)
    changes.extend(item for item, _targets in foreign.values())
    label = "controller-" + identity + "-" + uuid.uuid4().hex
    for source in sources:
        # Check the entire registration/cursor/legacy slot, not just the integer cursor.
        need(regular_bytes(Path(source["state_file"])) == source["raw"], "source_state_changed")
        info = checked_absolute(source["database"]).stat()
        need((info.st_dev, info.st_ino) == source["database_identity"], "source_database_changed")
        if source["through_entry"] != source["after_entry"]:
            value = dict(source["state"])
            value["last_saved_entry_id"] = source["through_entry"]
            value["last_saved_review"] = label + "-through-" + str(source["through_entry"])
            changes.append(change(Path(source["state_file"]), json_bytes(value), owner=identity,
                                  kind="state", expected=digest(source["raw"])))
    # Case/Unicode aliases can name the same APFS file even when Path objects
    # differ. Reject them BEFORE preparing a transaction, including foreign-link
    # destinations; otherwise partial application can make rollback ambiguous.
    names, inodes = set(), set()
    for item in changes:
        path = item["path"]
        name = unicodedata.normalize("NFD", str(path)).casefold()
        need(name not in names, "duplicate_target")
        names.add(name)
        if path.exists():
            info = path.stat()
            inode = (info.st_dev, info.st_ino)
            need(inode not in inodes, "duplicate_target")
            inodes.add(inode)
    return [item for item in changes if item["before"] != item["after"]]


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_replace(path, data, mode):
    """Same-directory temp + fsync + replace. Callers hold the commit lock."""
    checked_absolute(str(path), exists=False)
    fd, name = tempfile.mkstemp(prefix=".memory-review-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def transaction_paths(config):
    directory = config.runtime / "transactions"
    checked_absolute(str(directory), exists=False)
    if not directory.exists():
        return []
    need(directory.is_dir(), "invalid_journal")
    manifests = []
    for child in sorted(directory.iterdir()):
        checked_absolute(str(child), exists=False)
        need(child.is_dir() and re.fullmatch(r"[0-9a-f]{32}", child.name), "invalid_journal")
        path = child / "journal.json"
        checked_absolute(str(path), exists=False)
        if path.exists():
            value = decode_json(regular_bytes(path), "invalid_journal")
            need(isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 1
                 and value.get("phase") in ("prepared", "committed", "rolled_back")
                 and value.get("owner") in config.agents, "invalid_journal")
            if value["phase"] == "prepared":
                manifests.append((path, value))
        # No manifest means staging crashed BEFORE any target replacement.
    return manifests


def pending_recovery(config):
    return bool(transaction_paths(config))


def recovery_items(config, manifest, value):
    need(isinstance(value.get("files"), list), "invalid_journal")
    items, seen = [], set()
    for index, record in enumerate(value["files"]):
        object_keys(record, {"kind", "owner", "path", "before_sha256", "after_sha256", "mode"}, "invalid_journal")
        owner = record["owner"]
        need(isinstance(owner, str) and owner in config.agents, "invalid_journal")
        if record["kind"] == "memory":
            path = config.memory_path(owner, record["path"])
        else:
            need(record["kind"] == "state" and owner == value["owner"]
                 and record["path"] in config.agents[owner]["state_files"], "invalid_journal")
            path = checked_absolute(record["path"])
        need(path not in seen and type(record["mode"]) is int and 0 <= record["mode"] <= 0o777,
             "invalid_journal")
        seen.add(path)
        before_hash, after_hash = record["before_sha256"], record["after_sha256"]
        need((before_hash is None or isinstance(before_hash, str) and HASH_RE.fullmatch(before_hash))
             and isinstance(after_hash, str) and HASH_RE.fullmatch(after_hash), "invalid_journal")
        before = None if before_hash is None else regular_bytes(manifest.parent / (str(index) + ".before"))
        after = regular_bytes(manifest.parent / (str(index) + ".after"))
        need((digest(before) if before is not None else None) == before_hash and digest(after) == after_hash,
             "invalid_backup")
        current = regular_bytes(path) if path.exists() else None
        need((digest(current) if current is not None else None) in (before_hash, after_hash), "recovery_conflict")
        items.append({"path": path, "before": before, "after": after, "mode": record["mode"]})
    return items


def rollback(config, manifest, value):
    # Validate ALL pre/post images and current hashes before undoing anything.
    items = recovery_items(config, manifest, value)
    for item in reversed(items):
        path = item["path"]
        current = regular_bytes(path) if path.exists() else None
        need(current in (item["before"], item["after"]), "recovery_conflict")
        if current != item["before"]:
            if item["before"] is None:
                path.unlink()
                fsync_directory(path.parent)
            else:
                atomic_replace(path, item["before"], item["mode"])
    value["phase"] = "rolled_back"
    atomic_replace(manifest, json_bytes(value), 0o600)


def recover_owner(config, identity):
    pending = transaction_paths(config)
    need(all(value["owner"] == identity for _manifest, value in pending), "recovery_required")
    for manifest, value in pending:
        rollback(config, manifest, value)
    return len(pending)


def commit(config, identity, sources, result):
    with lock(config.runtime / "commit.lock", create=True, blocking=True):
        need(not pending_recovery(config), "recovery_required")
        changes = plan_changes(config, identity, sources, result)
        if not changes:
            return
        directory = config.runtime / "transactions"
        if not directory.exists():
            directory.mkdir(mode=0o700)
            fsync_directory(config.runtime)
        txn = directory / uuid.uuid4().hex
        txn.mkdir(mode=0o700)
        fsync_directory(directory)
        value = {"version": 1, "owner": identity, "phase": "prepared", "files": []}
        for index, item in enumerate(changes):
            if item["before"] is not None:
                atomic_replace(txn / (str(index) + ".before"), item["before"], 0o600)
            atomic_replace(txn / (str(index) + ".after"), item["after"], 0o600)
            value["files"].append({"kind": item["kind"], "owner": item["owner"],
                                   "path": item["relative"] if item["kind"] == "memory" else str(item["path"]),
                                   "before_sha256": digest(item["before"]) if item["before"] is not None else None,
                                   "after_sha256": digest(item["after"]), "mode": item["mode"]})
        manifest = txn / "journal.json"
        atomic_replace(manifest, json_bytes(value), 0o600)
        try:
            # Memory (including additive foreign links) first, checkpoints LAST.
            for item in changes:
                current = regular_bytes(item["path"]) if item["path"].exists() else None
                need(current == item["before"], "stale_cas")
                atomic_replace(item["path"], item["after"], item["mode"])
            value["phase"] = "committed"
            atomic_replace(manifest, json_bytes(value), 0o600)
        except Exception:
            value["phase"] = "prepared"
            try:
                # A failed final-marker fsync may already have replaced the
                # journal with 'committed'. Re-establish durable recovery intent
                # BEFORE undoing files, so a second crash cannot hide rollback.
                atomic_replace(manifest, json_bytes(value), 0o600)
                rollback(config, manifest, value)
            except Exception:
                raise ReviewError("recovery_required") from None
            raise ReviewError("commit_failed") from None


def outcome(identity, status, **extra):
    return {"id": identity, "status": status, **extra}


def enabled_status(config, identity):
    agent = config.agents[identity]
    if not config.data["enabled"] or not agent["enabled"]:
        return "disabled"
    if not agent["state_files"]:
        return "unregistered"
    return None


def inspect_owner(config, identity, command):
    disabled = enabled_status(config, identity)
    if disabled:
        return outcome(identity, disabled)
    try:
        with lock(config.lock_path(identity)):
            need(not pending_recovery(config), "recovery_required")
            if command == "status":
                sources = [load_source(filename) for filename in config.agents[identity]["state_files"]]
                return outcome(identity, "idle", sources=len(sources), **last_result(config, identity))
            sources, eligible = freeze_sources(config, identity, pages=command != "doctor")
            return outcome(identity, "ready" if command == "doctor" else "pending" if eligible else "unchanged",
                           sources=len(sources))
    except ReviewError as error:
        return outcome(identity, "busy" if str(error) == "busy" else "blocked", error=str(error))
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
        return outcome(identity, "blocked", error="inspection_failed")


def _execute_owner(config, identity, *, audit=False, recover=False):
    disabled = enabled_status(config, identity)
    if disabled and not recover:
        return outcome(identity, disabled)
    try:
        need(not (audit and recover), "invalid_run_mode")
        config.runtime_valid(create=True)
        with lock(config.lock_path(identity), create=True) as owner_fd:
            with lock(config.runtime / "commit.lock", create=True, blocking=True):
                if recover:
                    count = recover_owner(config, identity)
                    return outcome(identity, "recovered" if count else "no_recovery", transactions=count)
                need(not pending_recovery(config), "recovery_required")
            sources, eligible = freeze_sources(config, identity, pages=True)
            if not eligible and not audit:
                return outcome(identity, "unchanged", sources=len(sources))
            job = {"version": 1, "mode": "audit" if audit else "capture", "owner": config.agents[identity],
                   "peers": [agent for key, agent in config.agents.items() if key != identity],
                   "history_helper": str(config.helper), "policy_file": str(config.policy),
                   "sources": [public_source(source) for source in sources]}
            try:
                response = subprocess.run(config.data["worker_command"], input=json_bytes(job),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          cwd=str(config.roots[identity]), close_fds=True,
                                          pass_fds=(owner_fd,), check=False)
            except OSError:
                raise ReviewError("worker_unavailable") from None
            need(response.returncode == 0, "worker_failed")
            result = decode_json(response.stdout, "invalid_worker_output")
            validate_result(result, sources)
            commit(config, identity, sources, result)
            return outcome(identity, "complete", sources=len(sources), edits=len(result["edits"]),
                           cross_links=len(result["cross_links"]))
    except ReviewError as error:
        return outcome(identity, "busy" if str(error) == "busy" else "blocked", error=str(error))
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
        return outcome(identity, "blocked", error="run_failed")


def last_result(config, identity):
    """Read one bounded operational receipt, never a transcript/proposal log."""
    path = config.lock_path(identity).with_suffix(".result.json")
    if not path.exists():
        return {}
    value = decode_json(regular_bytes(path), "invalid_last_result")
    object_keys(value, {"version", "recorded_at_ms", "result"}, "invalid_last_result")
    result = value["result"]
    allowed = {"id", "status", "error", "sources", "edits", "cross_links", "transactions"}
    need(type(value["version"]) is int and value["version"] == 1
         and integer(value["recorded_at_ms"], 1)
         and isinstance(result, dict) and {"id", "status"} <= set(result) <= allowed
         and result["id"] == identity
         and result["status"] in ("complete", "unchanged", "blocked", "recovered", "no_recovery"),
         "invalid_last_result")
    if "error" in result:
        need(isinstance(result["error"], str) and re.fullmatch(r"[a-z0-9_]{1,80}", result["error"]),
             "invalid_last_result")
    for key in ("sources", "edits", "cross_links", "transactions"):
        if key in result:
            need(integer(result[key]), "invalid_last_result")
    return {"last_result": result, "last_recorded_at_ms": value["recorded_at_ms"]}


def run_owner(config, identity, *, audit=False, recover=False):
    result = _execute_owner(config, identity, audit=audit, recover=recover)
    if result["status"] in ("disabled", "unregistered", "busy"):
        return result
    # Detached child stdout is intentionally discarded. Persist its sanitized
    # outcome separately so failures remain observable without accumulating logs.
    # This receipt is not a transaction marker or authority for advancing cursors.
    try:
        config.runtime_valid(create=True)
        path = config.lock_path(identity).with_suffix(".result.json")
        recorded_at = time.time_ns() // 1_000_000
        with lock(path.with_suffix(".lock"), create=True, blocking=True):
            previous = last_result(config, identity)
            if previous.get("last_recorded_at_ms", 0) <= recorded_at:
                atomic_replace(path, json_bytes({"version": 1, "recorded_at_ms": recorded_at,
                                                 "result": result}), 0o600)
    except (ReviewError, OSError, ValueError, UnicodeError, RecursionError, TypeError):
        result["diagnostic"] = "last_result_unavailable"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "check", "tick", "status"):
        commands.add_parser(name)
    runner = commands.add_parser("run")
    runner.add_argument("--agent", required=True)
    mode = runner.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true", help="explicitly review existing memory even without new text")
    mode.add_argument("--recover", action="store_true", help="restore inspected incomplete commit; never launch a worker")
    args = parser.parse_args(argv)
    try:
        config = Config(args.config)
        if args.command == "run":
            need(args.agent in config.agents, "unregistered_agent")
            statuses = [run_owner(config, args.agent, audit=args.audit, recover=args.recover)]
        else:
            statuses = [inspect_owner(config, identity, "check" if args.command == "tick" else args.command)
                        for identity in config.agents]
            if args.command == "tick":
                for item in statuses:
                    if item["status"] != "pending":
                        continue
                    try:
                        child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                                  "--config", str(config.path), "run", "--agent", item["id"]],
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
                        item.update(status="dispatched", pid=child.pid)
                    except OSError:
                        item.update(status="blocked", error="dispatch_failed")
        print(json_bytes({"version": 1, "command": args.command, "interval_seconds": 900,
                          "agents": statuses}).decode("ascii"), end="")
        return 1 if any(item["status"] == "blocked" for item in statuses) else 0
    except ReviewError as error:
        code = str(error)
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
        code = "configuration_unavailable"
    print(json_bytes({"version": 1, "command": args.command, "status": "blocked", "error": code}).decode("ascii"), end="")
    return 2


if __name__ == "__main__":
    sys.exit(main())
