English | [简体中文](docs/README.zh-CN.md)

# pfs2bos

Use rclone to copy models, data, and experiment artifacts that are no longer being written to BOS. After verifying each file, atomically replace the original file with an absolute symlink pointing to the BOS mount. Requires Python 3.10+, Linux, Git, and a configured rclone. `python-dotenv` is installed automatically as a dependency.

Example mapping (configure the paths yourself):

```text
/mnt/pfs/workspace/RMBench/data/example.parquet
  → bos:your-bucket/artifacts/RMBench/data/example.parquet
Symlink at the original path → /mnt/bos/artifacts/RMBench/data/example.parquet
```

## Usage

First, install into your current Python environment or virtual environment:

```bash
cd /mnt/pfs/workspace
python3 -m pip install --user ./tools/pfs2bos
cp ./tools/pfs2bos/example.env .env
# Edit .env and fill in all three configuration values before running
pfs2bos scan --path RMBench --out plan.json
```

After cloning the repository from GitHub, you can run `python3 -m pip install .` from the repository root. Omit `--user` inside a virtual environment. You can also use `python3 -m pfs2bos`. Scanning reports file counts in real time. The plan includes `entries` (selected files), `pending` (unknown types awaiting confirmation only), exclusion counts, and the estimated allocated bytes to be freed. Each item records its relative path, selection reason, destination, size, device number, inode, mtime/ctime in nanoseconds, link count, and permissions.

Review the generated plan, stop training, downloads, and any other tasks that write files, and confirm that the mount is readable. Then run these commands manually:

```bash
pfs2bos apply --plan plan.json
pfs2bos status --plan plan.json
pfs2bos status --plan plan.json --verify
pfs2bos restore --plan plan.json
```

`apply` uses 4 concurrent transfers by default; adjust this with `--transfers 2`. `status` outputs JSON and checks the current state of each path. Adding `--verify` reads the full contents, computes SHA-256, and compares them against the recorded content. It does not access the rclone remote or modify the plan. Exit codes are 1 on failure, 0 on success, and 130 on Ctrl-C.

First, run the complete workflow above on a very small artifact directory that is no longer being written to. Confirm that reading from the original paths works, the contents match, and local disk usage reported by `du` decreases before expanding the scope. During the initial test, you can use `restore` to verify rollback. The tool does not initiate an actual migration automatically.

## Selecting the scope

By default, `scan` reads `.env` from **the directory where you run the command**, without searching parent directories. Copy the repository's `example.env` to `.env` and edit it:

```dotenv
PFS2BOS_ROOT=/mnt/pfs/workspace
PFS2BOS_REMOTE=bos:your-bucket/artifacts
PFS2BOS_MOUNT=/mnt/bos/artifacts
```

When running from another directory, you can specify the file explicitly:

```bash
pfs2bos scan --env-file /mnt/pfs/workspace/tools/pfs2bos/.env --path RMBench --out plan.json
```

The precedence is command-line `--root` / `--remote` / `--mount` > existing environment variables > `.env`, with no built-in defaults. Scanning requires `.env` (or the file specified by `--env-file`). If the file is missing, the tool prompts you to copy and fill in `example.env`, even if command-line arguments or environment variables are provided. If any final configuration value is missing or empty, the tool also stops and prompts you to fill it in. Values are read literally, without expanding `${VAR}`. `.env` is ignored by Git; only `example.env` is committed to the repository.

These settings only affect scans that create new plans. `apply`, `status`, and `restore` always use the mapping saved in the plan.

`--path` specifies a project directory relative to the configured workspace root and can be repeated. If omitted, the entire root is scanned. Default extensions cover model weights, data such as Parquet/Arrow/NumPy, images, audio, video, and archives, with no minimum size limit. Files with unknown extensions and ordinary JSON/index files go into `pending` and are not uploaded.

To migrate accompanying metadata as well, explicitly specify a data directory:

```bash
pfs2bos scan --path RMBench --data-dir RMBench/data --out rmbench-plan.json
```

Or use a configuration file:

```json
{
  "data_dirs": ["RMBench/data", "RMBench/outputs/frozen-run"],
  "exclude_dirs": ["RMBench/outputs/active-run"]
}
```

```bash
pfs2bos scan --path RMBench --config config.json --out rmbench-plan.json
```

Configured directories are also relative to the workspace root and are matched on directory boundaries. Explicit data directories include all their regular files, while still respecting exclusion rules. Do not indiscriminately list project roots as data directories. To adjust the scope, generate a new plan instead of manually moving `pending` items into `entries`.

The scan skips `.git`, snapshot directories, common virtual environments and environments identified by `pyvenv.cfg`/`conda-meta`, miniforge, caches, development tool configuration, `bos-cli`, `tools`, and the tool's own temporary and lock files. It also skips Git-tracked files (including those in ancestor and nested repositories), existing symlinks, special files, and files with multiple hard links, and does not follow directory symlinks. A failed Git query aborts the scan. Filenames containing newlines remain in `pending` and are not migrated in the initial version. Chinese characters, spaces, and `#` in filenames are supported.

Use `--root`, `--remote`, and `--mount` to override the mapping when scanning; subsequent commands read these parameters from the plan. The mount path must be separate from the source tree. The plan fixes the processing scope: `apply` does not include files created after the scan. The plan is the migration log; keep it until restoration is complete.

## Verification, conflicts, and interruptions

1. `apply` checks each file's identity against the scan, reads it to compute SHA-256, and persists the pending-upload state.
2. Files are uploaded using `rclone copy --files-from-raw ... --ignore-existing --no-traverse`, with a file list containing only files in the plan. Existing objects with the same name are not overwritten, and other files in the remote directory are not modified. If the entire upload batch fails, all files pending processing in that batch retain their local copies, and the operation can be rerun later.
3. Each file's actual content is verified with `rclone check --download --one-way`. Existing objects with the same name and content are reused. If the content differs, a verification failure is recorded and the source file is retained. Content verification is required even when sizes match; ETags are not relied on. The tool does not automatically resolve conflicts or overwrite objects.
4. The tool then reads the full file from the mount path and compares its SHA-256, rereads the source file, and checks its identity. If the mount is unreadable, the contents differ, or the source file has changed, the local file is retained.
5. The tool persists the `linking` state and creates a temporary symlink in the original directory. After one final identity check, it switches the path using `os.replace`, fsyncs the directory, and records `linked`. The plan is saved atomically and fsynced at every stage.

Verification increases BOS read traffic and processing time, since each file's remote, mounted, and local contents must be read. Per-file verification prioritizes isolating failures; directories containing many small files incur process startup overhead.

Errors are recorded in each file's `error` field, while other files that can be verified continue processing. After resolving mount or network issues, rerun the original `apply` command. If a source file has changed, generate a new plan. The `linking` state may indicate an interruption before or after the switch; rerunning checks the actual path and continues. Each source root uses an exclusive `.pfs2bos.lock` lock to prevent different plans from operating concurrently on the same root. The lock is released automatically when the process exits, and the lock file can remain.

`restore` downloads from the rclone remote to a temporary file in the original directory, verifies the original SHA-256, restores permissions and mtime, fsyncs the file, and atomically replaces the symlink created by the tool. If the download fails or the content has changed, the existing symlink is retained. You can rerun `restore` after an interruption before or after the restoration switch; files already restored are not overwritten again. BOS copies are always retained. To migrate restored files again, run a new scan.

## Limitations

You must stop writes first. Identity and hash checks cannot rule out concurrent writes between the final check and rename, nor prevent other clients from modifying remote objects after verification. The tool's lock only coordinates this tool's operations. Once a source file has been replaced, writes to the original path follow the symlink to BOS. Read performance and availability depend on the mount.

Development environments, code, and tool files are retained; the tool does not promise to leave only code in the workspace. Space estimates use `st_blocks * 512` and do not subtract symlink, directory, or log overhead. PFS delayed allocation may cause files that have not yet been flushed to disk to be underestimated, and snapshots or open file handles may still retain the underlying storage. Restoration requires sufficient local space and does not restore the original atime, owner, ACLs, or extended attributes.

The tool only copies files and does not delete remote objects. `--ignore-existing` prevents overwriting existing objects but cannot provide object-level transactions with external uploaders. Avoid having other programs write to the same BOS prefix during execution. The plan and source directories should be controlled by trusted users.

## Development and verification

The code uses the `src/pfs2bos/` package layout:

- `cli.py`: argument parsing, command dispatch, and exit codes.
- `scan.py`, `config.py`: scanning, Git tracking checks, and selection rules.
- `migrate.py`: rclone copying, content verification, symlink switching, and restoration.
- `plan.py`: plan validation and persistent stage records.
- `filesystem.py`: path checks, file identity, hashing, and locking.
- `status.py`, `errors.py`: status checks and shared exceptions.

From the repository root, install the development version before running tests:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests import the installed package directly and invoke the installed `pfs2bos` command without modifying `sys.path`. Before a release, also install the built wheel and run the same tests to confirm that all modules are included in the package.

Tests use a real rclone local backend in temporary directories that are automatically cleaned up. They cover path preservation, Chinese characters/spaces, empty files, exclusion rules, Git tracking, same-name conflicts with different content of equal length, upload/verification/mount failures, source file changes, interruptions before and after symlink and restoration switches, reruns, locking, and restoration safeguards. They do not use real BOS credentials or migrate existing models/data. End-to-end acceptance testing of BOS and the mount must be performed during a small manual trial.
