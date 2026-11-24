# aipane Agent Skills

`aipane` 内置的中文 Agent Skills 合集，统一采用 `aipane-*` 命名空间，包含 **1 个终端工作台配套技能**（`aipane-sessions`）与 **7 个可脱离 `aipane` 运行时独立复用的通用工作流技能**（代码与文档治理、零前情对抗评审、第一性原理推演、公开长文改写、Obsidian CLI 操作、Skill 编写规范）。

## 技能目录

每个技能的完整规则、触发边界与验收标准以其各自的 `SKILL.md` 为唯一事实来源：

| 序号 | 分类 | 技能名称 | 用途说明 | 运行依赖 | 入口 |
|---:|---|---|---|---|---|
| 1 | 工作台配套 | `aipane-sessions` | 零索引查询、定位并读取跨 7 个编程 Agent 的本地会话历史与同窗口 tmux pane 交接 | 本机已安装 `aipane-sessions` CLI | [SKILL.md](aipane-sessions/SKILL.md) |
| 2 | 代码与文档治理 | `aipane-semantic-residue-review` | 概念、状态模型或接口契约变更后，全仓审计旧逻辑、旧假设与过期分支残留 | 无（纯文档，可独立复用） | [SKILL.md](aipane-semantic-residue-review/SKILL.md) |
| 3 | 代码与文档治理 | `aipane-readme-governance` | 约束 README 只承载稳定入口与权威文档索引，避免多处重复维护 | 无（纯文档，可独立复用） | [SKILL.md](aipane-readme-governance/SKILL.md) |
| 4 | 独立评审与思考 | `aipane-cold-critique` | 切断长上下文，用零前情、禁工具的独立双通道模型对结论或草稿做对抗挑刺 | Python 3.9+；本地 CLI（`codex`/`claude`/`agy`）或 OpenAI 兼容 API（可独立复用） | [SKILL.md](aipane-cold-critique/SKILL.md) |
| 5 | 独立评审与思考 | `aipane-first-principles` | 从底层约束、边界前提与可核验证据分析架构缺陷与方案取舍 | 无（纯文档，可独立复用） | [SKILL.md](aipane-first-principles/SKILL.md) |
| 6 | 写作与知识库 | `aipane-article-polish` | 将素材、初稿或内部复盘做“领域移植”深度脱敏，改写为公开独立长文 | 无（纯文档，可独立复用） | [SKILL.md](aipane-article-polish/SKILL.md) |
| 7 | 写作与知识库 | `aipane-obsidian-cli` | 查搜管理走官方 Obsidian CLI（不占 MCP 上下文），写正文走 Agent 原生文件编辑并回读校验 | 官方 `obsidian` CLI（可独立复用） | [SKILL.md](aipane-obsidian-cli/SKILL.md) |
| 8 | Skill 编写规范 | `aipane-skill-craft` | 编写、精简与重构 `SKILL.md` 和 `AGENTS.md` 的内容分层与删减测试 | 无（纯文档，可独立复用） | [SKILL.md](aipane-skill-craft/SKILL.md) |

## 安装方式

### 方式一：通过 `skills` CLI 安装（推荐）

```bash
# 安装全部技能
npx skills add Async23/aipane

# 仅安装指定技能（不依赖 aipane 终端工作台时可按需挑选）
npx skills add Async23/aipane --skill aipane-cold-critique
npx skills add Async23/aipane --skill aipane-semantic-residue-review
```

### 方式二：按需复制或软链单个技能目录

将所需技能目录复制到你的 Agent 技能目录（例如 `~/.agents/skills/`、`~/.claude/skills/` 或项目级 `.agents/skills/`）：

```bash
git clone https://github.com/Async23/aipane.git ~/.aipane
cp -r ~/.aipane/skills/aipane-semantic-residue-review ~/.agents/skills/
# 若已在本机维护 aipane 仓库，也可使用软链保持同步
ln -sfn ~/.aipane/skills/aipane-cold-critique ~/.agents/skills/aipane-cold-critique
```

## 边界说明

- **运行时解耦**：除 `aipane-sessions` 依赖本机 `aipane-sessions` CLI 外，其余 7 个技能均不依赖 `aipane` 终端工作台、macOS、zsh 或 tmux，可在任意平台独立安装使用。
- **命名空间**：本目录全部技能统一使用 `aipane-*` 前缀，避免与宿主内置技能或社区同名技能冲突。
- **触发方式**：除 `aipane-cold-critique` 内置零第三方依赖的 Python 脚本且设为点名触发（`disable-model-invocation: true`，防误调外部模型）外，其余 7 个技能均支持自然语言自触发。
- **按需配置**：`aipane-obsidian-cli` 需要在个人 `AGENTS.md` 或调用指令中指定实际 Vault 名称；`aipane-cold-critique` 至少需要配置一个可用后端（支持通过 `COLD_CRITIQUE_CHANNELS` 指定默认通道），详见各自 `SKILL.md`。

## 自测

```bash
# 独立运行冷审技能目录内单元测试
python3 -m unittest discover -s skills/aipane-cold-critique/tests -p 'test_*.py'

# 通过仓库顶层桥接测试同时校验技能契约与冷审脚本
python3 tests/test_skills_cold_critique.py
```
