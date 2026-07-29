[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$systemConfigPath = Join-Path $projectRoot "config\system.json"
$environmentConfigPath = Join-Path $projectRoot "config\environment.local.json"

if (-not (Test-Path -LiteralPath $environmentConfigPath -PathType Leaf)) {
    throw (
        "Research environment is not configured. Run " +
        "tools\install-research-workflow.ps1 first."
    )
}
$environmentConfig = Get-Content `
    -LiteralPath $environmentConfigPath `
    -Raw `
    -Encoding UTF8 |
    ConvertFrom-Json
if (
    $environmentConfig.execution_policy -ne "gpu_only" -or
    $environmentConfig.cpu_fallback -ne $false
) {
    throw "This research workflow requires GPU-only execution."
}
$gpuPython = [string]$environmentConfig.python_executable
if (-not (Test-Path -LiteralPath $gpuPython -PathType Leaf)) {
    throw "Configured GPU Python was not found: $gpuPython"
}

& (Join-Path $PSScriptRoot "core-env.ps1") verify-gpu
if ($LASTEXITCODE -ne 0) {
    throw "GPU verification failed."
}

$runtimeRoot = Join-Path $projectRoot ".runtime"
$logRoot = Join-Path $runtimeRoot "logs"
foreach ($directory in @($runtimeRoot, $logRoot)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$tokenFile = Join-Path $runtimeRoot "access.token"
if (-not (Test-Path -LiteralPath $tokenFile -PathType Leaf)) {
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    $token = [Convert]::ToBase64String($bytes).TrimEnd("=").
        Replace("+", "-").Replace("/", "_")
    [IO.File]::WriteAllText(
        $tokenFile,
        $token,
        [Text.UTF8Encoding]::new($false)
    )
}
$token = (Get-Content -LiteralPath $tokenFile -Raw -Encoding UTF8).Trim()
if ($token.Length -lt 32) {
    throw "Local access token is invalid."
}

function Test-LocalPort {
    param([int]$Port)
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $task.Wait(300)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

if (-not (Test-LocalPort -Port 18082)) {
    $coreProcess = Start-Process `
        -FilePath $gpuPython `
        -ArgumentList @(
            "-B", "-X", "utf8", "-m", "app.server",
            "--config", $systemConfigPath,
            "--index", (Join-Path $projectRoot "web\index.html"),
            "--token-file", $tokenFile,
            "--host", "127.0.0.1",
            "--port", "18082"
        ) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logRoot "research.stdout.log") `
        -RedirectStandardError (Join-Path $logRoot "research.stderr.log") `
        -PassThru
    [IO.File]::WriteAllText(
        (Join-Path $runtimeRoot "research.pid"),
        [string]$coreProcess.Id
    )
}

$deadline = [DateTime]::UtcNow.AddSeconds(30)
while (
    [DateTime]::UtcNow -lt $deadline -and
    -not (Test-LocalPort -Port 18082)
) {
    Start-Sleep -Milliseconds 250
}
if (-not (Test-LocalPort -Port 18082)) {
    throw "Research entry did not start. See $logRoot."
}

$entry = "http://127.0.0.1:18082/#token=$token"
Write-Output "Research workflow is ready: $entry"
if (-not $NoBrowser) {
    Start-Process $entry
}
