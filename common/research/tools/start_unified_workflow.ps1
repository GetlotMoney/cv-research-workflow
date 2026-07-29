[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Project,
    [Parameter(Mandatory = $true)] [string] $PaperFlowRoot,
    [string] $LibraryRoot = "",
    [string] $PapersRoot = "",
    [ValidateRange(1, 65535)] [int] $ConsolePort = 8765,
    [ValidateRange(1, 65535)] [int] $PaperFlowPort = 8766,
    [string] $Python = "python",
    [string] $PaperFlowPython = "",
    [switch] $NoBrowser,
    [switch] $HealthCheckOnly
)

$ErrorActionPreference = "Stop"
$candidateRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$projectRoot = (Resolve-Path -LiteralPath $Project).Path
$instancesRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $projectRoot)).Path
$paperFlowRoot = (Resolve-Path -LiteralPath $PaperFlowRoot).Path
$libraryCandidate = if ($LibraryRoot) { $LibraryRoot } else { Join-Path $paperFlowRoot "runtime_v2" }
$libraryPath = (Resolve-Path -LiteralPath $libraryCandidate).Path
$workflowRoot = Split-Path -Parent (Split-Path -Parent $candidateRoot)
$environmentConfig = Join-Path $workflowRoot "config\environment.json"
$configuredPaperFlowPython = ""
if (Test-Path -LiteralPath $environmentConfig -PathType Leaf) {
    $workflowEnvironment = Get-Content -LiteralPath $environmentConfig `
        -Raw -Encoding UTF8 | ConvertFrom-Json
    $configuredPaperFlowPython = [string](
        $workflowEnvironment.paperflow.python_executable
    )
    $env:PAPERFLOW_DOCLING_ARTIFACTS = [string](
        $workflowEnvironment.paperflow.docling_artifacts
    )
}
$paperFlowPythonCommand = if ($PaperFlowPython) {
    $PaperFlowPython
} elseif (
    $configuredPaperFlowPython -and
    (Test-Path -LiteralPath $configuredPaperFlowPython -PathType Leaf)
) {
    $configuredPaperFlowPython
} else {
    $Python
}
$consoleServer = Join-Path $candidateRoot "skills\cv-experiment-workflow\scripts\console_server.py"
$preflightTool = Join-Path $candidateRoot "tools\check_unified_preflight.py"
$paperFlowModule = Join-Path $paperFlowRoot "paperflow_v2\__main__.py"
if (-not (Test-Path -LiteralPath $consoleServer -PathType Leaf)) { throw "候选科研控制台不存在：$consoleServer" }
if (-not (Test-Path -LiteralPath $preflightTool -PathType Leaf)) { throw "统一入口只读预检工具不存在：$preflightTool" }
if (-not (Test-Path -LiteralPath $paperFlowModule -PathType Leaf)) { throw "候选 PaperFlow 模块不存在：$paperFlowModule" }

$preflightOutput = @(
    & $Python "-B" "-X" "utf8" $preflightTool `
        "--project" $projectRoot `
        "--paperflow-root" $paperFlowRoot `
        "--library-root" $libraryPath
)
if ($LASTEXITCODE -ne 0) {
    throw "统一入口启动前核对失败；没有创建论文目录或启动服务。"
}
try {
    $preflight = ([string]::Join([Environment]::NewLine, $preflightOutput) | ConvertFrom-Json)
} catch {
    throw "统一入口预检没有返回有效 JSON。"
}
if (
    $preflight.schema -ne "cv-unified-preflight.v1" -or
    $preflight.status -ne "pass" -or
    $preflight.producer.release_version -ne "1.5.0" -or
    $preflight.producer.system_version -ne "SYS-V2.13.0" -or
    $preflight.paperflow.receiver_profile -ne "bound_evidence_v15"
) {
    throw "科研系统与 PaperFlow 的版本握手没有通过。"
}

if ($ConsolePort -eq $PaperFlowPort) {
    throw "科研控制台与 PaperFlow 不能使用同一个端口。"
}
if ((Get-NetTCPConnection -LocalPort $ConsolePort -State Listen -ErrorAction SilentlyContinue) -or (Get-NetTCPConnection -LocalPort $PaperFlowPort -State Listen -ErrorAction SilentlyContinue)) {
    throw "统一入口端口已被占用；不会接管已有进程。"
}

$papersCandidate = if ($PapersRoot) { $PapersRoot } else { Join-Path $projectRoot "paperflow-papers" }
if (-not (Test-Path -LiteralPath $papersCandidate)) {
    New-Item -ItemType Directory -Path $papersCandidate -ErrorAction Stop | Out-Null
}
$papersPath = (Resolve-Path -LiteralPath $papersCandidate).Path
$sessionRoot = Join-Path -Path ([System.IO.Path]::GetTempPath()) -ChildPath ("cv-unified-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $sessionRoot -ErrorAction Stop | Out-Null
$ownedPidPath = Join-Path $sessionRoot "owned-pids.json"
$paperFlowStdout = Join-Path $sessionRoot "paperflow.stdout"
$paperFlowStderr = Join-Path $sessionRoot "paperflow.stderr"
$children = @()

function ConvertTo-ProcessArgument([string] $Value) {
    if ($Value.Contains([char]34)) { throw "启动参数不能包含双引号。" }
    $escaped = [regex]::Replace($Value, '(\\+)$', '$1$1')
    return [string]::Concat([char]34, $escaped, [char]34)
}

function Stop-OwnedChildren {
    foreach ($child in $children) {
        $process = Get-Process -Id $child.Id -ErrorAction SilentlyContinue
        if ($null -eq $process) { continue }
        try {
            if ($process.StartTime.ToUniversalTime().Ticks -eq $child.StartTimeUtc) {
                Stop-Process -Id $child.Id -ErrorAction SilentlyContinue
            }
        } catch {
            # 无法确认进程身份时宁可不停止，避免误伤 PID 已复用的用户进程。
        }
    }
    foreach ($temporaryFile in @($ownedPidPath, $paperFlowStdout, $paperFlowStderr)) {
        if (Test-Path -LiteralPath $temporaryFile -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryFile -Force
        }
    }
    if (Test-Path -LiteralPath $sessionRoot -PathType Container) {
        Remove-Item -LiteralPath $sessionRoot
    }
}

function Wait-Healthy(
    [int] $Port,
    [string] $Path,
    [ValidateSet("paperflow-health", "paperflow-bootstrap", "console-state")]
    [string] $Contract,
    [hashtable] $Headers = @{},
    [System.Diagnostics.Process[]] $Processes = @()
) {
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        foreach ($process in $Processes) {
            if ($process.HasExited) {
                throw "候选服务在健康检查完成前退出。"
            }
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 -Headers $Headers -Uri "http://127.0.0.1:$Port$Path"
            if ($response.StatusCode -ne 200) { throw "unexpected status" }
            $payload = $response.Content | ConvertFrom-Json
            $valid = switch ($Contract) {
                "paperflow-health" {
                    $payload.status -eq "ok"
                }
                "paperflow-bootstrap" {
                    $null -ne $payload.catalog -and
                    $payload.catalog.cards.Count -gt 0 -and
                    $payload.catalog.combinations.Count -gt 0
                }
                "console-state" {
                    $payload.schema -eq "cv-experiment-workflow.console-state.v1" -and
                    $payload.snapshot.status -eq "valid" -and
                    $payload.execution_enabled -eq $true
                }
            }
            if ($valid) { return }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw "候选服务未通过健康检查：127.0.0.1:$Port$Path"
}

function Wait-PaperFlowEntrypoint([System.Diagnostics.Process] $Process) {
    $pattern = [regex]'http://127\.0\.0\.1:(?<port>[0-9]{1,5})/#token=(?<token>[A-Za-z0-9_-]{16,256})'
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        if (Test-Path -LiteralPath $paperFlowStdout -PathType Leaf) {
            $text = Get-Content -LiteralPath $paperFlowStdout -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
            if ($null -ne $text) {
                $match = $pattern.Match([string]$text)
                if ($match.Success) {
                    if ([int]$match.Groups["port"].Value -ne $PaperFlowPort) {
                        throw "PaperFlow 返回了意外端口。"
                    }
                    return [pscustomobject]@{
                        Url = $match.Value
                        Token = $match.Groups["token"].Value
                    }
                }
            }
        }
        if ($Process.HasExited) {
            throw "PaperFlow 在完成本机启动前退出。"
        }
        Start-Sleep -Milliseconds 250
    }
    throw "等待 PaperFlow 本机入口超时。"
}

try {
    $paperFlowArguments = @(
        "-u",
        "-m",
        "paperflow_v2",
        "start-workflow",
        "--workspace-root",
        (ConvertTo-ProcessArgument $paperFlowRoot),
        "--library-root",
        (ConvertTo-ProcessArgument $libraryPath),
        "--papers-root",
        (ConvertTo-ProcessArgument $papersPath),
        "--port",
        $PaperFlowPort,
        "--no-browser"
    )
    $paperFlow = Start-Process -FilePath $paperFlowPythonCommand -ArgumentList $paperFlowArguments -WorkingDirectory $paperFlowRoot -RedirectStandardOutput $paperFlowStdout -RedirectStandardError $paperFlowStderr -PassThru -WindowStyle Hidden
    $children += [pscustomobject]@{ Id = $paperFlow.Id; StartTimeUtc = $paperFlow.StartTime.ToUniversalTime().Ticks }
    $paperFlowSession = Wait-PaperFlowEntrypoint $paperFlow
    Wait-Healthy -Port $PaperFlowPort -Path "/api/health" -Contract "paperflow-health" -Processes @($paperFlow)
    Wait-Healthy -Port $PaperFlowPort -Path "/api/bootstrap" -Contract "paperflow-bootstrap" -Headers @{ "X-PaperFlow-Token" = $paperFlowSession.Token } -Processes @($paperFlow)

    $consoleArguments = @(
        (ConvertTo-ProcessArgument $consoleServer),
        "--project",
        (ConvertTo-ProcessArgument $projectRoot),
        "--instances-root",
        (ConvertTo-ProcessArgument $instancesRoot),
        "--port",
        $ConsolePort,
        "--paperflow-entrypoint",
        (ConvertTo-ProcessArgument $paperFlowSession.Url)
    )
    $console = Start-Process -FilePath $Python -ArgumentList $consoleArguments -WorkingDirectory $candidateRoot -PassThru -WindowStyle Hidden
    $children += [pscustomobject]@{ Id = $console.Id; StartTimeUtc = $console.StartTime.ToUniversalTime().Ticks }
    $ownedPidJson = @{ schema = "cv-unified-owned-pids.v1"; processes = @($children) } | ConvertTo-Json
    [System.IO.File]::WriteAllText($ownedPidPath, $ownedPidJson, (New-Object System.Text.UTF8Encoding($false)))
    Wait-Healthy -Port $ConsolePort -Path "/api/state" -Contract "console-state" -Processes @($paperFlow, $console)
    if ($HealthCheckOnly) {
        @{
            schema = "cv-unified-launcher-health.v1"
            status = "pass"
            producer = $preflight.producer
            services = @("research-console", "paperflow")
        } | ConvertTo-Json -Compress
        return
    }
    if (-not $NoBrowser) {
        Start-Process "http://127.0.0.1:$ConsolePort/"
    }
    Write-Host "统一入口已启动：http://127.0.0.1:$ConsolePort/"
    Write-Host "在当前窗口按 Ctrl+C 会停止本次启动的两个本机服务；不会关机。"
    while ($true) {
        if ($paperFlow.HasExited) { throw "PaperFlow 服务意外退出。" }
        if ($console.HasExited) { throw "科研控制台服务意外退出。" }
        Start-Sleep -Milliseconds 500
    }
} finally {
    Stop-OwnedChildren
}
