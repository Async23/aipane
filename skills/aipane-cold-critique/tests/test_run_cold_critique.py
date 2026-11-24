import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import run_cold_critique as rcc  # noqa: E402


class StripMarkdownShellTests(unittest.TestCase):
    def test_strips_frontmatter_trailing_refs_and_external_links(self):
        raw = (
            "---\n"
            "title: 测试文章\n"
            "category: 工程\n"
            "---\n\n"
            "核心观点见 [某论文](https://example.com/paper) 和 ![示意图](https://example.com/a.png)。\n\n"
            "## 正文\n"
            "本地路径 /Users/alice/work/demo 和 C:\\Users\\bob\\repo。\n\n"
            "## 参考资料\n"
            "- [外链](https://example.org)\n"
        )
        cleaned = rcc.strip_markdown_shell(raw, redact_paths=True)
        self.assertNotIn("title: 测试文章", cleaned)
        self.assertNotIn("https://example.com", cleaned)
        self.assertNotIn("参考资料", cleaned)
        self.assertIn("核心观点见 某论文 和 [图片: 示意图]。", cleaned)
        self.assertIn("<HOME>/work/demo", cleaned)
        self.assertIn("<HOME>\\repo", cleaned)

    def test_strips_english_references_and_crlf(self):
        raw = "---\r\ntitle: Demo\r\n---\r\n\r\nBody text.\r\n\r\n## References\r\n- item\r\n"
        self.assertEqual(rcc.strip_markdown_shell(raw), "Body text.")


class ChannelSpecTests(unittest.TestCase):
    def test_parse_cli_specs(self):
        s1 = rcc.parse_channel_spec("codex")
        self.assertEqual((s1.backend, s1.model, s1.effort, s1.env_prefix), ("codex", None, None, None))

        s2 = rcc.parse_channel_spec("claude:opus@high")
        self.assertEqual((s2.backend, s2.model, s2.effort, s2.env_prefix), ("claude", "opus", "high", None))

        s3 = rcc.parse_channel_spec("codex@xhigh")
        self.assertEqual((s3.backend, s3.model, s3.effort, s3.env_prefix), ("codex", None, "xhigh", None))

    def test_parse_openai_with_colon_in_model_and_env_prefix(self):
        spec = rcc.parse_channel_spec("openai:qwen3:32b@medium#OLLAMA")
        self.assertEqual(spec.backend, "openai")
        self.assertEqual(spec.model, "qwen3:32b")
        self.assertEqual(spec.effort, "medium")
        self.assertEqual(spec.env_prefix, "OLLAMA")

    def test_reject_invalid_specs(self):
        with self.assertRaises(ValueError):
            rcc.parse_channel_spec("unknown")
        with self.assertRaises(ValueError):
            rcc.parse_channel_spec("codex#CUSTOM")
        with self.assertRaises(ValueError):
            rcc.parse_channel_spec("openai:gpt-4.1#bad-prefix")


class CommandBuilderTests(unittest.TestCase):
    def test_codex_command_enforces_readonly_and_ephemeral(self):
        spec = rcc.parse_channel_spec("codex:o3@high")
        cmd = rcc.build_codex_command(spec, "/tmp/out.txt")
        self.assertEqual(cmd[:6], ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "--ephemeral"])
        self.assertIn("-m", cmd)
        self.assertIn("o3", cmd)
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertEqual(cmd[-3:], ["-o", "/tmp/out.txt", "-"])

    def test_claude_command_disables_all_tools_and_session(self):
        spec = rcc.parse_channel_spec("claude:sonnet@medium")
        cmd = rcc.build_claude_command(spec)
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--disable-slash-commands", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("sonnet", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("medium", cmd)

    def test_agy_command_disables_slash_commands(self):
        spec = rcc.parse_channel_spec("agy:demo-model@high")
        cmd = rcc.build_agy_command(spec, "hello")
        self.assertIn("--disable-slash-commands", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("demo-model", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("high", cmd)
        self.assertEqual(cmd[-1], "--print=hello")


class OpenAIBackendTests(unittest.TestCase):
    def test_resolve_openai_env_custom_prefix(self):
        spec = rcc.parse_channel_spec("openai:deepseek-reasoner#DEEPSEEK")
        env = {
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1/",
            "DEEPSEEK_API_KEY": "sk-test-secret",
        }
        base_url, api_key, model = rcc.resolve_openai_env(spec, env=env)
        self.assertEqual(base_url, "https://api.deepseek.com/v1")
        self.assertEqual(api_key, "sk-test-secret")
        self.assertEqual(model, "deepseek-reasoner")

    def test_openai_payload_and_response_extraction(self):
        payload = rcc.build_openai_payload("gpt-4.1", "prompt text", effort="high")
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["reasoning_effort"], "high")

        resp_json = {
            "model": "gpt-4.1",
            "choices": [{"message": {"role": "assistant", "content": "反驳要点如下"}}],
            "usage": {"total_tokens": 42},
        }
        fake_response = mock.MagicMock()
        fake_response.read.return_value = json.dumps(resp_json).encode("utf-8")
        fake_response.__enter__.return_value = fake_response

        spec = rcc.parse_channel_spec("openai:gpt-4.1")
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-fake"}, clear=True):
            with mock.patch("urllib.request.urlopen", return_value=fake_response) as mocked_open:
                res = rcc.run_channel(spec, "prompt text", timeout_s=30)
                self.assertTrue(res["ok"])
                self.assertEqual(res["response"], "反驳要点如下")
                self.assertEqual(res["usage"]["total_tokens"], 42)
                req = mocked_open.call_args[0][0]
                self.assertEqual(req.full_url, "https://api.openai.com/v1/chat/completions")


class CliIntegrationTests(unittest.TestCase):
    def test_dry_run_via_stdin(self):
        fake_stdin = io.StringIO("---\ntitle: x\n---\n\n一段待审文本 /Users/demo/a.md\n")
        fake_stdin.isatty = lambda: False  # type: ignore[attr-defined]
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", fake_stdin), mock.patch.object(sys, "stdout", buf):
            rc = rcc.main(["-c", "codex", "-c", "claude", "--redact-paths", "--dry-run"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["channels"], ["codex", "claude"])
        self.assertFalse(out["single_channel_warning"])
        self.assertIn("<HOME>/a.md", out["preview_tail"])

    def test_env_channels_used_when_cli_channels_omitted(self):
        fake_stdin = io.StringIO("待审正文内容\n")
        fake_stdin.isatty = lambda: False  # type: ignore[attr-defined]
        buf = io.StringIO()
        env = {"COLD_CRITIQUE_CHANNELS": "codex@xhigh, agy:gemini-4-neon-xhigh"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(sys, "stdin", fake_stdin), mock.patch.object(sys, "stdout", buf):
                rc = rcc.main(["--dry-run"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["channels"], ["codex@xhigh", "agy:gemini-4-neon-xhigh"])
        self.assertFalse(out["single_channel_warning"])

    def test_cli_backends_run_in_fresh_empty_temp_workdir(self):
        seen_cwds: list[str] = []

        def fake_run(cmd, **kwargs):
            cwd = kwargs["cwd"]
            seen_cwds.append(cwd)
            self.assertNotEqual(os.path.realpath(cwd), os.path.realpath(tempfile.gettempdir()))
            self.assertEqual(os.listdir(cwd), [])
            if cmd[0] == "codex":
                out_path = cmd[cmd.index("-o") + 1]
                pathlib.Path(out_path).write_text("codex critique", encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")
            if cmd[0] == "claude":
                return mock.Mock(returncode=0, stdout=json.dumps({"result": "claude critique"}), stderr="")
            if cmd[0] == "agy":
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"status": "SUCCESS", "response": "agy critique"}),
                    stderr="",
                )
            raise AssertionError(f"unexpected cmd: {cmd}")

        with mock.patch("subprocess.run", side_effect=fake_run):
            r_codex = rcc.run_channel(rcc.parse_channel_spec("codex"), "prompt", 30)
            r_claude = rcc.run_channel(rcc.parse_channel_spec("claude"), "prompt", 30)
            r_agy = rcc.run_channel(rcc.parse_channel_spec("agy"), "prompt", 30)

        self.assertTrue(r_codex["ok"] and r_claude["ok"] and r_agy["ok"])
        self.assertEqual(len(set(seen_cwds)), 3)
        for d in seen_cwds:
            self.assertFalse(os.path.exists(d))


if __name__ == "__main__":
    unittest.main()
