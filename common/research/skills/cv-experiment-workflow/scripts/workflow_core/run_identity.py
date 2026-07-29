from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import sys
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any

from .git_safe import run_git_bounded


FROZEN_FIELDS = {"code", "config", "seed", "data", "environment"}
BOUND_CODE_FIELDS = {
    "schema",
    "codebase_id",
    "branch",
    "commit",
    "tag",
    "clean_required",
    "worktree_clean",
    "adapter_path",
    "adapter_sha256",
    "declared_code",
    "fingerprints",
}
BOUND_CODE_SCHEMA = "cv-experiment-workflow.bound-code.v2"
FINGERPRINT_FIELDS = {"config", "data", "evaluation", "environment"}
RUN_KINDS = {"synthetic_debug_only", "real_experiment"}
DATASET_IDENTITY_SCHEMA = "cv-experiment-workflow.dataset-identity.v1"
DATASET_IDENTITY_FIELDS = {
    "schema",
    "dataset_id",
    "version",
    "source_uri",
    "manifest_sha256",
    "split",
}
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
RAW_SHA256 = re.compile(r"[0-9a-f]{64}")
COMPARISON_POLICY_ENV_KEY = "workflow_comparison_policy"
COMPARISON_POLICY_SCHEMA = "cv-experiment-workflow.comparison-policy/v1"
CUDA_ATTESTATION_ENV_KEY = "central_cuda_attestation"
CUDA_ATTESTATION_SCHEMA = (
    "cv-experiment-workflow.central-cuda-attestation/v1"
)
CUDA_ATTESTATION_VERIFIER = (
    "workflow_core.run_identity.create_central_cuda_attestation"
)
TRUSTED_CUDA_TEMPLATES = {
    "PACK-CLS": ("cls", "1.0.0"),
    "PACK-DET": ("det", "1.0.0"),
    "PACK-GZSL": ("gzsl", "1.1.1"),
    "PACK-INSTSEG": ("instseg", "1.0.0"),
    "PACK-SEG": ("seg", "1.0.0"),
    "PACK-SR": ("sr", "1.0.0"),
}
CUDA_ATTESTATION_FIELDS = {
    "schema",
    "verified_by",
    "template_id",
    "template_version",
    "adapter_sha256",
    "python_executable_sha256",
    "torch_version",
    "cuda_runtime",
    "device_index",
    "device_count",
    "device_name",
    "compute_capability",
    "probe",
}
CUDA_PROBE_FIELDS = {
    "operation",
    "result",
    "tensor_device",
}
CURRENT_COMPARISON_POLICY = {
    "schema": COMPARISON_POLICY_SCHEMA,
    "algorithm": "fair-live-sealed-runs-v1",
    "require_live_output_seal": True,
}
REQUIREMENTS_LOCK_CANDIDATES = (
    "requirements.lock",
    "requirements.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "conda-lock.yml",
    "environment.yml",
)
MAX_REQUIREMENTS_LIST_BYTES = 64 * 1024
MAX_REQUIREMENTS_LOCK_BYTES = 4 * 1024 * 1024


def _require_cuda_for_evidence(
    *,
    config: dict[str, Any],
    declared_environment: dict[str, Any],
    run_kind: str,
    evidence_requested: bool,
) -> None:
    if (
        not evidence_requested
        or run_kind != "real_experiment"
    ):
        return
    if (
        config.get("device") != "cuda"
        or declared_environment.get("device") != "cuda"
    ):
        raise ValueError(
            "正式 real evidence 必须使用 CUDA/GPU；cpu 只允许 debug"
        )


def create_central_cuda_attestation(
    *,
    gate: dict[str, Any],
    adapter: dict[str, str],
) -> dict[str, Any]:
    template_id = gate.get("template_id")
    template_version = gate.get("template_version")
    trusted = TRUSTED_CUDA_TEMPLATES.get(str(template_id))
    if (
        gate.get("source") != "domain_pack"
        or trusted is None
        or template_version != trusted[1]
    ):
        raise ValueError(
            "正式 evidence 只允许带受信任 CUDA 验证器的内置方向模板"
        )
    from .domain_packs import resolve_domain_pack, validate_domain_pack

    pack_root = resolve_domain_pack(f"{trusted[0]}@{trusted[1]}")
    manifest = validate_domain_pack(pack_root)
    adapter_entries = [
        item
        for item in manifest["files"]
        if item.get("path") == "workflow_adapter.py"
    ]
    adapter_sha256 = adapter.get("sha256")
    if (
        len(adapter_entries) != 1
        or adapter_entries[0].get("sha256") != adapter_sha256
    ):
        raise ValueError(
            "正式 evidence 的 Adapter 不匹配受信任 CUDA 验证器"
        )

    try:
        import torch
    except (ImportError, OSError) as error:
        raise ValueError("正式 evidence 的中央 CUDA 验证无法导入 PyTorch") from error
    try:
        if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
            raise ValueError("正式 evidence 的中央 CUDA 验证未发现可用 GPU")
        device_index = int(torch.cuda.current_device())
        device_count = int(torch.cuda.device_count())
        device = f"cuda:{device_index}"
        left = torch.ones((2, 2), device=device)
        right = torch.ones((2, 2), device=device)
        result = float((left @ right).sum().item())
        torch.cuda.synchronize(device_index)
        if result != 8.0:
            raise ValueError("正式 evidence 的中央 CUDA 真算子结果异常")
        capability = torch.cuda.get_device_capability(device_index)
        attestation = {
            "schema": CUDA_ATTESTATION_SCHEMA,
            "verified_by": CUDA_ATTESTATION_VERIFIER,
            "template_id": template_id,
            "template_version": template_version,
            "adapter_sha256": adapter_sha256,
            "python_executable_sha256": _sha256_file(Path(sys.executable)),
            "torch_version": str(torch.__version__),
            "cuda_runtime": str(torch.version.cuda),
            "device_index": device_index,
            "device_count": device_count,
            "device_name": str(torch.cuda.get_device_name(device_index)),
            "compute_capability": [
                int(capability[0]),
                int(capability[1]),
            ],
            "probe": {
                "operation": "2x2_ones_matmul_sum",
                "result": result,
                "tensor_device": str(left.device),
            },
        }
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("正式 evidence 的中央 CUDA 真算子验证失败") from error
    return _validate_cuda_attestation(attestation)


def build_bound_frozen(
    variant: dict[str, Any],
    *,
    binding: dict[str, Any],
    gate: dict[str, Any],
    adapter: dict[str, str],
    purpose: str,
) -> dict[str, Any]:
    if not isinstance(variant, dict) or set(variant) != FROZEN_FIELDS:
        raise ValueError(
            "绑定 Codebase 的 Adapter 候选必须严格包含 "
            "code/config/seed/data/environment"
        )
    if purpose not in {"debug", "evidence"}:
        raise ValueError("Run purpose 必须是 debug/evidence")
    declared_code = _finite_object(variant["code"], "Adapter declared code")
    config = _finite_object(variant["config"], "Adapter config")
    data = _finite_object(variant["data"], "Adapter data")
    declared_environment = _finite_object(
        variant["environment"],
        "Adapter declared environment",
    )
    seed = variant["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("Adapter seed 必须是整数")
    evaluation = data.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError(
            "绑定 Codebase 的 Adapter data 必须显式提供 evaluation object"
        )
    _finite_object(evaluation, "Adapter evaluation")

    if (
        gate.get("codebase_id") != binding.get("codebase_id")
        or gate.get("branch") != binding.get("branch")
        or gate.get("commit") != binding.get("commit")
        or gate.get("tag") != binding.get("tag")
    ):
        raise ValueError("Codebase gate 返回身份与 Task binding 不一致")
    worktree_clean = gate.get("worktree_clean")
    if type(worktree_clean) is not bool:
        raise ValueError("Codebase gate 缺少 worktree_clean")
    clean_required = purpose == "evidence"
    if clean_required and not worktree_clean:
        raise ValueError("evidence Run 必须来自 clean Codebase 工作树")

    declared_kind = _data_kind(data.get("kind"))
    dataset_identity = data.get("dataset_identity")
    if declared_kind == "synthetic_debug_only":
        if dataset_identity is not None:
            data["dataset_identity"] = _dataset_identity(dataset_identity)
    else:
        data["dataset_identity"] = _dataset_identity(dataset_identity)
    run_kind = (
        "synthetic_debug_only"
        if declared_kind == "synthetic_debug_only"
        else "real_experiment"
    )
    _require_cuda_for_evidence(
        config=config,
        declared_environment=declared_environment,
        run_kind=run_kind,
        evidence_requested=purpose == "evidence",
    )
    data["run_kind"] = run_kind
    data["paper_eligible"] = bool(
        purpose == "evidence"
        and run_kind == "real_experiment"
        and worktree_clean
    )
    environment = live_environment_snapshot()
    environment["requirements_locks"] = committed_requirements_locks(
        Path(gate["repo_path"]),
        binding["commit"],
    )
    environment["declared_environment"] = declared_environment
    environment[COMPARISON_POLICY_ENV_KEY] = deepcopy(
        CURRENT_COMPARISON_POLICY
    )
    if purpose == "evidence" and run_kind == "real_experiment":
        environment[CUDA_ATTESTATION_ENV_KEY] = (
            create_central_cuda_attestation(
                gate=gate,
                adapter=adapter,
            )
        )

    adapter_sha256 = adapter.get("sha256")
    if (
        not isinstance(adapter_sha256, str)
        or RAW_SHA256.fullmatch(adapter_sha256) is None
    ):
        raise ValueError("Adapter 摘要缺少规范 sha256")
    code = {
        "schema": BOUND_CODE_SCHEMA,
        "codebase_id": binding["codebase_id"],
        "branch": binding["branch"],
        "commit": binding["commit"],
        "tag": binding["tag"],
        "clean_required": clean_required,
        "worktree_clean": worktree_clean,
        "adapter_path": "workflow_adapter.py",
        "adapter_sha256": adapter_sha256,
        "declared_code": declared_code,
        "fingerprints": {
            "config": canonical_fingerprint(config),
            "data": data_contract_fingerprint(data),
            "evaluation": canonical_fingerprint(evaluation),
            "environment": canonical_fingerprint(environment),
        },
    }
    frozen = {
        "code": code,
        "config": config,
        "seed": seed,
        "data": data,
        "environment": environment,
    }
    validate_bound_frozen(frozen, purpose=purpose)
    return frozen


def is_bound_frozen(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    code = value.get("code")
    return (
        isinstance(code, dict)
        and code.get("schema") == BOUND_CODE_SCHEMA
    )


def bind_current_comparison_policy(
    frozen: dict[str, Any],
) -> dict[str, Any]:
    """把当前比较规则写进待创建 Run 的冻结环境，不改调用方对象。"""
    if not isinstance(frozen, dict):
        raise ValueError("Run frozen 必须是 JSON object")
    environment = frozen.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("Run frozen.environment 必须是 JSON object")
    current = environment.get(COMPARISON_POLICY_ENV_KEY)
    if current is not None and current != CURRENT_COMPARISON_POLICY:
        raise ValueError("Run frozen comparison policy 与当前中央规则冲突")
    bound = deepcopy(frozen)
    bound["environment"][COMPARISON_POLICY_ENV_KEY] = deepcopy(
        CURRENT_COMPARISON_POLICY
    )
    return bound


def comparison_policy_for_frozen(
    frozen: object,
) -> dict[str, Any] | None:
    """读取并严格校验冻结的比较规则；没有标记表示只读历史记录。"""
    if not isinstance(frozen, dict):
        return None
    environment = frozen.get("environment")
    if not isinstance(environment, dict):
        return None
    policy = environment.get(COMPARISON_POLICY_ENV_KEY)
    if policy is None:
        return None
    if policy != CURRENT_COMPARISON_POLICY:
        raise ValueError("Run frozen comparison policy 无效或不受支持")
    return deepcopy(CURRENT_COMPARISON_POLICY)


def validate_bound_frozen(
    frozen: dict[str, Any],
    *,
    purpose: str | None = None,
) -> None:
    if set(frozen) != FROZEN_FIELDS:
        raise ValueError("绑定 Run frozen 五字段结构无效")
    code = frozen.get("code")
    config = frozen.get("config")
    data = frozen.get("data")
    environment = frozen.get("environment")
    if not all(
        isinstance(value, dict)
        for value in (code, config, data, environment)
    ):
        raise ValueError("绑定 Run frozen 的对象字段无效")
    assert isinstance(code, dict)
    assert isinstance(config, dict)
    assert isinstance(data, dict)
    assert isinstance(environment, dict)
    comparison_policy_for_frozen(frozen)
    if set(code) != BOUND_CODE_FIELDS:
        raise ValueError("绑定 Run frozen.code 中央字段无效")
    if code["schema"] != BOUND_CODE_SCHEMA:
        raise ValueError("绑定 Run frozen.code schema 无效")
    if re.fullmatch(r"CB-[0-9]{4}", str(code["codebase_id"])) is None:
        raise ValueError("绑定 Run codebase_id 无效")
    branch = code["branch"]
    if (
        not isinstance(branch, str)
        or not branch
        or branch != branch.strip()
        or _has_control_character(branch)
    ):
        raise ValueError("绑定 Run branch 无效")
    if re.fullmatch(r"[0-9a-f]{40}", str(code["commit"])) is None:
        raise ValueError("绑定 Run commit 无效")
    tag = code["tag"]
    if tag is not None and (
        not isinstance(tag, str)
        or not tag
        or tag != tag.strip()
        or _has_control_character(tag)
    ):
        raise ValueError("绑定 Run tag 无效")
    if type(code["clean_required"]) is not bool:
        raise ValueError("绑定 Run clean_required 无效")
    if type(code["worktree_clean"]) is not bool:
        raise ValueError("绑定 Run worktree_clean 无效")
    if code["clean_required"] and not code["worktree_clean"]:
        raise ValueError("绑定 evidence Run 不能冻结 dirty 工作树")
    if code["adapter_path"] != "workflow_adapter.py":
        raise ValueError("绑定 Run Adapter 路径必须固定为 workflow_adapter.py")
    if (
        not isinstance(code["adapter_sha256"], str)
        or RAW_SHA256.fullmatch(code["adapter_sha256"]) is None
    ):
        raise ValueError("绑定 Run adapter_sha256 无效")
    declared_code = _finite_object(
        code["declared_code"],
        "绑定 Run declared_code",
    )

    evaluation = data.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError("绑定 Run data.evaluation 缺失")
    _finite_object(evaluation, "绑定 Run evaluation")
    if data.get("run_kind") not in RUN_KINDS:
        raise ValueError("绑定 Run run_kind 无效")
    declared_kind = _data_kind(data.get("kind"))
    expected_run_kind = (
        "synthetic_debug_only"
        if declared_kind == "synthetic_debug_only"
        else "real_experiment"
    )
    if data["run_kind"] != expected_run_kind:
        raise ValueError("绑定 Run run_kind 与 data.kind 不一致")
    declared_environment = environment.get("declared_environment")
    if not isinstance(declared_environment, dict):
        raise ValueError("绑定 Run declared_environment 无效")
    _require_cuda_for_evidence(
        config=config,
        declared_environment=declared_environment,
        run_kind=expected_run_kind,
        evidence_requested=code["clean_required"],
    )
    central_cuda_attestation = environment.get(CUDA_ATTESTATION_ENV_KEY)
    if code["clean_required"] and expected_run_kind == "real_experiment":
        if not isinstance(central_cuda_attestation, dict):
            raise ValueError(
                "正式 evidence 缺少中央 CUDA 运行证明"
            )
        python_snapshot = environment.get("python")
        expected_python_sha256 = (
            python_snapshot.get("executable_sha256")
            if isinstance(python_snapshot, dict)
            else None
        )
        _validate_cuda_attestation(
            central_cuda_attestation,
            expected_adapter_sha256=code["adapter_sha256"],
            expected_python_sha256=expected_python_sha256,
        )
    elif central_cuda_attestation is not None:
        raise ValueError("非正式 Run 不得携带中央 CUDA 运行证明")
    dataset_identity = data.get("dataset_identity")
    if expected_run_kind == "real_experiment":
        _dataset_identity(dataset_identity)
    elif dataset_identity is not None:
        _dataset_identity(dataset_identity)
    if type(data.get("paper_eligible")) is not bool:
        raise ValueError("绑定 Run paper_eligible 无效")
    expected_paper_eligible = bool(
        code["clean_required"]
        and code["worktree_clean"]
        and data["run_kind"] == "real_experiment"
    )
    if data["paper_eligible"] != expected_paper_eligible:
        raise ValueError("绑定 Run paper_eligible 与中央规则不一致")
    if purpose is not None:
        if purpose not in {"debug", "evidence"}:
            raise ValueError("绑定 Run purpose 无效")
        if code["clean_required"] != (purpose == "evidence"):
            raise ValueError("绑定 Run clean_required 与 purpose 不一致")

    _validate_environment(environment)
    fingerprints = code["fingerprints"]
    if (
        not isinstance(fingerprints, dict)
        or set(fingerprints) != FINGERPRINT_FIELDS
        or any(
            not isinstance(value, str) or SHA256.fullmatch(value) is None
            for value in fingerprints.values()
        )
    ):
        raise ValueError("绑定 Run fingerprints 结构无效")
    expected_fingerprints = {
        "config": canonical_fingerprint(config),
        "data": data_contract_fingerprint(data),
        "evaluation": canonical_fingerprint(evaluation),
        "environment": canonical_fingerprint(environment),
    }
    if fingerprints != expected_fingerprints:
        raise ValueError("绑定 Run fingerprint 与冻结内容不一致")
    _finite_object(frozen, "绑定 Run frozen")


def live_environment_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag or "missing",
            "abi_flags": getattr(sys, "abiflags", "") or "none",
            "executable_sha256": _sha256_file(Path(sys.executable)),
        },
        "packages": {
            distribution: _installed_version(distribution)
            for distribution in (
                "torch",
                "numpy",
                "Pillow",
                "torchvision",
            )
        },
    }
    return _finite_object(snapshot, "live environment")


def committed_requirements_locks(
    repository: Path,
    commit: str,
) -> dict[str, str]:
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("requirements lock commit 无效")
    result = run_git_bounded(
        repository,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        commit,
        "--",
        *REQUIREMENTS_LOCK_CANDIDATES,
        stdout_limit=MAX_REQUIREMENTS_LIST_BYTES,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError("无法从绑定 commit 读取 requirements lock 清单")
    names = [
        item.decode("utf-8", errors="strict")
        for item in result.stdout.split(b"\0")
        if item
    ]
    locks: dict[str, str] = {}
    for name in sorted(names):
        if name not in REQUIREMENTS_LOCK_CANDIDATES:
            raise ValueError("requirements lock 路径必须位于仓库根目录")
        blob = run_git_bounded(
            repository,
            "cat-file",
            "blob",
            f"{commit}:{name}",
            stdout_limit=MAX_REQUIREMENTS_LOCK_BYTES,
            timeout=30,
        )
        if blob.returncode != 0:
            raise ValueError(f"无法读取 requirements lock：{name}")
        locks[name] = "sha256:" + hashlib.sha256(blob.stdout).hexdigest()
    return locks


def _installed_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as error:
        raise ValueError("无法读取当前 Python 解释器以生成摘要") from error
    return "sha256:" + digest.hexdigest()


def canonical_fingerprint(value: object) -> str:
    normalized = _finite_json(value, "fingerprint input")
    content = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def data_contract_fingerprint(data: object) -> str:
    normalized = _finite_object(data, "data fingerprint input")
    pure_data_contract = {
        key: value
        for key, value in normalized.items()
        if key not in {"evaluation", "run_kind", "paper_eligible"}
    }
    return canonical_fingerprint(pure_data_contract)


def _data_kind(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 4096
        or _has_control_character(value)
    ):
        raise ValueError(
            "绑定 Codebase 的 Adapter data.kind 必须是规范非空字符串"
        )
    return value


def _dataset_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != DATASET_IDENTITY_FIELDS:
        raise ValueError(
            "real_experiment 必须提供精确 dataset_identity 合同"
        )
    if value.get("schema") != DATASET_IDENTITY_SCHEMA:
        raise ValueError("dataset_identity.schema 无效")
    for field in ("dataset_id", "version", "split"):
        text = value.get(field)
        if (
            not isinstance(text, str)
            or not text
            or text != text.strip()
            or len(text) > 4096
            or _has_control_character(text)
        ):
            raise ValueError(f"dataset_identity.{field} 必须是规范非空字符串")
    source_uri = value.get("source_uri")
    if (
        not isinstance(source_uri, str)
        or not source_uri
        or source_uri != source_uri.strip()
        or len(source_uri) > 4096
        or _has_control_character(source_uri)
        or any(character.isspace() for character in source_uri)
        or "\\" in source_uri
        or re.fullmatch(
            r"[A-Za-z][A-Za-z0-9+.-]*:[^\s]+",
            source_uri,
        )
        is None
        or source_uri.split(":", 1)[0].lower() == "file"
        or re.match(r"^[A-Za-z]:", source_uri) is not None
    ):
        raise ValueError(
            "dataset_identity.source_uri 必须是非 file、本机路径无关的合法 URI"
        )
    manifest = value.get("manifest_sha256")
    if not isinstance(manifest, str) or SHA256.fullmatch(manifest) is None:
        raise ValueError(
            "dataset_identity.manifest_sha256 必须是 sha256: 加 64 位小写 hex"
        )
    return _finite_object(value, "dataset_identity")


def _has_control_character(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C")
        for character in value
    )


def _validate_cuda_attestation(
    value: object,
    *,
    expected_adapter_sha256: str | None = None,
    expected_python_sha256: object = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != CUDA_ATTESTATION_FIELDS:
        raise ValueError("中央 CUDA 运行证明结构无效")
    if (
        value.get("schema") != CUDA_ATTESTATION_SCHEMA
        or value.get("verified_by") != CUDA_ATTESTATION_VERIFIER
        or value.get("template_id") not in TRUSTED_CUDA_TEMPLATES
    ):
        raise ValueError("中央 CUDA 运行证明身份无效")
    trusted = TRUSTED_CUDA_TEMPLATES[value["template_id"]]
    if value.get("template_version") != trusted[1]:
        raise ValueError("中央 CUDA 运行证明模板版本无效")
    adapter_sha256 = value.get("adapter_sha256")
    python_sha256 = value.get("python_executable_sha256")
    if (
        not isinstance(adapter_sha256, str)
        or RAW_SHA256.fullmatch(adapter_sha256) is None
        or (
            expected_adapter_sha256 is not None
            and adapter_sha256 != expected_adapter_sha256
        )
    ):
        raise ValueError("中央 CUDA 运行证明 Adapter 摘要无效")
    if (
        not isinstance(python_sha256, str)
        or SHA256.fullmatch(python_sha256) is None
        or (
            expected_python_sha256 is not None
            and python_sha256 != expected_python_sha256
        )
    ):
        raise ValueError("中央 CUDA 运行证明 Python 身份无效")
    for field in (
        "torch_version",
        "cuda_runtime",
        "device_name",
    ):
        field_value = value.get(field)
        if (
            not isinstance(field_value, str)
            or not field_value
            or field_value != field_value.strip()
            or _has_control_character(field_value)
        ):
            raise ValueError(f"中央 CUDA 运行证明 {field} 无效")
    device_index = value.get("device_index")
    device_count = value.get("device_count")
    if (
        isinstance(device_index, bool)
        or not isinstance(device_index, int)
        or isinstance(device_count, bool)
        or not isinstance(device_count, int)
        or device_count < 1
        or not 0 <= device_index < device_count
    ):
        raise ValueError("中央 CUDA 运行证明设备编号无效")
    capability = value.get("compute_capability")
    if (
        not isinstance(capability, list)
        or len(capability) != 2
        or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            for item in capability
        )
    ):
        raise ValueError("中央 CUDA 运行证明计算能力无效")
    probe = value.get("probe")
    if (
        not isinstance(probe, dict)
        or set(probe) != CUDA_PROBE_FIELDS
        or probe.get("operation") != "2x2_ones_matmul_sum"
        or probe.get("result") != 8.0
        or probe.get("tensor_device") != f"cuda:{device_index}"
    ):
        raise ValueError("中央 CUDA 运行证明真算子记录无效")
    return _finite_object(value, "中央 CUDA 运行证明")


def _validate_environment(environment: dict[str, Any]) -> None:
    legacy_fields = {
        "platform",
        "python",
        "packages",
        "requirements_locks",
        "declared_environment",
    }
    current_fields = legacy_fields | {COMPARISON_POLICY_ENV_KEY}
    attested_fields = current_fields | {CUDA_ATTESTATION_ENV_KEY}
    if set(environment) not in (
        legacy_fields,
        current_fields,
        attested_fields,
    ):
        raise ValueError("绑定 Run environment 中央字段无效")
    if COMPARISON_POLICY_ENV_KEY in environment:
        comparison_policy_for_frozen({"environment": environment})
    if CUDA_ATTESTATION_ENV_KEY in environment:
        _validate_cuda_attestation(environment[CUDA_ATTESTATION_ENV_KEY])
    platform_value = environment["platform"]
    python_value = environment["python"]
    if (
        not isinstance(platform_value, dict)
        or set(platform_value) != {"system", "release", "machine"}
        or not all(isinstance(value, str) for value in platform_value.values())
    ):
        raise ValueError("绑定 Run platform 快照无效")
    if (
        not isinstance(python_value, dict)
        or set(python_value)
        != {
            "implementation",
            "version",
            "cache_tag",
            "abi_flags",
            "executable_sha256",
        }
        or not all(isinstance(value, str) and value for value in python_value.values())
        or SHA256.fullmatch(python_value["executable_sha256"]) is None
    ):
        raise ValueError("绑定 Run python 快照无效")
    _finite_object(
        environment["declared_environment"],
        "绑定 Run declared_environment",
    )
    packages = environment["packages"]
    if (
        not isinstance(packages, dict)
        or set(packages) != {"torch", "numpy", "Pillow", "torchvision"}
        or not all(
            isinstance(value, str) and value
            for value in packages.values()
        )
    ):
        raise ValueError("绑定 Run package 版本快照无效")
    locks = environment["requirements_locks"]
    if (
        not isinstance(locks, dict)
        or not set(locks).issubset(REQUIREMENTS_LOCK_CANDIDATES)
        or any(
            not isinstance(value, str) or SHA256.fullmatch(value) is None
            for value in locks.values()
        )
    ):
        raise ValueError("绑定 Run requirements lock 摘要无效")


def _finite_object(value: object, label: str) -> dict[str, Any]:
    normalized = _finite_json(value, label)
    if not isinstance(normalized, dict):
        raise ValueError(f"{label} 必须是 JSON object")
    return normalized


def _finite_json(value: object, label: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} 必须是有限 JSON 值") from error
    if normalized != value:
        raise ValueError(f"{label} 序列化后会改变")
    return normalized
