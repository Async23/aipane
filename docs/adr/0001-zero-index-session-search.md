# ADR-0001: Zero-Index Time-Pruned Local Session Search

- **Status**: Accepted (2026-11-19)

## Context

`aipane` orchestrates 7 coding-agent CLIs whose native session logs live in heterogeneous formats (plain JSONL, concatenated `.zstd` frames for dsh-TUI, per-workspace directories, and nested subagent files). Cross-agent handoffs and historical lookup require three orthogonal operations: exact session-ID export, recent metadata listing, and content search.

## Decision

Implement `bin/aipane-sessions` (`lib/agent_sessions.py`) as a self-contained, zero-index CLI:

1. **No persistent full-text index or background daemon**: avoid duplicating multi-gigabyte logs into a derived database and eliminate stale-index drift.
2. **Temporal locality pruning by default**: `list` and `find` sort by `mtime` descending and scan a 14-day hot window unless `--all` is passed, always returning an explicit `scanned_scope` block so callers never mistake a windowed miss for global absence.
3. **Delegate heavy text/compression work to native tools**: use `rg -z` for parallel plain + `.zstd` content search with character-window snippet truncation, and `zstd -dcq` for lossless streaming export with SHA-256 verification.

## Trade-offs

- **Gains**: zero space amplification, zero maintenance overhead, 100% freshness on live sessions, and no external indexer binary dependency.
- **Costs**: unscoped `--all` content searches across multi-gigabyte cold histories take seconds rather than sub-second inverted-index lookups. If sub-second cold-corpus search ever becomes a frequent requirement, a pluggable index backend can sit behind `find` without changing the CLI or skill contract.

## Addendum (2026-11-23): Logical Sessions, Physical Segments, and Completeness Verification

Real-world Codex usage exposed that upstream CLIs do not preserve a 1:1 mapping between a logical conversation and a single on-disk file: on turn rollback/rewind or fork, Codex opens a new suffixed rollout segment (`rollout-<ts>-<session_id>_<rollout_id>.jsonl[.zst]`) with `session_meta.payload.history_base` pointing to the parent physical rollout ID and exclusive bounds (`end_ordinal_exclusive`, `end_byte_offset`), and may compress or move older logs to `archived_sessions/`.

To uphold zero-index freshness without silent truncation:

1. **Filenames are pre-filters, never final identity**: `list` and `find` prune cold sessions by `mtime` before reading headers and deduplicate physical files by `(dev, inode)` (preferring plain `.jsonl` over `.jsonl.zst`). Multi-UUID or non-standard Codex/Pi filenames resolve authoritative identity from the file header (`session_meta.payload.id` up to 4 MiB via `lib/codex_sessions.py`, shared across recovery, live pane resolution, activity tracking, history lookup, and notifications), while excluding uncompressed DSH logs from Pi discovery.
2. **Separate `LogicalSession` from physical segments and follow `history_base` lineage**: `list` and `find` aggregate, rank, and time-prune by logical session; `read --view timeline` starts from the identity-verified committed head (`state_*.sqlite` > `active_path` > latest segment) and follows `history_base` chains (including forked parent prefixes) so uncommitted rollback files never truncate live history, falling back to timestamp/filename-timestamp ordering + suffix-minimum `ordinal` pruning (opt-in `--include-superseded`). `--view export` hashes and copies raw bytes (preserving CRLF) into a temporary staging directory before atomic publish, recording ordinal/byte bounds in `manifest.json`.
3. **Verify completeness instead of hardcoding `complete: true`**: `read --pane` validates `--agent` consistency and propagates the live pane's `active_path` and `storage_root` (`CODEX_HOME`). Codex reads cross-check `state_*.sqlite` (root and `sqlite/` subdir) for every included Codex SID (primary and children), rejecting SID-conflicting anchors and surfacing missing rollouts, malformed trailing lines, unreadable/oversized headers, and ambiguous legacy boundaries via `complete: false` + `warnings`. `aipane-doctor` independently audits recent files, SQLite coverage/schema health, and live panes for layout drift.
