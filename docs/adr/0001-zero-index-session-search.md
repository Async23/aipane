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

Real-world Codex usage exposed that upstream CLIs do not preserve a 1:1 mapping between a logical conversation and a single on-disk file: on turn rollback/rewind, Codex opens a new suffixed rollout segment (`rollout-<ts>-<session_id>_<rollout_id>.jsonl`) whose `ordinal` restarts from the rolled-back boundary, and may move older logs to `archived_sessions/`.

To uphold zero-index freshness without silent truncation:

1. **Filenames are pre-filters, never final identity**: single-UUID Codex/Pi filenames keep a fast path, while multi-UUID or non-standard filenames resolve authoritative identity from the file header (`session_meta.payload.id` via `lib/codex_sessions.py`, shared across recovery, live pane resolution, history lookup, and notifications).
2. **Separate `LogicalSession` from physical segments**: `list` and `find` aggregate and time-prune by logical session; `read --view timeline` orders Codex segments by creation timestamp and prunes superseded rollback tails by suffix-minimum `ordinal` (opt-in `--include-superseded`), while `--view export` stays byte-lossless and records ordinal bounds in `manifest.json`.
3. **Verify completeness instead of hardcoding `complete: true`**: `read --pane` propagates the live `active_path` resolved via `lsof`, and Codex reads cross-check existing `state_*.sqlite` `rollout_path` entries read-only. Any uncovered file sets `complete: false` with explicit `warnings` and is recovered into the output; `aipane-doctor` audits recent files for layout drift.
