"""Regression tests for tmux server-scoped AI session restoration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "bin" / "aipane-snapshot"
RESTORE = ROOT / "bin" / "ai-restore"

VALID_CODEX_SID = "019fdad7-16c3-7a13-89cf-ec8c2184e5f7"
FOREIGN_SID = "1fa8ab6a-cba1-4189-abcd-64a29ffb2fc3"


class SessionRestoreTests(unittest.TestCase):
    def test_snapshot_ignores_newer_binding_from_another_tmux_server(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            state_dir = tmp / "state"
            state_dir.mkdir()
            registry = state_dir / "registry.jsonl"
            records = [
                {
                    "ts": 1,
                    "sock": "/private/tmp/tmux-main",
                    "srv": "999",
                    "pane": "%3",
                    "tool": "x",
                    "sid": VALID_CODEX_SID,
                },
                {
                    "ts": 2,
                    "sock": "aipane-test",
                    "srv": "aipane-test",
                    "pane": "%3",
                    "tool": "g",
                    "sid": FOREIGN_SID,
                },
            ]
            registry.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            fake_tmux = tmp / "tmux"
            fake_tmux.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    case "$1" in
                      display-message)
                        printf '/private/tmp/tmux-main\\t999\\n'
                        ;;
                      list-panes)
                        printf '%%3\\t0:4.1\\t/Users/test\\n'
                        ;;
                      *)
                        exit 99
                        ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "AIPANE_STATE_DIR": str(state_dir),
                    "AIPANE_REGISTRY": str(registry),
                    "AIPANE_TMUX": str(fake_tmux),
                }
            )
            subprocess.run(
                [sys.executable, str(SNAPSHOT)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            coords = json.loads((state_dir / "coords-last.json").read_text())
            self.assertEqual(
                coords["0:4.1"]["sid"],
                VALID_CODEX_SID,
                "a newer record from another tmux server must not win by pane id",
            )
            self.assertEqual(coords["0:4.1"]["tool"], "x")

    def test_restore_rejects_coord_binding_for_a_different_tool(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": FOREIGN_SID, "tool": "g"},
                title=VALID_CODEX_SID,
                saved_codex_ids={VALID_CODEX_SID},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                f"codex --yolo --disable plugins resume {VALID_CODEX_SID}",
                result.stdout,
            )
            self.assertNotIn(FOREIGN_SID, result.stdout)

    def test_restore_migrates_retired_codex_override(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": VALID_CODEX_SID, "tool": "x"},
                title="custom-title",
                saved_codex_ids={VALID_CODEX_SID},
                extra_env={"AIPANE_CODEX_LAUNCH_CMD": "codexx"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                f"codex --yolo --disable plugins resume {VALID_CODEX_SID}",
                result.stdout,
            )
            self.assertNotIn("codexx resume", result.stdout)

    def test_restore_marks_missing_codex_session_invalid(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_restore(
                tmp,
                coord={"sid": FOREIGN_SID, "tool": "x"},
                title="custom-title",
                saved_codex_ids=set(),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            matching_line = next(
                line for line in result.stdout.splitlines() if "0:4.1" in line
            )
            self.assertIn("invalid", matching_line)

    def test_successful_restore_rebinds_session_to_the_new_pane_id(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            home = tmp / "home"
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            tmux_log = tmp / "tmux.log"
            bind_log = tmp / "bind.log"

            fake_tmux = local_bin / "tmux"
            fake_tmux.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    case "$1" in
                      has-session)
                        exit 0
                        ;;
                      display-message)
                        case "$*" in
                          *pane_current_command*) printf 'zsh\\n' ;;
                          *pane_id*) printf '%%42\\n' ;;
                          *socket_path*) printf '/private/tmp/tmux-test\\n' ;;
                          *"#{pid}"*) printf '4242\\n' ;;
                        esac
                        ;;
                      send-keys)
                        printf '%s\\n' "$*" >> "$TMUX_LOG"
                        ;;
                      *)
                        exit 99
                        ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)

            fake_bind = local_bin / "aipane-bind"
            fake_bind.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BIND_LOG\"\n",
                encoding="utf-8",
            )
            fake_bind.chmod(0o755)

            result = self.run_restore(
                tmp,
                coord={"sid": VALID_CODEX_SID, "tool": "x"},
                title="custom-title",
                saved_codex_ids={VALID_CODEX_SID},
                dry_run=False,
                extra_env={
                    "HOME": str(home),
                    "AIPANE_TMUX": str(fake_tmux),
                    "TMUX_LOG": str(tmux_log),
                    "BIND_LOG": str(bind_log),
                    "AI_RESTORE_SETTLE_DELAY": "0",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "send-keys -t 0:4.1 "
                f"codex --yolo --disable plugins resume {VALID_CODEX_SID} Enter",
                tmux_log.read_text(),
            )
            binding = bind_log.read_text()
            self.assertIn(f"--tool x --sid {VALID_CODEX_SID}", binding)
            self.assertIn("--pane %42", binding)

    def run_restore(
        self,
        tmp: Path,
        *,
        coord: dict[str, str],
        title: str,
        saved_codex_ids: set[str],
        dry_run: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        state_dir = tmp / "state"
        state_dir.mkdir()
        (state_dir / "coords-last.json").write_text(
            json.dumps({"0:4.1": coord}),
            encoding="utf-8",
        )

        codex_home = tmp / "codex"
        sessions_dir = codex_home / "sessions" / "2026" / "08" / "09"
        sessions_dir.mkdir(parents=True)
        for sid in saved_codex_ids:
            (sessions_dir / f"rollout-2026-08-09T00-00-00-{sid}.jsonl").touch()

        dump = tmp / "resurrect.txt"
        dump.write_text(
            "\t".join(
                [
                    "pane",
                    "0",
                    "4",
                    "0",
                    ":-",
                    "1",
                    title,
                    f":{tmp}",
                    "1",
                    "codex",
                    ":codex --yolo",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.update(
            {
                "AIPANE_STATE_DIR": str(state_dir),
                "AIPANE_CODEX_LAUNCH_CMD": "codex --yolo --disable plugins",
                "CODEX_HOME": str(codex_home),
            }
        )
        if extra_env:
            env.update(extra_env)
        args = ["zsh", str(RESTORE)]
        if dry_run:
            args.append("--dry-run")
        args.extend(["--dump", str(dump)])
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )


if __name__ == "__main__":
    unittest.main()
