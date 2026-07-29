from __future__ import annotations

import hashlib
import io
import os
import stat
import struct
from pathlib import Path
from typing import Any

from .contract import (
    MAX_FILES,
    MAX_TOTAL_SIZE,
    canonical_sha256,
    is_link_or_reparse,
    require_directory,
    require_runtime,
    secure_read,
    validate_component,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_DIMENSION = 8192
MAX_PNG_PIXELS = 16 * 1024 * 1024
MAX_DATASET_PIXELS = 64 * 1024 * 1024
MAX_PNG_COMPRESSION_RATIO = 2048.0


def inspect_png_budget(
    content: bytes,
    *,
    label: str,
    channels: int,
) -> tuple[int, int, int]:
    """在 Pillow 解码前检查 PNG IHDR 和资源预算。"""

    if channels != 3:
        raise ValueError("SR PNG 预算只接受三通道 RGB")
    if (
        len(content) < 33
        or content[:8] != PNG_SIGNATURE
        or struct.unpack(">I", content[8:12])[0] != 13
        or content[12:16] != b"IHDR"
    ):
        raise ValueError(f"{label} 缺少有效 PNG IHDR")
    width, height, bit_depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", content[16:29])
    )
    if (
        width <= 0
        or height <= 0
        or width > MAX_PNG_DIMENSION
        or height > MAX_PNG_DIMENSION
    ):
        raise ValueError(f"{label} PNG 尺寸超过上限")
    pixels = width * height
    if pixels > MAX_PNG_PIXELS:
        raise ValueError(f"{label} PNG 单图像素超过上限")
    if (
        bit_depth != 8
        or color_type != 2
        or compression != 0
        or filtering != 0
        or interlace not in {0, 1}
    ):
        raise ValueError(f"{label} PNG IHDR 必须是 8-bit RGB")
    if pixels * channels / max(len(content), 1) > MAX_PNG_COMPRESSION_RATIO:
        raise ValueError(f"{label} PNG 解码压缩比超过上限")
    return width, height, pixels


def _scan_png(root: Path) -> list[Path]:
    directory = require_directory(root, "PNG 目录")
    files: list[Path] = []

    def visit(current: Path) -> None:
        require_directory(current, "PNG 子目录")
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda item: item.name.casefold())
        folded: set[str] = set()
        for entry in children:
            validate_component(entry.name, "数据名称")
            if entry.name.casefold() in folded:
                raise ValueError(f"数据含重复/大小写冲突名称：{entry.name}")
            folded.add(entry.name.casefold())
            path = current / entry.name
            if entry.is_symlink() or is_link_or_reparse(path):
                raise ValueError(f"数据不能含 link/junction/reparse：{path}")
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                if path.suffix != ".png":
                    raise ValueError(f"SR 数据只允许小写 .png：{path}")
                files.append(path)
                if len(files) > MAX_FILES:
                    raise ValueError("SR PNG 数量超过上限")
            else:
                raise ValueError(f"SR 数据含特殊文件：{path}")

    visit(directory)
    return sorted(files, key=lambda path: path.relative_to(directory).as_posix())


def _hr_map(root: Path) -> dict[str, Path]:
    directory = require_directory(root, "HR 目录")
    result: dict[str, Path] = {}
    folded: set[str] = set()
    for path in _scan_png(directory):
        sample_id = path.relative_to(directory).with_suffix("").as_posix()
        if sample_id.casefold() in folded:
            raise ValueError(f"HR 样本 ID 重复：{sample_id}")
        folded.add(sample_id.casefold())
        result[sample_id] = path
    if not result:
        raise ValueError("HR 目录为空")
    return result


def _lr_map(root: Path) -> dict[str, Path]:
    directory = require_directory(root, "LR/x2 目录")
    result: dict[str, Path] = {}
    folded: set[str] = set()
    for path in _scan_png(directory):
        relative = path.relative_to(directory)
        if not relative.stem.endswith("x2") or len(relative.stem) <= 2:
            raise ValueError(f"LR 文件名必须是 <id>x2.png：{relative}")
        sample_id = (
            relative.parent / relative.stem[:-2]
        ).as_posix()
        if sample_id.casefold() in folded:
            raise ValueError(f"LR 样本 ID 重复：{sample_id}")
        folded.add(sample_id.casefold())
        result[sample_id] = path
    if not result:
        raise ValueError("LR/x2 目录为空")
    return result


def _snapshot_sample_maps(
    snapshot: dict[str, bytes],
    *,
    split_root: Path,
    snapshot_root: Path,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    root = Path(os.path.abspath(os.fspath(snapshot_root)))
    split_path = Path(os.path.abspath(os.fspath(split_root)))
    try:
        relative_split = split_path.relative_to(root)
    except ValueError as error:
        raise ValueError("split 路径不在冻结数据根内") from error
    if len(relative_split.parts) != 1:
        raise ValueError("冻结分割路径必须是数据根下的单级目录")
    split = validate_component(relative_split.parts[0], "split")
    hr: dict[str, bytes] = {}
    lr: dict[str, bytes] = {}
    folded = {"HR": set(), "LR": set()}
    for relative, content in snapshot.items():
        if not isinstance(relative, str) or not isinstance(content, bytes):
            raise ValueError("snapshot 必须是相对路径到 bytes 的映射")
        parts = relative.split("/")
        is_hr = (
            len(parts) >= 3
            and parts[0] in {"train", "val"}
            and parts[1] == "HR"
        )
        is_lr = (
            len(parts) >= 4
            and parts[0] in {"train", "val"}
            and parts[1:3] == ["LR", "x2"]
        )
        if not (is_hr or is_lr) or not parts[-1].endswith(".png"):
            raise ValueError(f"snapshot 含契约外路径：{relative!r}")
        for component in parts:
            validate_component(component, "snapshot 路径分量")
        if parts[0] != split:
            continue
        if is_hr:
            sample_id = "/".join((*parts[2:-1], parts[-1][:-4]))
            target = hr
            group = "HR"
        else:
            if not parts[-1].endswith("x2.png") or len(parts[-1]) <= 6:
                raise ValueError(f"LR 文件名必须是 <id>x2.png：{parts[-1]}")
            sample_id = "/".join((*parts[3:-1], parts[-1][:-6]))
            target = lr
            group = "LR"
        key = sample_id.casefold()
        if key in folded[group]:
            raise ValueError(f"snapshot 样本 ID 重复或大小写冲突：{sample_id}")
        folded[group].add(key)
        target[sample_id] = content
    if not hr or not lr:
        raise ValueError("snapshot 分割不能为空")
    return hr, lr


def _decode_rgb(content: bytes, label: str) -> tuple[Any, Any]:
    torch, numpy, Image = require_runtime()
    inspect_png_budget(content, label=label, channels=3)
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "PNG" or image.mode != "RGB":
                raise ValueError(f"{label} 必须是真实 RGB PNG")
            image.load()
            array_uint8 = numpy.asarray(image, dtype=numpy.uint8).copy()
    except (OSError, SyntaxError) as error:
        raise ValueError(f"{label} 无法解码") from error
    tensor = torch.from_numpy(array_uint8.astype(numpy.float32) / 255.0)
    return tensor.permute(2, 0, 1).contiguous(), array_uint8


class PairedX2Dataset:
    def __init__(
        self,
        split_root: Path,
        *,
        snapshot: dict[str, bytes] | None = None,
        snapshot_root: Path | None = None,
    ) -> None:
        if snapshot is None:
            self.root = require_directory(split_root, "SR split")
            hr: dict[str, Path | bytes] = _hr_map(self.root / "HR")
            lr: dict[str, Path | bytes] = _lr_map(self.root / "LR" / "x2")
        else:
            if snapshot_root is None:
                raise ValueError("提供 snapshot 时必须同时提供 snapshot_root")
            self.root = Path(split_root)
            hr, lr = _snapshot_sample_maps(
                snapshot,
                split_root=split_root,
                snapshot_root=snapshot_root,
            )
        if set(hr) != set(lr):
            raise ValueError(
                "HR 与 LR/x2 必须严格配对；"
                f"缺 LR={sorted(set(hr)-set(lr))} 缺 HR={sorted(set(lr)-set(hr))}"
            )
        self.samples: list[tuple[str, bytes, bytes]] = []
        total_bytes = 0
        total_pixels = 0
        for sample_id in sorted(hr):
            lr_source = lr[sample_id]
            hr_source = hr[sample_id]
            lr_bytes = (
                lr_source
                if isinstance(lr_source, bytes)
                else secure_read(lr_source, "LR")
            )
            hr_bytes = (
                hr_source
                if isinstance(hr_source, bytes)
                else secure_read(hr_source, "HR")
            )
            total_bytes += len(lr_bytes) + len(hr_bytes)
            if total_bytes > MAX_TOTAL_SIZE:
                raise ValueError("SR split 文件总体积超过上限")
            total_pixels += inspect_png_budget(
                lr_bytes,
                label="LR",
                channels=3,
            )[2]
            total_pixels += inspect_png_budget(
                hr_bytes,
                label="HR",
                channels=3,
            )[2]
            if total_pixels > MAX_DATASET_PIXELS:
                raise ValueError("SR split 累计像素超过上限")
            lr_tensor, _ = _decode_rgb(lr_bytes, "LR")
            hr_tensor, _ = _decode_rgb(hr_bytes, "HR")
            if (
                hr_tensor.shape[1] != lr_tensor.shape[1] * 2
                or hr_tensor.shape[2] != lr_tensor.shape[2] * 2
            ):
                raise ValueError("HR 宽高必须精确等于 LR 的 x2 尺寸")
            self.samples.append((sample_id, lr_bytes, hr_bytes))

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[Any, Any, str]:
        sample_id, lr_bytes, hr_bytes = self.samples[index]
        lr, _ = _decode_rgb(lr_bytes, "LR")
        hr, _ = _decode_rgb(hr_bytes, "HR")
        if hr.shape[1] != lr.shape[1] * 2 or hr.shape[2] != lr.shape[2] * 2:
            raise ValueError("读取时 HR/LR x2 尺寸发生变化")
        return lr, hr, sample_id


def capture_dataset(
    data_root: Path,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    root = require_directory(data_root, "SR 数据根")
    with os.scandir(root) as entries:
        if sorted(entry.name for entry in entries) != ["train", "val"]:
            raise ValueError("SR 数据根必须精确包含 train 和 val")
    records = []
    snapshot: dict[str, bytes] = {}
    total = 0
    total_pixels = 0
    for split in ("train", "val"):
        split_root = require_directory(root / split, f"{split} split")
        with os.scandir(split_root) as entries:
            if sorted(entry.name for entry in entries) != ["HR", "LR"]:
                raise ValueError(f"{split} 必须精确包含 HR 和 LR")
        lr_root = require_directory(split_root / "LR", "LR 目录")
        with os.scandir(lr_root) as entries:
            if [entry.name for entry in entries] != ["x2"]:
                raise ValueError("LR 必须精确包含 x2")
        for directory in (split_root / "HR", split_root / "LR" / "x2"):
            for path in _scan_png(directory):
                content = secure_read(path, "SR 数据文件", root=root)
                relative = path.relative_to(root).as_posix()
                total_pixels += inspect_png_budget(
                    content,
                    label=relative,
                    channels=3,
                )[2]
                if total_pixels > MAX_DATASET_PIXELS:
                    raise ValueError("SR 数据集累计像素超过上限")
                total += len(content)
                if total > MAX_TOTAL_SIZE:
                    raise ValueError("SR 数据清单总体积超过上限")
                snapshot[relative] = content
                records.append(
                    {
                        "path": relative,
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
    body = {
        "schema": "pack-sr.dataset-manifest.v1",
        "files": sorted(records, key=lambda item: item["path"]),
    }
    if not records or len(records) > MAX_FILES:
        raise ValueError("SR 数据清单为空或过大")
    manifest = {**body, "manifest_sha256": canonical_sha256(body)}
    return manifest, dict(sorted(snapshot.items()))


def build_dataset_manifest(data_root: Path) -> dict[str, Any]:
    manifest, _ = capture_dataset(data_root)
    return manifest


def synthetic_pairs(
    *,
    sample_count: int,
    lr_height: int,
    lr_width: int,
    seed: int,
) -> tuple[list[Any], list[Any], list[str]]:
    torch, _, _ = require_runtime()
    if sample_count < 2 or lr_height < 2 or lr_width < 2:
        raise ValueError("合成 SR 参数过小")
    generator = torch.Generator(device="cpu").manual_seed(seed)
    low_resolution = []
    high_resolution = []
    names = []
    for index in range(sample_count):
        lr = torch.rand((3, lr_height, lr_width), generator=generator) * 0.7
        lr[index % 3] += 0.2
        lr = lr.clamp(0.0, 1.0)
        hr = lr.repeat_interleave(2, dim=1).repeat_interleave(2, dim=2)
        pattern = torch.zeros_like(hr)
        pattern[:, index % 2 :: 2, (index + 1) % 2 :: 2] = 0.02
        high_resolution.append((hr + pattern).clamp(0.0, 1.0))
        low_resolution.append(lr)
        names.append(f"synthetic-{index:04d}")
    return low_resolution, high_resolution, names
