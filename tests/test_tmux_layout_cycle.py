"""Unit and live tmux regression tests for bin/tmux-layout-cycle."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "bin" / "tmux-layout-cycle"
TMUX_WS = ROOT / "conf" / "tmux-workstation.conf"


def _load_module():
    import sys

    loader = importlib.machinery.SourceFileLoader("tmux_layout_cycle", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


MOD = _load_module()


class LayoutCycleUnitTests(unittest.TestCase):
    def test_column_counts_balanced_ceil_sqrt(self) -> None:
        self.assertEqual(MOD.column_counts(4), [2, 2])
        self.assertEqual(MOD.column_counts(5), [2, 2, 1])
        self.assertEqual(MOD.column_counts(6), [2, 2, 2])
        self.assertEqual(MOD.column_counts(7), [3, 2, 2])
        self.assertEqual(MOD.column_counts(8), [3, 3, 2])
        self.assertEqual(MOD.column_counts(9), [3, 3, 3])
        self.assertEqual(MOD.column_counts(10), [3, 3, 2, 2])
        self.assertEqual(MOD.column_counts(12), [3, 3, 3, 3])

    def test_v1_and_v2_parsers_classify_identically(self) -> None:
        # 4-pane column-major grid built in v1 format
        v1_col4 = MOD.build_column_tiled_v1(158, 39, [10, 11, 12, 13])
        assert v1_col4 is not None
        self.assertEqual(MOD.classify_layout(MOD.parse_layout(v1_col4), 4), "column-tiled")

        # Equivalent v2 JSON with manually resized widths/heights
        v2_col4_resized = json.dumps(
            {
                "V": 2,
                "L": {
                    "t": "h",
                    "w": 158,
                    "h": 39,
                    "c": [
                        {"t": "v", "c": [{"t": "p"}, {"t": "p"}]},
                        {"t": "v", "c": [{"t": "p"}, {"t": "p"}]},
                    ],
                },
            }
        )
        self.assertEqual(MOD.classify_layout(MOD.parse_layout(v2_col4_resized), 4), "column-tiled")

        # 4-pane main-vertical (v1 & v2)
        v1_mv4 = "abcd,158x39,0,0{78x39,0,0,1,79x39,79,0[79x12,79,0,2,79x12,79,13,3,79x13,79,26,4]}"
        self.assertEqual(MOD.classify_layout(MOD.parse_layout(v1_mv4), 4), "main-vertical")

        # 4-pane row-major tiled is outside curated states -> "other" -> normalizes to column-tiled
        v1_tiled4 = "abcd,158x39,0,0[158x19,0,0{78x19,0,0,1,79x19,79,0,2},158x19,0,20{78x19,0,20,3,79x19,79,20,4}]"
        self.assertEqual(MOD.classify_layout(MOD.parse_layout(v1_tiled4), 4), "other")
        self.assertEqual(MOD.next_target("other", 4), "column-tiled")

        # Nested manual split h[v(1,2), h(3,4)] -> "other" -> column-tiled
        v2_nested = json.dumps(
            {
                "V": 2,
                "L": {
                    "t": "h",
                    "c": [
                        {"t": "v", "c": [{"t": "p"}, {"t": "p"}]},
                        {"t": "h", "c": [{"t": "p"}, {"t": "p"}]},
                    ],
                },
            }
        )
        self.assertEqual(MOD.classify_layout(MOD.parse_layout(v2_nested), 4), "other")

        # 5-pane non-canonical column distribution [2, 1, 2] -> "other" -> canonical [2, 2, 1]
        v2_noncanon_5 = json.dumps(
            {
                "V": 2,
                "L": {
                    "t": "h",
                    "c": [
                        {"t": "v", "c": [{"t": "p"}, {"t": "p"}]},
                        {"t": "p"},
                        {"t": "v", "c": [{"t": "p"}, {"t": "p"}]},
                    ],
                },
            }
        )
        self.assertEqual(MOD.classify_layout(MOD.parse_layout(v2_noncanon_5), 5), "other")

    def test_build_column_tiled_v1_rejects_tiny_spans(self) -> None:
        self.assertIsNone(MOD.build_column_tiled_v1(2, 39, [1, 2, 3, 4]))
        self.assertIsNone(MOD.build_column_tiled_v1(158, 2, [1, 2, 3, 4]))
        self.assertIsNone(MOD.build_column_tiled_v1(158, 39, [1, 2, 3]))


class LayoutCycleLiveTmuxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.socket = f"layout-cycle-{os.getpid()}-{id(self)}-{time.time_ns()}"
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = os.environ.copy()
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.env["HOME"] = self.temp_dir.name
        self.env["TERM"] = "xterm-256color"

    def tearDown(self) -> None:
        subprocess.run(
            ["tmux", "-L", self.socket, "kill-server"],
            check=False,
            capture_output=True,
            env=self.env,
        )
        self.temp_dir.cleanup()

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            check=check,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def run_cycle(self, window_id: str) -> None:
        server_socket = self.tmux("display-message", "-p", "#{socket_path}").stdout.strip()
        cycle_env = self.env.copy()
        cycle_env["TMUX"] = f"{server_socket},0,0"
        proc = subprocess.run(
            [str(SCRIPT_PATH), window_id],
            check=False,
            capture_output=True,
            text=True,
            env=cycle_env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def start_window(self, pane_count: int, width: int = 158, height: int = 39) -> str:
        self.tmux(
            "-f", "/dev/null", "new-session", "-d", "-s", "demo",
            "-x", str(width), "-y", str(height), "sleep 60",
        )
        self.tmux("source-file", str(TMUX_WS))
        wid = self.tmux("display-message", "-p", "-t", "demo:1", "#{window_id}").stdout.strip()
        for _ in range(pane_count - 1):
            self.tmux("split-window", "-t", wid, "sleep 60")
            self.tmux("select-layout", "-t", wid, "tiled")
        return wid

    def pane_columns(self, window_id: str) -> list[list[tuple[int, str]]]:
        lines = self.tmux(
            "list-panes",
            "-t",
            window_id,
            "-F",
            "#{pane_index} #{pane_id} #{pane_left} #{pane_top}",
        ).stdout.splitlines()
        by_left: dict[int, list[tuple[int, int, str]]] = {}
        for line in lines:
            idx_s, pid, left_s, top_s = line.split()
            by_left.setdefault(int(left_s), []).append((int(top_s), int(idx_s), pid))
        result: list[list[tuple[int, str]]] = []
        for left in sorted(by_left):
            col = sorted(by_left[left], key=lambda item: item[0])
            result.append([(idx, pid) for _, idx, pid in col])
        return result

    def test_4_pane_column_major_order_and_full_cycle(self) -> None:
        wid = self.start_window(4)
        self.tmux("select-layout", "-t", wid, "even-horizontal")
        initial_ids = self.tmux("list-panes", "-t", wid, "-F", "#{pane_id}").stdout.splitlines()

        # even-horizontal -> main-vertical
        self.run_cycle(wid)
        cols = self.pane_columns(wid)
        self.assertEqual([len(c) for c in cols], [1, 3])
        self.assertEqual([pid for col in cols for _, pid in col], initial_ids)

        # main-vertical -> column-tiled (left: 1, 2; right: 3, 4)
        self.run_cycle(wid)
        cols = self.pane_columns(wid)
        self.assertEqual([len(c) for c in cols], [2, 2])
        self.assertEqual(
            [[idx for idx, _ in col] for col in cols],
            [[1, 2], [3, 4]],
        )
        self.assertEqual([pid for col in cols for _, pid in col], initial_ids)

        # column-tiled -> even-horizontal
        self.run_cycle(wid)
        cols = self.pane_columns(wid)
        self.assertEqual([len(c) for c in cols], [1, 1, 1, 1])
        self.assertEqual([pid for col in cols for _, pid in col], initial_ids)

    def test_row_major_tiled_and_resized_column_tiled_transitions(self) -> None:
        wid = self.start_window(4)
        # Start from row-major tiled (1|2 over 3|4)
        self.tmux("select-layout", "-t", wid, "tiled")
        before_ids = self.tmux("list-panes", "-t", wid, "-F", "#{pane_id}").stdout.splitlines()

        # One press normalizes row-major tiled directly to column-major 2x2 preserving 1..4 order
        self.run_cycle(wid)
        cols = self.pane_columns(wid)
        self.assertEqual([[idx for idx, _ in col] for col in cols], [[1, 2], [3, 4]])
        self.assertEqual([pid for col in cols for _, pid in col], before_ids)

        # Manually resize pane 1 height and width; still recognized as column-tiled -> advances to even-horizontal
        self.tmux("resize-pane", "-t", f"{wid}.1", "-D", "4")
        self.tmux("resize-pane", "-t", f"{wid}.1", "-R", "6")
        self.run_cycle(wid)
        cols = self.pane_columns(wid)
        self.assertEqual([len(c) for c in cols], [1, 1, 1, 1])

    def test_swapped_pane_order_preserved_in_column_grid(self) -> None:
        wid = self.start_window(4)
        self.tmux("select-layout", "-t", wid, "tiled")
        self.tmux("swap-pane", "-s", f"{wid}.2", "-t", f"{wid}.3")
        ordered_ids = self.tmux("list-panes", "-t", wid, "-F", "#{pane_id}").stdout.splitlines()

        self.run_cycle(wid)
        cols = self.pane_columns(wid)
        self.assertEqual([[idx for idx, _ in col] for col in cols], [[1, 2], [3, 4]])
        self.assertEqual([pid for col in cols for _, pid in col], ordered_ids)

    def test_5_to_9_panes_column_distributions(self) -> None:
        expected = {
            5: [[1, 2], [3, 4], [5]],
            6: [[1, 2], [3, 4], [5, 6]],
            7: [[1, 2, 3], [4, 5], [6, 7]],
            8: [[1, 2, 3], [4, 5, 6], [7, 8]],
            9: [[1, 2, 3], [4, 5, 6], [7, 8, 9]],
        }
        wid = self.start_window(5)
        for pane_count, want_cols in expected.items():
            while int(self.tmux("display-message", "-p", "-t", wid, "#{window_panes}").stdout.strip()) < pane_count:
                self.tmux("split-window", "-t", wid, "sleep 60")
                self.tmux("select-layout", "-t", wid, "tiled")
            self.tmux("select-layout", "-t", wid, "main-vertical")
            self.run_cycle(wid)
            cols = self.pane_columns(wid)
            self.assertEqual(
                [[idx for idx, _ in col] for col in cols],
                want_cols,
                f"pane_count={pane_count}",
            )
            # Next press advances from column-tiled to even-horizontal
            self.run_cycle(wid)
            self.assertEqual(len(self.pane_columns(wid)), pane_count)

    def test_zoomed_window_unzooms_and_cycles(self) -> None:
        wid = self.start_window(4)
        self.tmux("select-layout", "-t", wid, "main-vertical")
        self.tmux("resize-pane", "-Z", "-t", f"{wid}.2")
        self.assertEqual(
            self.tmux("display-message", "-p", "-t", wid, "#{window_zoomed_flag}").stdout.strip(),
            "1",
        )
        self.run_cycle(wid)
        self.assertEqual(
            self.tmux("display-message", "-p", "-t", wid, "#{window_zoomed_flag}").stdout.strip(),
            "0",
        )
        self.assertEqual([[idx for idx, _ in col] for col in self.pane_columns(wid)], [[1, 2], [3, 4]])

    def test_3_panes_keeps_row_tiled_and_normalizes_non_curated(self) -> None:
        wid = self.start_window(3)
        self.tmux("select-layout", "-t", wid, "even-vertical")
        self.run_cycle(wid)
        # Non-curated 3-pane layout normalizes to tiled (2 on top, 1 on bottom)
        layout = self.tmux("display-message", "-p", "-t", wid, "#{window_layout}").stdout.strip()
        self.assertEqual(MOD.classify_layout(MOD.parse_layout(layout), 3), "tiled")
        self.run_cycle(wid)
        layout = self.tmux("display-message", "-p", "-t", wid, "#{window_layout}").stdout.strip()
        self.assertEqual(MOD.classify_layout(MOD.parse_layout(layout), 3), "even-horizontal")


if __name__ == "__main__":
    unittest.main()
