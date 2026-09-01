# 一键干净启动整套环境：
#   清理卡死的 socket -> 启动 Docker Desktop -> 拉起 pgvector -> 初始化数据库
# 之后可再启动后端（uvicorn）与前端（bun run dev）。
#
# 注意：请始终通过本脚本启动 Docker Desktop（它会先清理 socket 残留）。
# 直接点击 Docker Desktop 图标启动，可能在未清理残留时触发 #531/#532 崩溃。

$ErrorActionPreference = 'Continue'

$repoRoot    = 'E:\EnterpriseKB'
$dockerCli   = 'C:\Users\cheng\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe'
$dockerExe   = 'C:\Users\cheng\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe'
$composeFile = Join-Path $repoRoot 'docker-compose.yml'

Set-Location $repoRoot

Write-Output '[start-docker] 1/5 清理卡死的 socket 目录...'
& (Join-Path $repoRoot 'scripts\reset-docker-sockets.ps1') 2>&1 | ForEach-Object { Write-Output "  $_" }

Write-Output '[start-docker] 2/5 启动 Docker Desktop 并等待引擎就绪...'
if (-not (Get-Process -Name 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue)) {
  Start-Process -FilePath $dockerExe -WindowStyle Hidden
}
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
  # 仅检测命名管道是否存在不可靠（管道可能先出现、daemon 尚未就绪，导致
  # 随后的 docker compose 报 "cannot find the file"）。改为直接探测引擎：
  # `docker info` 返回 0 才算真正就绪。
  & $dockerCli info 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Output "  engine ready (~$($i*3)s)"; $ready = $true; break }
  Start-Sleep -Seconds 3
}
if (-not $ready) { Write-Output '  ERROR: Docker engine not ready within 180s'; exit 1 }

Write-Output '[start-docker] 3/5 启动 pgvector 容器...'
& $dockerCli compose -f $composeFile up -d
if ($LASTEXITCODE -ne 0) { Write-Output '  ERROR: docker compose up -d failed'; exit 1 }

Write-Output '[start-docker] 4/5 等待数据库就绪 (init-db)...'
$dbReady = $false
for ($i = 0; $i -lt 30; $i++) {
  & (Join-Path $repoRoot '.venv\Scripts\python.exe') -m app.cli init-db 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Output '  database ready'; $dbReady = $true; break }
  Start-Sleep -Seconds 3
}
if (-not $dbReady) { Write-Output '  ERROR: database not ready'; exit 1 }

Write-Output '[start-docker] 5/5 完成'
& $dockerCli ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
