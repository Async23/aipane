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
