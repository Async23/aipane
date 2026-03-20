# rod Chromium 定时清理 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 自动清理 xiaohongshu-mcp 泄漏的 rod Chromium 进程（按存活时间筛选），并升级手动 killrod 为全杀模式。

**Architecture:** 独立清理脚本 `rod-cleanup`，通过 LaunchAgent 每 5 分钟执行，只杀存活 >=5 分钟的 leakless 进程（连带清理其 Chromium 子树）。手动 `killrod` 升级为全杀所有 rod 相关进程。同时从 `aipane-cleanup` 中移除重叠的 rod 清理逻辑，避免职责混乱。

**Tech Stack:** POSIX sh（脚本）、launchd plist（调度）、zsh function（killrod）

---

### Task 1: 创建 rod-cleanup 脚本

**Files:**
- Create: `~/code/aipane/bin/rod-cleanup`

**Step 1: 创建脚本**

```sh
#!/bin/sh
# rod-cleanup: kill stale rod/leakless + Chromium process trees
# Usage: rod-cleanup [--max-age <seconds>]   (default: 300)

MAX_AGE=300
LOG_FILE="${HOME}/logs/rod-cleanup.log"
mkdir -p "$(dirname "$LOG_FILE")"

# parse args
while [ $# -gt 0 ]; do
  case "$1" in
    --max-age) MAX_AGE="$2"; shift 2 ;;
    *) shift ;;
  esac
done

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] rod-cleanup: $*" >> "$LOG_FILE"
}

# convert ps etime (DD-HH:MM:SS / HH:MM:SS / MM:SS) to seconds
etime_to_sec() {
  local et="$1" days=0 hours=0 mins=0 secs=0
  case "$et" in
    *-*)
      days="${et%%-*}"
      et="${et#*-}"
      ;;
  esac
  # now et is HH:MM:SS or MM:SS
  local colons
  colons=$(echo "$et" | tr -cd ':' | wc -c | tr -d ' ')
  if [ "$colons" -eq 2 ]; then
    hours=$(echo "$et" | cut -d: -f1)
    mins=$(echo "$et" | cut -d: -f2)
    secs=$(echo "$et" | cut -d: -f3)
  else
    mins=$(echo "$et" | cut -d: -f1)
    secs=$(echo "$et" | cut -d: -f2)
  fi
  # strip leading zeros to avoid octal interpretation
  days=$((${days#0}+0))
  hours=$((${hours#0}+0))
  mins=$((${mins#0}+0))
  secs=$((${secs#0}+0))
  echo $(( days*86400 + hours*3600 + mins*60 + secs ))
}

# --- 1. Find stale leakless processes ---
stale_leakless=""
ps -axo pid=,etime=,command= | while IFS= read -r line; do
  pid=$(echo "$line" | awk '{print $1}')
  etime=$(echo "$line" | awk '{print $2}')
  cmd=$(echo "$line" | awk '{$1=""; $2=""; print}' | sed 's/^ *//')
  case "$cmd" in
    */leakless*rod*)
      elapsed=$(etime_to_sec "$etime")
      if [ "$elapsed" -ge "$MAX_AGE" ]; then
        echo "$pid"
      fi
      ;;
  esac
done > /tmp/rod-cleanup-stale-pids.$$

stale_leakless=$(cat /tmp/rod-cleanup-stale-pids.$$ 2>/dev/null)
rm -f /tmp/rod-cleanup-stale-pids.$$

if [ -z "$stale_leakless" ]; then
  exit 0
fi

count=$(echo "$stale_leakless" | wc -l | tr -d ' ')
log "killing $count stale leakless process(es) (age >= ${MAX_AGE}s): $(echo $stale_leakless | tr '\n' ' ')"

# --- 2. Kill leakless (children die with parent via leakless design) ---
echo "$stale_leakless" | xargs kill 2>/dev/null
sleep 1

# --- 3. Force-kill any surviving Chromium children ---
survivors=$(ps -axo pid=,command= | awk '$0 ~ /\.cache\/rod\/browser\/.*Chromium/ { print $1 }')
# only kill processes whose leakless parent is gone
if [ -n "$survivors" ]; then
  orphans=""
  for pid in $survivors; do
    ppid=$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')
    if [ -n "$ppid" ] && ! kill -0 "$ppid" 2>/dev/null; then
      orphans="$orphans $pid"
    fi
  done
  if [ -n "$orphans" ]; then
    log "force-killing orphaned Chromium:$orphans"
    kill -9 $orphans 2>/dev/null
  fi
fi

# --- 4. Clean up stale user-data dirs ---
ROD_TMP=$(find /var/folders -path "*/rod/user-data" -type d 2>/dev/null | head -1)
if [ -d "$ROD_TMP" ]; then
  dir_count=$(ls -1 "$ROD_TMP" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$dir_count" -gt 0 ]; then
    rm -rf "$ROD_TMP"
    log "cleaned up ${dir_count} stale user-data dir(s)"
  fi
fi
```

**Step 2: 设置可执行权限**

Run: `chmod +x ~/code/aipane/bin/rod-cleanup`

**Step 3: 手动测试脚本**

Run: `~/code/aipane/bin/rod-cleanup --max-age 300`
然后: `cat ~/logs/rod-cleanup.log`
Expected: 如果有超 5 分钟的 leakless，日志显示已清理；否则静默退出。

**Step 4: Commit**

```bash
cd ~/code/aipane
git add bin/rod-cleanup
git commit -m "feat: add rod-cleanup script with age-based filtering"
```

---

### Task 2: 创建 LaunchAgent

**Files:**
- Create: `~/Library/LaunchAgents/com.rod.cleanup.plist`

**Step 1: 创建 plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.rod.cleanup</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/alfheim/code/aipane/bin/rod-cleanup</string>
    <string>--max-age</string>
    <string>300</string>
  </array>

  <key>StartInterval</key>
  <integer>300</integer>

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/Users/alfheim/logs/rod-cleanup-stdout.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/alfheim/logs/rod-cleanup-stderr.log</string>
</dict>
</plist>
```

**Step 2: 加载 LaunchAgent**

Run: `launchctl load ~/Library/LaunchAgents/com.rod.cleanup.plist`

**Step 3: 验证已加载**

Run: `launchctl list | grep rod.cleanup`
Expected: 输出包含 `com.rod.cleanup`

**Step 4: Commit**

```bash
cd ~/code/aipane
git add -f ~/Library/LaunchAgents/com.rod.cleanup.plist  # 如果在 repo 外则跳过
git commit -m "feat: add LaunchAgent for rod-cleanup (every 5 min)"
```

> 注意：plist 在 `~/Library/LaunchAgents/` 不在 repo 内，可选择不 commit。

---

### Task 3: 升级 killrod 为全杀模式

**Files:**
- Modify: `~/code/aipane/cmd/killrod.zsh`

**Step 1: 重写 killrod 函数**

将 `ppid == 1` 限制移除，改为匹配所有 rod 相关的 leakless + Chromium 进程：

```zsh
#!/usr/bin/env zsh
# Command: killrod
# Kill ALL rod/leakless + Chromium processes spawned by xiaohongshu-mcp

killrod() {
  local -a pids survivors
  local pid

  # collect all leakless (rod guardian) PIDs
  pids=("${(@f)$(
    ps -axo pid=,command= | awk '
      $0 ~ /leakless.*rod/ { print $1 }
    '
  )}")

  # also collect any rod Chromium PIDs not under leakless
  pids+=("${(@f)$(
    ps -axo pid=,command= | awk '
      $0 ~ /\.cache\/rod\/browser\/.*Chromium/ { print $1 }
    '
  )}")

  # filter empty entries and deduplicate
  pids=("${(@u)pids:#}")

  if (( ${#pids[@]} == 0 )); then
    print "killrod: no rod processes found"
    return 0
  fi

  print "killrod: terminating ${#pids[@]} process(es): ${pids[*]}"
  kill "${pids[@]}" 2>/dev/null

  sleep 1

  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      survivors+=("$pid")
    fi
  done

  if (( ${#survivors[@]} > 0 )); then
    print "killrod: forcing ${#survivors[@]} remaining process(es): ${survivors[*]}"
    kill -9 "${survivors[@]}" 2>/dev/null
  fi

  # clean up stale user-data dirs
  local rod_tmp
  rod_tmp=$(find /var/folders -path "*/rod/user-data" -type d 2>/dev/null | head -1)
  if [[ -d "$rod_tmp" ]]; then
    local count=$(ls -1 "$rod_tmp" 2>/dev/null | wc -l | tr -d ' ')
    if (( count > 0 )); then
      rm -rf "$rod_tmp"
      print "killrod: cleaned up ${count} stale user-data dir(s)"
    fi
  fi
}
```

**Step 2: 验证函数加载**

Run: `source ~/code/aipane/cmd/killrod.zsh && type killrod`
Expected: 函数定义中不再有 `$2 == 1` 条件

**Step 3: Commit**

```bash
cd ~/code/aipane
git add cmd/killrod.zsh
git commit -m "feat: upgrade killrod to kill all rod processes (not just orphans)"
```

---

### Task 4: 从 aipane-cleanup 中移除重叠的 rod 清理逻辑

**Files:**
- Modify: `~/code/aipane/bin/aipane-cleanup` (line 38-56)

**Step 1: 移除 section 2 和 section 3（rod 相关）**

删除以下部分（line 38-56）：
- `# --- 2. Orphaned rod Chromium processes (PPID=1) ---` 整段
- `# --- 3. Clean up stale rod user-data dirs ---` 整段

更新 Summary 部分，移除 `rod_count`：

```sh
# --- Summary ---
if [ "$ai_count" -gt 0 ]; then
  log "cleaned up ${ai_count} detached AI CLI process(es)"
else
  log "no detached AI CLI processes found"
fi
```

**Step 2: 验证脚本语法**

Run: `sh -n ~/code/aipane/bin/aipane-cleanup`
Expected: 无输出（语法正确）

**Step 3: Commit**

```bash
cd ~/code/aipane
git add bin/aipane-cleanup
git commit -m "refactor: remove rod cleanup from aipane-cleanup (moved to rod-cleanup)"
```

---

### Task 5: 端到端验证

**Step 1: 确认 LaunchAgent 运行正常**

Run: `launchctl list | grep rod.cleanup`

**Step 2: 确认 killrod 全杀生效**

Run: `killrod`
Expected: 杀掉所有 rod 进程（不再报 "no orphaned"）

**Step 3: 等待 5 分钟后检查日志**

Run: `tail -5 ~/logs/rod-cleanup.log`
Expected: 有定时执行记录

**Step 4: 确认 aipane-cleanup 仍正常**

Run: `~/code/aipane/bin/aipane-cleanup && tail -1 ~/logs/aipane-cleanup.log`
Expected: 只处理 AI CLI 进程，不再提及 rod
