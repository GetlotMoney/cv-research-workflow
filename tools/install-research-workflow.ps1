[CmdletBinding()]
param(
    [string]$PythonExecutable = "",

    [string]$SkillsRoot = (Join-Path $env:USERPROFILE ".codex\skills"),

    [switch]$Check
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$sourcePath = Join-Path $projectRoot "common\research\skills\cv-experiment-workflow"
$researchPackagePath = Join-Path $projectRoot "common\research"
$targetPath = Join-Path $SkillsRoot "cv-experiment-workflow"
$configPath = Join-Path $projectRoot "config\environment.local.json"

function Get-NormalizedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Resolve-ResearchPython {
    param([string]$RequestedPython)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPython)) {
        return Get-NormalizedPath -Path $RequestedPython
    }

    $condaCommand = Get-Command conda -ErrorAction SilentlyContinue
    if ($null -eq $condaCommand) {
        throw (
            "Conda was not found. Pass -PythonExecutable with the " +
            "dvsr_gpu python.exe path."
        )
    }
    $rawEnvironments = & $condaCommand.Source env list --json
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to list Conda environments."
    }
    try {
        $environmentList = $rawEnvironments | ConvertFrom-Json
    } catch {
        throw "Conda returned invalid environment JSON."
    }
    $matches = @(
        $environmentList.envs |
            Where-Object {
                (Split-Path -Leaf ([string]$_)) -eq "dvsr_gpu"
            }
    )
    if ($matches.Count -ne 1) {
        throw (
            "Expected exactly one Conda environment named dvsr_gpu. " +
            "Pass -PythonExecutable to select it explicitly."
        )
    }
    return Get-NormalizedPath -Path (Join-Path $matches[0] "python.exe")
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Left,

        [Parameter(Mandatory = $true)]
        [string]$Right
    )

    return [System.StringComparer]::OrdinalIgnoreCase.Equals(
        (Get-NormalizedPath -Path $Left),
        (Get-NormalizedPath -Path $Right)
    )
}

function Invoke-GpuProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python
    )

    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Python executable was not found: $Python"
    }

    $probeCode = @'
import json
import sys
import torch
if sys.version_info < (3, 11):
    import tomli
else:
    import tomllib

payload = {
    "python_version": sys.version.split()[0],
    "torch_version": torch.__version__,
    "cuda_available": torch.cuda.is_available(),
    "cuda_runtime": torch.version.cuda,
    "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
}
print(json.dumps(payload, ensure_ascii=False))
'@

    # Pipe the probe through stdin so Windows PowerShell cannot strip the
    # Python dictionary quotes while building a native `-c` argument.
    $rawProbe = $probeCode | & $Python -B -u -
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query PyTorch and CUDA with: $Python"
    }

    try {
        $probe = $rawProbe | ConvertFrom-Json
    } catch {
        throw "The Python environment returned invalid probe JSON: $($_.Exception.Message)"
    }

    if (-not $probe.cuda_available) {
        throw "CUDA is unavailable; CPU fallback is forbidden."
    }
    if ([string]::IsNullOrWhiteSpace([string]$probe.cuda_runtime)) {
        throw "PyTorch did not report a CUDA runtime; CPU fallback is forbidden."
    }
    if ([string]::IsNullOrWhiteSpace([string]$probe.gpu_name)) {
        throw "PyTorch did not report a GPU name; CPU fallback is forbidden."
    }

    return $probe
}

function Get-SkillTargetState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Target,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedSource
    )

    if (-not (Test-Path -LiteralPath $Target)) {
        return "missing"
    }

    $item = Get-Item -LiteralPath $Target -Force
    if ($item.LinkType -ne "Junction") {
        return "conflict"
    }

    $junctionTarget = @($item.Target)[0]
    if (
        [string]::IsNullOrWhiteSpace([string]$junctionTarget) -or
        -not (Test-SamePath -Left $junctionTarget -Right $ExpectedSource)
    ) {
        return "conflict"
    }

    return "matching_junction"
}

function Test-InstalledState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EnvironmentConfig,

        [Parameter(Mandatory = $true)]
        [string]$SkillTarget,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedSource,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedPython,

        [Parameter(Mandatory = $true)]
        [object]$CurrentProbe
    )

    if (-not (Test-Path -LiteralPath $EnvironmentConfig -PathType Leaf)) {
        throw "Research environment config was not found: $EnvironmentConfig"
    }

    try {
        $config = Get-Content -LiteralPath $EnvironmentConfig -Raw -Encoding UTF8 |
            ConvertFrom-Json
    } catch {
        throw "Research environment config is invalid JSON: $($_.Exception.Message)"
    }

    $allowedFields = @(
        "schema",
        "platform",
        "execution_policy",
        "cpu_fallback",
        "manager",
        "environment_name",
        "environment_prefix",
        "python_executable",
        "python_version",
        "pytorch_version",
        "cuda_runtime",
        "gpu",
        "status",
        "last_verified_at",
        "verification_command"
    )
    foreach ($property in $config.PSObject.Properties.Name) {
        if ($property -notin $allowedFields) {
            throw "Research environment config contains an unrelated field: $property"
        }
    }
    if ($config.schema -ne "cvwf.research-environment.v2") {
        throw "Research environment config has an unsupported schema."
    }
    if ($config.platform -ne "windows_only") {
        throw "Research environment config must set platform=windows_only."
    }
    if ($config.execution_policy -ne "gpu_only" -or $config.cpu_fallback -ne $false) {
        throw "Research environment config must require GPU and forbid CPU fallback."
    }
    if ($config.manager -ne "conda_named_environment") {
        throw "Research environment config must describe the named Conda environment."
    }
    $expectedPrefix = Split-Path -Parent $ExpectedPython
    if (
        -not (Test-SamePath -Left ([string]$config.environment_prefix) -Right $expectedPrefix) -or
        [string]$config.environment_name -ne [string](Split-Path -Leaf $expectedPrefix)
    ) {
        throw "Research environment config points to a different Conda environment."
    }
    if (-not (Test-SamePath -Left ([string]$config.python_executable) -Right $ExpectedPython)) {
        throw "Research environment config points to a different Python executable."
    }
    if ([string]$config.python_version -ne [string]$CurrentProbe.python_version) {
        throw "Research environment config has a stale Python version."
    }
    if ([string]$config.pytorch_version -ne [string]$CurrentProbe.torch_version) {
        throw "Research environment config has a stale PyTorch version."
    }
    if ([string]$config.cuda_runtime -ne [string]$CurrentProbe.cuda_runtime) {
        throw "Research environment config has a stale CUDA runtime."
    }
    if ([string]$config.gpu.name -ne [string]$CurrentProbe.gpu_name) {
        throw "Research environment config has a stale GPU name."
    }
    if ($config.status -ne "verified") {
        throw "Research environment config is not marked as verified."
    }

    $state = Get-SkillTargetState -Target $SkillTarget -ExpectedSource $ExpectedSource
    if ($state -ne "matching_junction") {
        throw "The installed cv-experiment-workflow is not a Junction to the shared source."
    }

    Write-Output "Research environment and shared Skill Junction are synchronized."
}

if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
    throw "Shared research Skill source was not found: $sourcePath"
}
$PythonExecutable = Resolve-ResearchPython `
    -RequestedPython $PythonExecutable
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python executable was not found: $PythonExecutable"
}

if (-not $Check) {
    & $PythonExecutable -B -m pip install `
        --disable-pip-version-check `
        --no-input `
        --editable `
        $researchPackagePath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install the shared research package dependencies."
    }
}

$probe = Invoke-GpuProbe -Python $PythonExecutable

if ($Check) {
    Test-InstalledState `
        -EnvironmentConfig $configPath `
        -SkillTarget $targetPath `
        -ExpectedSource $sourcePath `
        -ExpectedPython $PythonExecutable `
        -CurrentProbe $probe
    exit 0
}

$targetState = Get-SkillTargetState -Target $targetPath -ExpectedSource $sourcePath
if ($targetState -eq "conflict") {
    throw "Refusing to overwrite '$targetPath': it is not a Junction to '$sourcePath'."
}

if ($targetState -eq "missing") {
    if (Test-Path -LiteralPath $SkillsRoot) {
        if (-not (Test-Path -LiteralPath $SkillsRoot -PathType Container)) {
            throw "SkillsRoot exists but is not a directory: $SkillsRoot"
        }
    } else {
        $null = New-Item -ItemType Directory -Path $SkillsRoot
    }

    $null = New-Item -ItemType Junction -Path $targetPath -Target $sourcePath
    Write-Output "Installed shared Skill Junction: $targetPath -> $sourcePath"
} else {
    Write-Output "Skill Junction already points to the shared source: $targetPath"
}

$environmentPrefix = Split-Path -Parent $PythonExecutable
$environmentName = Split-Path -Leaf $environmentPrefix
$payload = [ordered]@{
    schema = "cvwf.research-environment.v2"
    platform = "windows_only"
    execution_policy = "gpu_only"
    cpu_fallback = $false
    manager = "conda_named_environment"
    environment_name = $environmentName
    environment_prefix = $environmentPrefix
    python_executable = (Get-NormalizedPath -Path $PythonExecutable)
    python_version = [string]$probe.python_version
    pytorch_version = [string]$probe.torch_version
    cuda_runtime = [string]$probe.cuda_runtime
    gpu = [ordered]@{
        name = [string]$probe.gpu_name
    }
    status = "verified"
    last_verified_at = [DateTimeOffset]::Now.ToString("o")
    verification_command = "tools\install-research-workflow.ps1 -Check"
}

$configJson = ($payload | ConvertTo-Json -Depth 4) + [Environment]::NewLine
[IO.File]::WriteAllText(
    $configPath,
    $configJson,
    [Text.UTF8Encoding]::new($false)
)

Write-Output "Wrote research environment config: $configPath"
