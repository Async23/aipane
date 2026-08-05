"""Structural tests for public Ghostty/tmux workstation fragments.

Drives the shipped conf files under conf/ (not re-implemented copies).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
README_CN = ROOT / "README_CN.md"
GHOSTTY_CONF = ROOT / "conf" / "ghostty-tmux.conf"
TMUX_WS = ROOT / "conf" / "tmux-workstation.conf"
TMUX_WRAP = ROOT / "conf" / "tmux-window-wrap.conf"
WRAP_BIN = ROOT / "bin" / "tmux-window-wrap"
WINDOW_JUMP_BIN = ROOT / "bin" / "tmux-window-jump"
RENAME_BIN = ROOT / "bin" / "tmux-rename-window-popup"
COLOUR_PALETTE_BIN = ROOT / "bin" / "tmux-colour-palette"
CHEATSHEET = ROOT / "docs" / "cheatsheet.md"
WS_DOC = ROOT / "docs" / "ghostty-tmux-workstation.md"


class GhosttyFragmentTests(unittest.TestCase):
    def test_ghostty_fragment_defines_one_shot_cmd_bridge_and_shift_enter(self):
        text = GHOSTTY_CONF.read_text(encoding="utf-8")
        self.assertIn("keybind = cmd+s=text:\\x00", text)
        self.assertNotIn("keybind = cmd+s=unbind", text)
        self.assertIn("keybind = super+alt+p=text:\\x00Q", text)
        self.assertIn("keybind = cmd+t=text:\\x00c", text)
        self.assertIn("keybind = cmd+d=text:\\x00|", text)
        self.assertIn("keybind = shift+enter=text:\\x0a", text)
        self.assertIn("macos-option-as-alt = true", text)
        for digit in range(10):
            self.assertIn(
                f"keybind = cmd+digit_{digit}=text:\\x00{digit}", text
            )
        # public fragment must not pin personal fonts
        self.assertNotIn("font-family", text)


class TmuxWorkstationFragmentTests(unittest.TestCase):
    def test_workstation_sets_c_space_prefix_and_broadcast(self):
        text = TMUX_WS.read_text(encoding="utf-8")
        self.assertIn("set -g prefix C-Space", text)
        self.assertIn("synchronize-panes", text)
        self.assertIn("window_zoomed_flag", text)
        self.assertIn("bind v copy-mode", text)
        self.assertIn("bind P display-popup", text)
        self.assertIn("bind Q display-popup", text)
        self.assertIn("bind , run-shell -C", text)
        self.assertIn("AIPANE_RENAME_WINDOW_TARGET=#{window_id}", text)
        self.assertIn("-w 52 -h 5", text)
        self.assertIn('-T "#[align=centre] Rename window #I "', text)
        self.assertIn("tmux-rename-window-popup", text)
        self.assertIn("tmux-colour-palette", text)
        self.assertIn("tmux-shot-capture", text)
        self.assertTrue(WINDOW_JUMP_BIN.is_file())
        self.assertTrue(os.access(WINDOW_JUMP_BIN, os.X_OK))
        self.assertTrue(RENAME_BIN.is_file())
        self.assertTrue(COLOUR_PALETTE_BIN.is_file())
        # stay a fragment: no TPM / no personal home paths
        self.assertNotIn("@plugin", text)
        self.assertNotIn("/Users/", text)

    def test_window_wrap_fragment_and_binary_exist(self):
        self.assertTrue(TMUX_WRAP.is_file())
        self.assertTrue(WRAP_BIN.is_file())
        wrap = TMUX_WRAP.read_text(encoding="utf-8")
        self.assertIn("tmux-window-wrap render", wrap)
        self.assertIn("status-format[0]", wrap)

    def test_workstation_sources_cleanly_in_tmux(self):
        socket = f"ws-frag-{Path(__file__).stat().st_mtime_ns}"
        try:
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-s",
                    "t",
                    "sleep 5",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["tmux", "-L", socket, "source-file", str(TMUX_WS)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["tmux", "-L", socket, "source-file", str(TMUX_WRAP)],
                check=True,
                capture_output=True,
                text=True,
            )
            prefix = subprocess.run(
                ["tmux", "-L", socket, "display-message", "-p", "#{prefix}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(prefix, "C-Space")
            jump_timeout = subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "show-options",
                    "-g",
                    "-v",
                    "@tmux-window-jump-timeout-ms",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(jump_timeout, "700")
            keys = subprocess.run(
                ["tmux", "-L", socket, "list-keys", "-T", "prefix"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertRegex(keys, re.compile(r"prefix\s+B\s+.*synchronize-panes"))
            self.assertRegex(keys, re.compile(r"prefix\s+v\s+.*copy-mode"))
            self.assertRegex(
                keys,
                re.compile(
                    r"prefix\s+P\s+.*display-popup.*tmux-colour-palette"
                ),
            )
            self.assertRegex(keys, re.compile(r"prefix\s+Q\s+.*display-popup"))
            for digit in range(1, 10):
                self.assertRegex(
                    keys,
                    re.compile(
                        rf"prefix\s+{digit}\s+.*run-shell"
                        rf".*tmux-window-jump.*select {digit}"
                        rf".*select-window.*:={digit}"
                    ),
                )
            self.assertRegex(
                keys,
                re.compile(
                    r"prefix\s+0\s+.*run-shell.*tmux-window-jump.*end"
                    r".*select-window.*\{end\}"
                ),
            )
            self.assertRegex(
                keys,
                re.compile(
                    r"prefix\s+,\s+.*run-shell -C.*display-popup"
                    r".*align=centre"
                    r".*AIPANE_RENAME_WINDOW_TARGET=#\{window_id\}"
                    r".*tmux-rename-window-popup"
                ),
            )
            fmt0 = subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "show-options",
                    "-g",
                    "-v",
                    "status-format[0]",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("tmux-window-wrap", fmt0)
        finally:
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"],
                check=False,
                capture_output=True,
            )


class RenameWindowPopupTests(unittest.TestCase):
    def setUp(self):
        self.socket = f"rename-popup-{os.getpid()}-{id(self)}"
        subprocess.run(
            [
                "tmux",
                "-L",
                self.socket,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "rename",
                "-n",
                "original",
                "sleep 30",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.window_id = self.tmux(
            "display-message", "-p", "-t", "rename:0", "#{window_id}"
        ).stdout.strip()
        socket_path = self.tmux(
            "display-message", "-p", "#{socket_path}"
        ).stdout.strip()
        server_pid = self.tmux(
            "display-message", "-p", "#{pid}"
        ).stdout.strip()
        pane_id = self.tmux(
            "display-message", "-p", "-t", "rename:0", "#{pane_id}"
        ).stdout.strip()
        self.tmux_environment = f"{socket_path},{server_pid},{pane_id.lstrip('%')}"
        self.pane_id = pane_id

    def tearDown(self):
        subprocess.run(
            ["tmux", "-L", self.socket, "kill-server"],
            check=False,
            capture_output=True,
        )

    def tmux(self, *args):
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def run_helper(self, fake_fzf_body, new_name=None, target=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_fzf = Path(temp_dir) / "fzf"
            fake_fzf.write_text(
                "#!/bin/sh\n" + fake_fzf_body,
                encoding="utf-8",
            )
            fake_fzf.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "TMUX": self.tmux_environment,
                    "TMUX_PANE": self.pane_id,
                    "AIPANE_FZF_BIN": str(fake_fzf),
                }
            )
            environment["AIPANE_RENAME_WINDOW_TARGET"] = (
                self.window_id if target is None else target
            )
            if new_name is not None:
                environment["AIPANE_TEST_NEW_NAME"] = new_name

            return subprocess.run(
                [str(RENAME_BIN)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

    def test_helper_renames_stable_window_id_with_literal_name(self):
        new_name = "  -release $HOME; 'quoted' 中文  "
        result = self.run_helper(
            "printf '%s\\n' \"$AIPANE_TEST_NEW_NAME\"\n",
            new_name=new_name,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        actual_name = self.tmux(
            "display-message", "-p", "-t", self.window_id, "#{window_name}"
        ).stdout.rstrip("\n")
        self.assertEqual(actual_name, new_name)

    def test_helper_uses_query_only_fzf_mode_without_blank_candidate(self):
        new_name = "query-only"
        result = self.run_helper(
            """
byte_count=$(wc -c | tr -d ' ')
[ "$byte_count" = 0 ] || exit 90
case "$*" in
  *enter:accept-or-print-query*) ;;
  *) exit 91 ;;
esac
printf '%s\\n' "$AIPANE_TEST_NEW_NAME"
""",
            new_name=new_name,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        actual_name = self.tmux(
            "display-message", "-p", "-t", self.window_id, "#{window_name}"
        ).stdout.rstrip("\n")
        self.assertEqual(actual_name, new_name)

    def test_helper_layout_uses_short_prompt_and_english_right_aligned_footer(self):
        text = RENAME_BIN.read_text(encoding="utf-8")

        self.assertIn("--prompt='› '", text)
        self.assertIn('--footer="$footer_text"', text)
        self.assertIn("--footer-border=none", text)
        self.assertIn("footer_hint='Enter Save · Esc Cancel'", text)
        self.assertIn("footer_hint_width=23", text)
        self.assertNotIn("--header=", text)
        self.assertNotIn("新名称", text)
        self.assertNotIn("保存", text)
        self.assertNotIn("取消", text)

    def test_helper_rejects_unexpanded_window_id(self):
        result = self.run_helper(
            "exit 99\n",
            target="#{window_id}",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid window target", result.stderr)

    def test_helper_cancel_keeps_window_name(self):
        result = self.run_helper("exit 130\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        actual_name = self.tmux(
            "display-message", "-p", "-t", self.window_id, "#{window_name}"
        ).stdout.rstrip("\n")
        self.assertEqual(actual_name, "original")


class ColourPalettePopupTests(unittest.TestCase):
    def test_print_mode_groups_ansi_rgb_cube_and_grayscale_colours(self):
        result = subprocess.run(
            [str(COLOUR_PALETTE_BIN), "--print"],
            check=True,
            capture_output=True,
            text=True,
        )

        colour_lines = [
            line
            for line in result.stdout.splitlines()
            if "\x1b[48;5;" in line
        ]
        colour_rows = [
            [int(value) for value in re.findall(r"\x1b\[48;5;(\d+)m", line)]
            for line in colour_lines
        ]

        self.assertIn("ANSI colours (0-15)", result.stdout)
        self.assertIn(
            "RGB cube (16-231): R blocks, G rows, B columns",
            result.stdout,
        )
        self.assertIn("Grayscale (232-255)", result.stdout)
        self.assertEqual(colour_rows[:2], [list(range(8)), list(range(8, 16))])

        expected_cube_rows = []
        for left_red in (0, 2, 4):
            right_red = left_red + 1
            for green in range(6):
                expected_cube_rows.append(
                    [
                        16 + 36 * left_red + 6 * green + blue
                        for blue in range(6)
                    ]
                    + [
                        16 + 36 * right_red + 6 * green + blue
                        for blue in range(6)
                    ]
                )
        self.assertEqual(colour_rows[2:20], expected_cube_rows)
        self.assertEqual(
            colour_rows[20:],
            [list(range(232, 244)), list(range(244, 256))],
        )

        all_colours = [colour for row in colour_rows for colour in row]
        self.assertEqual(len(all_colours), 256)
        self.assertEqual(sorted(all_colours), list(range(256)))

    def test_interactive_mode_starts_at_top_scrolls_and_quits(self):
        for exit_key in ("q", "Escape"):
            socket = f"palette-popup-{os.getpid()}-{id(self)}-{exit_key}"
            session = "palette"
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "-f",
                    "/dev/null",
                    "new-session",
                    "-d",
                    "-x",
                    "86",
                    "-y",
                    "20",
                    "-s",
                    session,
                    str(COLOUR_PALETTE_BIN),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                time.sleep(0.1)
                first_screen = subprocess.run(
                    ["tmux", "-L", socket, "capture-pane", "-p", "-t", session],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertEqual(
                    first_screen.splitlines()[0],
                    "tmux 256-colour palette",
                )

                subprocess.run(
                    ["tmux", "-L", socket, "send-keys", "-t", session, "Down"],
                    check=True,
                    capture_output=True,
                )
                time.sleep(0.1)
                self.assertEqual(
                    subprocess.run(
                        ["tmux", "-L", socket, "has-session", "-t", session],
                        check=False,
                        capture_output=True,
                    ).returncode,
                    0,
                    "Down must scroll rather than trigger the Esc binding",
                )

                subprocess.run(
                    ["tmux", "-L", socket, "send-keys", "-t", session, exit_key],
                    check=True,
                    capture_output=True,
                )
                time.sleep(0.2)
                self.assertNotEqual(
                    subprocess.run(
                        ["tmux", "-L", socket, "has-session", "-t", session],
                        check=False,
                        capture_output=True,
                    ).returncode,
                    0,
                )
            finally:
                subprocess.run(
                    ["tmux", "-L", socket, "kill-server"],
                    check=False,
                    capture_output=True,
                )


class DocsPointerTests(unittest.TestCase):
    def test_docs_exist_and_name_public_confs(self):
        self.assertTrue(CHEATSHEET.is_file())
        self.assertTrue(WS_DOC.is_file())
        doc = WS_DOC.read_text(encoding="utf-8")
        self.assertIn("conf/ghostty-tmux.conf", doc)
        self.assertIn("conf/tmux-workstation.conf", doc)
        self.assertIn("conf/tmux-window-wrap.conf", doc)
        self.assertIn("bin/tmux-rename-window-popup", doc)
        self.assertIn("bin/tmux-colour-palette", doc)
        self.assertIn("`Cmd+Opt+P`", doc)

        cheatsheet = CHEATSHEET.read_text(encoding="utf-8")
        self.assertIn("| `Cmd+S` | prefix only |", cheatsheet)
        self.assertIn("| `Cmd+Opt+P` | popup pane ID list |", cheatsheet)
        self.assertIn("| `Cmd+I` | centered popup rename window |", cheatsheet)
        self.assertIn(
            "| `prefix P` | indexed colour palette "
            "(`0–255`; `q`/`Esc` closes) |",
            cheatsheet,
        )

    def test_window_jump_install_option_and_repeat_behavior_are_documented(self):
        readme = README.read_text(encoding="utf-8")
        readme_cn = README_CN.read_text(encoding="utf-8")
        workstation = WS_DOC.read_text(encoding="utf-8")
        cheatsheet = CHEATSHEET.read_text(encoding="utf-8")

        for text in (readme, readme_cn, workstation, cheatsheet):
            self.assertIn("tmux-window-jump", text)
        self.assertIn("@tmux-window-jump-timeout-ms", workstation)
        self.assertIn("1 → 11 → 21", cheatsheet)
        self.assertIn("9 → 19 → 29", cheatsheet)


if __name__ == "__main__":
    unittest.main()
