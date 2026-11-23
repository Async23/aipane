import os
import hashlib
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
DOCTOR = ROOT / "bin" / "aipane-doctor"


class AipaneDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temporary.name)
        self.root = temporary_root / "aipane"
        self.home = temporary_root / "home"
        self.local_bin = self.home / ".local" / "bin"
        self.local_bin.mkdir(parents=True)
        (self.root / "bin").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def executable(self, name: str) -> Path:
        path = self.root / "bin" / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def link(self, name: str) -> None:
        (self.local_bin / name).symlink_to(self.executable(name))

    def configure_core(self) -> None:
        for name in ("aipane-doctor", "aipane-activity", "tmux-window-wrap"):
            self.link(name)

    def run_doctor(self, **overrides) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "DSH_HOME",
                "AIPANE_DSH_BIN",
                "AIPANE_DSH_NOTIFIER_APP",
                "CODEX_HOME",
                "PI_CODING_AGENT_DIR",
            }
        }
        return subprocess.run(
            [
                sys.executable,
                str(DOCTOR),
                "--root",
                str(self.root),
                "--home",
                str(self.home),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={**environment, "NO_COLOR": "1", **overrides},
        )

    def test_healthy_core_installation_passes(self) -> None:
        self.configure_core()
        hooks = self.home / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text(
            '{"command":"$HOME/.local/bin/aipane-activity report busy"}',
            encoding="utf-8",
        )

        result = self.run_doctor()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS aipane doctor", result.stdout)
        self.assertIn("canonical activity registrations: 1", result.stdout)

    def test_missing_canonical_entrypoint_fails(self) -> None:
        self.configure_core()
        (self.local_bin / "aipane-activity").unlink()

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL ~/.local/bin/aipane-activity: missing", result.stdout)

    def test_wrong_symlink_target_fails(self) -> None:
        self.configure_core()
        wrong = self.home / "wrong-aipane-activity"
        wrong.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        wrong.chmod(0o755)
        destination = self.local_bin / "aipane-activity"
        destination.unlink()
        destination.symlink_to(wrong)

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not point to this checkout", result.stdout)

    def test_recovery_helper_requires_validated_unchanged_runtime(self) -> None:
        self.configure_core()
        source = self.executable("aipane-bind")
        runtime = self.home / ".local/share/aipane/runtime/current"
        (runtime / "bin").mkdir(parents=True)
        helper = runtime / "bin/aipane-bind"
        shutil.copy2(source, helper)
        (self.local_bin / "aipane-bind").symlink_to(helper)
        config = self.home / ".codex/hooks.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"command": "$HOME/.local/bin/aipane-bind"}))
        digest = hashlib.sha256(helper.read_bytes()).hexdigest()
        for validation, stored_digest, expected_status in (
            ("passed", digest, 0),
            ("passed", "wrong", 1),
            ("imported-baseline", digest, 1),
        ):
            with self.subTest(validation=validation, digest=stored_digest):
                (runtime / "release.json").write_text(json.dumps({
                    "validation": validation,
                    "files": {"bin/aipane-bind": stored_digest},
                }))
                result = self.run_doctor()
                self.assertEqual(result.returncode, expected_status, result.stdout)

    def test_wrong_registration_symlink_target_fails(self) -> None:
        self.configure_core()
        expected = self.root / "integrations" / "grok" / "hooks.json"
        expected.parent.mkdir(parents=True)
        expected.write_text("{}", encoding="utf-8")
        wrong = self.home / "wrong-grok-hooks.json"
        wrong.write_text("{}", encoding="utf-8")
        registration = self.home / ".grok" / "hooks" / "aipane-activity.json"
        registration.parent.mkdir(parents=True)
        registration.symlink_to(wrong)

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "registration does not point to this checkout",
            result.stdout,
        )

    def test_referenced_wrapper_must_be_installed_from_this_checkout(self) -> None:
        self.configure_core()
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            '{"command":"$HOME/.local/bin/aipane-claude-activity"}',
            encoding="utf-8",
        )
        self.executable("aipane-claude-activity")

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "FAIL ~/.local/bin/aipane-claude-activity: missing",
            result.stdout,
        )

    def test_escaped_local_bin_reference_is_discovered(self) -> None:
        self.configure_core()
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            r'notify = ["[\"\\/tmp\\/.local\\/bin\\/aipane-codex-notify\"]"]',
            encoding="utf-8",
        )
        self.executable("aipane-codex-notify")

        result = self.run_doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "FAIL ~/.local/bin/aipane-codex-notify: missing",
            result.stdout,
        )

    def test_legacy_activity_command_is_an_explicit_warning(self) -> None:
        self.configure_core()
        hooks = self.home / ".claude" / "settings.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text(
            '{"command":"$HOME/.local/bin/tmux-window-wrap activity busy"}',
            encoding="utf-8",
        )

        result = self.run_doctor()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN legacy activity command", result.stdout)
        self.assertIn("compatibility references: 1", result.stdout)

    def configure_dsh(self, *, profile="dsh-tui", custom_home=None,
                      helpers=("aipane-activity",)):
        executable = shutil.which("dsh")
        if not executable or not shutil.which("node"):
            self.skipTest("native dsh profile parser requires installed Node and dsh")
        dsh_home = custom_home or self.home / ".dsh"
        directory = dsh_home / "profiles" / profile
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps({
            "dsh": {"profile": {"bundles": []}},
        }))
        (directory / "cordis.yml").write_text("[]\n")
        integration = self.root / "integrations/dsh"
        integration.mkdir(parents=True, exist_ok=True)
        rows = []
        for helper in helpers:
            source = integration / f"{helper}.mjs"
            source.write_text(f'const helper = join(home, ".local/bin/{helper}")\n')
            rows.append({"id": helper, "name": str(source)})
            if not (self.local_bin / helper).exists():
                self.link(helper)
        template = integration / "cordis.patch.yml"
        template.write_text(json.dumps([{"insert": rows}]))
        patch = directory / "cordis.patch.yml"
        patch.write_text(json.dumps([{"insert": rows}]))
        return patch, {"AIPANE_DSH_BIN": executable, "DSH_HOME": str(dsh_home)}

    def test_dsh_native_composition_is_read_only_and_does_not_evaluate_js(self):
        self.configure_core()
        patch, environment = self.configure_dsh()
        # Valid native !!js is retained as data. Calling dsh --dump-config
        # would rewrite cordis.yml; evaluating this expression would throw.
        patch.write_text(
            f"- insert:\n    - id: aipane-activity\n"
            f"      name: {self.root}/integrations/dsh/aipane-activity.mjs\n"
            "      config: !!js \"(() => { throw new Error('must not run') })()\"\n"
        )
        files = list(patch.parent.iterdir())
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}

        result = self.run_doctor(**environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("aipane-activity ->", result.stdout)
        self.assertEqual(set(files), set(patch.parent.iterdir()))
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files})
        self.assertNotIn("notifier App", result.stdout)

    def test_dsh_missing_registered_source_fails(self):
        self.configure_core()
        patch, environment = self.configure_dsh()
        rows = json.loads(patch.read_text())
        rows[0]["insert"][0]["name"] = str(self.home / "does-not-exist.mjs")
        patch.write_text(json.dumps(rows))

        result = self.run_doctor(**environment)

        self.assertEqual(result.returncode, 1)
        self.assertIn("source is missing/unreadable", result.stdout)

    def test_dsh_source_symlink_must_resolve_to_checkout(self):
        self.configure_core()
        patch, environment = self.configure_dsh()
        source = self.root / "integrations/dsh/aipane-activity.mjs"
        alias = self.home / "adapter.mjs"
        alias.symlink_to(source)
        rows = json.loads(patch.read_text())
        rows[0]["insert"][0]["name"] = str(alias)
        patch.write_text(json.dumps(rows))
        self.assertEqual(self.run_doctor(**environment).returncode, 0)
        alias.unlink()
        wrong = self.home / "old-adapter.mjs"
        wrong.write_text("// old checkout\n")
        alias.symlink_to(wrong)
        result = self.run_doctor(**environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not point to this checkout", result.stdout)
        wrong.unlink()
        self.assertIn("broken symlink", self.run_doctor(**environment).stdout)

    def test_dsh_web_custom_home_is_checked(self):
        self.configure_core()
        patch, environment = self.configure_dsh(
            profile="web", custom_home=self.home / "custom-dsh",
        )
        patch.write_text("[]\n")
        result = self.run_doctor(**environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("custom-dsh/profiles/web: aipane-activity missing", result.stdout)

    def test_dsh_home_override_disables_profile_entry(self):
        self.configure_core()
        _, environment = self.configure_dsh()
        home_patch = Path(environment["DSH_HOME"]) / "cordis.patch.yml"
        home_patch.write_text("- id: aipane-activity\n  disabled: true\n")
        result = self.run_doctor(**environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("aipane-activity is disabled", result.stdout)
        home_patch.write_text("- id: aipane-activity\n  disabled: true\n- id: aipane-activity\n  disabled: false\n")
        self.assertEqual(self.run_doctor(**environment).returncode, 0)

    def test_dsh_malformed_patch_fails_actionably(self):
        self.configure_core()
        patch, environment = self.configure_dsh()
        patch.write_text("[not valid: yaml\n")
        result = self.run_doctor(**environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read native dsh profile composition", result.stdout)

    def test_dsh_new_plugin_helper_is_discovered(self):
        self.configure_core()
        _, environment = self.configure_dsh(helpers=("aipane-dsh-session",))
        (self.local_bin / "aipane-dsh-session").unlink()
        result = self.run_doctor(**environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("~/.local/bin/aipane-dsh-session: missing", result.stdout)

    def test_dsh_session_restore_helper_is_required_without_direct_plugin_call(self):
        self.configure_core()
        _, environment = self.configure_dsh(helpers=("aipane-dsh-session",))
        source = self.root / "integrations/dsh/aipane-dsh-session.mjs"
        source.write_text('const helper = join(home, ".local/bin/aipane-bind")\n')
        self.link("aipane-bind")
        (self.local_bin / "aipane-dsh-session").unlink()

        result = self.run_doctor(**environment)

        self.assertEqual(result.returncode, 1)
        self.assertIn("~/.local/bin/aipane-dsh-session: missing", result.stdout)
        self.link("aipane-dsh-session")
        self.assertEqual(self.run_doctor(**environment).returncode, 0)

    def test_dsh_notifier_requires_focus_helper_app_and_icon(self):
        self.configure_core()
        _, environment = self.configure_dsh(helpers=("aipane-dsh-notify",))
        self.executable("aipane-dsh-focus")
        result = self.run_doctor(**environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("aipane-dsh-focus: missing", result.stdout)
        self.assertIn("run bin/build-dsh-notifier", result.stdout)
        self.link("aipane-dsh-focus")
        app = self.home / "Applications/dsh Notifier.app/Contents"
        (app / "MacOS").mkdir(parents=True)
        (app / "Resources").mkdir()
        executable = app / "MacOS/dsh-notifier"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        with (app / "Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleIdentifier": "com.alfheim.dsh-notifier.v2",
                          "CFBundleExecutable": "dsh-notifier",
                          "CFBundleIconFile": "DshBlackWhale.icns"}, stream)
        result = self.run_doctor(**environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("notification icon is missing", result.stdout)
        (app / "Resources/DshBlackWhale.icns").write_bytes(b"fixture")
        self.assertEqual(self.run_doctor(**environment).returncode, 0)

    def test_session_storage_drift_passes_for_suffixed_codex_segments(self) -> None:
        from session_fixtures import seed_codex_multi_segment

        self.configure_core()
        sid = "12341234-1234-7234-8234-123412341234"
        seed_codex_multi_segment(
            self.home,
            sid,
            rollout_ids=("23452345-2345-7345-8345-234523452345",),
            include_sqlite=True,
        )
        result = self.run_doctor(CODEX_HOME=str(self.home / ".codex"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK   session storage drift: 2 codex, 1 sqlite", result.stdout)

    def test_session_storage_drift_fails_when_sqlite_points_to_uncovered_rollout(self) -> None:
        from session_fixtures import seed_codex_multi_segment, write_jsonl

        self.configure_core()
        sid = "56785678-5678-7678-8678-567856785678"
        uncovered = self.home / ".codex/new_layout" / f"rollout-2026-11-23T10-00-00-{sid}.jsonl"
        write_jsonl(
            uncovered,
            [{"ordinal": 50, "type": "session_meta", "timestamp": "2026-11-23T02:00:00Z", "payload": {"id": sid}}],
        )
        seed_codex_multi_segment(
            self.home,
            sid,
            untracked_sqlite_rollout=uncovered,
        )
        result = self.run_doctor(CODEX_HOME=str(self.home / ".codex"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("codex_sqlite_rollout_uncovered", result.stdout)

    def test_session_storage_drift_catches_double_uuid_drift_oversized_header_and_sqlite_schema_error(self) -> None:
        import sqlite3
        from contextlib import closing
        from session_fixtures import write_jsonl

        self.configure_core()
        codex_home = self.home / ".codex"
        sid_a = "11111111-1111-7111-8111-111111111111"
        sid_b = "22222222-2222-7222-8222-222222222222"
        sid_c = "33333333-3333-7333-8333-333333333333"

        # 1. Double-UUID filename says A_B, header says C
        drift_file = codex_home / "sessions/2026/11/23" / f"rollout-2026-11-23T10-00-00-{sid_a}_{sid_b}.jsonl"
        write_jsonl(drift_file, [{"type": "session_meta", "payload": {"id": sid_c}}])

        # 2. Oversized >4MB header fails verification explicitly
        huge_file = codex_home / "sessions/2026/11/23" / f"rollout-2026-11-23T10-01-00-{sid_c}.jsonl"
        huge_file.parent.mkdir(parents=True, exist_ok=True)
        huge_file.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": sid_c, "blob": "X" * (4 * 1024 * 1024 + 1024)}})
            + "\n",
            encoding="utf-8",
        )

        # 3. Broken schema in sqlite/state_5.sqlite fails explicitly instead of checked_sqlite=0
        db_path = codex_home / "sqlite/state_5.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("CREATE TABLE other_table (x INTEGER)")

        result = self.run_doctor(CODEX_HOME=str(codex_home))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("codex_sid_drift", result.stdout)
        self.assertIn("codex_header_header_too_large", result.stdout)
        self.assertIn("codex_sqlite_query_failed", result.stdout)

    def test_r2_p2_2_doctor_catches_missing_recent_sqlite_row_shadowed_db_and_corrupt_pi_header(self) -> None:
        import sqlite3
        from contextlib import closing

        self.configure_core()
        codex_home = self.home / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        sid = "44444444-4444-7444-8444-444444444444"

        # Higher-version state_6.sqlite is empty; state_5.sqlite has a recent row pointing to missing file
        with closing(sqlite3.connect(codex_home / "state_6.sqlite")) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
        with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER)")
            conn.execute(
                "INSERT INTO threads VALUES (?, ?, ?)",
                (sid, str(codex_home / "sessions/deleted-recent.jsonl"), int(time.time())),
            )

        # Corrupt Pi session header missing id must fail doctor
        pi_dir = self.home / ".pi/agent"
        bad_pi = pi_dir / "sessions/--demo--" / f"2026-11-23_{sid}.jsonl"
        bad_pi.parent.mkdir(parents=True, exist_ok=True)
        bad_pi.write_text("{corrupt json\n", encoding="utf-8")

        result = self.run_doctor(CODEX_HOME=str(codex_home), PI_CODING_AGENT_DIR=str(pi_dir))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("codex_sqlite_rollout_missing", result.stdout)
        self.assertIn("pi_header_invalid", result.stdout)


if __name__ == "__main__":
    unittest.main()
