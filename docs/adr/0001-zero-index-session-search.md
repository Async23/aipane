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

### Known Boundaries and Zero-Index Trade-offs

To keep the zero-index runtime small, fast, and free of a secondary database, the following low-probability synthetic/extreme corner cases are intentionally treated as documented boundaries rather than solved with heavier indexing or full-file re-decoding:

1. **Ultra-deep recursive fork chains**: Multi-level fork lineage verification walks physical `history_base` chains with cycle detection and memoized bounds rather than a secondary graph index. Deep chains remain bounded by Python's recursion limit and the in-memory rollout index; arbitrarily deep synthetic fork trees (e.g., dozens to hundreds of empty intermediate forks) are outside the primary design target.
2. **Sub-hour DST fall-back precision in foreign timezones**: The once-per-year IANA DST fold cache (`_iana_year_fold_table`) indexes fall-back transitions at hour granularity so cross-timezone reads stay O(1) per lookup with a single IANA enumeration per year. For rare sub-hour DST zones (such as `Australia/Lord_Howe`'s 30-minute fall-back at `01:30–01:59`), reading a log missing UTC `session_meta.timestamp` from a *different* host timezone may conservatively emit `ambiguous_timestamp_order` across the whole local hour (`01:00–01:59`).
3. **Artificially stacked multi-DB conflicts and partial cold-group remainders**:
   - Reading (`read`/`list`/`preview`) scans all `state_*.sqlite` versions highest-to-lowest (recording upper-DB conflict warnings while falling back to a verified lower-DB committed head), whereas native recovery (`ai-restore` / `codex resume <SID>`) checks only the highest-version DB and standard session roots because Codex itself does not read lower schema DBs.
   - Under default 14-day time pruning, when a hot session's `history_base` pulls only *part* of a multi-file cold rough group via `cold_by_rid`, remaining unlinked historical rollouts in that cold rough group are not exhaustively header-scanned until `--all` is passed, so `skipped_cold_sessions` may conservatively retain that rough group.
4. **Corrupt zstd trailers or mid-line byte cuts in Codex Goal notification checks**: Desktop notification Goal suppression (`lib/agent_notifications.py`) streams compressed rollouts with `zstd -dcq` up to the declared `history_base` boundary for low latency. A synthetic `.jsonl.zst` file whose prefix up to the exclusive boundary decodes cleanly while trailing bytes *past* the boundary contain a corrupt zstd frame—or a malformed `end_byte_offset` slicing mid-line before `\n`—may disagree with full `timeline` reads (`unreadable_source`).
5. **Extreme JSON key+value Unicode escaping in compressed DSH tail previews**: Bounded compressed preview (`_line_may_be_turn`) uses byte-level pre-filtering plus structural value-escape hints to keep preview memory O(limit) and prevent ANSI `\u001b` tool bursts from evicting real turns. Synthetic DSH logs that Unicode-escape structural *keys* together with values (e.g. `"t\u0079pe":"assi\u0073tant/message"`) or escape non-turn type strings (`tool\u002fresult`) are not JSON-parsed line-by-line during bounded compressed preview; full `read` always decodes complete RFC 8259 JSON accurately.
6. **Shared-ancestor SQLite lookups in `list` previews**: `list` batches all hot Codex SIDs into a single SQLite pass per DB and resolves fork ancestors primarily from the in-memory rollout index (`__sids__`/`__rids__`). If a fork ancestor lives strictly outside static discovery roots (e.g. only in a custom SQLite-only directory) and is shared across many listed grandchildren, fallback verification may perform additional read-only SQLite lookups per distinct ancestor.
