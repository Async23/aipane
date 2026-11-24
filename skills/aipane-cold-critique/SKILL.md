---
name: aipane-cold-critique
description: 物理切断长上下文，将结论、方案或文章草稿投入零前情独立评审通道，用带成立豁免权的双边对抗提示词做客观挑刺。仅在用户点名 /aipane-cold-critique 或明确要求“冷审”“反附和盲测”“零前情挑刺”时使用。
disable-model-invocation: true
user-invocable: true
argument-hint: "[可选：文件路径、-c <通道> 或直接审上一轮总结]"
---

# 零前情对抗冷审契约

用于打破长上下文对话里的“顺着说”与同源内省盲区：把待审正文剥离前情后，交给全新、彼此独立的异构模型做纯文本对抗评审。

## 1. 隔离硬边界

1. **禁止同会话自辩**：当前会话严禁在自身上下文中直接替待审文本做“拿红笔自我反思”，也不得在收到冷审结果后替原稿强行辩解。
2. **剥壳职责分工**：
   - **调用方模型先手动剥壳**：投递前剥离聊天寒暄、上下文指代（如“正如你刚才所说”“把前后两段接起来”）以及个人/企业敏感实体。
   - **脚本自动剥壳**：`scripts/run_cold_critique.py` 默认剥离 YAML frontmatter、文末参考/检查章节与 Markdown 外链 URL（保留锚文本）；加 `--redact-paths` 同时脱敏本机主目录路径。
3. **独立通道与真实隔离级别**：统一通过 `scripts/run_cold_critique.py` 拉起独立非交互子进程或 HTTP 请求，各 CLI 通道均在每次新建的**独立空临时目录**中执行，并固定注入带成立豁免权的双边对抗提示词（锁定“硬凑概念合并 + 因果断链”，同时显式允许经得起推敲时直说成立）。按后端区分工具隔离级别：
   - **硬禁工具**：`claude`（`--tools ""` + `--strict-mcp-config` + `--no-session-persistence`）与 `openai`（无 `tools` 字段的纯文本 Chat Completions）。
   - **沙箱/提示词约束**：`codex`（`-s read-only --ephemeral` + 独立空目录 + 提示词禁工具）与 `agy`（`--disable-slash-commands` + 独立空目录 + 提示词禁工具；正文经命令行参数 `--print=` 传入，超长文本须留意系统参数长度限制）。

## 2. 脚本调用

脚本仅依赖 Python 3.9+ 标准库，通道语法为 `<backend>[:<model>][@<effort>][#<ENV_PREFIX>]`（支持 `codex`、`claude`、`agy`、`openai`）。可重复传 `-c/--channel` 并发执行异构通道；省略 `-c` 时优先读取 `COLD_CRITIQUE_CHANNELS` 环境变量（逗号或分号分隔），未配置时自动探测最多两个可用后端。完整参数与环境变量规则见 `--help` 和 `--list-backends`。

```bash
# 查看当前环境可用的后端与默认通道配置
python3 <skill目录>/scripts/run_cold_critique.py --list-backends

# 使用默认通道（COLD_CRITIQUE_CHANNELS 或自动探测）冷审 Markdown 文件
python3 <skill目录>/scripts/run_cold_critique.py --file /path/to/target.md --redact-paths

# 显式指定异构双通道，或通过 stdin 传入已剥壳的对话总结
python3 <skill目录>/scripts/run_cold_critique.py -c codex@high -c claude:opus@high --redact-paths <<'EOF'
<剥壳后的待审裸文本>
EOF
```

## 3. 结果边界与完成态输出契约

**先明确冷审的证据边界**：零前情对抗评审产出的是**可逐条核对的反驳清单**，不是最终裁决。若多个通道同时指出了同一处断裂，存在两种可能——**原命题确实有硬伤**，或**剥壳后的待审文本漏写了关键前提**；交付时必须区分核对，不能把“多方都挑刺”直接等同于“原命题必错”。

拿到脚本输出后，直接向用户交付以下三项结构化结论（不替原稿辩解）：

1. **多通道共同指出的疑点（最高优先级）**：各通道独立命中同一处的概念硬凑（如独立维度被硬揉成一维、不同性质问题塞进同一标签）与因果跳步（如单点事实被外推为普遍结论）；顺带标明哪些可能是“原稿漏写前提”所致。
2. **各通道的互补发现**：分别列出不同通道各自独到的反例、边界漏洞或前后口径冲突，避免重复搬运。
3. **确认成立的内核与收口建议**：明确列出经校准后站得住脚的核心判断，并把原文说得过满的部分收口为两条具体判据——**“下一步具体依赖哪一句判断”**与**“现有证据足以支撑该判断到什么边界”**。
