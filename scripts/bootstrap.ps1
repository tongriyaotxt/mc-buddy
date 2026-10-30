# scripts/bootstrap.ps1 —— 一条命令把环境准备好（新人的第 0 步）
#
# 设计原则（为什么是"检查 + 能做就做"而不是"一键全自动"）：
#   · **不猜**：Paper jar 的版本/路径因人而异 ⇒ 只检查、不下载（下载走 `docs/README.md` 的链接）
#   · **不静默失败**：每一步都打印"✅ / ⚠️ / ❌ + 下一步该干什么" ✓
#   · **不改变仓库状态**：除了 `node_modules/`（gitignore ✓）什么也不写 ✓
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -WithServer      # 额外拉一次服务端探活
#   $env:BUDDY_MCSERVERS='%MC_SERVERS%'; powershell -File scripts\bootstrap.ps1     # 自定义服务端目录
#
# ⚠️ 本文件是为了消灭"硬编码路径"：所有路径都先读环境变量、再退回默认值 ✓
#    （老代码里 `%MC_SERVERS%\` / `%JAVA_HOME%` 是写死的 —— 见 docs/COMMUNITY-DESIGN.md §4）

param(
  [switch]$WithServer,
  [string]$McServers = $(if ($env:BUDDY_MCSERVERS) { $env:BUDDY_MCSERVERS } else { '%MC_SERVERS%' }),
  [string]$JavaPath  = $(if ($env:BUDDY_JAVA)       { $env:BUDDY_JAVA }       else { '' })
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Step($n, $t) { Write-Host "`n[$n] $t" -ForegroundColor Cyan }
function OK($m)   { Write-Host "    ✅ $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    ⚠️  $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "    ❌ $m" -ForegroundColor Red; $script:fail++ }
$script:fail = 0

Write-Host "=== Buddy / for-xiaorun bootstrap ===" -ForegroundColor Cyan
Write-Host "仓库：$root"
Write-Host "服务端目录：$McServers （可用 `$env:BUDDY_MCSERVERS 覆盖）"

# ── 1 运行时 ───────────────────────────────────────────────────────────────
Step 1 '运行时（node / python）'
try {
  $nodeV = (& node --version) -replace 'v', ''
  $nodeMajor = [int]($nodeV.Split('.')[0])
  if ($nodeMajor -ge 18) { OK "node v$nodeV" } else { Bad "node v$nodeV 太老（要 ≥18）" }
} catch { Bad '找不到 node（装 Node.js 18+ / 20 LTS）' }
try {
  $pyRaw = (& python --version) 2>&1
  if ($pyRaw -match '(\d+)\.(\d+)') {
    $maj = [int]$Matches[1]; $min = [int]$Matches[2]
    if ($maj -ge 3 -and $min -ge 11) { OK "python $maj.$min" } else { Bad "python $maj.$min 太老（要 ≥3.11）" }
  }
} catch { Bad '找不到 python（装 Python 3.11+，并把 python 加进 PATH）' }

# ── 2 node 依赖 ────────────────────────────────────────────────────────────
Step 2 'node 依赖（mineflayer / minecraft-data …）'
if (Test-Path node_modules) {
  OK "node_modules 已存在（$( [math]::Round((Get-ChildItem node_modules -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum/1MB) ) MB）"
} else {
  Warn 'node_modules 不存在 ⇒ 现在装（npm ci，约 1~3 分钟）'
  & npm ci --no-audit --no-fund
  if ($LASTEXITCODE -eq 0) { OK 'npm ci 完成' } else { Bad 'npm ci 失败（看上面的报错；常见是网络/代理）' }
}

# ── 3 python 依赖（控制平面，可选）─────────────────────────────────────────
Step 3 'python 依赖（PyYAML / psycopg，控制平面才需要）'
$pyOk = $true
try { & python -c "import yaml" 2>$null; if ($LASTEXITCODE -ne 0) { $pyOk = $false } } catch { $pyOk = $false }
if ($pyOk) { OK 'PyYAML ✓' } else { Warn '缺 PyYAML ⇒ pip install PyYAML（只有 orchestrator/ 需要）' }

# ── 4 四道闸（最重要的一步：证明代码树是好的）──────────────────────────────
Step 4 '四道闸（node --check / tdz / freevar / smoke）'
& python -X utf8 tools/ci_gates.py
if ($LASTEXITCODE -eq 0) {
  OK '四道闸全绿 —— 代码树是好的，不是你的环境问题 ✓'
} else {
  Bad '四道闸没过 ⇒ **别开批**；把上面输出整段贴到 issue（这是必过项 ✓）'
}

# ── 5 服务端（只检查，不自动下载 ✗）────────────────────────────────────────
Step 5 "Minecraft 服务端（$McServers）"
$paper = Get-ChildItem $McServers -Filter 'paper*.jar' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($paper) {
  OK "找到 Paper：$($paper.FullName)（$([math]::Round($paper.Length/1MB)) MB）"
} else {
  Warn "没找到 paper*.jar ⇒ 从 https://fill.papermc.io 下 1.20.4 放到 $McServers\paper-1.20.4\"
  Warn '   或者：把 $env:BUDDY_MCSERVERS 指到你已有的服务端目录'
}
if ($JavaPath) {
  if (Test-Path $JavaPath) { OK "java：$JavaPath" } else { Bad "`$env:BUDDY_JAVA 指向的文件不存在：$JavaPath" }
} else {
  try { & java -version 2>&1 | Out-Null; if ($LASTEXITCODE -eq 0) { OK 'java 在 PATH 上' } else { throw } }
  catch { Warn 'java 不在 PATH ⇒ 设 $env:BUDDY_JAVA=<java.exe 完整路径>（Paper 1.20.4 要 Java 21）' }
}

# ── 6 可选：真的拉一次服务端 ───────────────────────────────────────────────
if ($WithServer) {
  Step 6 '服务端探活（会起一个 3GB 的 JVM；没内存就别跑）'
  & python tools/server_warden.py status
  & python tools/server_warden.py ensure --port 25565 --wait-sec 300 --settle-sec 30
  if ($LASTEXITCODE -eq 0) { OK '服务端就绪' } else { Bad '服务端没起来 ⇒ 看 warden 的输出（常见：内存不足 / JVM 参数写 1.5G 会被拒 ✗）' }
}

# ── 收尾 ──────────────────────────────────────────────────────────────────
Write-Host "`n=== 下一步（照抄）===" -ForegroundColor Cyan
if ($script:fail -gt 0) {
  Write-Host '❌ 有 $script:fail 项没通过 —— 先修它们（它们会导致后面所有步骤都失败）' -ForegroundColor Red
  Write-Host '   常见三件：node_modules 没装 / python 不在 PATH / 四道闸不过'
  exit 1
}
Write-Host @'
  ① 读交接（3 分钟）：docs\HANDOFF-MORNING.md
  ② 看今天该打哪一刀：WORKBOARD.md 的 §3
  ③ 认领一条取证（不需要机器，最快上手）：
       python -X utf8 tools\replay_index.py --stats
       python -X utf8 tools\replay_index.py --sig gather_unreachable
  ④ 只有拿到车道主的许可才跑批：
       python -X utf8 tools\start_batch.py --scenario scenarios\nether_ready_real_v1.yaml `
         --count 8 --tag mytag1 --start-index 1 --port 25571 --rcon-port 25581 `
         --extra="--stop-on-pass --abort-on-death"
  ⑤ 交东西之前：python -X utf8 tools\ci_gates.py   （必须 ✅ 全绿）
'@ -ForegroundColor Gray
exit 0
