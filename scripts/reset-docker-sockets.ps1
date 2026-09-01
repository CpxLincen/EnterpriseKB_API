# 清理 Docker Desktop 卡死的 AF_UNIX socket 目录，修复启动报错：
#   "starting services: initializing ... :
#    listening on unix://...: remove ...: The file cannot be accessed by the system."
#   （受影响组件会轮换：Inference manager / Secrets Engine / Ingest server 等）
#
# 根因（Docker Desktop 已知 bug docker/desktop-feedback #531/#532，截至
# 2026-08-30 的 4.88.1 仍未修复）：
#   %LOCALAPPDATA%\Docker\run 与 %LOCALAPPDATA%\docker-secrets-engine 中会留下
#   无法删除的 AF_UNIX socket 重解析点（错误 1920 "文件无法被系统访问"）。
#   下次启动时后端对这些文件做 remove-then-bind 会失败，导致整个引擎启动中止。
#   这些 socket 文件本身删不掉，但"重命名其父目录"是可靠的，Docker 会在下次
#   启动时重新创建干净目录。
#
# 用法：
#   reset-docker-sockets.ps1              仅清理（配合"启动"文件夹在每次登录时自动运行）
#   reset-docker-sockets.ps1 -StartDocker  清理后立即启动 Docker Desktop

param(
  [switch]$StartDocker
)

$ErrorActionPreference = 'Continue'

$dockerDesktopExe = 'C:\Users\cheng\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe'
$dockerCli        = 'C:\Users\cheng\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe'
$localDocker      = Join-Path $env:LOCALAPPDATA 'Docker'
$socketDirs       = @(
  (Join-Path $localDocker 'run'),
  (Join-Path $env:LOCALAPPDATA 'docker-secrets-engine')
)

Write-Output '[reset-docker-sockets] start'

# 1) 若 Docker 相关进程仍在运行，先彻底停止（GUI / backend / build 等都要清，
#    否则 socket 目录会被占用，改名会失败）。优先优雅退出，残留则强制结束。
$dockerProcesses = @('Docker Desktop', 'com.docker.backend', 'com.docker.build', 'com.docker.dev-envs', 'Docker Desktop Installer', 'DockerDesktopInstaller')
$running = Get-Process -Name $dockerProcesses -ErrorAction SilentlyContinue
if ($running) {
  Write-Output '  Docker processes running -> stopping...'
  & $dockerCli desktop stop 2>&1 | Out-Null
  for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Name $dockerProcesses -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 2
  }
  # 仍有残留则强制结束（改名父目录前必须无进程占用）
  foreach ($p in (Get-Process -Name $dockerProcesses -ErrorAction SilentlyContinue)) {
    try { Stop-Process -Id $p.Id -Force -ErrorAction Stop; Write-Output "  force-stopped: $($p.Name) ($($p.Id))" } catch { Write-Output "  could not stop: $($p.Name) ($($p.Id))" }
  }
  Start-Sleep -Seconds 2
}

# 2) 尽力清掉历史遗留的 *.stale-* / *.broken-* 目录（递归删除在部分机器上可行；
#    删不掉的 0 字节残留不影响使用，保留即可）
$leftovers = @()
$leftovers += @(Get-ChildItem -LiteralPath $localDocker -Directory -Filter 'run.stale-*' -ErrorAction SilentlyContinue)
$leftovers += @(Get-ChildItem -LiteralPath $localDocker -Directory -Filter 'run.broken-*' -ErrorAction SilentlyContinue)
$leftovers += @(Get-ChildItem -LiteralPath $env:LOCALAPPDATA -Directory -Filter 'docker-secrets-engine.stale-*' -ErrorAction SilentlyContinue)
$leftovers += @(Get-ChildItem -LiteralPath $env:LOCALAPPDATA -Directory -Filter 'docker-secrets-engine.broken-*' -ErrorAction SilentlyContinue)
foreach ($d in $leftovers) {
  try {
    Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction Stop
    Write-Output "  purged leftover: $($d.Name)"
  } catch {
    Write-Output "  (leftover kept) $($d.Name)"
  }
}

# 3) 把当前卡死的 socket 目录改名挪走（改名父目录是唯一可靠手段）
$suffix = Get-Date -Format 'yyyyMMdd-HHmmss'
if ([string]::IsNullOrWhiteSpace($suffix)) {
  $suffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
}
foreach ($dir in $socketDirs) {
  if (Test-Path -LiteralPath $dir) {
    $target = "$dir.stale-$suffix"
    try {
      Move-Item -LiteralPath $dir -Destination $target -Force
      Write-Output "  renamed aside: $dir -> $target"
    } catch {
      Write-Output "  FAILED to rename $dir : $($_.Exception.Message)"
    }
  } else {
    Write-Output "  clean (not present): $dir"
  }
}

# 4) 可选：随后启动 Docker Desktop
if ($StartDocker) {
  if (Test-Path -LiteralPath $dockerDesktopExe) {
    Start-Process -FilePath $dockerDesktopExe -WindowStyle Hidden
    Write-Output '  Docker Desktop started'
  } else {
    Write-Output '  Docker Desktop.exe not found'
  }
}

Write-Output '[reset-docker-sockets] done'
