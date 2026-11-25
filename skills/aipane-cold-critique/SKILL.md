---
name: aipane-cold-critique
description: 物理切断长上下文，将结论、方案、文章或代码对照投入零前情异构评审通道，用带成立豁免权的双边对抗提示词做客观挑刺。仅在用户点名 /aipane-cold-critique 或明确要求“冷审”“反附和盲测”“零前情挑刺”时使用。
disable-model-invocation: true
user-invocable: true
argument-hint: "[可选：新问题、文件路径、指定通道/模型，或直接审上轮结论]"
---

# 零前情对抗冷审契约

打破长上下文对话里的“顺着说”与同源内省盲区：剥离前情后交给全新、彼此独立的异构模型做纯文本对抗评审，再面向最终读者交付校准后的结论。

## 1. 隔离与剥壳边界

1. **禁止同会话自辩**：严禁在当前上下文内自评替代外部冷审，也不得在收到冷审结果后替原稿强行辩解。
2. **剥壳留前提**：
   - **调用方模型先手动剥壳**：去掉寒暄、上下文指代（如“正如刚才所说”）及个人/企业敏感实体，但**必须保留决定结论成立的关键前提与用户已声明约束**，避免因漏前提引发无效挑刺。
   - **脚本自动剥壳**：`scripts/run_cold_critique.py` 默认剥离 YAML frontmatter、文末参考/检查章节与外链 URL（保留锚文本）；加 `--redact-paths` 同时脱敏本机用户目录路径。
3. **真实隔离以脚本为准**：所有通道统一经 `scripts/run_cold_critique.py` 在独立空临时目录并发执行；各后端的可用状态、真实隔离级别（硬禁工具 vs 沙箱/配置隔离）与 `effort` 支持情况，以 `--list-backends --json` 实时输出为唯一事实来源。

## 2. 输入衔接与单轮确认

1. **两类输入衔接**：
   - 已有待审物（上轮结论、本地文件、隔壁 pane 输出）直接剥壳送审。
   - 用户直接带新问题调用（如 `/aipane-cold-critique <问题>`）时，先写出紧凑、可证伪的裸初稿落盘；若还需等用户确认通道，只展示核心立场摘要，不把几千字未校准初稿整篇刷屏。
2. **何时跳过确认**：用户已指定具体通道/模型、同会话要求“沿用上次配置”，或明确“按默认直接跑”时，直接执行。
3. **默认单轮确认菜单**：其余情况先运行 `python3 <skill目录>/scripts/run_cold_critique.py --list-backends --json`，一次性展示：
   - 动态推荐预设（来自 `presets`，若配置了 `COLD_CRITIQUE_CHANNELS` 则置为预设 1；日常优先 `high`/`xhigh`，关键终稿再开 `max`）；
   - 本机可用后端速查表（隔离级别、默认模型、候选摘要与是否支持 `effort`）；
   - 说明可直接回复预设编号，或自由指定任意组合、同后端多开及本地唯一模型简写（如 `codex:<模型简写>@xhigh`）。

## 3. 脚本调用

仅依赖 Python 3.9+ 标准库。通道语法：`<backend>[:<model>][@<effort>][#<ENV_PREFIX>]`（完整参数见 `--help`）。未显式传 `--timeout` 时脚本按 effort 自动放大默认超时（常规 300s、`xhigh` 600s、`max` 900s）。

```bash
# 探测可用后端、隔离级别、模型摘要与真异构推荐预设
python3 <skill目录>/scripts/run_cold_critique.py --list-backends --json

# 按体裁并发运行并逐路原子落盘（--profile 可选 summary / article / code）
python3 <skill目录>/scripts/run_cold_critique.py \
  --profile article --out-dir /path/to/cold_run --redact-paths --json \
  -c codex@xhigh -c agy -c grok@high --file /path/to/target.md

# 若个别通道超时或失败，用相同 --out-dir 加 --resume 仅补跑未成功通道
python3 <skill目录>/scripts/run_cold_critique.py \
  --profile article --out-dir /path/to/cold_run --resume --redact-paths --json \
  -c codex@xhigh -c agy -c grok@high --file /path/to/target.md
```

`--json` 输出核心字段：`input_chars`、`out_dir`、`results[].{channel, backend, model, ok, duration_seconds, chars, response, error, result_file}`。

## 4. 证据边界与交付契约

**证据边界**：冷审产出的是可逐条核对的反驳清单，不是自动裁决；多路同时命中同一处时，须先核对区分**原命题真硬伤**与**剥壳漏写前提**。

**面向最终读者交付（默认平实直白，不把多路审稿底稿整篇倒进对话）**：

1. **校准后的直接结论与行动建议（放最前面）**：用日常语言直接回答当前问题，说明相比原稿修正了什么、哪些核心判断确认成立，以及下一步依赖哪一句判断与现有证据边界。
2. **真正改变结论的关键疑点（去重精简）**：仅列经核实的多路共指真硬伤，以及足以改变判断的单路独到反例；不搬运重复细枝末节或纯漏前提误伤。
3. **底稿按需查阅**：在文末给出 `--out-dir` 中的送审剥壳稿与原始结果路径，并附简明通道成败与耗时摘要。仅当用户明确要求逐条审校或生成批量报告时，再展开全量清单。
