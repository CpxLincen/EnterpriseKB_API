# 下载 BGE reranker 模型（约 2.3GB）到本地 models/bge-reranker-v2-m3。
# 用法：
#   .\scripts\download-rerank-model.ps1                 # 默认走 ModelScope
#   .\scripts\download-rerank-model.ps1 -Source huggingface
param(
    [string]$Target = (Join-Path $PSScriptRoot "..\models\bge-reranker-v2-m3"),
    [ValidateSet("modelscope", "huggingface")]
    [string]$Source = "modelscope"
)

$ErrorActionPreference = "Stop"
$Target = [System.IO.Path]::GetFullPath($Target)
New-Item -ItemType Directory -Force -Path $Target | Out-Null

Write-Host "下载 BGE reranker 模型到：$Target"
if ($Source -eq "modelscope") {
    if (-not (Get-Command modelscope -ErrorAction SilentlyContinue)) {
        Write-Error "未找到 modelscope CLI，请先安装：pip install modelscope"
    }
    modelscope download --model BAAI/bge-reranker-v2-m3 --local_dir $Target
}
else {
    if (-not (Get-Command huggingface-cli -ErrorAction SilentlyContinue)) {
        Write-Error "未找到 huggingface-cli，请先安装：pip install -U huggingface_hub"
    }
    if (-not $env:HF_ENDPOINT) { $env:HF_ENDPOINT = "https://hf-mirror.com" }
    huggingface-cli download BAAI/bge-reranker-v2-m3 --local-dir $Target
}
Write-Host "完成。请确认 config/models.yaml 的 retrieval.rerank.model 指向：$Target"
