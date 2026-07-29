from __future__ import annotations

import hashlib
import io
import os
import stat
import struct
from pathlib import Path
from typing import Any

from .contract import (
    MAX_DATASET_FILES,
    MAX_DATASET_SIZE,
    canonical_sha256,
    is_link_or_reparse,
    require_plain_directory,
    require_runtime,
    secure_read_bytes,
    validate_safe_component,
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
    """只读 PNG IHDR，在 Pillow 解码前限制尺寸、像素和压缩比。"""

    if channels not in {1, 3}:
        raise ValueError("PNG 预算通道数只允许 1 或 3")
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
    expected_color_types = {2} if channels == 3 else {0, 3}
    if (
        bit_depth != 8
        or color_type not in expected_color_types
        or compression != 0
        or filtering != 0
        or interlace not in {0, 1}
    ):
        raise ValueError(f"{label} PNG IHDR 颜色或编码参数不符合契约")
    decoded_bytes = pixels * channels
    if decoded_bytes / max(len(content), 1) > MAX_PNG_COMPRESSION_RATIO:
        raise ValueError(f"{label} PNG 解码压缩比超过上限")
    return width, height, pixels


def _scan_png_files(root: Path) -> list[Path]:
    directory = require_plain_directory(root, "PNG 目录")
    files: list[Path] = []

    def visit(current: Path) -> None:
        require_plain_directory(current, "PNG 子目录")
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda item: item.name.casefold())
        folded: set[str] = set()
        for entry in children:
            validate_safe_component(entry.name, "数据文件名")
            folded_name = entry.name.casefold()
            if folded_name in folded:
                raise ValueError(f"数据目录含大小写冲突/重复名称：{entry.name}")
            folded.add(folded_name)
            path = current / entry.name
            if entry.is_symlink() or is_link_or_reparse(path):
                raise ValueError(f"数据不能含 link/junction/reparse：{path}")
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                if path.suffix != ".png":
                    raise ValueError(f"语义分割数据只允许小写 .png：{path}")
                files.append(path)
                if len(files) > MAX_DATASET_FILES:
                    raise ValueError("PNG 文件数量超过上限")
            else:
                raise ValueError(f"数据含特殊文件：{path}")

    visit(directory)
    return sorted(files, key=lambda path: path.relative_to(directory).as_posix())


def _sample_map(root: Path) -> dict[str, Path]:
    directory = require_plain_directory(root, "样本目录")
    result: dict[str, Path] = {}
    folded: set[str] = set()
    for path in _scan_png_files(directory):
        relative = path.relative_to(directory)
        sample_id = relative.with_suffix("").as_posix()
        key = sample_id.casefold()
        if key in folded:
            raise ValueError(f"样本 ID 重复或大小写冲突：{sample_id}")
        folded.add(key)
        result[sample_id] = path
    if not result:
        raise ValueError(f"样本目录没有 PNG：{directory}")
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
    split = validate_safe_component(relative_split.parts[0], "split")
    images: dict[str, bytes] = {}
    masks: dict[str, bytes] = {}
    folded = {"images": set(), "masks": set()}
    for relative, content in snapshot.items():
        if not isinstance(relative, str) or not isinstance(content, bytes):
            raise ValueError("snapshot 必须是相对路径到 bytes 的映射")
        parts = relative.split("/")
        if (
            len(parts) < 3
            or parts[0] not in {"train", "val"}
            or parts[1] not in {"images", "masks"}
            or not parts[-1].endswith(".png")
        ):
            raise ValueError(f"snapshot 含契约外路径：{relative!r}")
        for component in parts:
            validate_safe_component(component, "snapshot 路径分量")
        if parts[0] != split:
            continue
        sample_id = "/".join((*parts[2:-1], parts[-1][:-4]))
        key = sample_id.casefold()
        if key in folded[parts[1]]:
            raise ValueError(f"snapshot 样本 ID 重复或大小写冲突：{sample_id}")
        folded[parts[1]].add(key)
        target = images if parts[1] == "images" else masks
        target[sample_id] = content
    if not images or not masks:
        raise ValueError("snapshot 分割不能为空")
    return images, masks


def _decode_pair(
    image_bytes: bytes,
    mask_bytes: bytes,
    *,
    image_size: int,
    num_classes: int,
) -> tuple[Any, Any]:
    torch, numpy, Image = require_runtime()
    inspect_png_budget(image_bytes, label="RGB 图片", channels=3)
    inspect_png_budget(mask_bytes, label="分割掩码", channels=1)
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.format != "PNG" or image.mode != "RGB":
                raise ValueError("输入图片必须是真实 RGB PNG，不能隐式转换")
            image.load()
            image_dimensions = image.size
            resized_image = image.resize(
                (image_size, image_size),
                Image.Resampling.BILINEAR,
            )
            image_array = numpy.asarray(resized_image, dtype=numpy.float32).copy() / 255.0
        with Image.open(io.BytesIO(mask_bytes)) as mask:
            if mask.format != "PNG" or mask.mode not in {"P", "L"}:
                raise ValueError("分割 mask 只能是 P/L 模式 PNG")
            mask.load()
            if mask.size != image_dimensions:
                raise ValueError("RGB 图片与 mask 原始尺寸必须完全相同")
            mask_array_original = numpy.asarray(mask, dtype=numpy.uint8).copy()
            invalid = (mask_array_original != 255) & (
                mask_array_original >= num_classes
            )
            if bool(invalid.any()):
                raise ValueError("mask 含 0..C-1 和 ignore=255 之外的非法类别 ID")
            resized_mask = mask.resize(
                (image_size, image_size),
                Image.Resampling.NEAREST,
            )
            mask_array = numpy.asarray(resized_mask, dtype=numpy.int64).copy()
    except (OSError, SyntaxError) as error:
        raise ValueError("无法解码 RGB/mask PNG") from error
    return (
        torch.from_numpy(image_array).permute(2, 0, 1).contiguous(),
        torch.from_numpy(mask_array).to(dtype=torch.long),
    )


class SegmentationDataset:
    def __init__(
        self,
        split_root: Path,
        image_size: int,
        num_classes: int,
        *,
        snapshot: dict[str, bytes] | None = None,
        snapshot_root: Path | None = None,
    ) -> None:
        if isinstance(image_size, bool) or not isinstance(image_size, int) or image_size < 4:
            raise ValueError("image_size 必须是不小于 4 的整数")
        if isinstance(num_classes, bool) or not isinstance(num_classes, int) or num_classes < 2:
            raise ValueError("num_classes 必须是不小于 2 的整数")
        self.image_size = image_size
        self.num_classes = num_classes
        if snapshot is None:
            self.root = require_plain_directory(split_root, "分割 split")
            image_map: dict[str, Path | bytes] = _sample_map(self.root / "images")
            mask_map: dict[str, Path | bytes] = _sample_map(self.root / "masks")
        else:
            if snapshot_root is None:
                raise ValueError("提供 snapshot 时必须同时提供 snapshot_root")
            self.root = Path(split_root)
            image_map, mask_map = _snapshot_sample_maps(
                snapshot,
                split_root=split_root,
                snapshot_root=snapshot_root,
            )
        if set(image_map) != set(mask_map):
            missing_masks = sorted(set(image_map) - set(mask_map))
            missing_images = sorted(set(mask_map) - set(image_map))
            raise ValueError(
                "images/masks 必须一一配对；"
                f"缺 mask={missing_masks} 缺 image={missing_images}"
            )
        self.samples: list[tuple[str, bytes, bytes]] = []
        valid_pixels = 0
        total_bytes = 0
        total_pixels = 0
        for sample_id in sorted(image_map):
            image_source = image_map[sample_id]
            mask_source = mask_map[sample_id]
            image_bytes = (
                image_source
                if isinstance(image_source, bytes)
                else secure_read_bytes(image_source, "RGB 图片")
            )
            mask_bytes = (
                mask_source
                if isinstance(mask_source, bytes)
                else secure_read_bytes(mask_source, "分割掩码")
            )
            total_bytes += len(image_bytes) + len(mask_bytes)
            if total_bytes > MAX_DATASET_SIZE:
                raise ValueError("分割 split 文件总体积超过上限")
            total_pixels += inspect_png_budget(
                image_bytes,
                label="RGB 图片",
                channels=3,
            )[2]
            total_pixels += inspect_png_budget(
                mask_bytes,
                label="分割掩码",
                channels=1,
            )[2]
            if total_pixels > MAX_DATASET_PIXELS:
                raise ValueError("分割 split 累计像素超过上限")
            _, mask = _decode_pair(
                image_bytes,
                mask_bytes,
                image_size=image_size,
                num_classes=num_classes,
            )
            valid_pixels += int((mask != 255).sum().item())
            self.samples.append((sample_id, image_bytes, mask_bytes))
        if valid_pixels == 0:
            raise ValueError("整个 split 的 mask 全部是 ignore=255")

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[Any, Any, str]:
        sample_id, image_bytes, mask_bytes = self.samples[index]
        image, mask = _decode_pair(
            image_bytes,
            mask_bytes,
            image_size=self.image_size,
            num_classes=self.num_classes,
        )
        return image, mask, sample_id


def capture_dataset(
    data_root: Path,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    root = require_plain_directory(data_root, "分割数据根")
    with os.scandir(root) as entries:
        names = sorted(entry.name for entry in entries)
    if names != ["train", "val"]:
        raise ValueError("分割数据根必须精确包含 train 和 val")
    files: list[dict[str, Any]] = []
    snapshot: dict[str, bytes] = {}
    total_size = 0
    total_pixels = 0
    for split in ("train", "val"):
        split_root = require_plain_directory(root / split, f"{split} split")
        with os.scandir(split_root) as entries:
            split_names = sorted(entry.name for entry in entries)
        if split_names != ["images", "masks"]:
            raise ValueError(f"{split} 必须精确包含 images 和 masks")
        for leaf in ("images", "masks"):
            directory = split_root / leaf
            for path in _scan_png_files(directory):
                content = secure_read_bytes(path, "数据文件", root=root)
                relative = path.relative_to(root).as_posix()
                total_pixels += inspect_png_budget(
                    content,
                    label=f"{split}/{leaf}",
                    channels=3 if leaf == "images" else 1,
                )[2]
                if total_pixels > MAX_DATASET_PIXELS:
                    raise ValueError("分割数据集累计像素超过上限")
                total_size += len(content)
                if total_size > MAX_DATASET_SIZE:
                    raise ValueError("数据清单总体积超过上限")
                snapshot[relative] = content
                files.append(
                    {
                        "path": relative,
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
    if not files or len(files) > MAX_DATASET_FILES:
        raise ValueError("数据清单文件数量无效")
    body = {
        "schema": "pack-seg.dataset-manifest.v1",
        "files": sorted(files, key=lambda item: item["path"]),
    }
    manifest = {**body, "manifest_sha256": canonical_sha256(body)}
    return manifest, dict(sorted(snapshot.items()))


def build_dataset_manifest(data_root: Path) -> dict[str, Any]:
    manifest, _ = capture_dataset(data_root)
    return manifest


def synthetic_tensors(
    *,
    sample_count: int,
    image_size: int,
    num_classes: int,
    seed: int,
) -> tuple[Any, Any, list[str]]:
    torch, _, _ = require_runtime()
    if sample_count < 2 or image_size < 4 or num_classes < 2:
        raise ValueError("合成分割数据参数过小")
    generator = torch.Generator(device="cpu").manual_seed(seed)
    images = []
    masks = []
    names = []
    for index in range(sample_count):
        rows = torch.arange(image_size).view(-1, 1).expand(image_size, image_size)
        columns = torch.arange(image_size).view(1, -1).expand(image_size, image_size)
        mask = ((rows // 3 + columns // 3 + index) % num_classes).long()
        image = torch.zeros((3, image_size, image_size), dtype=torch.float32)
        for class_index in range(num_classes):
            selected = mask == class_index
            image[class_index % 3][selected] = (class_index + 1) / (num_classes + 1)
        image = (
            image
            + torch.rand((3, image_size, image_size), generator=generator) * 0.01
        ).clamp(0.0, 1.0)
        images.append(image)
        masks.append(mask)
        names.append(f"synthetic-{index:04d}")
    return torch.stack(images), torch.stack(masks), names
