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


class ChannelSpecAndShorthandTests(unittest.TestCase):
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

    def test_resolve_model_shorthand_unique_effort_ambiguous_and_passthrough(self):
        codex_models = ["gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-astra"]
        r1 = rcc.resolve_model_shorthand(rcc.parse_channel_spec("codex:6.2-astra@xhigh"), codex_models)
        self.assertEqual(r1.model, "gpt-6.2-astra")
        self.assertEqual(r1.label, "codex:gpt-6.2-astra@xhigh")

        claude_models = ["kimi-k3", "kimi-k3.1-pro", "grok-4.7"]
        r2 = rcc.resolve_model_shorthand(rcc.parse_channel_spec("claude:k3@max"), claude_models)
        self.assertEqual(r2.model, "kimi-k3")

        agy_models = ["gemini-4-neon-high", "gemini-4-neon-xhigh", "gemini-4-neon-low", "gemini-4-carbon-high"]
        r3 = rcc.resolve_model_shorthand(rcc.parse_channel_spec("agy:neon@xhigh"), agy_models)
        self.assertEqual(r3.model, "gemini-4-neon-xhigh")

        # Ambiguous shorthand without effort must fail fast
        with self.assertRaisesRegex(ValueError, "匹配到多个候选模型"):
            rcc.resolve_model_shorthand(rcc.parse_channel_spec("agy:neon"), agy_models)

        # Unknown new remote model passes through untouched
        r4 = rcc.resolve_model_shorthand(rcc.parse_channel_spec("codex:future-model-9@high"), codex_models)
        self.assertEqual(r4.model, "future-model-9")

    def test_adaptive_channel_timeouts(self):
        self.assertEqual(rcc.resolve_channel_timeout(rcc.parse_channel_spec("codex@high")), 300)
        self.assertEqual(rcc.resolve_channel_timeout(rcc.parse_channel_spec("codex@xhigh")), 900)
        self.assertEqual(rcc.resolve_channel_timeout(rcc.parse_channel_spec("codex@max")), 1200)
        self.assertEqual(rcc.resolve_channel_timeout(rcc.parse_channel_spec("agy:gemini-4-neon-xhigh")), 900)
        self.assertEqual(rcc.resolve_channel_timeout(rcc.parse_channel_spec("codex@max"), explicit_timeout=420), 420)
        self.assertIsNone(rcc.resolve_channel_timeout(rcc.parse_channel_spec("codex@max"), explicit_timeout=0))
        with self.assertRaises(ValueError):
            rcc.resolve_channel_timeout(rcc.parse_channel_spec("codex@high"), explicit_timeout=-1)

        # Bare backend inherits local default model suffix (including raw display name) and default effort
        self.assertEqual(
            rcc.resolve_channel_timeout(rcc.parse_channel_spec("agy"), default_model="Gemini 4 Neon (XHigh)"),
            900,
        )
        self.assertEqual(
            rcc.resolve_channel_timeout(rcc.parse_channel_spec("codex"), default_effort="xhigh"),
            900,
        )
        self.assertEqual(
            rcc.resolve_channel_timeout(rcc.parse_channel_spec("pi"), default_effort="max"),
            1200,
        )
        self.assertEqual(
            rcc.resolve_channel_timeout(rcc.parse_channel_spec("codex:gpt-6.2-astra"), default_effort="xhigh"),
            900,
        )
        # Explicit @effort overrides local defaults instead of being widened by them
        self.assertEqual(
            rcc.resolve_channel_timeout(
                rcc.parse_channel_spec("agy@high"),
                default_model="gemini-4-neon-xhigh",
                default_effort="xhigh",
            ),
            300,
        )
        # Explicit non-xhigh model does not inherit local default model suffix
        self.assertEqual(
            rcc.resolve_channel_timeout(
                rcc.parse_channel_spec("agy:gemini-4-neon-low"),
                default_model="gemini-4-neon-xhigh",
            ),
            300,
        )


class CommandBuilderTests(unittest.TestCase):
    def test_codex_command_enforces_readonly_ephemeral_and_config_isolation(self):
        spec = rcc.parse_channel_spec("codex:o3@xhigh")
        cmd = rcc.build_codex_command(
            spec,
            "/tmp/out.txt",
            executable="/usr/local/bin/codex",
            disabled_mcp_servers=["chrome-devtools", "custom.srv"],
        )
        self.assertEqual(cmd[:6], ["/usr/local/bin/codex", "exec", "--skip-git-repo-check", "-s", "read-only", "--ephemeral"])
        for feat in rcc.CODEX_DISABLED_FEATURES:
            idx = [i for i, v in enumerate(cmd) if v == "--disable" and i + 1 < len(cmd) and cmd[i + 1] == feat]
            self.assertTrue(idx, f"missing --disable {feat}")
        self.assertIn('developer_instructions=""', cmd)
        self.assertIn('model_verbosity="medium"', cmd)
        self.assertIn("notify=[]", cmd)
        self.assertIn("mcp_servers.chrome-devtools.enabled=false", cmd)
        self.assertIn('mcp_servers."custom.srv".enabled=false', cmd)
        self.assertIn("-m", cmd)
        self.assertIn("o3", cmd)
        self.assertIn('model_reasoning_effort="xhigh"', cmd)
        self.assertEqual(cmd[-3:], ["-o", "/tmp/out.txt", "-"])

    def test_list_codex_enabled_mcp_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(
                '[mcp_servers.alpha]\ncommand = "a"\n\n'
                '[mcp_servers.beta]\ncommand = "b"\nenabled = false\n\n'
                '[mcp_servers.gamma]\nurl = "https://example.com"\nenabled = true\n',
                encoding="utf-8",
            )
            self.assertEqual(rcc.list_codex_enabled_mcp_servers(home=home), ["alpha", "gamma"])

    def test_claude_command_disables_all_tools_hooks_and_session(self):
        spec = rcc.parse_channel_spec("claude:sonnet@medium")
        cmd = rcc.build_claude_command(spec)
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--disable-slash-commands", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--settings", cmd)
        self.assertEqual(json.loads(cmd[cmd.index("--settings") + 1]), {"disableAllHooks": True})
        self.assertNotIn("--bare", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("sonnet", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("medium", cmd)

    def test_agy_command_uses_sandbox_and_stream_json_stdin(self):
        spec = rcc.parse_channel_spec("agy:demo-model@high")
        cmd = rcc.build_agy_command(spec)
        self.assertIn("--disable-slash-commands", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertEqual(cmd[cmd.index("--input-format") + 1], "stream-json")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--model", cmd)
        self.assertIn("demo-model", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("high", cmd)
        payload = json.loads(rcc.build_agy_stream_input("hello"))
        self.assertEqual(payload, {"event": "user", "message": {"role": "user", "content": "hello"}})

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
    def test_local_probes_claude_picker_and_agy_slug_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            cache_dir = home / "cache"
            env = {"COLD_CRITIQUE_CACHE_DIR": str(cache_dir)}

            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text('model = "gpt-test"\nmodel_reasoning_effort = "high"\n', encoding="utf-8")
            (home / ".codex" / "models_cache.json").write_text(
                json.dumps({"models": [{"slug": "gpt-test"}, {"slug": "gpt-alt"}]}), encoding="utf-8"
            )
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(
                json.dumps({
                    "model": "gcp/gemini-4-argon",
                    "modelPicker": {"options": [{"model": "kimi-k3"}, {"model": "deepseek-v4-pro"}]},
                    "modelSettings": {"qwen3.8-max": {}},
                    "availableModels": ["shadow-ignored"],
                }),
                encoding="utf-8",
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
                json.dumps({"model": "Gemini 4 Neon (XHigh)"}), encoding="utf-8"
            )

            codex_meta = rcc.probe_backend_models("codex", home=home, env=env)
            self.assertEqual(codex_meta["default_model"], "gpt-test")
            self.assertIn("gpt-alt", codex_meta["models"])

            claude_meta = rcc.probe_backend_models("claude", home=home, env=env)
            self.assertEqual(claude_meta["default_model"], "gcp/gemini-4-argon")
            self.assertIn("kimi-k3", claude_meta["models"])
            self.assertIn("deepseek-v4-pro", claude_meta["models"])
            self.assertIn("qwen3.8-max", claude_meta["models"])
            self.assertNotIn("shadow-ignored", claude_meta["models"])

            kimi_meta = rcc.probe_backend_models("kimi", home=home, env=env)
            self.assertEqual(kimi_meta["default_model"], "kimi-code/k3")
            self.assertIn("kimi-code/k3.1-pro", kimi_meta["models"])

            pi_meta = rcc.probe_backend_models("pi", home=home, env=env)
            self.assertEqual(pi_meta["default_model"], "p1/m1")
            self.assertIn("m2", pi_meta["models"])

            fake_agy_out = "Fetching available models...\ngemini-4-neon-xhigh\tGemini 4 Neon (XHigh)\nclaude-opus-5-5-high\tClaude Opus\n"
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=fake_agy_out, stderr="")) as mrun:
                first = rcc.probe_backend_models("agy", executable="/usr/local/bin/agy", home=home, env=env)
                self.assertEqual(first["model_source"], "live_probe")
                self.assertEqual(first["default_model"], "gemini-4-neon-xhigh")
                self.assertEqual(first["models"], ["gemini-4-neon-xhigh", "claude-opus-5-5-high"])
                self.assertEqual(mrun.call_count, 1)

                second = rcc.probe_backend_models("agy", executable="/usr/local/bin/agy", home=home, env=env)
                self.assertEqual(second["model_source"], "ttl_cache")
                self.assertEqual(second["default_model"], "gemini-4-neon-xhigh")
                self.assertEqual(mrun.call_count, 1)

                third = rcc.probe_backend_models(
                    "agy", executable="/usr/local/bin/agy", home=home, env=env, refresh_models=True
                )
                self.assertEqual(third["model_source"], "live_probe")
                self.assertEqual(mrun.call_count, 2)

                cache_file = rcc.get_cache_file_path(env=env, home=home)
                doc = json.loads(cache_file.read_text(encoding="utf-8"))
                doc["backends"]["agy"]["fetched_at"] = time.time() - (rcc.MODEL_CACHE_TTL_SECONDS + 10)
                cache_file.write_text(json.dumps(doc), encoding="utf-8")
                fourth = rcc.probe_backend_models("agy", executable="/usr/local/bin/agy", home=home, env=env)
                self.assertEqual(fourth["model_source"], "live_probe")
                self.assertEqual(mrun.call_count, 3)

    def test_build_recommended_presets_avoids_same_family_false_heterogeneity(self):
        fake_detected = [
            {"backend": "codex", "available": True, "supports_effort": True, "default_model": "gpt-6.2-astra"},
            {"backend": "agy", "available": True, "supports_effort": True, "default_model": "gemini-4-neon-xhigh"},
            {"backend": "claude", "available": True, "supports_effort": True, "default_model": "gcp/gemini-4-argon"},
            {"backend": "grok", "available": True, "supports_effort": True, "default_model": "grok-4.7"},
        ]
        presets = rcc.build_recommended_presets(fake_detected, env={})
        self.assertEqual(len(presets), 3)
        self.assertEqual(presets[0]["channels"], ["codex@high", "agy@high"])
        # Preset 2 must skip claude (also gemini) and pick grok (xai) for true 3-family heterogeneity
        self.assertEqual(presets[1]["channels"], ["codex@high", "agy@high", "grok@high"])
        self.assertEqual(presets[2]["channels"], ["codex@low", "agy@low"])

        env_presets = rcc.build_recommended_presets(
            fake_detected, env={"COLD_CRITIQUE_CHANNELS": "pi:m1@high,pi:m2@high"}
        )
        self.assertEqual(env_presets[0]["channels"], ["pi:m1@high", "pi:m2@high"])


class CliIntegrationAndPersistenceTests(unittest.TestCase):
    def test_dry_run_profiles_adaptive_timeouts_and_argv_warning(self):
        long_text = "测试正文内容。" * 20000  # > 100 KB UTF-8
        fake_stdin = io.StringIO(long_text)
        fake_stdin.isatty = lambda: False  # type: ignore[attr-defined]
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", fake_stdin), mock.patch.object(sys, "stdout", buf), mock.patch.object(
            rcc, "probe_local_timeout_defaults", return_value={}
        ):
            rc = rcc.main(["--profile", "article", "-c", "pi:m1@xhigh", "-c", "agy:demo@max", "-c", "kimi", "--dry-run"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["profile"], "article")
        self.assertEqual(out["channels"], ["pi:m1@xhigh", "agy:demo@max", "kimi"])
        self.assertEqual(out["timeouts"], {"pi:m1@xhigh": 900, "agy:demo@max": 1200, "kimi": 300})
        self.assertIn("跨学科概念借用", out["preview_head"])
        self.assertFalse(out["single_channel_warning"])
        # Only kimi uses argv now; agy uses stream-json stdin so must not warn
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("通道 'kimi'", out["warnings"][0])
        self.assertNotIn("通道 'agy'", out["warnings"][0])

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

    def test_out_dir_atomic_persistence_and_resume_skips_succeeded_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = pathlib.Path(tmp) / "run1"
            target = pathlib.Path(tmp) / "target.md"
            target.write_text("测试待审命题正文。\n", encoding="utf-8")

            calls: list[tuple[str, int]] = []

            def fake_run_channel(spec: rcc.ChannelSpec, full_prompt: str, timeout_s: int):
                calls.append((spec.label, timeout_s))
                if spec.backend == "grok" and len(calls) <= 2:
                    return rcc._err_result(spec, "timed out after 600s", 600.0)
                return rcc._ok_result(spec, f"critique from {spec.label}", 12.5)

            with mock.patch.object(rcc, "run_channel", side_effect=fake_run_channel):
                buf1 = io.StringIO()
                with mock.patch.object(sys, "stdout", buf1):
                    rc1 = rcc.main([
                        "--profile", "code",
                        "--file", str(target),
                        "--out-dir", str(out_dir),
                        "--json",
                        "-c", "codex@xhigh",
                        "-c", "grok@xhigh",
                    ])
                self.assertEqual(rc1, 0)
                out1 = json.loads(buf1.getvalue())
                self.assertTrue((out_dir / "input.md").is_file())
                self.assertTrue((out_dir / "prompt.txt").is_file())
                self.assertTrue((out_dir / "summary.json").is_file())
                self.assertEqual(len(calls), 2)
                self.assertTrue(out1["results"][0]["ok"])
                self.assertFalse(out1["results"][1]["ok"])

                # Resume must skip succeeded codex@xhigh and only rerun failed grok@xhigh
                buf2 = io.StringIO()
                with mock.patch.object(sys, "stdout", buf2):
                    rc2 = rcc.main([
                        "--profile", "code",
                        "--file", str(target),
                        "--out-dir", str(out_dir),
                        "--resume",
                        "--timeout", "720",
                        "--json",
                        "-c", "codex@xhigh",
                        "-c", "grok@xhigh",
                    ])
                self.assertEqual(rc2, 0)
                out2 = json.loads(buf2.getvalue())
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[2], ("grok@xhigh", 720))
                self.assertTrue(out2["results"][0]["resumed"])
                self.assertTrue(out2["results"][1]["ok"])
                self.assertFalse(out2["results"][1].get("resumed", False))

                # Changing input content must invalidate cache even with --resume
                target.write_text("修改后的待审正文。\n", encoding="utf-8")
                buf3 = io.StringIO()
                with mock.patch.object(sys, "stdout", buf3):
                    rc3 = rcc.main([
                        "--profile", "code",
                        "--file", str(target),
                        "--out-dir", str(out_dir),
                        "--resume",
                        "--json",
                        "-c", "codex@xhigh",
                        "-c", "grok@xhigh",
                    ])
                self.assertEqual(rc3, 0)
                self.assertEqual(len(calls), 5)

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
                self.assertIn("--settings", cmd)
                return mock.Mock(returncode=0, stdout=json.dumps({"result": "claude critique"}), stderr="")
            if exe_name.startswith("agy"):
                self.assertIn("--sandbox", cmd)
                sent = json.loads(kwargs.get("input", ""))
                self.assertEqual(sent["event"], "user")
                self.assertEqual(sent["message"]["content"], "prompt")
                ndjson = "\n".join([
                    json.dumps({"event": "init", "init": {"model": "gemini-4-neon-xhigh"}}),
                    json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "agy critique", "duration_seconds": 3.2}}),
                ])
                return mock.Mock(returncode=0, stdout=ndjson, stderr="")
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
                ndjson = "\n".join([
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
        ), mock.patch.object(rcc, "list_codex_enabled_mcp_servers", return_value=[]):
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
        self.assertEqual(results[2]["model"], "gemini-4-neon-xhigh")
        self.assertEqual(results[3]["model"], "grok-4.7-build")
        self.assertEqual(len(set(seen_cwds)), 7)
        for d in seen_cwds:
            self.assertFalse(os.path.exists(d))

    def test_probe_local_timeout_defaults_and_zero_timeout_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text('model = "gpt-test"\nmodel_reasoning_effort = "xhigh"\n', encoding="utf-8")
            (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
            (home / ".gemini" / "antigravity-cli" / "settings.json").write_text(
                json.dumps({"model": "Gemini 4 Neon (XHigh)"}), encoding="utf-8"
            )
            env = {
                "COLD_CRITIQUE_CACHE_DIR": str(home / "cache"),
                "OPENAI_API_KEY": "sk-fake",
                "OPENAI_MODEL": "demo-max",
            }
            specs = [
                rcc.parse_channel_spec("codex"),
                rcc.parse_channel_spec("agy"),
                rcc.parse_channel_spec("openai"),
                rcc.parse_channel_spec("codex@high"),
            ]
            with mock.patch("subprocess.run") as msub:
                defaults = rcc.probe_local_timeout_defaults(specs, env=env, home=home)
                self.assertEqual(msub.call_count, 0)

            self.assertEqual(
                rcc.resolve_channel_timeout(
                    specs[0],
                    default_model=defaults[("codex", None)][0],
                    default_effort=defaults[("codex", None)][1],
                ),
                900,
            )
            self.assertEqual(
                rcc.resolve_channel_timeout(
                    specs[1],
                    default_model=defaults[("agy", None)][0],
                    default_effort=defaults[("agy", None)][1],
                ),
                900,
            )
            self.assertEqual(
                rcc.resolve_channel_timeout(
                    specs[2],
                    default_model=defaults[("openai", None)][0],
                    default_effort=defaults[("openai", None)][1],
                ),
                1200,
            )
            self.assertEqual(
                rcc.resolve_channel_timeout(
                    specs[3],
                    default_model=defaults[("codex", None)][0],
                    default_effort=defaults[("codex", None)][1],
                ),
                300,
            )

        # CLI --timeout 0 -> None in dry-run, negative rejected
        fake_stdin = io.StringIO("待审内容\n")
        fake_stdin.isatty = lambda: False  # type: ignore[attr-defined]
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", fake_stdin), mock.patch.object(sys, "stdout", buf):
            rc = rcc.main(["-c", "pi:m1@xhigh", "--timeout", "0", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["timeouts"], {"pi:m1@xhigh": None})

        with mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                rcc.main(["-c", "pi:m1@xhigh", "--timeout", "-1", "--dry-run"])
            self.assertEqual(cm.exception.code, 2)

        # End-to-end --timeout 0 passes timeout=None to subprocess.run and urlopen
        seen_sub_timeouts: list[object] = []

        def fake_sub_run(cmd, **kwargs):
            seen_sub_timeouts.append(kwargs.get("timeout"))
            return mock.Mock(returncode=0, stdout="pi ok\n", stderr="")

        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = json.dumps({
            "model": "gpt-4.1",
            "choices": [{"message": {"role": "assistant", "content": "openai ok"}}],
        }).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-fake"}, clear=True), mock.patch(
            "shutil.which", return_value="/mock/bin/pi"
        ), mock.patch("subprocess.run", side_effect=fake_sub_run), mock.patch(
            "urllib.request.urlopen", return_value=fake_resp
        ) as mocked_open:
            results, _ = rcc.execute_channels(
                [rcc.parse_channel_spec("pi"), rcc.parse_channel_spec("openai:gpt-4.1")],
                "prompt",
                explicit_timeout=0,
            )
            self.assertTrue(all(r["ok"] for r in results), results)
            self.assertEqual([r["timeout_seconds"] for r in results], [None, None])
            self.assertEqual(seen_sub_timeouts, [None])
            self.assertIsNone(mocked_open.call_args.kwargs.get("timeout"))


if __name__ == "__main__":
    unittest.main()
