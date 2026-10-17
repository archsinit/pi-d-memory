# Contributor instructions

This repository contains reusable memory-maintenance code, not anyone's live memory or deployment.

- Keep actual notes, source/session databases, credentials, local configuration, checkpoints, transaction images, receipts and deployment diagnostics outside Git.
- Read conversation sources only through the read-only history helper, with explicit identities, frozen bounds and complete paging. Never discover a source, initialize a cursor to latest, authorize replay implicitly, or open a source through a writable Harness.
- Reviewers propose changes; the deterministic controller owns memory writes, owner-lifetime locks, commit locks, CAS, recovery and checkpoints. Do not add a second writer/coordinator or idle audits.
- Configurations controlling overlapping workspaces must keep the same configuration-parent coordination directory. Moving code is not permission to move their locks or reset their state. Quiesce deployed workers before changing live code; disabling a timer alone does not stop an in-flight review.
- Preserve the 900-second interval, no-model idle behavior and proposal-only tool restrictions. Do not introduce production model-time, token or tool-call budgets without explicit authorization.
- OAuth refresh requires an explicitly enabled local policy. Reuse the existing pinned credential adapter and its cross-process lock; never copy live tokens into a separate store. Login/logout/deletion remain disabled in the reviewer. Join started credential persistence before worker completion, including cancellation.
- Tests must use synthetic histories/notes, faux providers and explicitly temporary credential files. Never instantiate the live default credential store in tests, print secrets, or contact a real provider.
- The external pi-d checkout is a read-only dependency. Do not modify it or install/upgrade dependencies as an incidental test step.
- Run focused tests first for small changes; run the complete suite for publishing or high-risk lifecycle/security changes. Report verification limits. Do not delete or skip failing tests.
- Before a public commit/push, inspect the staged file list and scan it for personal paths, private data and credentials. Do not force-push or overwrite another repository's work.
