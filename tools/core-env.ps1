[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("verify-gpu", "python", "pytest")]
    [string]$Action = "verify-gpu",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot "config\environment.local.json"

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Fixed environment config not found: $configPath"
}

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 |
    ConvertFrom-Json
$python = [string]$config.python_executable

if ($config.execution_policy -ne "gpu_only") {
    throw "This project requires execution_policy=gpu_only."
}
if ($config.cpu_fallback -ne $false) {
    throw "CPU fallback is forbidden for this project."
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python was not found in the fixed Conda environment: $python"
}

switch ($Action) {
    "python" {
        & $python @RemainingArgs
        exit $LASTEXITCODE
    }
    "pytest" {
        & $python -m pytest @RemainingArgs
        exit $LASTEXITCODE
    }
    "verify-gpu" {
        $asJson = $RemainingArgs -contains "--json"
        $probeCode = @'
import json
import torch

if not torch.cuda.is_available():
    raise SystemExit('CUDA is unavailable; CPU fallback is forbidden')

left = torch.randn((256, 256), device='cuda')
right = torch.randn((256, 256), device='cuda')
result = left @ right
torch.cuda.synchronize()

payload = {
    'status': 'pass',
    'environment_name': 'dvsr_gpu',
    'python': __import__('sys').executable,
    'torch': torch.__version__,
    'cuda_runtime': torch.version.cuda,
    'cuda_available': True,
    'tensor_device': str(result.device),
    'device_name': torch.cuda.get_device_name(0),
    'cpu_fallback': False,
}
print(json.dumps(payload, ensure_ascii=False))
'@
        $raw = & $python -u -c $probeCode
        if ($LASTEXITCODE -ne 0) {
            throw "GPU verification failed; CPU fallback is forbidden."
        }
        if ($asJson) {
            $raw
        } else {
            $payload = $raw | ConvertFrom-Json
            Write-Output (
                "GPU verified: {0}; PyTorch {1}; CUDA {2}" -f
                $payload.device_name,
                $payload.torch,
                $payload.cuda_runtime
            )
        }
        exit 0
    }
}
