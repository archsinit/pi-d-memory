# pi-d-memory

Scheduled, owner-scoped memory maintenance using the **stock pi-durable agent engine used by pi-d**, with a deterministic Python controller around it.

This is a small headless host of that engine, **not the interactive pi-d CLI** and not a hand-written model loop. It imports pinned SDK packages and the credential adapter from a compatible, separately installed pi-d checkout. It never opens a source conversation through a writable Harness.

**This repository is code only.** Actual knowledge bases, session databases, credentials, source registrations, deployment configuration, checkpoints, receipts and backups belong outside Git. The examples are deliberately disabled and contain placeholders.

## How it works

```text
15-minute scheduler → Python controller → read-only frozen history capture
                                      → isolated Durable MemoryStorage reviewer
                                      → validated proposals → locked application/checkpoint
```

- **900-second polling.** No new eligible user/assistant text means no reviewer, model or credential-store construction. Busy owners are skipped without losing history. Idle ticks never initiate audits.
- **Separate owners.** Each workspace has its own registered sources, scope, index, review and checkpoint. Register only explicitly approved owners/peers. Foreign reads are selective; foreign writes are limited to controller-generated additive, deduplicated cross-links.
- **Read/propose only.** The model receives exactly `list_notes` and `read_note`; no shell, editing tools, subagents, mailbox/browser tools or host extension discovery. Existing notes must be fully read before replacement.
- **Verified coverage.** The read-only helper pages an explicit `(after, through]` range with fixed upper bounds. Missing pages, invalid cursors, unsupported schemas and unsafe WAL conditions fail closed. Checkpoints advance only after complete coverage and successful application.
- **Coordinated writes.** An owner lock lasts through the worker's lifetime; a shorter common commit lock covers validation, compare-and-swap checks, application and state replacement. Prepared journals and private pre/post images support inspected recovery after interrupted multi-file commits.
- **Explicit credentials policy.** The default example is read-only. Separately approved `allow_auth_refresh: true` permits only renewal of the selected provider's existing OAuth entry through pi-d's canonical shared store and cross-process lock. No second token store, login/logout, deletion, missing-credential login or API-key fallback. Started refresh persistence is joined before worker completion, including cancellation.

No production model-time, token or tool-call budgets are imposed. Page sizes are transport limits, not a total-review budget.

## Components

| Path | Purpose |
| --- | --- |
| `automation/memory_review.py` | Source checks, dispatch, lifetime/commit locks, proposal validation, recovery and checkpoints |
| `automation/run_review.mjs` | Ephemeral native Durable reviewer and pinned runtime/credential policy |
| `automation/register_source.py` | Explicit, verified source/cursor registration; no discovery or latest-cursor initialization |
| `helpers/read_conversation.py` | Read-only native SQLite history snapshots and bounded, fully paged reads |
| `automation/MEMORY-POLICY.md` | Generic retrieval, capture, correction, consolidation and audit policy |
| `automation/AGENTS-snippet.md` | Onboarding guidance for an explicitly activated owner |
| `examples/` | Disabled, sanitized configuration and launchd templates |

## Requirements

- POSIX filesystem/process support and `flock`; the included timer example is for macOS `launchd`.
- Python **3.9+**, with SQLite **3.37+** and JSON support. Python **3.14** is the deployment-tested interpreter.
- Node **22.19.0+**; Node **24.21.0** is deployment-tested.
- A compatible pi-d checkout with installed dependencies: `pi-d-chat` **0.1.0**, `@earendil-works/pi-durable` **1.2.0** and `@earendil-works/pi-ai` **1.2.0**. The adapter must export `chatModels` and `SubscriptionCredentials` and match the locally approved SHA-256.
- Existing usable subscription OAuth credentials for the selected provider. Missing/revoked credentials require a separately authorized manual login in the appropriate host, not automatic login by this reviewer.

These are intentionally pinned internal integration points, **not a stable public pi-d host SDK**. Version/hash mismatches block execution. This project does not install, upgrade or modify pi-d for you.

## Configure a deployment

1. Clone this repository and obtain the compatible pi-d installation separately. Do not copy pi-d's credential file into this repository.
2. Copy `examples/agents.example.json` and `examples/runtime.example.json` into **one private local configuration directory outside Git and outside owned memory**, for example `~/.config/pi-d-memory/`, naming them `agents.json` and `runtime.json`.
3. Replace all placeholder paths with actual absolute paths. Point the registry at existing owner workspaces, allowed Markdown paths and indexes. Review the common policy and owner-specific `AGENTS.md` instructions. Keep global and owner scheduling disabled during registration.
4. Configure `pi_d_root`, the chosen model/provider and the SHA-256 of the reviewed `src/auth.ts` adapter. The all-zero example hash is not usable. Set an explicit absolute `history_python` to an approved interpreter; it is also used by Node for every history page. A configured interpreter failure blocks rather than silently falling back. Optional runtime fields are `thinking_level` and `global_instructions` (an explicit absolute instructions-file path).
5. From trusted pi-d session/conversation/workspace metadata, explicitly register each source and a deliberately mapped previous-review cursor. Do not guess sessions, initialize to latest or silently authorize replay. The registration command is:

   ```sh
   python3 -B automation/register_source.py \
     --config "$LOCAL_CONFIG" --agent planning --workspace "$WORKSPACE" \
     --database "$VERIFIED_DATABASE" --conversation "$VERIFIED_CONVERSATION" \
     --state-file "$LOCAL_STATE_FILE" --after-entry "$VERIFIED_PREVIOUS_CURSOR"
   ```

   Use `--replay-from-start` **instead of** `--after-entry` only with explicit replay authorization. Registration never overwrites an existing state and leaves scheduling disabled. It also acquires the controller's owner and common commit locks and refuses pending recovery; disabled flags alone do not prove an earlier review has finished. Existing deployments must preserve their actual source, cursor and state rather than re-registering during a code move.
6. Run the read-only checks below. Authenticate only through an independently authorized host workflow if necessary. Leave `allow_auth_refresh` false unless its narrow shared-store exception has been separately approved.
7. After validation, explicitly enable only the intended owners and dispatcher. Install/configure the scheduler separately; a config flag or installed plist alone does not prove unattended operation.

**One coordination directory:** runtime files live at `<configuration-parent>/.memory-review-runtime`. Configurations controlling overlapping stores must use that same parent directory. A code checkout can move, but moving a live config into another directory can create a competing coordinator; do not reset locks or checkpoints as a shortcut. Keep notes, private configuration and runtime state separate from code.

## Commands

Run from the repository root, using the approved Python executable and an **absolute** local config path:

```sh
python3 -B automation/memory_review.py --config "$LOCAL_CONFIG" doctor
python3 -B automation/memory_review.py --config "$LOCAL_CONFIG" check
python3 -B automation/memory_review.py --config "$LOCAL_CONFIG" status
node automation/run_review.mjs --runtime "$LOCAL_RUNTIME" --check
```

These checks do not run a model. Runtime `--check` also does not construct credentials, even with refresh enabled. `tick` dispatches pending owners; an explicit save uses the same controller:

```sh
python3 -B automation/memory_review.py --config "$LOCAL_CONFIG" tick
python3 -B automation/memory_review.py --config "$LOCAL_CONFIG" run --agent planning
```

Only an explicitly requested existing-memory audit authorizes `run --agent planning --audit`. Unresolved prepared transactions block ordinary runs; after inspecting their private journal/images, `run --agent planning --recover` performs guarded rollback without launching a reviewer. Unknown external edits are refused rather than overwritten. Recovery is not an automatic error-hiding retry.

In pi-d, `/new` and `/clear` retain source history/identity. New or switched sessions require separate verified registration; this code must not follow them automatically.

## macOS scheduler

`examples/com.example.memory-review.example.plist` checks every **900 seconds while awake**, with `RunAtLoad: false`. Replace every `__ABSOLUTE_...__` marker before use. `launchd` does **not** expand these markers or shell variables for you. Use absolute executable/script/config paths, the intended `HOME`/`XDG_CONFIG_HOME`, and the approved Python in both the plist and runtime `history_python`.

The application does not keep the Mac awake, bypass privacy permissions or change other startup services. Validate source access and reviewer execution under the **actual LaunchAgent environment**, not just an interactive shell. Default timer output goes to `/dev/null`; any temporary diagnostics should be private and contain no credentials/transcripts.

## Tests

The offline suite has **114 tests**: 26 history-helper, 52 controller/registration and 36 reviewer tests. It covers full paging, source continuity, idle/busy behavior, scope/alias/link checks, inherited owner-lock lifetime, interrupted commits/recovery, metadata, native faux-provider reviews, shared-store refresh locking/preservation and cancellation-safe persistence.

Tests use synthetic histories/notes, faux models and explicit temporary credential files. They do not require real credentials or contact a model provider. Some fixture owner labels are illustrative, not live deployments or data.

By default, SDK-dependent tests use a sibling `../pi-d` checkout. Override it explicitly if needed:

```sh
TEST_PI_D_ROOT=/absolute/path/to/pi-d make test \
  PYTHON=/absolute/path/to/python3 NODE=/absolute/path/to/node
```

Or run the individual `test-history`, `test-controller` and `test-reviewer` Make targets. No dependency installation or package publication is part of these targets.

## Limits and safety

- **Not an OS sandbox.** Restriction applies to this scripted reviewer's model tools/proposals. Trusted code, ordinary interactive agents and unrelated external writers still have their OS permissions. External writers that ignore these locks can race checks.
- Source support is intentionally conservative and schema-specific. Only explicitly registered eligible user/assistant text triggers normal reviews; hidden-only ranges stay pending rather than silently advancing.
- Closed-chat/database-WAL lifecycle accessibility is deployment-dependent and not guaranteed. Verify it separately before relying on unattended maintenance after closing pi-d. A blocked source must stay pending, not be opened writable or replayed under a guessed identity.
- Atomic replacement is per file, not a multi-file filesystem transaction. Durable journals support inspected recovery. Backups remain private, outside memory, and require deliberate pruning.
- The frontmatter envelope and Markdown link checks are deliberately bounded validators, not full YAML/Markdown parsers. New nested directories and automatic deletion/rename are not supported proposal operations.
- Live authentication access is not evidence that a remote expired-token renewal has occurred. Offline refresh tests do not replace deployment-specific observation. Do not force token rotation just to demonstrate it.

## License

MIT. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
