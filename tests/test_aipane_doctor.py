import os
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"DSH_HOME", "AIPANE_DSH_BIN", "AIPANE_DSH_NOTIFIER_APP"}}
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
        hooks = self.home / ".cursor" / "hooks.json"
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
            plistlib.dump({"CFBundleIdentifier": "com.alfheim.dsh-notifier",
                          "CFBundleExecutable": "dsh-notifier",
                          "CFBundleIconFile": "Dsh.icns"}, stream)
        result = self.run_doctor(**environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("notification icon is missing", result.stdout)
        (app / "Resources/Dsh.icns").write_bytes(b"fixture")
        self.assertEqual(self.run_doctor(**environment).returncode, 0)


if __name__ == "__main__":
    unittest.main()
