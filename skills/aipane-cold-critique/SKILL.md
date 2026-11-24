---
name: aipane-cold-critique
description: 物理切断长上下文，将结论、方案或文章草稿投入零前情独立评审通道，用带成立豁免权的双边对抗提示词做客观挑刺。仅在用户点名 /aipane-cold-critique 或明确要求“冷审”“反附和盲测”“零前情挑刺”时使用。
disable-model-invocation: true
user-invocable: true
argument-hint: "[可选：文件路径、指定通道/模型，或直接审上一轮总结]"
---

# 零前情对抗冷审契约

用于打破长上下文对话里的“顺着说”与同源内省盲区：把待审正文剥离前情后，交给全新、彼此独立的异构模型做纯文本对抗评审。

## 1. 隔离硬边界

1. **禁止同会话自辩**：当前会话严禁在自身上下文中直接替待审文本做“拿红笔自我反思”，也不得在收到冷审结果后替原稿强行辩解。
2. **剥壳职责分工**：
   - **调用方模型先手动剥壳**：投递前剥离聊天寒暄、上下文指代（如“正如你刚才所说”“把前后两段接起来”）以及个人/企业敏感实体。
   - **脚本自动剥壳**：`scripts/run_cold_critique.py` 默认剥离 YAML frontmatter、文末参考/检查章节与 Markdown 外链 URL（保留锚文本）；加 `--redact-paths` 同时脱敏本机主目录路径。
3. **独立通道与真实隔离级别**：统一通过 `scripts/run_cold_critique.py` 并发拉起独立非交互子进程或 HTTP 请求，各 CLI 通道均在每次新建的**独立空临时目录**中执行，并固定注入带成立豁免权的双边对抗提示词（锁定“硬凑概念合并 + 因果断链”，同时显式允许经得起推敲时直说成立）。支持对齐 `aipane` 的 7 个本地 CLI 与任意 OpenAI 兼容 API（允许同后端多次选用不同模型），按后端如实区分两级隔离：
   - **硬禁工具**：
     - `claude`：`--tools ""` + `--strict-mcp-config` + `--disable-slash-commands` + `--no-session-persistence`（stdin，支持 `effort`）。
     - `grok`：`--prompt-file` + `--tools ""` + `--no-memory` + `--no-subagents` + `--disable-web-search`（支持 `effort`）。
     - `pi`：`--no-tools` + `--no-mcp` + `--no-extensions` + `--no-skills` + `--no-context-files` + `--no-session`（stdin，支持 `effort` 映射 `--thinking`）。
     - `dsh`（别名 `dsh-tui`）：`dsh --profile headless --permission-mode read-only --approval-policy never` + 临时 `--patch` 启用凭证并禁用全部工具/技能/MCP 插件、可选覆写模型（stdin，**不支持命令行 `effort`**）。
     - `openai`：纯文本 Chat Completions API，无 `tools` 字段（HTTP JSON，支持 `effort`）。
   - **沙箱/提示词约束**：
     - `codex`：`-s read-only --ephemeral` + 独立空目录 + 提示词禁工具（stdin，支持 `effort`）。
     - `agy`（别名 `antigravity`）：`--disable-slash-commands` + 独立空目录 + 提示词禁工具（`--print=` 参数传正文，超长文本须留意系统参数上限，支持 `effort`）。
     - `kimi`：空目录 + 空 `--skills-dir` + 不传 `--auto`/`--yolo` + 提示词禁工具（`-p` 参数传正文，超长文本须留意系统参数上限，**不支持命令行 `effort`**）。

## 2. 交互选型与确认（默认先确认再执行）

在调用脚本前，优先把“起几个 Agent、分别用什么模型与 effort”的选择权交给用户，而不是默默按默认配置直接开跑：

1. **何时跳过确认**：仅当用户本次指令已明确写出具体通道/模型（例如指定了 `-c ...` 或直接点名用哪几个 Agent/模型），或明确要求“按默认直接跑”时，才跳过确认直接执行。
2. **默认单轮紧凑确认菜单**：其余情况必须先运行 `python3 <skill目录>/scripts/run_cold_critique.py --list-backends --json` 探测本机状态（6 个有本地配置/缓存的后端毫秒级直读；`agy` 等走 24h TTL 本地快照，命中时整体 `<50ms`），然后向用户一次性展示：
   - **2–3 个推荐组合预设**（来自脚本动态生成的 `presets`，不写死个人模型名，例如标准异构双通道、异构三通道深度盲审、轻量快速双审；若用户配了 `COLD_CRITIQUE_CHANNELS` 则将其置为预设 1）；
   - **本机可用 Agent 速查表**（列出各可用后端的隔离级别、当前默认模型、候选模型摘要以及是否支持命令行 `effort`）；
   - **单轮敲定提示**：说明用户可以直接回复预设编号（或“直接跑”），也可以一句话自由指定任意数量、后端组合、模型与 effort（支持同后端多开不同模型，例如 `pi:modelA@high` + `pi:modelB@high`）；若用户想看某后端全量模型或刷新缓存，按需运行 `--list-models <backend>` / `--refresh-models`。
3. **严守 `effort` 边界**：对不支持命令行 `effort` 的后端（`kimi`、`dsh`）不得擅自加 `@effort`（脚本也会显式报错拦截，防止误以为高推理生效）。

## 3. 脚本调用

脚本仅依赖 Python 3.9+ 标准库（兼容 macOS / Linux / Windows），通道语法为 `<backend>[:<model>][@<effort>][#<ENV_PREFIX>]`（支持 `claude`、`codex`、`dsh`/`dsh-tui`、`grok`、`pi`、`kimi`、`agy`/`antigravity`、`openai`）。完整参数见 `--help`。

```bash
# 探测本机可用后端、隔离级别、模型摘要与动态推荐预设（加 --refresh-models 强制刷新 TTL 缓存）
python3 <skill目录>/scripts/run_cold_critique.py --list-backends --json

# 查看单个后端的完整候选模型列表
python3 <skill目录>/scripts/run_cold_critique.py --list-models agy

# 并发运行选定的多通道组合（支持同后端多开不同模型）冷审 Markdown 文件
python3 <skill目录>/scripts/run_cold_critique.py \
  -c codex@high -c grok@high -c dsh --file /path/to/target.md --redact-paths

# 通过 stdin 传入已手动剥壳的对话总结
python3 <skill目录>/scripts/run_cold_critique.py \
  -c pi:deepseek-v4-pro@high -c pi:qwen3.8-max@high --redact-paths <<'EOF'
<剥壳后的待审裸文本>
EOF
```

## 4. 结果边界与完成态输出契约

**先明确冷审的证据边界**：零前情对抗评审产出的是**可逐条核对的反驳清单**，不是最终裁决。若多个通道同时指出了同一处断裂，存在两种可能——**原命题确实有硬伤**，或**剥壳后的待审文本漏写了关键前提**；交付时必须区分核对，不能把“多方都挑刺”直接等同于“原命题必错”。

拿到脚本输出后，直接向用户交付以下三项结构化结论（不替原稿辩解）：

1. **多通道共同指出的疑点（最高优先级）**：各通道独立命中同一处的概念硬凑（如独立维度被硬揉成一维、不同性质问题塞进同一标签）与因果跳步（如单点事实被外推为普遍结论）；顺带标明哪些可能是“原稿漏写前提”所致。
2. **各通道的互补发现**：分别列出不同通道各自独到的反例、边界漏洞或前后口径冲突，避免重复搬运。
3. **确认成立的内核与收口建议**：明确列出经校准后站得住脚的核心判断，并把原文说得过满的部分收口为两条具体判据——**“下一步具体依赖哪一句判断”**与**“现有证据足以支撑该判断到什么边界”**。
