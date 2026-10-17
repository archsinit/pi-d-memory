## Owner-scoped memory review

Apply this section to an explicitly registered workspace's `AGENTS.md` during activation, preserving its task-specific instructions. Replace references below with the deployment's actual local configuration and canonical controller path.

- Use the deployment's local `agents.json` to identify this workspace's owner, note scope, indexes and explicitly registered peers. Consult relevant existing knowledge before creating records. Read peers selectively, not as a combined memory dump.
- Follow the common procedure referenced by `policy_file`, preserving this workspace's own layout and safeguards.
- Only propose edits to this owner's memory. The sole foreign-write exception is a controller-generated additive cross-memory link; do not rewrite foreign facts, metadata, instructions or existing links.
- Scheduled polling is every 900 seconds / 15 minutes. Owners have separate reviews and busy guards. No new eligible conversation text means no model/credential construction; idle ticks never initiate audits. Only describe a deployment as active after its enabled registration and actual scheduler behavior have been verified.
- Explicit save/audit requests use the same controller, configuration, locks and checkpoints. Do not manually replace notes, invoke a legacy writer or launch a competing memory-writing subagent. Only an explicit audit request authorizes `--audit`.
- Advance a checkpoint only after complete frozen-range coverage and verified application. Failures leave the range pending. Never reset a source or initialize a cursor to latest to hide unread history.
- Source registration requires trusted session/conversation/workspace references and a deliberately mapped previous-review cursor or explicit authorization to replay from the start. In pi-d, `/new` and `/clear` retain history/identity; new or switched sessions require separate explicit registration.
- Preserve uncertainty, provenance and old verification dates; distinguish user decisions from assistant proposals. Link authoritative task systems instead of duplicating their execution state.
- Maintenance does not authorize external account actions, mailbox/browser access, task changes, software installation, login/logout, source-session writes or restarting an agent. Any narrow credential-refresh exception must be separately approved in local policy.
