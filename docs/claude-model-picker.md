# Claude Code 模型菜单

`integrations/claude/model-launch.py` 为 Claude Code 提供启动前的模型菜单，
并按所选模型设置上下文容量。菜单确认选择后，下次打开时会默认选中这个模型。
同一个 Claude 配置目录中的选择记录跨终端、跨项目共享。

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

启动时会设置该模型的上下文容量，并将会话内部的模型选项限制为相同容量的模型。
自动恢复标记与恢复执行器的配合见 [恢复运行规则](recovery-runtime.md)。

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_claude_model_*.py'
```

测试使用临时配置和占位 Claude 程序，覆盖跨进程记忆、取消、模型移除、配置隔离、
恢复优先级和真实 `fzf` 交互，不调用模型 API。
