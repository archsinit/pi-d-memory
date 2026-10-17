#!/usr/bin/env python3
"""Deliberately register a verified source; never discover sessions or choose a cursor.

Registration leaves scheduling disabled. Existing review state is never overwritten.
Only use database/conversation/workspace references from a trusted local /status.
"""
import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

from memory_review import Config, ReviewError, lock, pending_recovery


class RegistrationError(Exception):
    pass


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RegistrationError("Expected a regular, single-link file, not a symlink")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".memory-registration-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_helper(helper, database, conversation, command, after=None):
    argv = [sys.executable, "-B", str(helper), command, "--db", str(database),
            "--conversation", str(conversation)]
    if after is not None:
        argv += ["--after-entry", str(after), "--through-entry", str(after),
                 "--limit", "1", "--max-bytes", "32000"]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        raise RegistrationError("Read-only history verification failed; no registration performed")
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RegistrationError("History verification did not return complete JSON") from None
    if not isinstance(value, dict) or value.get("format") != "pi-d-history-v1" or \
            value.get("database") != str(database) or value.get("conversation_id") != conversation:
        raise RegistrationError("History source identity did not match the explicit reference")
    return value


def register(args):
    config_path = Path(args.config)
    if not config_path.is_absolute():
        raise RegistrationError("Configuration path must be absolute")
    regular(config_path)
    lock_path = config_path.with_name(config_path.name + ".registration.lock")
    descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    coordination = ExitStack()
    try:
        if os.fstat(descriptor).st_nlink != 1:
            raise RegistrationError("Unsafe registration lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original_config = config_path.read_bytes()
        config = json.loads(original_config)
        if config.get("version") != 1 or config.get("enabled") is not False:
            raise RegistrationError("Registration requires a disabled version-1 scheduler")
        matches = [item for item in config.get("agents", []) if item.get("id") == args.agent]
        if len(matches) != 1 or matches[0].get("enabled") is not False:
            raise RegistrationError("Select exactly one disabled registered owner")
        owner = matches[0]
        files = owner.get("state_files", [])
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise RegistrationError("Owner state_files must be an explicit path list")
        workspace_arg = Path(args.workspace)
        if not workspace_arg.is_absolute() or not workspace_arg.is_dir():
            raise RegistrationError("Confirm the owner's existing absolute workspace")
        workspace = workspace_arg.resolve(strict=True)
        if workspace != Path(owner["workspace"]).resolve(strict=True):
            raise RegistrationError("Confirmed workspace does not match this owner")
        state_path = Path(args.state_file)
        if not state_path.is_absolute():
            raise RegistrationError("State path must be absolute")
        try:
            relative = state_path.relative_to(workspace)
        except ValueError:
            raise RegistrationError("New state must be inside its owner's workspace") from None
        if not relative.parts or any(part in (".", "..") for part in relative.parts):
            raise RegistrationError("Invalid state destination")
        cursor = workspace
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise RegistrationError("State paths must not traverse symlinks")
        if state_path.exists():
            raise RegistrationError("Existing state is never overwritten; map or migrate it explicitly")
        if state_path.suffix != ".json":
            raise RegistrationError("Review state must be a JSON file")
        database_arg = Path(args.database)
        if not database_arg.is_absolute():
            raise RegistrationError("Database reference must be absolute")
        regular(database_arg)
        database = database_arg.resolve(strict=True)
        for registered_owner in config.get("agents", []):
            for registered_path in registered_owner.get("state_files", []):
                existing_path = Path(registered_path)
                regular(existing_path)
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
                source = existing.get("source", {})
                if source.get("database") == str(database) and source.get("conversation_id") == args.conversation:
                    raise RegistrationError("This source is already registered; migrate its existing cursor instead of duplicating it")
        helper = Path(config["history_helper"])
        if not helper.is_absolute():
            raise RegistrationError("History helper must be an explicit absolute path")
        regular(helper)
        if args.conversation < 1:
            raise RegistrationError("Conversation ID must be positive")
        after = 0 if args.replay_from_start else args.after_entry
        if after is None or after < 0:
            raise RegistrationError("Supply an explicit nonnegative checkpoint or authorize replay")
        snapshot = read_helper(helper, database, args.conversation, "snapshot")
        through = snapshot.get("through_entry")
        if type(through) is not int or through < after:
            raise RegistrationError("Checkpoint exceeds the verified source boundary")
        # Zero is the deliberate replay sentinel, not an entry. Through=0 is
        # valid only for an empty source, so do not query (0,0] on a nonempty one.
        if after:
            boundary = read_helper(helper, database, args.conversation, "read", after)
            if boundary.get("after_entry") != after or boundary.get("through_entry") != after or \
                    boundary.get("processed_through_entry") != after or boundary.get("has_more") is not False or \
                    boundary.get("messages") != []:
                raise RegistrationError("Checkpoint was not verified as an empty native range")
        state = {
            "version": 1, "mode": "history-reference",
            "source": {"database": str(database), "conversation_id": args.conversation},
            "last_saved_entry_id": after,
            "last_saved_review": "explicit-replay-from-start" if after == 0 else "explicitly-mapped-checkpoint",
            "active_review": None,
        }
        # Disabled flags do not establish quiescence: an older controller may
        # still own a worker. Use the same owner -> commit lock order, namespace
        # and path validation as ordinary reviews, without starting a reviewer.
        registry = Config(config_path)
        registry.runtime_valid(create=True)
        coordination.enter_context(lock(registry.lock_path(args.agent), create=True))
        coordination.enter_context(lock(registry.runtime / "commit.lock", create=True))
        if config_path.read_bytes() != original_config:
            raise RegistrationError("Configuration changed during verification; verify again")
        if pending_recovery(registry):
            raise RegistrationError("Inspected recovery is required before source registration")
        # Create exclusively. Registration cannot replace a competing writer's state.
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(state_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        files = owner.setdefault("state_files", [])
        if str(state_path) not in files:
            files.append(str(state_path))
        # If this second write fails, retain the new state for explicit recovery;
        # never delete possibly updated state or pretend the registration succeeded.
        atomic_json(config_path, config)
        return {"status": "registered", "agent": args.agent, "enabled": False,
                "state_file": str(state_path), "after_entry": after}
    finally:
        try:
            coordination.close()
        finally:
            os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "agent", "workspace", "database", "state-file"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--conversation", required=True, type=int)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--after-entry", type=int)
    choice.add_argument("--replay-from-start", action="store_true")
    args = parser.parse_args()
    try:
        result = register(args)
    except RegistrationError as error:
        # These are fixed local diagnostics, never raw helper/provider output.
        print(json.dumps({"status": "blocked", "reason": str(error)}))
        return 1
    except ReviewError:
        print(json.dumps({"status": "blocked", "reason": "Registration requires an idle coordinator with valid configuration and no pending recovery"}))
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "blocked", "reason": "Invalid registration configuration or unavailable file; no existing source state was replaced"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
