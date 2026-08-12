import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTEGRATIONS = ROOT / "integrations"
WINDOW_WRAP = ROOT / "bin" / "tmux-window-wrap"
TMUX_CONFIG = ROOT / "conf" / "tmux-window-wrap.conf"
BUSY_COMMAND = "$HOME/.local/bin/tmux-window-wrap activity busy"
IDLE_COMMAND = "$HOME/.local/bin/tmux-window-wrap activity idle"


def nested_commands(hook_groups):
    return [
        hook["command"]
        for group in hook_groups
        for hook in group.get("hooks", [])
    ]


class AgentActivityIntegrationTests(unittest.TestCase):
    def load_json_hooks(self, tool):
        path = INTEGRATIONS / tool / "hooks.json"
        return json.loads(path.read_text(encoding="utf-8"))["hooks"]

    def assert_nested_event(self, hooks, event, command):
        self.assertIn(command, nested_commands(hooks[event]))

    def test_claude_hook_fragment_implements_turn_level_contract(self):
        hooks = self.load_json_hooks("claude")

        self.assert_nested_event(hooks, "UserPromptSubmit", BUSY_COMMAND)
        for event in ("SessionStart", "Stop", "StopFailure", "SessionEnd"):
            self.assert_nested_event(hooks, event, IDLE_COMMAND)
        self.assertNotIn("PermissionRequest", hooks)

    def test_standard_hook_fragments_implement_turn_level_contract(self):
        for tool in ("claude", "codex", "grok", "qoder"):
            with self.subTest(tool=tool):
                hooks = self.load_json_hooks(tool)
                self.assert_nested_event(hooks, "UserPromptSubmit", BUSY_COMMAND)
                for event in ("SessionStart", "Stop", "SessionEnd"):
                    self.assert_nested_event(hooks, event, IDLE_COMMAND)

        for tool in ("claude", "codex", "grok", "qoder"):
            with self.subTest(tool=tool, event="StopFailure"):
                hooks = self.load_json_hooks(tool)
                self.assert_nested_event(hooks, "StopFailure", IDLE_COMMAND)

    def test_cursor_hook_fragment_implements_turn_level_contract(self):
        hooks = self.load_json_hooks("cursor")

        self.assertEqual(hooks["beforeSubmitPrompt"][0]["command"], BUSY_COMMAND)
        self.assertEqual(hooks["stop"][0]["command"], IDLE_COMMAND)
        self.assertEqual(hooks["sessionEnd"][0]["command"], IDLE_COMMAND)

    def test_kimi_hook_fragment_has_no_permission_state_override(self):
        path = INTEGRATIONS / "kimi" / "hooks.toml"
        hooks = tomllib.loads(path.read_text(encoding="utf-8"))["hooks"]
        commands = {hook["event"]: hook["command"] for hook in hooks}

        self.assertEqual(commands["UserPromptSubmit"], BUSY_COMMAND)
        for event in ("SessionStart", "Stop", "StopFailure", "Interrupt", "SessionEnd"):
            self.assertEqual(commands[event], IDLE_COMMAND)
        self.assertNotIn("PermissionRequest", commands)
        self.assertNotIn("PermissionResult", commands)

    def test_opencode_adapter_owns_binding_and_activity_translation(self):
        path = INTEGRATIONS / "opencode" / "aipane-bind.js"
        source = path.read_text(encoding="utf-8")

        self.assertIn('["activity", state, "--pane", pane]', source)
        self.assertIn('"chat.message"', source)
        self.assertIn('event.type === "session.status"', source)
        self.assertIn('event.type === "session.idle"', source)
        self.assertIn('event.type === "session.error"', source)

    def test_renderer_has_no_terminal_title_activity_input(self):
        script = WINDOW_WRAP.read_text(encoding="utf-8")
        config = TMUX_CONFIG.read_text(encoding="utf-8")

        self.assertNotIn("pane_title", script)
        self.assertNotIn("pane_title", config)
        self.assertNotIn("CODEX_WORKING_SPINNER", script)


if __name__ == "__main__":
    unittest.main()
