# 会话恢复与稳定运行版本

`ai-restart` 展示当前 tmux 服务的完整重启计划，确认后在原窗格续接会话。
已有失败记录会单独显示，不会把普通命令切换成“只补恢复”。

## 日常操作

```bash
ai-restart --dry-run                 # 刷新快照，预览完整计划及遗留记录
ai-restart                           # 确认后执行当前计划
ai-restart --retry --dry-run         # 仅预览上次未完成的恢复
ai-restart --retry                   # 仅重试持久保存的恢复意图
ai-restart --discard-pending ID      # 确认后归档指定记录，不操作窗格
```

`ID` 来自遗留记录列表。重试失败会显示本次原因；会话被其他 Codex 实例占用时，
需要先处理重复实例。放弃记录只结束这条恢复意图，会话文件保留。

`--yes` 跳过普通重启确认；`unknown` 活动状态仍需要交互确认空闲，或显式使用
`--force`。`--force` 允许接受 busy / unknown 活动状态，
仍须通过窗格身份、会话身份和写入冲突检查。确认期间发生身份变化会中止计划。

未纳入当前计划的旧记录保持原状，并在执行结果中报告。不存在的窗格、其他 tmux
服务的记录和被复用的坐标不会授权停止或启动当前程序。

从 tmux 外的独立终端发起完整重启，避免把执行命令的窗格包含在重启目标中。
`ai-restore --plan-json --dump FILE` 只输出指定快照的计划，不混入遗留记录；
查询遗留记录使用 `aipane-restore-executor --retry-pending --plan-json`。

Pi 可以在进程内切换会话。安装 Pi 绑定扩展，让 `/new`、`/resume`、`/fork` 的选择
进入后续快照；下方固定运行版本安装器负责提供 `aipane-bind` 命令。

```bash
AIPANE_ROOT="${AIPANE_ROOT:-$HOME/.aipane}"
mkdir -p ~/.pi/agent/extensions
ln -sf "$AIPANE_ROOT/integrations/pi/aipane-bind.ts" ~/.pi/agent/extensions/aipane-bind.ts
```

已运行的 Pi 执行 `/reload` 加载扩展。预分配 ID 但尚无记录的空 Pi 会话会重建空 TUI，
标为 `recreated`；属于其他项目或已损坏的记录标为 `invalid`。Claude 与 Qwen Code
必须已有持久对话记录，否则保持原窗格。

Claude 自动恢复命令带有 `AIPANE_AUTO_RESUME=1`，支持该标记的启动器可据此
沿用已记录的模型并跳过模型选择。若无法确定原模型，启动器应报错，保留待恢复记录；
手动启动不受影响。执行器会等待原生 Claude 进程，并检查其恢复会话 ID。
带模型菜单的启动器及其记忆规则见 [Claude Code 模型菜单](claude-model-picker.md)。

## 快照与身份

tmux-resurrect 的 `post-save-layout` 钩子将刚写好的快照路径传给
`aipane-snapshot`。绑定以 `aipane_sessions` 记录嵌入同一快照，包含会话 ID、
数据目录、来源服务及布局内容的 SHA-256。tmux-resurrect 忽略该记录的执行含义，
但会在比较快照时看到会话变化，因此布局相同、会话已切换的快照也会保留。

快照复制或移动后仍可独立读取。绑定与布局不匹配、元数据损坏时拒绝恢复。
`coords-last.json` 仅供诊断；恢复不会拿它覆盖旧快照。旧格式快照仍可使用其自身
保存的启动命令和标题识别会话；缺少身份的项目不会由全局坐标表补猜。
无参数运行 `aipane-snapshot` 只更新诊断投影。

快照中前台程序为 shell 的窗格保持 shell。即使完整命令字段或绑定中残留 Agent
会话，也不会将该窗格纳入 AI 恢复计划，避免把其他窗格的会话重复启动到这里。

Codex 优先从当前前台进程的可写会话记录确定身份和 `CODEX_HOME`，再使用活动记录
消除同一进程多个会话的歧义。计划会固定数据目录，重试不受调用者后来修改环境变量影响。
同一数据目录的同一 Codex 会话只能由一个计划窗格恢复；计划外已有写入者时也会中止重启。

## 执行与持久记录

确认后的 sealed plan 使用 version 2：固定目标、命令、数据目录、窗格守卫、快照
路径及内容摘要。预览、加载和执行共享同一份字段与身份校验。执行器在保存恢复意图之后
才替换窗格，并在每个窗格操作前复核守卫；成功验证会话并重新绑定后才结束意图。

待恢复记录按“tmux socket、服务 PID、稳定 pane ID”识别，坐标只用于展示。
窗口移动和编号变化不会产生新记录；同一坐标上的新窗格不会继承旧恢复权限。
每条记录有稳定 `recovery_id`。验证成功、明确放弃、由新确认的计划替代及显式配置的
过期，分别记录为 `verified`、`discarded`、`superseded`、`expired`，保留原命令和原因。

`restore-pending.json` 保留原有 `version: 1` / `items` 格式，增加身份版本、记录 ID
和 `history`，兼容旧运行版本读取活动记录。所有状态变更在同一执行器锁内原子落盘，
保留其他服务的记录。文件损坏时中止，不把损坏状态当作空列表覆盖。

实现职责：

- `lib/recovery.py`：统一执行、验证、重试和发布绑定。
- `lib/recovery_plan.py`：计划校验、会话命名空间、冲突规则和确认摘要。
- `lib/recovery_snapshot.py`：快照内的身份记录和内容校验。
- `lib/recovery_state.py`：稳定恢复身份、兼容读取和终结历史。
- `lib/codex_sessions.py`：Codex 可写会话的运行时证据。
- `bin/ai-restart`：展示、确认和选择操作；`bin/ai-restore`：解析快照并构造各工具命令。
- `bin/aipane-restore-executor`：共享恢复实现的命令行入口。

## 固定运行版本

开发工作区和日常运行副本分开。先在仓库提交经过检查的修改，再安装：

```bash
python3 bin/aipane-release install --source /path/to/aipane
aipane-release status
aipane-release rollback
```

安装器拒绝未提交的工作区，从固定提交生成独立副本，在副本上运行完整 Python 测试。
失败时保持当前运行版本；通过后将恢复命令统一指向
`~/.local/share/aipane/runtime/current/bin/`，原子切换 `current`。副本文件只读，
以后编辑开发目录不会改变已安装的命令。执行器调用同一副本中的绑定程序。

前一版本保留在 `previous`，切回前校验文件摘要，并备份当时的待恢复记录。
首次迁移可使用 `--baseline COMMIT` 保存现用版本作为退回点；该基线标记为导入版本，
新安装版本必须经过测试。管理命令独立固定到已安装副本，退回早期版本后仍可使用。

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
zsh -n bin/ai-restore bin/aipane-bind
ai-restart --dry-run
ai-restart --retry --dry-run
aipane-release status
```

恢复测试使用隔离 tmux 服务、本地占位进程和临时状态目录，覆盖重复会话、计划外
写入者、跨服务记录、坐标复用、窗口移动、部分失败、重试、归档和部署失败保持旧版本。
本机验收预览不执行批量重启。历史问题与原有决策见 [恢复设计记录](session-restore-design.md)。
