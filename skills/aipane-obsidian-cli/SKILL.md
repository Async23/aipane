---
name: aipane-obsidian-cli
description: 使用官方 Obsidian CLI 查询、读取和管理本地 Obsidian Vault，并以宿主原生编辑工具修改 Markdown 正文。用户提到 Obsidian、笔记库或 Vault 操作时使用。
---

# Obsidian Vault 操作契约

## 1. 工具分工与硬边界

- **读写分工**：查询、读取及正文写入以外的 Vault 操作直接调用官方 `obsidian` CLI，不使用 MCP（避免常驻上下文开销）。新建或编辑 Markdown 正文（含写后修正）统一使用宿主原生文件编辑工具做精确局部修改，**严禁调用 `obsidian create`、`obsidian append`、`obsidian prepend` 或 Shell 重定向写入**（CLI 参数易转义破坏多行 Markdown 且缺乏精确局部 diff 保障，误操作易损坏整篇笔记）。
- **Vault 前置规范**：每条 CLI 命令必须将 `vault=<VaultName>` 置于具体子命令之前（例如 `obsidian vault=<VaultName> search query="MCP"`），不依赖当前目录或活动 Vault。默认 Vault 以用户 `AGENTS.md` 或本次指令为准；未明确且本机存在多个 Vault 时，先用 `obsidian vaults` 枚举确认。Obsidian 未运行时允许 CLI 自动启动应用。
- **根目录核验与注入防护**：原生写入前，通过 `obsidian vault=<VaultName> vault info=path` 确认目标 Vault 的本地根目录；写入目标必须位于该根目录内、扩展名为 `.md` 且不得位于 `.obsidian/`。所有来自用户或笔记的 Shell 参数须正确引用，禁止用反引号、`$()` 或 `eval` 拼接内容。

## 2. 查询、成稿与删改契约

- **检索与回读**：目标明确时直接用精确 `read path=<relative-path>`；否则用 `search` 或 `search:context` 定位后回读原文。
- **新建或追加判定**：用户未指明目标笔记时先检索相关笔记，命中后询问“新建还是追加”。
- **成稿原则**：除非用户明确要求保存工作日志、对话记录或编辑说明，写入 Vault 的内容只保留面向读者的最终成稿；操作计划、重构理由、核验过程与完成报告留在回复中。
- **写后必核验**：任何新建、修改、移动、重命名或删除完成后，须用 `obsidian vault=<VaultName> read path=<relative-path>`（或 `files`、`search`）回读核验最终状态，确认 frontmatter、标题结构完整且无过程残留或旧稿碎片，不只依赖命令退出码。
- **删除红线**：默认使用 `delete` 移入 Obsidian 回收站；仅在用户明确要求“永久删除”时加 `permanent`。

## 3. 结果展示

- 优先展示 Vault 名称、笔记标题和 Vault 内相对路径；避免只给 URL 编码的 `obsidian://` 链接或冗长绝对路径（用户明确索要时再提供）。
