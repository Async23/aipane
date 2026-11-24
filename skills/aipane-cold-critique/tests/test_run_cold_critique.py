import io
import json
import os
import pathlib
import sys
import tempfile
import time
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
    def test_parse_all_aipane_backends_and_aliases(self):
        s1 = rcc.parse_channel_spec("codex")
        self.assertEqual((s1.backend, s1.model, s1.effort, s1.env_prefix), ("codex", None, None, None))

        s2 = rcc.parse_channel_spec("claude:opus@high")
        self.assertEqual((s2.backend, s2.model, s2.effort, s2.env_prefix), ("claude", "opus", "high", None))

        s3 = rcc.parse_channel_spec("dsh-tui:deepseek-official/deepseek-reasoner")
        self.assertEqual(s3.backend, "dsh")
        self.assertEqual(s3.model, "deepseek-official/deepseek-reasoner")
        self.assertEqual(s3.label, "dsh:deepseek-official/deepseek-reasoner")

        s4 = rcc.parse_channel_spec("antigravity:gemini-4-neon-xhigh@xhigh")
        self.assertEqual((s4.backend, s4.model, s4.effort), ("agy", "gemini-4-neon-xhigh", "xhigh"))

        s5 = rcc.parse_channel_spec("grok:grok-4.7@high")
        self.assertEqual((s5.backend, s5.model, s5.effort), ("grok", "grok-4.7", "high"))

        s6 = rcc.parse_channel_spec("pi:aiproxy/deepseek-v4-pro@max")
        self.assertEqual((s6.backend, s6.model, s6.effort), ("pi", "aiproxy/deepseek-v4-pro", "max"))

        s7 = rcc.parse_channel_spec("kimi:kimi-code/k3.1-pro")
        self.assertEqual((s7.backend, s7.model, s7.effort), ("kimi", "kimi-code/k3.1-pro", None))

    def test_parse_openai_with_colon_in_model_and_env_prefix(self):
        spec = rcc.parse_channel_spec("openai:qwen3:32b@medium#OLLAMA")
        self.assertEqual(spec.backend, "openai")
        self.assertEqual(spec.model, "qwen3:32b")
        self.assertEqual(spec.effort, "medium")
        self.assertEqual(spec.env_prefix, "OLLAMA")

    def test_reject_effort_on_unsupported_backends(self):
        with self.assertRaisesRegex(ValueError, "不支持命令行推理强度"):
            rcc.parse_channel_spec("kimi@high")
        with self.assertRaisesRegex(ValueError, "不支持命令行推理强度"):
            rcc.parse_channel_spec("dsh@high")
        with self.assertRaisesRegex(ValueError, "不支持命令行推理强度"):
            rcc.parse_channel_spec("dsh-tui:deepseek-reasoner@high")

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
        cmd = rcc.build_codex_command(spec, "/tmp/out.txt", executable="/usr/local/bin/codex")
        self.assertEqual(cmd[:6], ["/usr/local/bin/codex", "exec", "--skip-git-repo-check", "-s", "read-only", "--ephemeral"])
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

    def test_grok_command_disables_tools_memory_subagents_and_uses_prompt_file(self):
        spec = rcc.parse_channel_spec("grok:grok-4.7@high")
        cmd = rcc.build_grok_command(spec, "/tmp/p.txt")
        self.assertIn("--prompt-file", cmd)
        self.assertEqual(cmd[cmd.index("--prompt-file") + 1], "/tmp/p.txt")
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--no-memory", cmd)
        self.assertIn("--no-subagents", cmd)
        self.assertIn("--disable-web-search", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("grok-4.7", cmd)
        self.assertIn("--reasoning-effort", cmd)
        self.assertIn("high", cmd)

    def test_pi_command_enforces_full_hard_isolation(self):
        spec = rcc.parse_channel_spec("pi:deepseek-v4-pro@xhigh")
        cmd = rcc.build_pi_command(spec)
        for flag in ("-p", "--no-tools", "--no-mcp", "--no-extensions", "--no-skills", "--no-context-files", "--no-session"):
            self.assertIn(flag, cmd)
        self.assertIn("--model", cmd)
        self.assertIn("deepseek-v4-pro", cmd)
        self.assertIn("--thinking", cmd)
        self.assertIn("xhigh", cmd)

    def test_kimi_command_uses_empty_skills_dir_and_stream_json(self):
        spec = rcc.parse_channel_spec("kimi:kimi-code/k3.1-pro")
        cmd = rcc.build_kimi_command(spec, "hello", skills_dir="/tmp/empty-skills")
        self.assertIn("--skills-dir", cmd)
        self.assertEqual(cmd[cmd.index("--skills-dir") + 1], "/tmp/empty-skills")
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--model", cmd)
        self.assertIn("kimi-code/k3.1-pro", cmd)
        self.assertEqual(cmd[-2:], ["-p", "hello"])
        self.assertNotIn("--auto", cmd)
        self.assertNotIn("--yolo", cmd)

    def test_dsh_command_and_patch_yaml_hard_disable_tools_and_override_model(self):
        spec = rcc.parse_channel_spec("dsh:deepseek-official/deepseek-reasoner")
        cmd = rcc.build_dsh_command(spec, "/tmp/patch.yml")
        self.assertEqual(
            cmd,
            ["dsh", "--profile", "headless", "--patch", "/tmp/patch.yml", "--permission-mode", "read-only", "--approval-policy", "never", "-"],
        )
        patch = rcc.build_dsh_patch_yaml(spec.model)
        self.assertIn("- id: credentials\n  disabled: false", patch)
        self.assertIn("- id: tool-bash\n  disabled: true", patch)
        self.assertIn("- id: tool-web\n  disabled: true", patch)
        self.assertIn("- id: agent-instructions\n  disabled: true", patch)
        self.assertIn('provider: "deepseek-official"', patch)
        self.assertIn('model: "deepseek-reasoner"', patch)

        bare_patch = rcc.build_dsh_patch_yaml("deepseek-chat")
        self.assertIn('model: "deepseek-chat"', bare_patch)
        self.assertNotIn("provider:", bare_patch)


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


class HybridModelProbeAndPresetTests(unittest.TestCase):
    def test_local_probes_and_agy_ttl_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            cache_dir = home / "cache"
            env = {"COLD_CRITIQUE_CACHE_DIR": str(cache_dir)}

            # Populate codex / kimi / pi / grok / agy local files
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text('model = "gpt-test"\nmodel_reasoning_effort = "high"\n', encoding="utf-8")
            (home / ".codex" / "models_cache.json").write_text(
                json.dumps({"models": [{"slug": "gpt-test"}, {"slug": "gpt-alt"}]}), encoding="utf-8"
            )
            (home / ".kimi-code").mkdir()
            (home / ".kimi-code" / "config.toml").write_text(
                'default_model = "kimi-code/k3"\n[models."kimi-code/k3"]\nmodel = "k3"\n[models."kimi-code/k3.1-pro"]\nmodel = "k3.1-pro"\n',
                encoding="utf-8",
            )
            (home / ".pi" / "agent").mkdir(parents=True)
            (home / ".pi" / "agent" / "settings.json").write_text(
                json.dumps({"defaultProvider": "p1", "defaultModel": "m1", "defaultThinkingLevel": "high"}), encoding="utf-8"
            )
            (home / ".pi" / "agent" / "models.json").write_text(
                json.dumps({"providers": {"p1": {"models": [{"id": "m1"}, {"id": "m2"}]}}}), encoding="utf-8"
            )
            (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
            (home / ".gemini" / "antigravity-cli" / "settings.json").write_text(
                json.dumps({"model": "Gemini 4 Argon (High)"}), encoding="utf-8"
            )

            codex_meta = rcc.probe_backend_models("codex", home=home, env=env)
            self.assertEqual(codex_meta["default_model"], "gpt-test")
            self.assertIn("gpt-alt", codex_meta["models"])

            kimi_meta = rcc.probe_backend_models("kimi", home=home, env=env)
            self.assertEqual(kimi_meta["default_model"], "kimi-code/k3")
            self.assertIn("kimi-code/k3.1-pro", kimi_meta["models"])

            pi_meta = rcc.probe_backend_models("pi", home=home, env=env)
            self.assertEqual(pi_meta["default_model"], "p1/m1")
            self.assertIn("m2", pi_meta["models"])

            # agy live probe writes TTL cache; second call hits cache without invoking subprocess
            fake_agy_out = "Fetching available models...\ngemini-4-neon-xhigh\tGemini 4 Neon (XHigh)\nclaude-opus-5-5-high\tClaude Opus\n"
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=fake_agy_out, stderr="")) as mrun:
                first = rcc.probe_backend_models("agy", executable="/usr/local/bin/agy", home=home, env=env)
                self.assertEqual(first["model_source"], "live_probe")
                self.assertEqual(first["models"], ["gemini-4-neon-xhigh", "claude-opus-5-5-high"])
                self.assertEqual(mrun.call_count, 1)

                second = rcc.probe_backend_models("agy", executable="/usr/local/bin/agy", home=home, env=env)
                self.assertEqual(second["model_source"], "ttl_cache")
                self.assertEqual(mrun.call_count, 1)

                # Explicit refresh bypasses TTL cache
                third = rcc.probe_backend_models(
                    "agy", executable="/usr/local/bin/agy", home=home, env=env, refresh_models=True
                )
                self.assertEqual(third["model_source"], "live_probe")
                self.assertEqual(mrun.call_count, 2)

                # Expired TTL triggers refresh
                cache_file = rcc.get_cache_file_path(env=env, home=home)
                doc = json.loads(cache_file.read_text(encoding="utf-8"))
                doc["backends"]["agy"]["fetched_at"] = time.time() - (rcc.MODEL_CACHE_TTL_SECONDS + 10)
                cache_file.write_text(json.dumps(doc), encoding="utf-8")
                fourth = rcc.probe_backend_models("agy", executable="/usr/local/bin/agy", home=home, env=env)
                self.assertEqual(fourth["model_source"], "live_probe")
                self.assertEqual(mrun.call_count, 3)

    def test_build_recommended_presets_dynamic_and_env_override(self):
        fake_detected = [
            {"backend": "codex", "available": True, "supports_effort": True},
            {"backend": "dsh", "available": True, "supports_effort": False},
            {"backend": "grok", "available": True, "supports_effort": True},
            {"backend": "kimi", "available": False, "supports_effort": False},
        ]
        presets = rcc.build_recommended_presets(fake_detected, env={})
        self.assertEqual(len(presets), 3)
        self.assertEqual(presets[0]["channels"], ["codex@high", "dsh"])
        self.assertEqual(presets[1]["channels"], ["codex@high", "dsh", "grok@high"])
        self.assertEqual(presets[2]["channels"], ["codex@low", "dsh"])

        env_presets = rcc.build_recommended_presets(
            fake_detected, env={"COLD_CRITIQUE_CHANNELS": "pi:m1@high,pi:m2@high"}
        )
        self.assertEqual(env_presets[0]["channels"], ["pi:m1@high", "pi:m2@high"])


class CliIntegrationTests(unittest.TestCase):
    def test_dry_run_supports_duplicate_backend_and_argv_warning(self):
        long_text = "测试正文内容。" * 20000  # > 100 KB UTF-8
        fake_stdin = io.StringIO(long_text)
        fake_stdin.isatty = lambda: False  # type: ignore[attr-defined]
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", fake_stdin), mock.patch.object(sys, "stdout", buf):
            rc = rcc.main(["-c", "pi:m1@high", "-c", "pi:m2@low", "-c", "kimi", "--dry-run"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["channels"], ["pi:m1@high", "pi:m2@low", "kimi"])
        self.assertFalse(out["single_channel_warning"])
        self.assertTrue(any("kimi" in w for w in out["warnings"]))

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

    def test_all_seven_cli_backends_run_in_fresh_empty_workdir_with_utf8(self):
        seen_cwds: list[str] = []

        def fake_run(cmd, **kwargs):
            cwd = kwargs["cwd"]
            seen_cwds.append(cwd)
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            self.assertEqual(kwargs.get("errors"), "replace")
            self.assertNotEqual(os.path.realpath(cwd), os.path.realpath(tempfile.gettempdir()))
            self.assertEqual(os.listdir(cwd), [])
            exe_name = os.path.basename(cmd[0]).lower()
            if exe_name.startswith("codex"):
                out_path = cmd[cmd.index("-o") + 1]
                pathlib.Path(out_path).write_text("codex critique", encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")
            if exe_name.startswith("claude"):
                return mock.Mock(returncode=0, stdout=json.dumps({"result": "claude critique"}), stderr="")
            if exe_name.startswith("agy"):
                self.assertEqual(kwargs.get("stdin"), rcc.subprocess.DEVNULL)
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"status": "SUCCESS", "response": "agy critique"}),
                    stderr="",
                )
            if exe_name.startswith("grok"):
                self.assertEqual(kwargs.get("stdin"), rcc.subprocess.DEVNULL)
                pf = cmd[cmd.index("--prompt-file") + 1]
                self.assertTrue(os.path.isfile(pf))
                self.assertEqual(os.listdir(cwd), [])
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"text": "grok critique", "modelUsage": {"grok-4.7-build": {}}}),
                    stderr="",
                )
            if exe_name.startswith("pi"):
                self.assertEqual(kwargs.get("input"), "prompt")
                return mock.Mock(returncode=0, stdout="pi critique\n", stderr="")
            if exe_name.startswith("kimi"):
                self.assertEqual(kwargs.get("stdin"), rcc.subprocess.DEVNULL)
                sk = cmd[cmd.index("--skills-dir") + 1]
                self.assertEqual(os.listdir(sk), [])
                ndjson = '\n'.join([
                    json.dumps({"role": "meta", "type": "system.version"}),
                    json.dumps({"role": "assistant", "content": "kimi critique"}),
                ])
                return mock.Mock(returncode=0, stdout=ndjson, stderr="")
            if exe_name.startswith("dsh"):
                patch_file = cmd[cmd.index("--patch") + 1]
                self.assertIn("tool-bash", pathlib.Path(patch_file).read_text(encoding="utf-8"))
                self.assertEqual(kwargs.get("input"), "prompt")
                return mock.Mock(returncode=0, stdout="dsh critique\n", stderr="")
            raise AssertionError(f"unexpected cmd: {cmd}")

        with mock.patch("shutil.which", side_effect=lambda b: f"/mock/bin/{b}"), mock.patch(
            "subprocess.run", side_effect=fake_run
        ):
            results = [
                rcc.run_channel(rcc.parse_channel_spec(ch), "prompt", 30)
                for ch in ("codex", "claude", "agy", "grok", "pi", "kimi", "dsh")
            ]

        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual([r["response"] for r in results], [
            "codex critique",
            "claude critique",
            "agy critique",
            "grok critique",
            "pi critique",
            "kimi critique",
            "dsh critique",
        ])
        self.assertEqual(results[3]["model"], "grok-4.7-build")
        self.assertEqual(len(set(seen_cwds)), 7)
        for d in seen_cwds:
            self.assertFalse(os.path.exists(d))


if __name__ == "__main__":
    unittest.main()
