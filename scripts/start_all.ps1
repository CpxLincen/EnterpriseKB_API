$ErrorActionPreference = 'Continue'
Set-Location 'E:\EnterpriseKB'

$dockerCli = 'C:\Users\cheng\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe'
$composeFile = 'E:\EnterpriseKB\docker-compose.yml'

Write-Output '[1/4] Waiting for Docker engine...'
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    & $dockerCli info *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Output "      engine ready after ~$($i * 3)s"
        $ready = $true
        break
    }
    Start-Sleep -Seconds 3
}
if (-not $ready) {
    Write-Output '      ERROR: Docker engine not ready after 180s'
    exit 1
}

Write-Output '[2/4] Starting pgvector container (docker compose up -d)...'
& $dockerCli compose -f $composeFile up -d
if ($LASTEXITCODE -ne 0) {
    Write-Output '      ERROR: docker compose up -d failed'
    exit 1
}

Write-Output '[3/4] Waiting for postgres to become ready (init-db retry)...'
& $dockerCli ps
$dbReady = $false
for ($i = 0; $i -lt 30; $i++) {
    $null = & 'E:\EnterpriseKB\.venv\Scripts\python.exe' -m app.cli init-db 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Output "      database ready (init-db OK)"
        $dbReady = $true
        break
    }
    Start-Sleep -Seconds 3
}
if (-not $dbReady) {
    Write-Output '      ERROR: database did not become ready in time'
    exit 1
}

Write-Output '[4/4] Starting FastAPI backend on 127.0.0.1:8000...'
$backend = Start-Process -FilePath 'E:\EnterpriseKB\.venv\Scripts\python.exe' `
    -ArgumentList '-m', 'uvicorn', 'app.api:app', '--host', '127.0.0.1', '--port', '8000' `
    -WorkingDirectory 'E:\EnterpriseKB' `
    -WindowStyle Hidden `
    -PassThru
Write-Output "      backend PID: $($backend.Id)"
