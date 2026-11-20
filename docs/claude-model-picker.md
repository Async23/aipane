# Claude Code 模型菜单

`integrations/claude/model-launch.py` 为 Claude Code 提供启动前的模型菜单，
并按所选模型设置上下文容量。菜单确认选择后，下次打开时会默认选中这个模型。
同一个 Claude 配置目录中的选择记录跨终端、跨项目共享。

菜单采用无模式交互：搜索框始终保持可见，直接输入文字即可实时筛选；按 `↑`/`↓`、`Ctrl+J`/`Ctrl+K`、`Ctrl+N`/`Ctrl+P` 或 `Tab`/`Shift+Tab` 可在列表中首尾循环移动；按一次 `Enter` 立即启动当前高亮模型。按 `Esc` 逐层回退：搜索框非空时清空筛选并恢复全量列表，搜索框已空时取消启动；`Ctrl+C` 随时直接取消。

## 安装

需要 Python 3、安装在 `~/.local/bin/claude` 的 Claude Code，以及本机的
`settings.json` 和 `model-routing.json`。安装 `fzf` 可使用交互列表；
没有 `fzf` 或终端过小时使用文字菜单。

```bash
AIPANE_ROOT="${AIPANE_ROOT:-$HOME/.aipane}"
mkdir -p ~/.local/bin
ln -sf "$AIPANE_ROOT/integrations/claude/model-launch.py" ~/.local/bin/claude-window
```

在 `~/.zshrc` 中、加载 `init.zsh` 之前设置：

```zsh
export AIPANE_CLAUDE_LAUNCH_CMD="$HOME/.local/bin/claude-window --dangerously-skip-permissions"
```

新终端中的 `ai c` 使用该菜单。现有终端可直接执行上述 `export` 使其生效。

## 默认选择与记忆

手动打开菜单时，按以下顺序确定默认模型：

1. 显式提供的模型默认值：`--settings` 的 `env.ANTHROPIC_MODEL`、环境变量
   `ANTHROPIC_MODEL`、`--settings` 的 `model`，按此顺序取第一个有效值。
2. `--continue` 或 `--resume` 对应会话最近使用的模型。
3. 当前配置目录中，最近一次在启动菜单确认的模型。
4. `settings.json` 的 `model`。

如果最终默认模型不在菜单中，选中第一个可用模型。记忆中的模型必须同时存在于
`modelPicker.options` 和 `model-routing.json`，失效的记忆会直接跳过。

确认选择后，启动器将模型 ID 原子写入配置目录下的 `model-picker-state.json`。
默认目录是 `~/.claude`；设置 `CLAUDE_CONFIG_DIR` 时，各配置目录分别记忆。
取消菜单保留原记录。`settings.json` 保持不变。记录损坏或无法读取时使用后续默认值；
记录无法写入时提示原因，仍继续启动本次所选模型。

`--model` 直接指定模型并跳过菜单，不更新菜单记忆。非交互运行、`-p`、后台运行和
带 `AIPANE_AUTO_RESUME=1` 的自动恢复也不读取或更新菜单记忆。自动恢复沿用会话模型，
无法确定时要求显式指定 `--model`。在 Claude 会话内部切换模型不会更新启动菜单记忆。

删除当前配置目录中的 `model-picker-state.json` 即可清除记忆。

## 模型配置

`settings.json` 的 `modelPicker.options` 提供模型 ID 和显示名称；
`model-routing.json` 的 `models` 为每个 ID 提供 `context_tokens` 和可选的
`billing_source`。启动器只展示同时出现在这两处的模型。配置与认证信息保留在本机。

启动时会设置该模型的上下文容量，会话内的模型菜单只显示同一提供方、同一容量的模型。
自动恢复标记与恢复执行器的配合见 [恢复运行规则](recovery-runtime.md)。

### Google Vertex AI

在 `model-routing.json` 中为 Google 原生 Claude 模型设置 `"provider": "vertex"`。
模型 ID 使用 Google 发布的原名，例如 `claude-sonnet-5-5`。顶层 `vertex.credentials`
指向 Google 服务账号 JSON 或包含 `service_account` 的 CLIProxyAPI 凭证。
启动器读取其中的项目和代理，也支持通过 `vertex.project_id`、`vertex.https_proxy`
和 `vertex.region` 指定，区域默认为 `global`。

使用封装凭证时，启动器将标准凭证同步到该 Claude 配置目录的
`cache/vertex-adc/service-account.json`，目录权限为 `0700`，文件权限为 `0600`。
后台会话可持续刷新访问令牌，源密钥轮换后再次启动即可更新副本。
`vertex.model_defaults` 可以按 Claude Code 环境变量名设置各模型别名及子代理模型。
跨提供方切换时重新运行启动器。云端模型准入和配额由 Google Cloud 项目单独管理。

## 自动信任工作目录

在本机 `model-routing.json` 顶层设置 `"auto_trust_workspace": true` 后，
启动器会在交互启动和自动恢复前，将当前目录、所在 Git 仓库及 worktree 的主检出
登记为已信任，跳过 `Accessing workspace` 确认。以后打开的新项目也会自动登记。
这表示你选择自动信任通过该启动器打开的目录，包括其中的项目设置、hooks 和 MCP 配置。

Claude Code 在家目录中接受信任时只记住本次会话，所以家目录通常每次启动都会再问。
启动器直接写入配置中的 `projects[路径].hasTrustDialogAccepted`，也覆盖这种情况。
写入使用与 Claude 相同的配置锁，保留其他配置，并以原子替换避免文件写到一半。
配置损坏或无法写入时，保留原文件，提示原因并继续启动，由 Claude 显示原有确认。

该选项默认关闭；删除该字段或设为 `false` 后停止自动登记，已保存的信任记录仍保留。
它只控制目录信任；工具执行授权仍由 Claude 的权限模式控制。直接运行原生
`~/.local/bin/claude` 不经过启动器。配置文件位置遵循 `CLAUDE_CONFIG_DIR` 和
旧版 `.config.json` 的选择规则，多个配置目录分别记录信任。

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_claude_model_*.py'
```

测试使用临时配置和占位 Claude 程序，覆盖跨进程记忆、取消、模型移除、配置隔离、
恢复优先级、真实 `fzf` 交互、自动信任、worktree 和并发写入，不调用模型 API。
