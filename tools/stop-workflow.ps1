[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot ".runtime"
$pidFile = Join-Path $runtimeRoot "research.pid"

if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
    $processId = 0
    if ([int]::TryParse(
        (Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8).Trim(),
        [ref]$processId
    )) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            Stop-Process -Id $processId
        }
    }
    Remove-Item -LiteralPath $pidFile -Force
}

Write-Output "Research workflow has stopped. The computer remains on."
