# 会话保留策略定时任务入口：供 Windows 任务计划程序（或其它调度器）周期性调用。
# 执行 `python -m app.cli retention` —— 自动归档超期活跃会话、永久删除超期归档会话。
# 阈值来自 .env 的 CONVERSATION_ARCHIVE_DAYS / CONVERSATION_RETENTION_DAYS。

$ErrorActionPreference = 'Continue'

# scripts/ 的上一级即项目根目录（本脚本必须位于 <repo>/scripts 下）
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
& $python -m app.cli retention

exit $LASTEXITCODE
