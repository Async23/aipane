# Claude Code Stop 回答字段修复

本机 Claude Code 2.1.322 在生成 `Stop` 和 `SubagentStop` 的
`last_assistant_message` 时，先选择最后一条助手消息，再提取其中的正文。
当流式响应依次出现正文块和 `thinking` 块时，最后一条助手消息没有正文，
事件中的回答字段因此缺失，通知显示“任务已完成”。

`integrations/claude/stop-response-patch.mjs` 只替换生成这两个事件时的消息选择：
从内存中的消息列表向前寻找最后一条有非空正文的助手消息，遇到用户消息
（包括工具结果）或上下文压缩边界就停止。其他地方使用的“最后一条助手消息”
函数保持原样。通知脚本直接使用修复后的事件字段，无需读取会话记录补齐回答。

## 应用与检查

依赖本机 `~/code/oss/tweakcc` 检出及其 `pnpm install --frozen-lockfile` 安装的依赖。
可用 `TWEAKCC_ROOT` 指定其他检出。补丁通过 tweakcc 的原生模块接口重打包，
不应用 tweakcc 的其他功能或配置。首次验证使用的 tweakcc 提交记在备份清单中。

在 aipane 仓库运行：

```bash
bin/aipane-patch-claude-stop --check
bin/aipane-patch-claude-stop --apply
```

默认目标为 `~/.local/bin/claude` 的实际二进制，可用 `--binary PATH` 指定。
`--check` 只读。`--apply` 先备份、修补副本，核对所有模块的源码及身份，
检查 macOS 签名和 `--version`，再原子替换实际二进制。替换前再次核对原文件
SHA-256，拒绝覆盖同时发生的升级。未知或多义的补丁匹配会直接报错。
同一补丁重复应用不会再次修改文件。

需要先验证副本时：

```bash
bin/aipane-patch-claude-stop --output /absolute/new/path/claude-patched
python3 tests/claude_stop_response_smoke.py /absolute/new/path/claude-patched
```

这是本机补丁，不是 Anthropic 的官方修复。修改后的二进制使用本地 ad-hoc
签名。已经运行的 Claude 进程仍使用旧代码，需要退出并恢复会话。官方升级
可能替换二进制；升级后重新执行 `--check`，需要时再执行 `--apply`。
新版本的源码结构若不匹配，应重新核验，而不是放宽匹配直接覆盖。

## 验证

```bash
node --test tests/test_claude_stop_response_patch.mjs
python3 -m unittest discover -s tests -p 'test_claude_notify.py'
python3 tests/claude_stop_response_smoke.py ~/.local/bin/claude
```

回归覆盖正文后有思考块、空白正文、多段正文、缺少回答、上一轮回答和压缩边界，
同时验证 `Stop` 和 `SubagentStop`。集成验证启动隔离配置的真实 Claude 进程，
连接本地 HTTP 服务接收模拟流式响应，捕获真实 `Stop` 输入，并通过现有通知
适配器的 dry-run 验证节选。它不使用真实模型凭据，也不发送桌面通知。

## 回滚

备份保存在 `~/Library/Application Support/aipane/claude-stop-patches.noindex/`。
每次安装打印本次 `manifest.json` 的绝对路径，其中包含原件、补丁副本、原始
SHA-256、修补后 SHA-256、版本和模块标识。

```bash
bin/aipane-patch-claude-stop --restore '/absolute/path/to/manifest.json'
```

回滚先验证备份，再确认活动文件仍是这次修补的版本，最后原子恢复原件。
若 Claude 已被升级或另行修改，回滚拒绝覆盖它。
