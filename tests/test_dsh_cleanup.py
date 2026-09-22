"""Exercise cleanup selection with isolated process tables; never kill processes."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DshMcpCleanupTests(unittest.TestCase):
    def run_cleanup(self, root_command, *, root_age="30:00",
                    helper_parent=999990, arguments=()):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            # All fixture IDs are well outside the host's process table. Every
            # invocation is dry-run even if selection regresses.
            snapshot = (
                f"999990 1 ?? {root_age} {root_command}\n"
                f"999991 {helper_parent} ?? 30:00 node /tmp/chrome-devtools-mcp/index.js\n"
            )
            fixture = home / "processes"
            fixture.write_text(snapshot)
            ps = fake_bin / "ps"
            ps.write_text(
                "#!/usr/bin/env python3\n"
                "import os,sys\n"
                "from pathlib import Path\n"
                "for line in Path(os.environ['AIPANE_PS_FIXTURE']).read_text().splitlines():\n"
                " p,pp,tty,age,cmd=line.split(maxsplit=4)\n"
                " fields=sys.argv[-1]\n"
                " if fields=='pid=,ppid=,tty=,etime=,command=': print(line)\n"
            )
            ps.chmod(0o755)
            result = subprocess.run(
                [str(ROOT / "bin/aipane-cleanup"), "mcp", "--dry-run", "--verbose",
                 "--max-age", "60", *arguments],
                text=True, capture_output=True, check=False,
                env={**os.environ, "HOME": str(home),
                     "PATH": f"{fake_bin}:{os.environ['PATH']}",
                     "AIPANE_TMUX_BIN": "/usr/bin/false",
                     "AIPANE_PS_FIXTURE": str(fixture)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_live_dsh_ancestor_without_tty_protects_mcp(self):
        for command in (
            "/Users/test/.local/bin/dsh --profile web",
            "node /Users/test/.local/bin/dsh-tui",
            "node /tmp/node_modules/@deepseek-ai/dsh/lib/bin.js",
            "node /tmp/node_modules/@deepseek-harness-tui/dsh-tui/bin/dsh-tui.js",
            "npx --yes @deepseek-ai/dsh",
        ):
            with self.subTest(command=command):
                self.assertNotIn("would kill", self.run_cleanup(command))

    def test_unrelated_argument_does_not_protect_mcp(self):
        for command in (
            "node worker.js --label dsh",
            "node -e dsh",
            "node /tmp/node_modules/@deepseek-ai/dsh-tools/lib/bin.js",
            "npm install @deepseek-ai/dsh",
            "npx echo dsh",
        ):
            with self.subTest(command=command):
                self.assertIn("999991", self.run_cleanup(command))

    def test_explicit_session_age_can_collect_expired_dsh_mcp(self):
        self.assertIn("999991", self.run_cleanup(
            "node /Users/test/.local/bin/dsh", arguments=("--session-age", "60"),
        ))
        self.assertNotIn("would kill", self.run_cleanup(
            "node /Users/test/.local/bin/dsh", root_age="00:30",
            arguments=("--session-age", "60"),
        ))

    def test_missing_ancestor_and_orphan_mcp_still_clean_up(self):
        for parent in (1, 999989):
            with self.subTest(parent=parent):
                self.assertIn("999991", self.run_cleanup(
                    "dsh", helper_parent=parent,
                ))

    def test_force_targets_mcp_helper_without_selecting_dsh_host(self):
        output = self.run_cleanup(
            "node /Users/test/.local/bin/dsh", arguments=("--force",),
        )
        self.assertIn("999991", output)
        self.assertNotIn("999990", output)


if __name__ == "__main__":
    unittest.main()
