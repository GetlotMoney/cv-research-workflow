from __future__ import annotations

import ast
import hashlib
import io
import math
import struct
import zipfile
from pathlib import Path
from typing import Any

from .contract import (
    canonical_sha256,
    require_runtime,
    secure_read,
)

MAX_NPZ_FILE_BYTES = 1024 * 1024 * 1024
MAX_NPZ_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_NPZ_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_NPZ_COMPRESSION_RATIO = 200.0
MAX_NPY_HEADER_BYTES = 10_000
MAX_NPY_DIMENSIONS = 8


NPZ_KEYS = {
    "features",
    "labels",
    "attributes",
    "class_ids",
    "seen_class_ids",
    "unseen_class_ids",
    "train_indices",
    "test_seen_indices",
    "test_unseen_indices",
}


def _read_exact(stream: Any, size: int, label: str) -> bytes:
    content = stream.read(size)
    if len(content) != size:
        raise ValueError(f"{label} 被截断")
    return content


def _inspect_npy_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    numpy: Any,
) -> int:
    with archive.open(info, "r") as stream:
        prefix = _read_exact(stream, 8, "NPY header")
        if prefix[:6] != b"\x93NUMPY":
            raise ValueError(f"NPZ member 不是 NPY：{info.filename}")
        version = (prefix[6], prefix[7])
        if version == (1, 0):
            header_size = struct.unpack(
                "<H",
                _read_exact(stream, 2, "NPY v1 header length"),
            )[0]
            prefix_size = 10
            encoding = "latin1"
        elif version in {(2, 0), (3, 0)}:
            header_size = struct.unpack(
                "<I",
                _read_exact(stream, 4, "NPY v2/v3 header length"),
            )[0]
            prefix_size = 12
            encoding = "utf-8" if version == (3, 0) else "latin1"
        else:
            raise ValueError(f"不支持的 NPY 版本：{version}")
        if header_size <= 0 or header_size > MAX_NPY_HEADER_BYTES:
            raise ValueError("NPY header 体积超过上限")
        try:
            header = ast.literal_eval(
                _read_exact(stream, header_size, "NPY header").decode(
                    encoding,
                    errors="strict",
                )
            )
        except (SyntaxError, UnicodeError, ValueError) as error:
            raise ValueError("NPY header 不是安全字面量") from error
        if not isinstance(header, dict) or set(header) != {
            "descr",
            "fortran_order",
            "shape",
        }:
            raise ValueError("NPY header 字段无效")
        if not isinstance(header["fortran_order"], bool):
            raise ValueError("NPY fortran_order 必须是布尔值")
        shape = header["shape"]
        if (
            not isinstance(shape, tuple)
            or len(shape) > MAX_NPY_DIMENSIONS
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in shape
            )
        ):
            raise ValueError("NPY shape 无效或维度过多")
        try:
            dtype = numpy.dtype(header["descr"])
        except (TypeError, ValueError) as error:
            raise ValueError("NPY dtype 无效") from error
        if dtype.hasobject:
            raise ValueError("NPY object dtype/pickle 不允许")
        expanded_bytes = math.prod(shape) * int(dtype.itemsize)
        if expanded_bytes > MAX_NPZ_MEMBER_BYTES:
            raise ValueError("NPY member 展开体积超过单项上限")
        if prefix_size + header_size + expanded_bytes != info.file_size:
            raise ValueError("NPY header 声明体积与 ZIP member 不一致")
        return expanded_bytes


def inspect_npz_budget(content: bytes) -> None:
    """在 numpy.load/materialize 前检查 ZIP 与每个 NPY header。"""

    _, numpy = require_runtime()
    try:
        with zipfile.ZipFile(io.BytesIO(content), mode="r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            expected_names = {f"{name}.npy" for name in NPZ_KEYS}
            if (
                len(names) != len(set(names))
                or set(names) != expected_names
                or len(names) != len(expected_names)
            ):
                raise ValueError("NPZ member 必须唯一且精确匹配契约 keys")
            total_expanded = 0
            for info in infos:
                if (
                    info.is_dir()
                    or info.flag_bits & 0x1
                    or info.compress_type
                    not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    raise ValueError("NPZ 不允许目录、加密或未知压缩方法")
                if info.file_size > MAX_NPZ_MEMBER_BYTES:
                    raise ValueError("NPZ member 展开体积超过单项上限")
                total_expanded += info.file_size
                if total_expanded > MAX_NPZ_EXPANDED_BYTES:
                    raise ValueError("NPZ 展开总体积超过上限")
                if (
                    info.file_size > 0
                    and info.file_size / max(info.compress_size, 1)
                    > MAX_NPZ_COMPRESSION_RATIO
                ):
                    raise ValueError("NPZ member 压缩比超过上限")
                declared_array_bytes = _inspect_npy_member(
                    archive,
                    info,
                    numpy,
                )
                if declared_array_bytes > MAX_NPZ_EXPANDED_BYTES:
                    raise ValueError("NPY header 声明展开体积超过上限")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ValueError("GZSL NPZ ZIP 结构损坏") from error


def _unique_int_vector(array: Any, name: str, sample_count: int | None = None) -> None:
    _, numpy = require_runtime()
    if array.dtype != numpy.int64 or array.ndim != 1 or array.size == 0:
        raise ValueError(f"{name} 必须是非空 int64 一维数组")
    if numpy.unique(array).size != array.size:
        raise ValueError(f"{name} 必须唯一，不能重复")
    if sample_count is not None and bool(((array < 0) | (array >= sample_count)).any()):
        raise ValueError(f"{name} 含越界索引")


def load_gzsl_npz_bytes(content: bytes) -> dict[str, Any]:
    _, numpy = require_runtime()
    inspect_npz_budget(content)
    try:
        with numpy.load(io.BytesIO(content), allow_pickle=False) as archive:
            if set(archive.files) != NPZ_KEYS:
                raise ValueError(
                    f"NPZ keys 必须精确为 {sorted(NPZ_KEYS)}"
                )
            arrays = {name: archive[name].copy() for name in archive.files}
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(
            "GZSL NPZ 无法在 allow_pickle=False 下读取；"
            "object dtype/pickle/损坏文件均不允许"
        ) from error
    total_nbytes = sum(array.nbytes for array in arrays.values())
    if total_nbytes > MAX_NPZ_EXPANDED_BYTES:
        raise ValueError("NPZ 解压后的数组总体积超过上限")
    features = arrays["features"]
    labels = arrays["labels"]
    attributes = arrays["attributes"]
    class_ids = arrays["class_ids"]
    seen = arrays["seen_class_ids"]
    unseen = arrays["unseen_class_ids"]
    if (
        features.dtype != numpy.float32
        or features.ndim != 2
        or features.shape[0] == 0
        or features.shape[1] == 0
        or not bool(numpy.isfinite(features).all())
    ):
        raise ValueError("features 必须是有限非空 float32 [N,D]")
    if (
        labels.dtype != numpy.int64
        or labels.ndim != 1
        or labels.shape[0] != features.shape[0]
    ):
        raise ValueError("labels 必须是与 features 等长的 int64 一维数组")
    if (
        attributes.dtype != numpy.float32
        or attributes.ndim != 2
        or attributes.shape[0] == 0
        or attributes.shape[1] == 0
        or not bool(numpy.isfinite(attributes).all())
    ):
        raise ValueError("attributes 必须是有限非空 float32 [C,A]")
    _unique_int_vector(class_ids, "class_ids")
    _unique_int_vector(seen, "seen_class_ids")
    _unique_int_vector(unseen, "unseen_class_ids")
    if attributes.shape[0] != class_ids.size:
        raise ValueError("attributes 行数必须等于 class_ids 数量")
    class_set = set(int(value) for value in class_ids.tolist())
    seen_set = set(int(value) for value in seen.tolist())
    unseen_set = set(int(value) for value in unseen.tolist())
    if seen_set & unseen_set or seen_set | unseen_set != class_set:
        raise ValueError("seen/unseen 必须互斥并完整覆盖 class_ids")
    if not set(int(value) for value in numpy.unique(labels).tolist()).issubset(class_set):
        raise ValueError("labels 含 class_ids 外的类别")
    splits = {
        "train_indices": arrays["train_indices"],
        "test_seen_indices": arrays["test_seen_indices"],
        "test_unseen_indices": arrays["test_unseen_indices"],
    }
    for name, indices in splits.items():
        _unique_int_vector(indices, name, features.shape[0])
    train_set = set(int(value) for value in splits["train_indices"].tolist())
    seen_test_set = set(int(value) for value in splits["test_seen_indices"].tolist())
    unseen_test_set = set(int(value) for value in splits["test_unseen_indices"].tolist())
    if (
        train_set & seen_test_set
        or train_set & unseen_test_set
        or seen_test_set & unseen_test_set
        or train_set | seen_test_set | unseen_test_set
        != set(range(features.shape[0]))
    ):
        raise ValueError("train/seen-test/unseen-test 必须互斥并覆盖所有样本")
    train_labels = set(int(value) for value in labels[splits["train_indices"]].tolist())
    seen_test_labels = set(int(value) for value in labels[splits["test_seen_indices"]].tolist())
    unseen_test_labels = set(int(value) for value in labels[splits["test_unseen_indices"]].tolist())
    if not train_labels.issubset(seen_set) or train_labels != seen_set:
        raise ValueError("训练 train_indices 只能含 seen 类且每个 seen 类都要出现")
    if seen_test_labels != seen_set:
        raise ValueError("test_seen_indices 必须覆盖每个 declared seen class")
    if unseen_test_labels != unseen_set:
        raise ValueError("test_unseen_indices 必须覆盖每个 declared unseen class")
    class_to_row = {
        int(class_id): index
        for index, class_id in enumerate(class_ids.tolist())
    }
    return {
        **arrays,
        "class_to_attribute_row": class_to_row,
        "attribute_row_mapping": "class_ids[index] maps to attributes[index]",
    }


def load_gzsl_npz(path: Path) -> dict[str, Any]:
    source = Path(path)
    if source.suffix != ".npz":
        raise ValueError("GZSL 数据必须是小写 .npz")
    content = secure_read(
        source,
        "GZSL NPZ",
        max_size=MAX_NPZ_FILE_BYTES,
    )
    return load_gzsl_npz_bytes(content)


def capture_dataset(path: Path) -> tuple[dict[str, Any], bytes]:
    source = Path(path)
    if source.suffix != ".npz":
        raise ValueError("GZSL 数据必须是小写 .npz")
    content = secure_read(
        source,
        "GZSL NPZ",
        max_size=MAX_NPZ_FILE_BYTES,
    )
    # 清单和后续训练共享同一份稳定 bytes；生成身份前先完成预检与 schema 校验。
    load_gzsl_npz_bytes(content)
    body = {
        "schema": "pack-gzsl.dataset-manifest.v1",
        "files": [
            {
                "path": source.name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    return {**body, "manifest_sha256": canonical_sha256(body)}, content


def build_dataset_manifest(path: Path) -> dict[str, Any]:
    manifest, _ = capture_dataset(path)
    return manifest


def synthetic_dataset(seed: int) -> dict[str, Any]:
    _, numpy = require_runtime()
    generator = numpy.random.default_rng(seed)
    class_ids = numpy.array([10, 20, 30, 40], dtype=numpy.int64)
    attributes = numpy.array(
        [
            [1.0, 0.0, 0.1, 0.0],
            [0.0, 1.0, 0.1, 0.0],
            [0.7, 0.7, 0.2, 0.1],
            [-0.7, 0.7, 0.2, 0.1],
        ],
        dtype=numpy.float32,
    )
    labels = numpy.repeat(class_ids, 3)
    features = numpy.vstack(
        [
            attributes[class_index]
            + generator.normal(0.0, 0.01, size=4).astype(numpy.float32)
            for class_index in range(4)
            for _ in range(3)
        ]
    ).astype(numpy.float32)
    arrays = {
        "features": features,
        "labels": labels,
        "attributes": attributes,
        "class_ids": class_ids,
        "seen_class_ids": numpy.array([10, 20], dtype=numpy.int64),
        "unseen_class_ids": numpy.array([30, 40], dtype=numpy.int64),
        "train_indices": numpy.array([0, 1, 3, 4], dtype=numpy.int64),
        "test_seen_indices": numpy.array([2, 5], dtype=numpy.int64),
        "test_unseen_indices": numpy.array([6, 7, 8, 9, 10, 11], dtype=numpy.int64),
    }
    arrays["class_to_attribute_row"] = {
        10: 0,
        20: 1,
        30: 2,
        40: 3,
    }
    arrays["attribute_row_mapping"] = "class_ids[index] maps to attributes[index]"
    return arrays
