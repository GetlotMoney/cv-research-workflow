from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .contract import (
    is_link_or_reparse,
    require_plain_directory,
    require_plain_file,
    require_runtime,
)


IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


class ImageFolderDataset:
    def __init__(
        self,
        root: Path,
        image_size: int,
        class_names: list[str] | None = None,
    ) -> None:
        torch, _, _ = require_runtime()
        self._torch = torch
        self.root = require_plain_directory(
            Path(root),
            "ImageFolder 目录",
        ).resolve()
        if isinstance(image_size, bool) or not isinstance(image_size, int) or image_size <= 0:
            raise ValueError("image_size 必须是正整数")
        self.image_size = image_size
        detected = []
        with os.scandir(self.root) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = self.root / entry.name
                if entry.is_symlink() or is_link_or_reparse(path):
                    raise ValueError(
                        f"ImageFolder 含 link/reparse，拒绝读取：{path}"
                    )
                metadata = path.lstat()
                if stat.S_ISDIR(metadata.st_mode):
                    detected.append(entry.name)
        selected = detected if class_names is None else list(class_names)
        if not selected or len(set(selected)) != len(selected):
            raise ValueError("ImageFolder 至少要有一个不重复的类别子目录")
        if class_names is not None and detected != selected:
            raise ValueError(
                "数据类别与 checkpoint 不一致；"
                f"期望={selected} 实际={detected}"
            )
        self.class_names = selected
        self.samples: list[tuple[Path, int]] = []
        for class_index, class_name in enumerate(self.class_names):
            directory = self.root / class_name
            files = _scan_image_files(directory)
            if not files:
                raise ValueError(f"类别目录没有可读图片：{directory}")
            self.samples.extend((path, class_index) for path in files)

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[Any, int, str]:
        path, target = self.samples[index]
        return (
            load_image_tensor(path, self.image_size),
            target,
            path.relative_to(self.root).as_posix(),
        )


def load_image_tensor(path: Path, image_size: int) -> Any:
    torch, numpy, Image = require_runtime()
    source = require_plain_file(Path(path), "图片")
    try:
        with Image.open(source) as image:
            rgb = image.convert("RGB").resize(
                (image_size, image_size),
                Image.Resampling.BILINEAR,
            )
            array = numpy.asarray(rgb, dtype=numpy.float32).copy() / 255.0
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取图片：{source}") from error
    return torch.from_numpy(array).permute(2, 0, 1).contiguous()


def _scan_image_files(root: Path) -> list[Path]:
    directory = require_plain_directory(root, "类别目录")
    files: list[Path] = []

    def visit(current: Path) -> None:
        require_plain_directory(current, "图片子目录")
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda item: item.name)
        for entry in children:
            path = current / entry.name
            if entry.is_symlink() or is_link_or_reparse(path):
                raise ValueError(
                    f"ImageFolder 含 link/reparse，拒绝读取：{path}"
                )
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                if path.suffix.lower() in IMAGE_EXTENSIONS:
                    files.append(path)
            else:
                raise ValueError(f"ImageFolder 含特殊文件，拒绝读取：{path}")

    visit(directory)
    return sorted(files)


def synthetic_tensors(
    *,
    num_classes: int,
    samples_per_class: int,
    image_size: int,
    seed: int,
) -> tuple[Any, Any, list[str]]:
    torch, _, _ = require_runtime()
    if num_classes < 2 or samples_per_class < 2 or image_size < 4:
        raise ValueError("合成数据至少需要 2 类、每类 2 张、边长 4")
    generator = torch.Generator(device="cpu").manual_seed(seed)
    images = []
    targets = []
    for class_index in range(num_classes):
        level = (class_index + 1) / (num_classes + 1)
        for sample_index in range(samples_per_class):
            image = torch.full((3, image_size, image_size), level)
            image[class_index % 3, :, :] = min(1.0, level + 0.25)
            stripe = (class_index * 2 + sample_index) % image_size
            image[:, stripe : stripe + 1, :] = min(1.0, level + 0.15)
            noise = torch.rand(
                (3, image_size, image_size),
                generator=generator,
            ) * 0.01
            images.append((image + noise).clamp(0.0, 1.0))
            targets.append(class_index)
    return (
        torch.stack(images),
        torch.tensor(targets, dtype=torch.long),
        [f"class_{index}" for index in range(num_classes)],
    )
