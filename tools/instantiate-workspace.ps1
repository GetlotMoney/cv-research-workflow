[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DisplayName,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[a-z0-9][a-z0-9-]{0,31}$")]
    [string]$Slug
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentConfig = Get-Content `
    -LiteralPath (Join-Path $projectRoot "config\environment.local.json") `
    -Raw `
    -Encoding UTF8 |
    ConvertFrom-Json
$python = [string]$environmentConfig.python_executable
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "固定 Conda 环境不存在：$python"
}

& $python -B -X utf8 -m app.workspace `
    --system-root $projectRoot `
    --display-name $DisplayName `
    --slug $Slug
exit $LASTEXITCODE
