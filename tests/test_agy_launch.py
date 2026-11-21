"""Unit and end-to-end PTY tests for Antigravity catalog, picker, routes, and recovery."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import agy_catalog
import agy_launch
import agy_sessions


LAUNCHER = ROOT / "bin/aipane-agy-launch"
SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

PLAN_MODELS_TSV = "\n".join([
    "gemini-4-neon-high\tGemini 4 Neon (High)",
    "gemini-4-neon-xhigh\tGemini 4 Neon (XHigh)",
    "gemini-4-neon-medium\tGemini 4 Neon (Medium)",
    "gemini-4-neon-low\tGemini 4 Neon (Low)",
    "gemini-4-carbon-medium\tGemini 4 Carbon (Medium)",
    "gemini-4-carbon-high\tGemini 4 Carbon (High)",
    "gemini-4-carbon-low\tGemini 4 Carbon (Low)",
    "gemini-4-argon-medium\tGemini 4 Argon (Medium)",
    "gemini-4-argon-high\tGemini 4 Argon (High)",
    "gemini-4-argon-low\tGemini 4 Argon (Low)",
    "gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)",
    "gemini-4-hydrogen-xhigh\tGemini 4 Hydrogen (XHigh)",
    "gemini-4-hydrogen-medium\tGemini 4 Hydrogen (Medium)",
    "gemini-4-hydrogen-low\tGemini 4 Hydrogen (Low)",
    "gemini-pro-orchestrator-neon\tOrchestrator (Neon)",
    "gemini-pro-orchestrator\tOrchestrator (Carbon)",
    "gemini-pro-orchestrator-argon\tOrchestrator (Argon)",
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)",
    "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)",
    "claude-opus-5-5-low\tClaude Opus 5.5 (Low)",
    "claude-opus-5-5-medium\tClaude Opus 5.5 (Medium)",
    "claude-opus-5-5-high\tClaude Opus 5.5 (High)",
    "claude-sonnet-5-5-low\tClaude Sonnet 5.5 (Low)",
    "claude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)",
    "claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)",
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)",
]) + "\n"

GCP_MODELS_TSV = "\n".join([
    "gemini-4-carbon-medium\tGemini 4 Carbon (Medium)",
    "gemini-4-carbon-high\tGemini 4 Carbon (High)",
    "gemini-4-carbon-low\tGemini 4 Carbon (Low)",
    "gemini-4-neon-high\tGemini 4 Neon (High)",
    "gemini-4-neon-xhigh\tGemini 4 Neon (XHigh)",
    "gemini-4-neon-medium\tGemini 4 Neon (Medium)",
    "gemini-4-neon-low\tGemini 4 Neon (Low)",
    "gemini-3.5-flash-medium\tGemini 3.5 Flash (Medium)",
    "gemini-3.5-flash\tGemini 3.5 Flash (High)",
    "gemini-3.5-flash-low\tGemini 3.5 Flash (Low)",
    "gemini-3.1-pro-low-thinking\tGemini 3.1 Pro (Low)",
    "gemini-3.1-flash-lite-preview\tGemini 3.1 Flash Lite",
    "gemini-3-flash-preview\tGemini 3 Flash",
    "claude-opus-5-5-max\tClaude Opus 5.5 (Max)",
    "claude-opus-5-5-high\tClaude Opus 5.5 (High)",
    "claude-opus-5-5-medium\tClaude Opus 5.5 (Medium)",
    "claude-opus-5-5-low\tClaude Opus 5.5 (Low)",
    "claude-sonnet-5-5-max\tClaude Sonnet 5.5 (Max)",
    "claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)",
    "gemini-pro-orchestrator\tOrchestrator",
]) + "\n"

FAKE_AGY = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
log_file = os.environ.get("FAKE_AGY_MODELS_LOG")
if "models" in args:
    if log_file:
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(os.environ.get("AGY_ADC_AUTH", "plan") + "\\n")
    if os.environ.get("FAKE_AGY_FAIL_MODELS") == "1":
        print("network unreachable", file=sys.stderr)
        raise SystemExit(1)
    fixture = os.environ["FAKE_AGY_GCP_MODELS"] if os.environ.get("AGY_ADC_AUTH") == "true" else os.environ["FAKE_AGY_PLAN_MODELS"]
    sys.stdout.write(Path(fixture).read_text(encoding="utf-8"))
    raise SystemExit(0)

settings_val = None
for i, a in enumerate(args):
    if a == "--settings" and i + 1 < len(args):
        settings_val = json.loads(args[i + 1])
print("NATIVE=" + json.dumps({
    "args": args,
    "settings": settings_val,
    "route": os.environ.get("AIPANE_AGY_ROUTE"),
    "adc": os.environ.get("AGY_ADC_AUTH"),
    "gcp_project": os.environ.get("GOOGLE_CLOUD_PROJECT"),
    "creds": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
    "proxy": os.environ.get("HTTPS_PROXY"),
    "auto_resume": os.environ.get("AIPANE_AUTO_RESUME"),
}, ensure_ascii=False), flush=True)
"""


class AgyCatalogUnitTests(unittest.TestCase):
    def test_plan_catalog_factorizes_27_variants_into_9_ordered_families(self):
        rows = agy_catalog.parse_models_output(PLAN_MODELS_TSV)
        self.assertEqual(len(rows), 27)
        catalog = agy_catalog.build_catalog(rows, route="plan")
        self.assertEqual(len(catalog.families), 9)
        by_fam = catalog.by_family()
        self.assertEqual(
            [v.effort for v in by_fam["Gemini 4 Neon"].variants],
            ["low", "medium", "high", "xhigh"],
        )
        self.assertEqual(
            [v.variant_label for v in by_fam["Orchestrator"].variants],
            ["neon", "carbon", "argon"],
        )
        self.assertFalse(by_fam["GPT-OSS 120B"].adjustable)
        self.assertTrue(by_fam["Orchestrator"].adjustable)

    def test_gcp_catalog_handles_unsuffixed_slugs_and_max_effort(self):
        rows = agy_catalog.parse_models_output(GCP_MODELS_TSV)
        catalog = agy_catalog.build_catalog(rows, route="gcp")
        by_fam = catalog.by_family()
        self.assertEqual(
            [v.slug for v in by_fam["Gemini 3.5 Flash"].variants],
            ["gemini-3.5-flash-low", "gemini-3.5-flash-medium", "gemini-3.5-flash"],
        )
        self.assertEqual(
            [v.effort for v in by_fam["Claude Opus 5.5"].variants],
            ["low", "medium", "high", "max"],
        )
        self.assertFalse(by_fam["Gemini 3.1 Flash Lite"].adjustable)
        self.assertFalse(by_fam["Orchestrator"].adjustable)

    def test_quota_formatter_is_honest_after_reset_boundary_and_staleness(self):
        now = 10_000.0
        snap = agy_catalog.normalize_quota_payload(
            {
                "quota": {
                    "gemini-5h": {"remaining_fraction": 0.82, "reset_in_seconds": 3600},
                    "gemini-weekly": {"remaining_fraction": 0.64, "reset_in_seconds": 86400},
                    "gemini-argon-wtu-weekly": {"remaining_fraction": 0.5, "reset_in_seconds": 100},
                }
            },
            now=now,
        )
        compact, detail = agy_catalog.format_quota_for_group("gemini", snap, now=now + 60)
        self.assertIn("82%/64%", compact)
        self.assertIn("↻", detail)
        # Past reset_at: must report 待刷新, never fabricate 100%.
        compact_after, detail_after = agy_catalog.format_quota_for_group("argon", snap, now=now + 200)
        self.assertIn("待刷新", compact_after)
        self.assertNotIn("100%", compact_after)
        self.assertIn("待刷新", detail_after)
        # Beyond staleness horizon: entire group becomes 待刷新.
        stale_compact, _ = agy_catalog.format_quota_for_group("gemini", snap, now=now + 8000)
        self.assertIn("待刷新", stale_compact)

    def test_settings_deep_merge_and_canonical_model_args(self):
        gcp = agy_launch.RouteConfig(name="gcp", label="GCP 按量", project="demo-proj", location="global")
        merged = agy_launch.compose_settings(
            scheme="solarized dark",
            route_cfg=gcp,
            user_settings_list=[{"verbosity": "low", "gcp": {"location": "us"}}],
        )
        self.assertEqual(merged["colorScheme"], "solarized dark")
        self.assertEqual(merged["verbosity"], "low")
        self.assertEqual(merged["gcp"], {"project": "demo-proj", "location": "us"})
        # Explicit user colorScheme wins over auto appearance.
        overridden = agy_launch.compose_settings(
            scheme="solarized dark",
            route_cfg=agy_launch.RouteConfig(name="plan", label="订阅配额"),
            user_settings_list=[{"colorScheme": "tokyo night"}],
        )
        self.assertEqual(overridden["colorScheme"], "tokyo night")
        # Fixed display-name model must never emit conflicting --effort.
        self.assertEqual(
            agy_launch.canonicalize_explicit_model_args("Gemini 4 Neon (XHigh)", "xhigh"),
            ["--model", "Gemini 4 Neon (XHigh)"],
        )
        self.assertEqual(
            agy_launch.canonicalize_explicit_model_args("gemini-4-neon-xhigh", "xhigh"),
            ["--model", "gemini-4-neon-xhigh"],
        )
        self.assertEqual(
            agy_launch.canonicalize_explicit_model_args("gemini-4-neon", "low"),
            ["--model", "gemini-4-neon", "--effort", "low"],
        )


class AgyLauncherEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.state = self.home / "state"
        self.cli_home = self.home / ".gemini/antigravity-cli"
        self.cli_home.mkdir(parents=True)
        (self.cli_home / "settings.json").write_text(
            json.dumps({"model": "Gemini 4 Neon (XHigh)"}), encoding="utf-8"
        )
        self.plan_models = self.home / "plan-models.tsv"
        self.plan_models.write_text(PLAN_MODELS_TSV, encoding="utf-8")
        self.gcp_models = self.home / "gcp-models.tsv"
        self.gcp_models.write_text(GCP_MODELS_TSV, encoding="utf-8")
        self.models_log = self.home / "models-calls.log"
        self.fake_agy = self.home / ".local/bin/agy"
        self.fake_agy.parent.mkdir(parents=True)
        self.fake_agy.write_text(FAKE_AGY, encoding="utf-8")
        self.fake_agy.chmod(0o755)
        self.launch_config = self.home / ".config/aipane/agy-launch.json"
        self.launch_config.parent.mkdir(parents=True)
        self.launch_config.write_text(
            json.dumps({
                "version": 1,
                "routes": {
                    "gcp": {
                        "label": "GCP 按量",
                        "project": "demo-gcp-proj",
                        "location": "global",
                        "credentials": str(self.home / "adc.json"),
                        "https_proxy": "http://127.0.0.1:7897",
                    }
                },
            }),
            encoding="utf-8",
        )
        # Pre-seed fresh catalogs so interactive tests run fast and deterministically.
        env = self.env()
        sig = agy_catalog.binary_signature(self.fake_agy)
        agy_catalog.save_cached_catalog(
            agy_catalog.build_catalog(
                agy_catalog.parse_models_output(PLAN_MODELS_TSV),
                route="plan", fetched_at=time.time(), binary_sig=sig, source="cache",
            ),
            environment=env,
        )
        agy_catalog.save_cached_catalog(
            agy_catalog.build_catalog(
                agy_catalog.parse_models_output(GCP_MODELS_TSV),
                route="gcp", fetched_at=time.time(), binary_sig=sig, source="cache",
            ),
            environment=env,
        )

    def env(self, **extra) -> dict[str, str]:
        base = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "LANG": "en_US.UTF-8",
            "TERM": "xterm-256color",
            "AIPANE_STATE_DIR": str(self.state),
            "AIPANE_AGY_BIN": str(self.fake_agy),
            "AIPANE_AGY_LAUNCH_CONFIG": str(self.launch_config),
            "AGY_APPEARANCE_OVERRIDE": "dark",
            "FAKE_AGY_PLAN_MODELS": str(self.plan_models),
            "FAKE_AGY_GCP_MODELS": str(self.gcp_models),
            "FAKE_AGY_MODELS_LOG": str(self.models_log),
        }
        base.update(extra)
        return base

    def run_direct(self, args: list[str], *, cwd: Path | None = None, **extra_env):
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), *args],
            cwd=cwd or self.home,
            env=self.env(**extra_env),
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NATIVE=", result.stdout)
        return json.loads(result.stdout.split("NATIVE=", 1)[1].splitlines()[0])

    def spawn_pty(self, args: list[str], *, columns=100, lines=28, cwd: Path | None = None, **extra_env):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
        proc = subprocess.Popen(
            [sys.executable, str(LAUNCHER), *args],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=cwd or self.home,
            env=self.env(**extra_env),
            start_new_session=True,
        )
        def cleanup():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
            os.close(master)
            os.close(slave)
        self.addCleanup(cleanup)
        return proc, master

    def read_until(self, proc, master, marker: bytes, timeout: float = 6.0) -> bytes:
        buf = b""
        tail = b""
        deadline = time.monotonic() + timeout
        while marker not in buf or (marker == b"NATIVE=" and b"\n" not in buf.partition(b"NATIVE=")[2]):
            if time.monotonic() >= deadline:
                self.fail(f"timed out waiting for {marker!r}; tail={buf[-600:]!r}")
            if select.select([master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                pend = tail + chunk
                for _ in range(pend.count(b"\x1b[6n")):
                    os.write(master, b"\x1b[1;1R")
                tail = pend[-3:]
                buf += chunk
            elif proc.poll() is not None:
                break
        return buf

    def native_from_pty(self, output: bytes) -> dict:
        self.assertIn(b"NATIVE=", output, output.decode(errors="ignore"))
        return json.loads(output.split(b"NATIVE=", 1)[1].splitlines()[0].decode())

    def test_subcommand_passthrough_and_explicit_model_skip_picker(self):
        # Subcommand `models` passes straight through to native agy without picker.
        res = subprocess.run(
            [sys.executable, str(LAUNCHER), "models"],
            env=self.env(), capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("gemini-4-neon-xhigh", res.stdout)

        # Explicit --model skips picker, merges user --settings with theme, inherits skills.
        project = self.home / "work/sub"
        (self.home / ".agents/skills").mkdir(parents=True)
        (self.home / "work/.agents/skills").mkdir(parents=True)
        project.mkdir(parents=True)
        native = self.run_direct(
            ["--model", "Gemini 4 Neon (XHigh)", "--effort", "xhigh", "--settings", '{"verbosity":"low"}'],
            cwd=project,
        )
        self.assertEqual(native["settings"], {"colorScheme": "solarized dark", "verbosity": "low"})
        self.assertNotIn("--effort", native["args"])
        self.assertIn("--add-dir", native["args"])
        self.assertIn(str(self.home / "work"), native["args"])
        self.assertIn(str(self.home), native["args"])
        self.assertFalse(agy_launch.picker_state_path(self.env()).exists())

    def test_auto_resume_uses_session_or_transcript_and_preserves_gcp_route(self):
        # Write transcript fallback with PLANNER_RESPONSE model.
        transcript = self.cli_home / "brain" / SID / ".system_generated/logs/transcript.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps({"type": "PLANNER_RESPONSE", "model": "claude-opus-5-5-max"}) + "\n",
            encoding="utf-8",
        )
        native = self.run_direct(
            ["--conversation", SID],
            AIPANE_AUTO_RESUME="1",
            AIPANE_AGY_ROUTE="gcp",
        )
        self.assertEqual(native["route"], "gcp")
        self.assertEqual(native["adc"], "true")
        self.assertEqual(native["gcp_project"], "demo-gcp-proj")
        self.assertEqual(native["settings"]["gcp"], {"project": "demo-gcp-proj", "location": "global"})
        self.assertEqual(native["args"][native["args"].index("--model") + 1], "claude-opus-5-5-max")
        self.assertIsNone(native["auto_resume"])

    def test_three_way_reconciliation_respects_native_slash_model_change_only(self):
        # Step 1: user picks Carbon Medium in text fallback -> saved with last_seen_settings_model = Neon XHigh.
        proc, master = self.spawn_pty([], TERM="dumb")
        self.read_until(proc, master, b"q ")
        os.write(master, b"carbon med\n")
        out = self.read_until(proc, master, b"NATIVE=")
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertEqual(self.native_from_pty(out)["args"][-1], "gemini-4-carbon-medium")

        # Step 2: settings.json is rewritten (trustedWorkspaces changed), model unchanged -> keeps Carbon Medium!
        (self.cli_home / "settings.json").write_text(
            json.dumps({"model": "Gemini 4 Neon (XHigh)", "trustedWorkspaces": ["/tmp/x"]}),
            encoding="utf-8",
        )
        proc, master = self.spawn_pty([], TERM="dumb")
        self.read_until(proc, master, b"q ")
        os.write(master, b"\n")
        out = self.read_until(proc, master, b"NATIVE=")
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertEqual(self.native_from_pty(out)["args"][-1], "gemini-4-carbon-medium")

        # Step 3: user changes default via in-session /model -> settings.json#model becomes Argon High -> picker follows!
        (self.cli_home / "settings.json").write_text(
            json.dumps({"model": "Gemini 4 Argon (High)"}),
            encoding="utf-8",
        )
        proc, master = self.spawn_pty([], TERM="dumb")
        self.read_until(proc, master, b"q ")
        os.write(master, b"\n")
        out = self.read_until(proc, master, b"NATIVE=")
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertEqual(self.native_from_pty(out)["args"][-1], "gemini-4-argon-high")

    def test_offline_cold_start_falls_back_gracefully(self):
        # Remove cache and make `agy models` fail.
        agy_catalog.catalog_cache_path("plan", self.env()).unlink()
        proc, master = self.spawn_pty([], TERM="dumb", FAKE_AGY_FAIL_MODELS="1")
        self.read_until(proc, master, b"q ")
        os.write(master, b"\n")
        out = self.read_until(proc, master, b"NATIVE=")
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertEqual(self.native_from_pty(out)["args"][-1], "gemini-4-neon-xhigh")

    @unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
    def test_pty_family_view_step_effort_and_clear_inherited_adc_on_plan(self):
        # Pollute parent env with AGY_ADC_AUTH=true to verify plan route scrubs it cleanly.
        proc, master = self.spawn_pty([], AGY_ADC_AUTH="true")
        self.read_until(proc, master, b"gemini-4-neon-xhigh")
        # Step right once on Neon: xhigh wraps around to low; step right again -> medium.
        os.write(master, b"\x1b[C")
        self.read_until(proc, master, b"gemini-4-neon-low")
        os.write(master, b"\x1b[C")
        self.read_until(proc, master, b"gemini-4-neon-medium")
        os.write(master, b"\r")
        out = self.read_until(proc, master, b"NATIVE=")
        self.assertEqual(proc.wait(timeout=4), 0)
        native = self.native_from_pty(out)
        self.assertEqual(native["args"][native["args"].index("--model") + 1], "gemini-4-neon-medium")
        self.assertEqual(native["route"], "plan")
        self.assertIsNone(native["adc"])
        self.assertEqual(native["settings"]["colorScheme"], "solarized dark")
        # Zero live `agy models` calls because cache was warm.
        self.assertFalse(self.models_log.exists())

    @unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
    def test_pty_auto_expands_on_effort_query_and_collapses_on_esc(self):
        proc, master = self.spawn_pty([])
        self.read_until(proc, master, b"gemini-4-neon-xhigh")
        # Type "opus low" -> auto expands to flat variants and matches Claude Opus 5.5 (Low).
        os.write(master, b"opus low")
        self.read_until(proc, master, b"claude-opus-5-5-low")
        # Esc clears query and collapses back to family view.
        os.write(master, b"\x1b")
        self.read_until(proc, master, "模型家族".encode())
        # Type "neon xh" -> auto expands and immediately launches gemini-4-neon-xhigh on Enter.
        os.write(master, b"neon xh")
        self.read_until(proc, master, b"gemini-4-neon-xhigh")
        os.write(master, b"\r")
        out = self.read_until(proc, master, b"NATIVE=")
        self.assertEqual(proc.wait(timeout=4), 0)
        native = self.native_from_pty(out)
        self.assertEqual(native["args"][native["args"].index("--model") + 1], "gemini-4-neon-xhigh")

    @unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
    def test_pty_ctrl_g_switches_to_gcp_and_launches_max_variant(self):
        proc, master = self.spawn_pty([])
        self.read_until(proc, master, b"gemini-4-neon-xhigh")
        # Ctrl+G switches to GCP route.
        os.write(master, b"\x07")
        self.read_until(proc, master, "GCP 按量".encode())
        os.write(master, b"opus max")
        self.read_until(proc, master, b"claude-opus-5-5-max")
        os.write(master, b"\r")
        out = self.read_until(proc, master, b"NATIVE=")
        self.assertEqual(proc.wait(timeout=4), 0)
        native = self.native_from_pty(out)
        self.assertEqual(native["route"], "gcp")
        self.assertEqual(native["adc"], "true")
        self.assertEqual(native["gcp_project"], "demo-gcp-proj")
        self.assertEqual(native["settings"]["colorScheme"], "solarized dark")
        self.assertEqual(native["settings"]["gcp"], {"project": "demo-gcp-proj", "location": "global"})
        self.assertEqual(native["args"][native["args"].index("--model") + 1], "claude-opus-5-5-max")

    @unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
    def test_pty_ctrl_r_refreshes_catalog_and_surfaces_new_model(self):
        # Add a brand new model to the upstream TSV after cache was seeded.
        self.plan_models.write_text(
            PLAN_MODELS_TSV + "gemini-5-nova-xhigh\tGemini 5 Nova (XHigh)\n",
            encoding="utf-8",
        )
        proc, master = self.spawn_pty([])
        self.read_until(proc, master, b"gemini-4-neon-xhigh")
        os.write(master, b"\x12")  # Ctrl+R
        self.read_until(proc, master, b"Gemini 5 Nova")
        os.write(master, b"nova")
        self.read_until(proc, master, b"gemini-5-nova-xhigh")
        os.write(master, b"\r")
        out = self.read_until(proc, master, b"NATIVE=")
        self.assertEqual(proc.wait(timeout=4), 0)
        self.assertEqual(self.native_from_pty(out)["args"][-1], "gemini-5-nova-xhigh")

    @unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
    def test_pty_ctrl_t_toggles_flat_and_option_dot_steps_during_query(self):
        proc, master = self.spawn_pty([])
        self.read_until(proc, master, b"gemini-4-neon-xhigh")
        # Type single-word query "carbon" (stays in family view, default Carbon Medium).
        os.write(master, b"carbon")
        self.read_until(proc, master, b"gemini-4-carbon-medium")
        # Option+. (\x1b.) steps Carbon effort from medium -> high without leaving query!
        os.write(master, b"\x1b.")
        self.read_until(proc, master, b"gemini-4-carbon-high")
        # Clear query, verify Carbon retains High, then Ctrl+T switches to flat view pinned on Carbon High.
        os.write(master, b"\x1b")
        self.read_until(proc, master, b"9/9")
        os.write(master, b"\x1b[B")  # move from Neon (row 1) to Carbon (row 2)
        self.read_until(proc, master, b"gemini-4-carbon-high")
        os.write(master, b"\x14")  # Ctrl+T -> flat view pinned on Carbon (High)
        self.read_until(proc, master, b"27/27")
        self.read_until(proc, master, b"gemini-4-carbon-high")
        os.write(master, b"\r")
        out = self.read_until(proc, master, b"NATIVE=")
        self.assertEqual(proc.wait(timeout=4), 0)
        self.assertEqual(self.native_from_pty(out)["args"][-1], "gemini-4-carbon-high")


if __name__ == "__main__":
    unittest.main()
