[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[a-z0-9][a-z0-9-]{0,31}$")]
    [string]$Slug
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceFile = Join-Path $projectRoot "users\$Slug\workspace.json"
if (-not (Test-Path -LiteralPath $workspaceFile -PathType Leaf)) {
    throw "Personal workspace does not exist: $workspaceFile"
}
$workspace = Get-Content -LiteralPath $workspaceFile -Raw -Encoding UTF8 |
    ConvertFrom-Json
if (
    $workspace.schema -ne "cvwf.personal-workspace.v2" -or
    $workspace.slug -ne $Slug -or
    $workspace.status -ne "active"
) {
    throw "Personal workspace manifest is invalid: $workspaceFile"
}
$payload = [ordered]@{
    schema = "cvwf.active-workspace.v1"
    workspace_file = "users/$Slug/workspace.json"
}
$destination = Join-Path $projectRoot "config\active-workspace.json"
$json = $payload | ConvertTo-Json -Depth 4
[IO.File]::WriteAllText(
    $destination,
    $json + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)
Write-Output "Active personal workspace: $Slug"
