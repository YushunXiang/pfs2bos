---
name: pfs2bos
description: Plan, run, verify, or restore file-level PFS-to-BOS offloads using the pfs2bos repository; use for migration scope and storage audits involving this tool.
---

# pfs2bos

Use the current repository README and CLI help as the authority for options and behavior. This skill records operational lessons, not proof that a particular mount, remote, plan, or previous migration is still valid. Locate the repository and its installed CLI or Python environment before use. Never print credentials or `.env` contents.

## Scope and preflight

- Determine the requested source paths, remote prefix, mount, and whether the user authorized migration or only an inventory. `scan` reads the source but writes a plan; `apply` changes source paths and `restore` changes them back. Preserve the user's scope and authorization.
- Classify artifacts by reproducibility and runtime dependency; do not treat an entire `tmp` directory as disposable. Preserve active worktrees, code, configuration, environments, native libraries, and paths referenced by running scripts. For checkpoints, include required companion files, including extensionless OCDBT blocks and metadata where applicable.
- Confirm the mapping, mount, rclone access, free space for any restore, and absence of active writers to source files and the destination prefix. Check hardlinks and open handles. Explain that migrated files become absolute symlinks into the mount, so availability, performance, and subsequent writes depend on it; storage savings may be delayed by snapshots or open handles.
- Inspect the plan's entries, pending items, exclusions, counts and estimated space. Default scans omit tracked files, symlinks, hardlinks, environments and caches; unknown extensions and ordinary metadata can remain pending. Use narrowly scoped `--data-dir` or configuration for complete artifact directories and generate a new plan when scope changes. Do not edit pending items into entries. Preserve unusual filenames and explain existing-object conflicts; differing content is not overwritten.

## Workflow

Run commands from the repository environment; specify `--env-file` when needed. The configuration file is required at scan time even if overrides are supplied; subsequent commands use the mapping saved in the plan. `--path` and `--data-dir` are relative to the configured root.

```bash
pfs2bos scan --env-file /path/to/.env --path project/artifacts --out plan.json
pfs2bos apply --plan plan.json
pfs2bos status --plan plan.json --verify
pfs2bos restore --plan plan.json
```

For a new environment or large migration, first trial a small, inactive artifact scope: scan, apply, verify content and original-path reads, inspect disk use, and test restore when authorized. Then scan the intended scope and retain the plan. Do not automatically restore a completed migration.

`apply` verifies remote content with rclone, then hashes mounted and source contents and checks identity before atomic replacement. `status --verify` hashes current local paths or link targets; it does not check the rclone remote. Report linked counts, errors, pending and unresolved items, and evidence of remote and mount/source verification. Upload progress alone is not completion. If source identity changed, rescan; after a transient interruption, inspect recorded state and resolve the cause before resuming. Avoid concurrent remote writers: verification cannot prevent later remote changes.

For large inventories, consult README's large-inventory section and consider `--batch-verify --sqlite-journal /absolute/path/states.sqlite`, with appropriately bounded `--verify-workers` and `--switch-workers`. Keep the plan and journal, including WAL/SHM files; JSON alone no longer reflects current state. `--resume-verified` reuses a recorded remote check only when remote writers remain stopped; mount and source are still checked.

Ordinary scan skips multiply linked files. For explicitly authorized complete hardlink groups, use `python -m pfs2bos.hardlinks` and its separate plan; inspect unresolved aliases and stop writers for every alias. Finish an interrupted partial group with `apply` before `restore`; restore recreates the hardlink relationship. Consult README for `--alias-file` and full commands.

Restore verifies a download before replacing the tool's symlink and retains BOS copies. It needs local space and does not restore owner, ACLs, extended attributes or original atime. A restored file needs a new scan before another migration.

If an existing bucket can be listed but bucket creation checks return `403 AccessDenied`, the documented `RCLONE_S3_NO_CHECK_BUCKET=true` may be appropriate. A `SignatureDoesNotMatch` requires investigating credentials and signing; neither case justifies exposing secrets or skipping content verification.
