from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import math
import os as _os
import random
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


CONFIG_SCHEMA = "pack-instseg.config.v1"
DATASET_SCHEMA = "cv-experiment-workflow.dataset-identity.v1"
RUN_SCHEMA = "pack-instseg.run.v1"
EVALUATION_SCHEMA = "pack-instseg.evaluation.v1"
PREDICTIONS_SCHEMA = "pack-instseg.predictions.v1"
CHECKPOINT_SCHEMA = "pack-instseg.checkpoint.v1"
DATA_MANIFEST_SCHEMA = "pack-instseg.data-manifest.v1"
DEBUG_METRICS = (
    "debug_mask_mean_iou",
    "debug_precision_at_mask_iou_0_5",
)
FORMAL_METRICS = ("coco_segm_ap", "coco_segm_ap50", "coco_segm_ap75")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
REPARSE_POINT = 0x400
MAX_IMAGES = 128
MAX_ANNOTATIONS = 4096
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DATASET_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_TOTAL_PIXELS = 64 * 1024 * 1024
MAX_INSTANCES_PER_IMAGE = 256
MAX_TOTAL_MASK_PIXELS = 64 * 1024 * 1024
MAX_MODEL_CLASSES = 256
MAX_MODEL_QUERIES = 256
MAX_MODEL_IMAGE_SIZE = 256
MAX_EPOCHS = 100
MAX_TRAINING_PIXELS = 16 * 1024 * 1024
MAX_MASK_HEAD_VALUES = 128 * 1024
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def require_runtime() -> tuple[Any, Any, Any, Any]:
    try:
        import numpy
        import torch
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError(
            "实例分割模板缺少 torch、numpy 或 Pillow；请按 "
            "requirements/windows-cpu.lock.txt 安装。"
        ) from error
    return torch, numpy, Image, ImageDraw


def require_pycocotools() -> tuple[Any, Any, Any]:
    try:
        version = importlib.metadata.version("pycocotools")
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError(
            "OPTIONAL_NOT_INSTALLED：正式 COCO segm 评估要求精确 "
            "pycocotools==2.0.11；不会使用近似 AP fallback。"
        ) from error
    if version != "2.0.11":
        raise RuntimeError(
            "OPTIONAL_NOT_INSTALLED：检测到 pycocotools=="
            f"{version}，正式评估只接受精确 2.0.11。"
        )
    try:
        from pycocotools import mask as mask_utils
        from pycocotools.coco import COCO
        from pycocotools.cocoeval import COCOeval
    except ImportError as error:
        raise RuntimeError(
            "OPTIONAL_NOT_INSTALLED：pycocotools==2.0.11 模块不完整。"
        ) from error
    return COCO, COCOeval, mask_utils


def require_device(device: object, *, synthetic: bool = False) -> Any:
    if device not in {"cpu", "cuda"}:
        raise ValueError("device 只允许 cpu 或 cuda")
    if synthetic and device != "cpu":
        raise ValueError("synthetic_debug 严格只允许 CPU")
    torch, _, _, _ = require_runtime()
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("请求了 CUDA，但当前 PyTorch/CUDA 运行环境不可用")
    return torch.device(str(device))


def set_deterministic(
    seed: int,
    device: str = "cpu",
    *,
    synthetic: bool = False,
) -> Any:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须是非负整数")
    torch, numpy, _, _ = require_runtime()
    require_device(device, synthetic=synthetic)
    random.seed(seed)
    numpy.random.seed(seed)
    torch.manual_seed(seed)
    if device == "cuda":
        torch.cuda.manual_seed_all(seed)
        if hasattr(torch.backends, "cudnn"):
            torch.backends.cudnn.benchmark = False
            torch.backends.cudnn.deterministic = True
    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(1)
    return torch


def canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def is_link_or_reparse(path: Path) -> bool:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError:
        return True
    return candidate.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT
    )


def _plain_existing(path: Path, label: str) -> Path:
    candidate = Path(_os.path.abspath(_os.fspath(path)))
    if not _os.path.lexists(candidate):
        raise ValueError(f"{label} 不存在")
    for current in (candidate, *candidate.parents):
        if _os.path.lexists(current) and is_link_or_reparse(current):
            raise ValueError(f"{label} 及其父路径不能包含 link/reparse")
    return candidate


def require_plain_file(path: Path, label: str) -> Path:
    candidate = _plain_existing(path, label)
    metadata = candidate.lstat()
    if not stat.S_ISREG(metadata.st_mode) or is_link_or_reparse(candidate):
        raise ValueError(f"{label} 必须是普通文件，不能是 link/reparse")
    return candidate


def require_plain_directory(path: Path, label: str) -> Path:
    candidate = _plain_existing(path, label)
    metadata = candidate.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or is_link_or_reparse(candidate):
        raise ValueError(f"{label} 必须是普通目录，不能是 link/reparse")
    return candidate


def _identity(metadata: _os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9))),
        int(getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9))),
    )


def _stable_identity(metadata: _os.stat_result) -> tuple[int, int, int, int]:
    return _identity(metadata)[:4]


def stable_read_bytes(path: Path, label: str, maximum: int) -> bytes:
    source = require_plain_file(path, label)
    before = source.lstat()
    if before.st_size > maximum:
        raise ValueError(f"{label} 超过大小上限")
    flags = _os.O_RDONLY | getattr(_os, "O_BINARY", 0)
    if hasattr(_os, "O_NOFOLLOW"):
        flags |= _os.O_NOFOLLOW
    try:
        descriptor = _os.open(source, flags)
    except OSError as error:
        raise ValueError(f"无法安全打开{label}") from error
    try:
        opened = _os.fstat(descriptor)
        if (
            _stable_identity(opened) != _stable_identity(before)
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise ValueError(f"{label} 在打开时被替换")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = _os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"{label} 超过大小上限")
        after_descriptor = _os.fstat(descriptor)
    finally:
        _os.close(descriptor)
    try:
        after_path = source.lstat()
    except OSError as error:
        raise ValueError(f"{label} 在读取期间被移除或替换") from error
    if (
        _stable_identity(before) != _stable_identity(opened)
        or _identity(opened) != _identity(after_descriptor)
        or _identity(before) != _identity(after_path)
        or _stable_identity(after_descriptor) != _stable_identity(after_path)
        or is_link_or_reparse(source)
    ):
        raise ValueError(f"{label} 在读取期间发生变化或被替换")
    return b"".join(chunks)


def safe_dataset_text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise ValueError(f"{label} 必须是规范非空文本")
    return value


def safe_source_uri(value: object) -> str:
    uri = safe_dataset_text(value, "source_uri")
    if "\\" in uri or re.match(r"^[A-Za-z]:", uri) or uri.startswith("//"):
        raise ValueError("source_uri 必须是真实 URI，不能是本机路径")
    parsed = urlsplit(uri)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("source_uri 必须是 http/https 公开 URI")
    return uri


def safe_relative_file(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("COCO file_name 必须是字符串")
    normalized = unicodedata.normalize("NFC", value)
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or normalized != value
        or normalized != normalized.strip()
        or "\\" in normalized
        or ":" in normalized
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(
            unicodedata.category(character) == "Cc"
            or character in '<>"|?*'
            for character in normalized
        )
    ):
        raise ValueError("COCO file_name 必须是安全规范相对路径")
    for part in pure.parts:
        if (
            part.endswith((" ", "."))
            or part.split(".", 1)[0].casefold() in _RESERVED
        ):
            raise ValueError("COCO file_name 含 Windows 保留名")
    return normalized


def safe_output_relative(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("artifact path 必须是字符串")
    normalized = unicodedata.normalize("NFC", value)
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or normalized != value
        or normalized != normalized.strip()
        or "\\" in normalized
        or ":" in normalized
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(
            unicodedata.category(character).startswith("C")
            or character in '<>"|?*'
            for character in normalized
        )
    ):
        raise ValueError("artifact path 必须是规范安全相对路径")
    for part in pure.parts:
        if (
            part.endswith((" ", "."))
            or part.split(".", 1)[0].casefold() in _RESERVED
        ):
            raise ValueError("artifact path 含 Windows 保留名")
    return normalized


def _enumerate_output_tree(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    total_bytes = 0

    def visit(current: Path) -> None:
        nonlocal total_bytes
        try:
            with _os.scandir(current) as entries:
                children = sorted(entries, key=lambda item: item.name.casefold())
        except OSError as error:
            raise ValueError("Run 输出目录无法安全枚举") from error
        folded: set[str] = set()
        for entry in children:
            if entry.name.casefold() in folded:
                raise ValueError("Run 输出含 Windows 大小写冲突名称")
            folded.add(entry.name.casefold())
            path = current / entry.name
            if entry.is_symlink() or is_link_or_reparse(path):
                raise ValueError("Run 输出不能含 link/reparse")
            try:
                metadata = path.lstat()
            except OSError as error:
                raise ValueError("Run 输出枚举期间发生变化") from error
            relative = safe_output_relative(path.relative_to(root).as_posix())
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(relative)
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.add(relative)
                total_bytes += metadata.st_size
                if total_bytes > MAX_OUTPUT_BYTES:
                    raise ValueError("Run 输出总字节数超过 64 MiB 上限")
            else:
                raise ValueError("Run 输出只能包含普通文件和普通目录")

    visit(root)
    return files, directories


def _finite_number(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} 必须是有限数字")
    return float(value)


def _polygon_area(points: list[float]) -> float:
    area = 0.0
    count = len(points) // 2
    for index in range(count):
        x1, y1 = points[2 * index], points[2 * index + 1]
        x2 = points[2 * ((index + 1) % count)]
        y2 = points[2 * ((index + 1) % count) + 1]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _polygon_mask(
    segmentation: list[list[float]],
    width: int,
    height: int,
) -> Any:
    torch, numpy, Image, ImageDraw = require_runtime()
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    for polygon in segmentation:
        points = [
            (polygon[index], polygon[index + 1])
            for index in range(0, len(polygon), 2)
        ]
        draw.polygon(points, fill=255)
    array = numpy.asarray(image, dtype=numpy.uint8).copy()
    return torch.from_numpy(array > 0)


def load_coco_dataset(data_root: Path, annotation_file: Path) -> dict[str, Any]:
    root = require_plain_directory(data_root, "COCO 图片目录")
    annotation = require_plain_file(annotation_file, "COCO polygon 标注")
    annotation_bytes = stable_read_bytes(
        annotation,
        "COCO polygon 标注",
        MAX_FILE_BYTES,
    )
    try:
        payload = json.loads(
            annotation_bytes.decode("utf-8"),
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"COCO 标注必须是严格 JSON：{error}") from error
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("images"), list)
        or not isinstance(payload.get("categories"), list)
        or not isinstance(payload.get("annotations"), list)
        or not 1 <= len(payload["images"]) <= MAX_IMAGES
        or not 1 <= len(payload["categories"]) <= MAX_MODEL_CLASSES
        or not 1 <= len(payload["annotations"]) <= MAX_ANNOTATIONS
    ):
        raise ValueError("COCO 必须包含非空 images/categories/annotations")
    categories: dict[int, str] = {}
    for item in payload["categories"]:
        if (
            not isinstance(item, dict)
            or not {"id", "name"}.issubset(item)
            or isinstance(item["id"], bool)
            or not isinstance(item["id"], int)
            or item["id"] <= 0
            or item["id"] in categories
        ):
            raise ValueError("COCO category id/name 无效或重复")
        categories[item["id"]] = safe_dataset_text(item["name"], "category name")
    if not categories:
        raise ValueError("COCO categories 不能为空")

    images: dict[int, dict[str, Any]] = {}
    names: set[str] = set()
    manifest = [{
        "path": f"annotations/{annotation.name}",
        "size": len(annotation_bytes),
        "sha256": f"sha256:{hashlib.sha256(annotation_bytes).hexdigest()}",
    }]
    total = len(annotation_bytes)
    total_pixels = 0
    for item in payload["images"]:
        if (
            not isinstance(item, dict)
            or not {"id", "file_name", "width", "height"}.issubset(item)
            or isinstance(item["id"], bool)
            or not isinstance(item["id"], int)
            or item["id"] <= 0
            or item["id"] in images
        ):
            raise ValueError("COCO image id 无效或重复")
        width = item["width"]
        height = item["height"]
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or not 1 <= width <= MAX_IMAGE_DIMENSION
            or isinstance(height, bool)
            or not isinstance(height, int)
            or not 1 <= height <= MAX_IMAGE_DIMENSION
        ):
            raise ValueError("COCO image width/height 无效")
        total_pixels += width * height
        if total_pixels > MAX_TOTAL_PIXELS:
            raise ValueError("COCO 图片总像素数超过模板安全上限")
        relative = safe_relative_file(item["file_name"])
        if relative.casefold() in names:
            raise ValueError("COCO file_name 在 Windows 上重复")
        names.add(relative.casefold())
        path = root.joinpath(*PurePosixPath(relative).parts)
        content = stable_read_bytes(path, "COCO RGB 图片", MAX_FILE_BYTES)
        total += len(content)
        if total > MAX_DATASET_BYTES:
            raise ValueError("COCO 数据超过模板安全体积上限")
        torch, numpy, Image, _ = require_runtime()
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.mode != "RGB":
                    raise ValueError("COCO 图片必须原生为 RGB")
                if image.size != (width, height):
                    raise ValueError("COCO 图片尺寸与标注不一致")
                image.load()
                array = numpy.asarray(image, dtype=numpy.float32).copy() / 255.0
        except (OSError, ValueError) as error:
            raise ValueError(f"无法读取严格 RGB 图片：{relative}；{error}") from error
        images[item["id"]] = {
            "id": item["id"],
            "file_name": relative,
            "width": width,
            "height": height,
            "content": content,
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            "tensor": torch.from_numpy(array).permute(2, 0, 1).contiguous(),
            "annotations": [],
        }
        manifest.append({
            "path": f"images/{relative}",
            "size": len(content),
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        })

    annotations: list[dict[str, Any]] = []
    identifiers: set[int] = set()
    total_mask_pixels = 0
    for item in payload["annotations"]:
        required = {
            "id",
            "image_id",
            "category_id",
            "bbox",
            "area",
            "iscrowd",
            "segmentation",
        }
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("COCO polygon annotation 字段必须严格")
        identifier = item["id"]
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or identifier <= 0
            or identifier in identifiers
        ):
            raise ValueError("COCO annotation id 无效或重复")
        identifiers.add(identifier)
        if (
            type(item["image_id"]) is not int
            or type(item["category_id"]) is not int
            or item["image_id"] not in images
            or item["category_id"] not in categories
        ):
            raise ValueError("COCO annotation 关联未知 image/category")
        if type(item["iscrowd"]) is not int or item["iscrowd"] != 0:
            raise ValueError("PACK-INSTSEG V1 的 iscrowd 必须是整数 0")
        if not isinstance(item["segmentation"], list) or not item["segmentation"]:
            raise ValueError("V1 segmentation 只接受非空 polygon 列表，拒绝 RLE")
        image = images[item["image_id"]]
        if len(image["annotations"]) >= MAX_INSTANCES_PER_IMAGE:
            raise ValueError("单张 COCO 图片的实例 annotation 数量超过上限")
        total_mask_pixels += image["width"] * image["height"]
        if total_mask_pixels > MAX_TOTAL_MASK_PIXELS:
            raise ValueError("COCO 实例掩码总像素数超过模板安全上限")
        segmentation = []
        total_polygon_area = 0.0
        all_x: list[float] = []
        all_y: list[float] = []
        for raw_polygon in item["segmentation"]:
            if (
                not isinstance(raw_polygon, list)
                or len(raw_polygon) < 6
                or len(raw_polygon) % 2
            ):
                raise ValueError("polygon 至少三个点且坐标数量必须为偶数")
            polygon = [
                _finite_number(value, "polygon 坐标")
                for value in raw_polygon
            ]
            xs = polygon[0::2]
            ys = polygon[1::2]
            if (
                any(value < 0.0 or value > image["width"] for value in xs)
                or any(value < 0.0 or value > image["height"] for value in ys)
            ):
                raise ValueError("polygon 坐标越界")
            area = _polygon_area(polygon)
            if area <= 1e-6:
                raise ValueError("polygon 退化或面积为零")
            total_polygon_area += area
            all_x.extend(xs)
            all_y.extend(ys)
            segmentation.append(polygon)
        bbox = item["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("polygon bbox 必须有四个数字")
        x, y, width, height = [
            _finite_number(value, "bbox")
            for value in bbox
        ]
        if (
            x < 0.0
            or y < 0.0
            or width <= 0.0
            or height <= 0.0
            or x + width > image["width"] + 1e-6
            or y + height > image["height"] + 1e-6
        ):
            raise ValueError("polygon bbox 退化或越界")
        expected_bbox = [
            min(all_x),
            min(all_y),
            max(all_x) - min(all_x),
            max(all_y) - min(all_y),
        ]
        if any(
            not math.isclose(actual, expected, abs_tol=1e-5, rel_tol=1e-5)
            for actual, expected in zip([x, y, width, height], expected_bbox)
        ):
            raise ValueError("polygon bbox 与多边形边界不一致")
        area = _finite_number(item["area"], "polygon area")
        if area <= 0.0 or not math.isclose(
            area,
            total_polygon_area,
            rel_tol=1e-5,
            abs_tol=1e-5,
        ):
            raise ValueError("polygon area 与多边形面积不一致")
        normalized = {
            "id": identifier,
            "image_id": item["image_id"],
            "category_id": item["category_id"],
            "bbox": [x, y, width, height],
            "area": area,
            "iscrowd": 0,
            "segmentation": segmentation,
            "mask": _polygon_mask(segmentation, image["width"], image["height"]),
        }
        annotations.append(normalized)
        image["annotations"].append(normalized)
    if any(not image["annotations"] for image in images.values()):
        raise ValueError("每张 COCO 图片至少需要一个 polygon annotation")
    return {
        "root": root,
        "annotation_file": annotation,
        "annotation_bytes": annotation_bytes,
        "images": [images[key] for key in sorted(images)],
        "categories": categories,
        "annotations": sorted(annotations, key=lambda item: item["id"]),
        "manifest_entries": sorted(manifest, key=lambda item: item["path"]),
    }


def dataset_identity(
    *,
    data_root: Path,
    annotation_file: Path,
    dataset_id: object,
    version: object,
    source_uri: object,
    split: object,
) -> dict[str, str]:
    dataset = load_coco_dataset(data_root, annotation_file)
    return {
        "schema": DATASET_SCHEMA,
        "dataset_id": safe_dataset_text(dataset_id, "dataset_id"),
        "version": safe_dataset_text(version, "version"),
        "source_uri": safe_source_uri(source_uri),
        "manifest_sha256": canonical_sha256(_data_manifest(dataset)),
        "split": safe_dataset_text(split, "split"),
    }


def load_config(path: Path) -> dict[str, Any]:
    content = stable_read_bytes(path, "配置", 256 * 1024)
    try:
        payload = json.loads(
            content.decode("utf-8"),
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"配置必须是严格 JSON：{error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != CONFIG_SCHEMA:
        raise ValueError(f"配置 schema 必须是 {CONFIG_SCHEMA}")
    return payload


def positive_int(
    payload: dict[str, Any],
    key: str,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    value = payload.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        suffix = "" if maximum is None else f"、至多 {maximum}"
        raise ValueError(f"{key} 必须是至少 {minimum}{suffix} 的整数")
    return value


def positive_float(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f"{key} 必须是有限正数")
    return float(value)


def _validated_model_config(config: dict[str, Any]) -> tuple[int, int, int]:
    num_classes = positive_int(
        config,
        "num_classes",
        maximum=MAX_MODEL_CLASSES,
    )
    num_queries = positive_int(
        config,
        "num_queries",
        maximum=MAX_MODEL_QUERIES,
    )
    image_size = positive_int(
        config,
        "image_size",
        8,
        MAX_MODEL_IMAGE_SIZE,
    )
    if num_queries * image_size * image_size > MAX_MASK_HEAD_VALUES:
        raise ValueError("model_config 查询数与 mask 像素乘积超过安全上限")
    return num_classes, num_queries, image_size


def _validate_training_shape(sample_count: int, image_size: int) -> None:
    if sample_count * image_size * image_size > MAX_TRAINING_PIXELS:
        raise ValueError("训练样本数与 image_size 的总像素超过安全上限")


def build_model(num_classes: int, num_queries: int, image_size: int) -> Any:
    num_classes, num_queries, image_size = _validated_model_config({
        "num_classes": num_classes,
        "num_queries": num_queries,
        "image_size": image_size,
    })
    torch, _, _, _ = require_runtime()

    class TinyFixedQueryInstanceSegmenter(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.backbone = torch.nn.Sequential(
                torch.nn.Conv2d(3, 8, 3, padding=1),
                torch.nn.ReLU(),
                torch.nn.Conv2d(8, 12, 3, padding=1),
                torch.nn.ReLU(),
                torch.nn.AdaptiveAvgPool2d((1, 1)),
            )
            self.class_head = torch.nn.Linear(
                12,
                num_queries * (num_classes + 1),
            )
            self.mask_head = torch.nn.Linear(
                12,
                num_queries * image_size * image_size,
            )

        def forward(self, images: Any) -> dict[str, Any]:
            features = self.backbone(images).flatten(1)
            return {
                "logits": self.class_head(features).view(
                    images.shape[0],
                    num_queries,
                    num_classes + 1,
                ),
                "mask_logits": self.mask_head(features).view(
                    images.shape[0],
                    num_queries,
                    image_size,
                    image_size,
                ),
            }

    return TinyFixedQueryInstanceSegmenter()


def synthetic_dataset(
    seed: int,
    sample_count: int,
    image_size: int,
    num_classes: int,
) -> dict[str, Any]:
    sample_count = positive_int(
        {"sample_count": sample_count},
        "sample_count",
        2,
        MAX_IMAGES,
    )
    num_classes = positive_int(
        {"num_classes": num_classes},
        "num_classes",
        maximum=MAX_MODEL_CLASSES,
    )
    image_size = positive_int(
        {"image_size": image_size},
        "image_size",
        8,
        MAX_MODEL_IMAGE_SIZE,
    )
    _validate_training_shape(sample_count, image_size)
    torch, _, _, _ = require_runtime()
    generator = torch.Generator(device="cpu").manual_seed(seed)
    images = []
    annotations = []
    for index in range(sample_count):
        class_index = index % num_classes
        x1 = 2 + index % 3
        y1 = 2 + (index * 2) % 3
        x2 = min(image_size - 1, x1 + max(4, image_size // 2))
        y2 = min(image_size - 1, y1 + max(4, image_size // 2))
        mask = torch.zeros((image_size, image_size), dtype=torch.bool)
        mask[y1:y2, x1:x2] = True
        image = torch.rand(
            (3, image_size, image_size),
            generator=generator,
        ) * 0.03
        image[class_index % 3, mask] = 0.9
        annotation = {
            "id": index + 1,
            "image_id": index + 1,
            "category_id": class_index + 1,
            "bbox": [float(x1), float(y1), float(x2 - x1), float(y2 - y1)],
            "area": float(mask.sum().item()),
            "iscrowd": 0,
            "segmentation": [[
                float(x1),
                float(y1),
                float(x2),
                float(y1),
                float(x2),
                float(y2),
                float(x1),
                float(y2),
            ]],
            "mask": mask,
        }
        annotations.append(annotation)
        images.append({
            "id": index + 1,
            "file_name": f"synthetic-{index:04d}.png",
            "width": image_size,
            "height": image_size,
            "tensor": image,
            "sha256": None,
            "annotations": [annotation],
        })
    return {
        "images": images,
        "categories": {
            index + 1: f"class_{index}"
            for index in range(num_classes)
        },
        "annotations": annotations,
        "manifest_entries": [{
            "path": "synthetic-generator",
            "size": sample_count,
            "sha256": canonical_sha256({
                "seed": seed,
                "sample_count": sample_count,
                "image_size": image_size,
                "num_classes": num_classes,
            }),
        }],
    }


def _resize_image(tensor: Any, image_size: int) -> Any:
    torch, _, _, _ = require_runtime()
    return torch.nn.functional.interpolate(
        tensor.unsqueeze(0),
        size=(image_size, image_size),
        mode="bilinear",
        align_corners=False,
    ).squeeze(0)


def _resize_mask(mask: Any, image_size: int) -> Any:
    torch, _, _, _ = require_runtime()
    return (
        torch.nn.functional.interpolate(
            mask.to(dtype=torch.float32).unsqueeze(0).unsqueeze(0),
            size=(image_size, image_size),
            mode="nearest",
        )
        .squeeze(0)
        .squeeze(0)
        .to(dtype=torch.bool)
    )


def _training_tensors(dataset: dict[str, Any], image_size: int) -> tuple[Any, list[dict[str, Any]]]:
    torch, _, _, _ = require_runtime()
    category_ids = sorted(dataset["categories"])
    category_index = {
        identifier: index
        for index, identifier in enumerate(category_ids)
    }
    images = []
    targets = []
    for image in dataset["images"]:
        images.append(_resize_image(image["tensor"], image_size))
        targets.append({
            "labels": torch.tensor(
                [
                    category_index[annotation["category_id"]]
                    for annotation in image["annotations"]
                ],
                dtype=torch.long,
            ),
            "masks": torch.stack([
                _resize_mask(annotation["mask"], image_size)
                for annotation in image["annotations"]
            ]),
        })
    return torch.stack(images), targets


def _mask_iou(first: Any, second: Any) -> float:
    intersection = int((first & second).sum().item())
    union = int((first | second).sum().item())
    return 0.0 if union == 0 else intersection / union


def _match_loss(outputs: dict[str, Any], targets: list[dict[str, Any]]) -> Any:
    torch, _, _, _ = require_runtime()
    logits = outputs["logits"]
    mask_logits = outputs["mask_logits"]
    background = logits.shape[-1] - 1
    losses = []
    for batch_index, target in enumerate(targets):
        assigned: set[int] = set()
        class_target = torch.full(
            (logits.shape[1],),
            background,
            dtype=torch.long,
            device=logits.device,
        )
        mask_losses = []
        for target_index in range(target["masks"].shape[0]):
            candidates = [
                query
                for query in range(logits.shape[1])
                if query not in assigned
            ]
            if not candidates:
                raise ValueError("num_queries 少于一张图中的实例数")
            candidate_tensor = torch.tensor(
                candidates,
                dtype=torch.long,
                device=logits.device,
            )
            probabilities = logits[batch_index, candidate_tensor].softmax(dim=-1)
            label = int(target["labels"][target_index].item())
            target_mask = target["masks"][target_index].to(dtype=torch.float32)
            mask_cost = torch.nn.functional.binary_cross_entropy_with_logits(
                mask_logits[batch_index, candidate_tensor],
                target_mask.expand(len(candidates), -1, -1),
                reduction="none",
            ).mean(dim=(1, 2))
            cost = mask_cost - probabilities[:, label]
            selected = candidates[int(cost.detach().argmin().item())]
            assigned.add(selected)
            class_target[selected] = label
            mask_losses.append(
                torch.nn.functional.binary_cross_entropy_with_logits(
                    mask_logits[batch_index, selected],
                    target_mask,
                )
            )
        class_loss = torch.nn.functional.cross_entropy(
            logits[batch_index],
            class_target,
        )
        losses.append(class_loss + 2.0 * torch.stack(mask_losses).mean())
    return torch.stack(losses).mean()


def prepare_output_directory(path: Path) -> Path:
    target = Path(_os.path.abspath(_os.fspath(path)))
    for parent in target.parents:
        if _os.path.lexists(parent) and is_link_or_reparse(parent):
            raise ValueError("输出父路径不能包含 link/reparse")
    if _os.path.lexists(target):
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{target}")
    target.mkdir(parents=True, exist_ok=False)
    if is_link_or_reparse(target):
        raise ValueError("新建输出目录不是普通目录")
    return target


def _exclusive_bytes(path: Path, content: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | getattr(_os, "O_BINARY", 0)
    descriptor = _os.open(target, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = _os.write(descriptor, view)
            view = view[written:]
        _os.fsync(descriptor)
    finally:
        _os.close(descriptor)


def write_json(path: Path, payload: object) -> None:
    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("输出 JSON 含非法值") from error
    _exclusive_bytes(path, encoded)


def _rgb_png(tensor: Any) -> bytes:
    torch, numpy, Image, _ = require_runtime()
    array = (
        tensor.detach()
        .clamp(0.0, 1.0)
        .permute(1, 2, 0)
        .mul(255.0)
        .round()
        .to(dtype=torch.uint8)
        .cpu()
        .numpy()
    )
    buffer = io.BytesIO()
    Image.fromarray(numpy.asarray(array), mode="RGB").save(
        buffer,
        format="PNG",
        optimize=False,
        compress_level=9,
    )
    return buffer.getvalue()


def _source_sha256(image: dict[str, Any]) -> str:
    source = image.get("sha256")
    if source is None:
        source = f"sha256:{hashlib.sha256(_rgb_png(image['tensor'])).hexdigest()}"
    if not isinstance(source, str) or SHA256.fullmatch(source) is None:
        raise ValueError("数据样本 source_sha256 无效")
    return source


def _data_manifest(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": DATA_MANIFEST_SCHEMA,
        "entries": [dict(item) for item in dataset["manifest_entries"]],
        "samples": [
            {
                "image_id": image["id"],
                "source_sha256": _source_sha256(image),
            }
            for image in dataset["images"]
        ],
        "category_ids": sorted(dataset["categories"]),
    }


def _validated_data_manifest(
    payload: dict[str, Any],
    *,
    sample_count: int,
    num_classes: int,
) -> tuple[dict[int, str], set[int]]:
    if (
        set(payload) != {"schema", "entries", "samples", "category_ids"}
        or payload.get("schema") != DATA_MANIFEST_SCHEMA
        or not isinstance(payload.get("entries"), list)
        or not 1 <= len(payload["entries"]) <= MAX_IMAGES + 1
        or not isinstance(payload.get("samples"), list)
        or len(payload["samples"]) != sample_count
        or not isinstance(payload.get("category_ids"), list)
        or len(payload["category_ids"]) != num_classes
    ):
        raise ValueError("data-manifest.json 顶层字段或数量无效")
    entry_paths: set[str] = set()
    total_bytes = 0
    for item in payload["entries"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item.get("path"), str)
            or safe_output_relative(item["path"]) != item["path"]
            or item["path"] in entry_paths
            or isinstance(item.get("size"), bool)
            or not isinstance(item.get("size"), int)
            or not 0 <= item["size"] <= MAX_FILE_BYTES
            or SHA256.fullmatch(item.get("sha256", "")) is None
        ):
            raise ValueError("data-manifest.json entries 无效")
        entry_paths.add(item["path"])
        total_bytes += item["size"]
        if total_bytes > MAX_DATASET_BYTES:
            raise ValueError("data-manifest.json 声明体积超过上限")
    samples: dict[int, str] = {}
    for item in payload["samples"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"image_id", "source_sha256"}
            or isinstance(item.get("image_id"), bool)
            or not isinstance(item.get("image_id"), int)
            or item["image_id"] <= 0
            or item["image_id"] in samples
            or SHA256.fullmatch(item.get("source_sha256", "")) is None
        ):
            raise ValueError("data-manifest.json 样本身份无效或重复")
        samples[item["image_id"]] = item["source_sha256"]
    if list(samples) != sorted(samples):
        raise ValueError("data-manifest.json 样本必须按 image_id 排序")
    category_ids = payload["category_ids"]
    if (
        any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item <= 0
            for item in category_ids
        )
        or category_ids != sorted(set(category_ids))
    ):
        raise ValueError("data-manifest.json 类别合同无效")
    return samples, set(category_ids)


def _mask_png(mask: Any) -> bytes:
    torch, numpy, Image, _ = require_runtime()
    array = (
        mask.to(dtype=torch.uint8)
        .mul(255)
        .cpu()
        .numpy()
    )
    buffer = io.BytesIO()
    Image.fromarray(numpy.asarray(array), mode="L").save(
        buffer,
        format="PNG",
        optimize=False,
        compress_level=9,
    )
    return buffer.getvalue()


def _save_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    torch, _, _, _ = require_runtime()
    flags = _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | getattr(_os, "O_BINARY", 0)
    descriptor = _os.open(path, flags, 0o600)
    try:
        with _os.fdopen(descriptor, "wb", closefd=False) as handle:
            torch.save(payload, handle)
            handle.flush()
            _os.fsync(handle.fileno())
    finally:
        _os.close(descriptor)


def load_checkpoint(path: Path) -> tuple[dict[str, Any], Any]:
    torch, _, _, _ = require_runtime()
    content = stable_read_bytes(Path(path), "checkpoint", MAX_FILE_BYTES)
    try:
        payload = torch.load(
            io.BytesIO(content),
            map_location="cpu",
            weights_only=True,
        )
    except (EOFError, OSError, RuntimeError, ValueError) as error:
        raise ValueError("checkpoint 无法按 state_dict 安全读取") from error
    expected = {
        "schema",
        "direction",
        "model_state_dict",
        "model_config",
        "seed",
        "run_id",
        "config_sha256",
        "data_manifest_sha256",
        "optimizer_steps",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or payload.get("schema") != CHECKPOINT_SCHEMA
        or payload.get("direction") != "instseg"
        or not isinstance(payload.get("model_state_dict"), dict)
        or not payload["model_state_dict"]
        or not isinstance(payload.get("model_config"), dict)
        or set(payload["model_config"]) != {"num_classes", "num_queries", "image_size"}
        or isinstance(payload.get("optimizer_steps"), bool)
        or not isinstance(payload.get("optimizer_steps"), int)
        or not 1 <= payload["optimizer_steps"] <= MAX_EPOCHS
        or SHA256.fullmatch(payload.get("config_sha256", "")) is None
        or SHA256.fullmatch(payload.get("data_manifest_sha256", "")) is None
    ):
        raise ValueError("checkpoint 字段或身份无效")
    config = payload["model_config"]
    num_classes, num_queries, image_size = _validated_model_config(config)
    if not all(
        isinstance(value, torch.Tensor) and bool(torch.isfinite(value).all())
        for value in payload["model_state_dict"].values()
    ):
        raise ValueError("checkpoint state_dict 含非 Tensor、NaN 或 Inf")
    model = build_model(
        num_classes,
        num_queries,
        image_size,
    )
    try:
        model.load_state_dict(payload["model_state_dict"], strict=True)
    except (RuntimeError, TypeError, ValueError) as error:
        raise ValueError("checkpoint state_dict 与固定模型结构不一致") from error
    model.eval()
    return payload, model


def _save_inputs(output: Path, dataset: dict[str, Any]) -> list[dict[str, str]]:
    (output / "inputs").mkdir()
    records = []
    for index, image in enumerate(dataset["images"]):
        relative = f"inputs/{index:04d}.png"
        content = _rgb_png(image["tensor"])
        _exclusive_bytes(output / relative, content)
        records.append({
            "path": relative,
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            "source_sha256": (
                image.get("sha256")
                or f"sha256:{hashlib.sha256(content).hexdigest()}"
            ),
        })
    return records


def _predict(
    model: Any,
    dataset: dict[str, Any],
    image_size: int,
    output: Path,
    inputs: list[dict[str, str]],
    *,
    device: str = "cpu",
) -> tuple[list[dict[str, Any]], list[Any]]:
    torch, _, _, _ = require_runtime()
    images, _ = _training_tensors(dataset, image_size)
    images = images.to(device=require_device(device))
    with torch.no_grad():
        model_output = model(images)
        probabilities = model_output["logits"].softmax(dim=-1)
        mask_probabilities = model_output["mask_logits"].sigmoid()
    (output / "masks").mkdir()
    category_ids = sorted(dataset["categories"])
    predictions = []
    masks = []
    for index, image in enumerate(dataset["images"]):
        foreground = probabilities[index, :, :-1]
        flat = int(foreground.argmax().item())
        query = flat // foreground.shape[1]
        class_index = flat % foreground.shape[1]
        probability_mask = mask_probabilities[index, query]
        mask = probability_mask >= 0.5
        if not bool(mask.any()):
            mask = torch.zeros_like(mask, dtype=torch.bool)
            mask.view(-1)[int(probability_mask.argmax().item())] = True
        if mask.shape != (image_size, image_size):
            raise ValueError("模型 mask shape 无效")
        original_mask = _resize_mask(mask, image["height"])
        if image["width"] != image["height"]:
            original_mask = (
                torch.nn.functional.interpolate(
                    mask.to(dtype=torch.float32).unsqueeze(0).unsqueeze(0),
                    size=(image["height"], image["width"]),
                    mode="nearest",
                )
                .squeeze()
                .to(dtype=torch.bool)
            )
        original_mask = original_mask.detach().cpu()
        relative = f"masks/{index:04d}.png"
        content = _mask_png(original_mask)
        _exclusive_bytes(output / relative, content)
        masks.append(original_mask)
        predictions.append({
            "image_id": image["id"],
            "category_id": category_ids[class_index],
            "score": float(foreground[query, class_index].item()),
            "input_path": inputs[index]["path"],
            "input_sha256": inputs[index]["sha256"],
            "source_sha256": inputs[index]["source_sha256"],
            "mask_path": relative,
            "mask_sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        })
    return predictions, masks


def _debug_metrics(
    predictions: list[dict[str, Any]],
    masks: list[Any],
    dataset: dict[str, Any],
) -> dict[str, float]:
    by_image = {item["id"]: item for item in dataset["images"]}
    values = []
    correct = 0
    for item, predicted_mask in zip(predictions, masks):
        target = by_image[item["image_id"]]["annotations"][0]
        iou = _mask_iou(predicted_mask, target["mask"])
        values.append(iou)
        if iou >= 0.5 and item["category_id"] == target["category_id"]:
            correct += 1
    result = {
        "debug_mask_mean_iou": float(sum(values) / len(values)),
        "debug_precision_at_mask_iou_0_5": float(correct / len(values)),
    }
    _validate_metrics(result, synthetic=True)
    return result


def _formal_metrics(
    output: Path,
    dataset: dict[str, Any],
    predictions: list[dict[str, Any]],
    masks: list[Any],
) -> dict[str, float]:
    COCO, COCOeval, mask_utils = require_pycocotools()
    torch, numpy, _, _ = require_runtime()
    ground_truth = output / "coco-ground-truth.json"
    _write_coco_ground_truth(ground_truth, dataset["annotation_bytes"])
    coco = COCO(str(ground_truth))
    results = []
    for item, mask in zip(predictions, masks):
        array = mask.to(dtype=torch.uint8).cpu().numpy()
        encoded = mask_utils.encode(numpy.asfortranarray(array))
        counts = encoded["counts"]
        if isinstance(counts, bytes):
            counts = counts.decode("ascii")
        results.append({
            "image_id": item["image_id"],
            "category_id": item["category_id"],
            "score": item["score"],
            "segmentation": {
                "size": [int(value) for value in encoded["size"]],
                "counts": counts,
            },
        })
    detections = coco.loadRes(results)
    evaluator = COCOeval(coco, detections, "segm")
    evaluator.evaluate()
    evaluator.accumulate()
    evaluator.summarize()
    metrics = {
        "coco_segm_ap": float(evaluator.stats[0]),
        "coco_segm_ap50": float(evaluator.stats[1]),
        "coco_segm_ap75": float(evaluator.stats[2]),
    }
    _validate_metrics(metrics, synthetic=False)
    return metrics


def _write_coco_ground_truth(path: Path, annotation_bytes: bytes) -> None:
    try:
        payload = json.loads(
            annotation_bytes.decode("utf-8"),
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("无法为 COCOeval 构造固定标注 sidecar") from error
    if not isinstance(payload, dict):
        raise ValueError("COCOeval 标注 sidecar 必须是 object")
    payload = dict(payload)
    payload.setdefault("info", {})
    payload.setdefault("licenses", [])
    write_json(path, payload)


def _validate_metrics(metrics: dict[str, Any], *, synthetic: bool) -> None:
    expected = set(DEBUG_METRICS if synthetic else FORMAL_METRICS)
    if set(metrics) != expected:
        raise ValueError("evaluation 指标集合无效")
    if any(
        type(value) is not float
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
        for value in metrics.values()
    ):
        raise ValueError("evaluation 指标必须是有限 [0,1] float")


def _artifact_hashes(output: Path, relatives: list[str]) -> dict[str, str]:
    result = {}
    for relative in sorted(relatives):
        content = stable_read_bytes(output / relative, "输出产物", MAX_FILE_BYTES)
        result[relative] = f"sha256:{hashlib.sha256(content).hexdigest()}"
    return result


def run_training(
    config_path: Path,
    output_dir: Path,
    *,
    mode: str,
    data_root: Path | None = None,
    annotation_file: Path | None = None,
    seed_override: int | None = None,
    run_id: str = "STANDALONE",
    config_sha256: str | None = None,
    device_override: str | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    if mode not in {"synthetic_debug", "local_coco"}:
        raise ValueError("mode 必须是 synthetic_debug 或 local_coco")
    seed = config.get("seed") if seed_override is None else seed_override
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须是非负整数")
    device = config.get("device") if device_override is None else device_override
    torch = set_deterministic(
        seed,
        device,
        synthetic=mode == "synthetic_debug",
    )
    torch_device = require_device(
        device,
        synthetic=mode == "synthetic_debug",
    )
    image_size = positive_int(
        config,
        "image_size",
        8,
        MAX_MODEL_IMAGE_SIZE,
    )
    num_queries = positive_int(
        config,
        "num_queries",
        maximum=MAX_MODEL_QUERIES,
    )
    epochs = positive_int(
        config,
        "epochs",
        maximum=MAX_EPOCHS,
    )
    learning_rate = positive_float(config, "learning_rate")
    if mode == "synthetic_debug":
        num_classes = positive_int(
            config,
            "num_classes",
            maximum=MAX_MODEL_CLASSES,
        )
        sample_count = positive_int(
            config,
            "sample_count",
            2,
            MAX_IMAGES,
        )
        _validated_model_config({
            "num_classes": num_classes,
            "num_queries": num_queries,
            "image_size": image_size,
        })
        _validate_training_shape(sample_count, image_size)
        dataset = synthetic_dataset(
            seed,
            sample_count,
            image_size,
            num_classes,
        )
    elif mode == "local_coco":
        if data_root is None or annotation_file is None:
            raise ValueError("local_coco 需要 data_root 和 annotation_file")
        require_pycocotools()
        dataset = load_coco_dataset(data_root, annotation_file)
        num_classes = len(dataset["categories"])
    if any(len(image["annotations"]) > num_queries for image in dataset["images"]):
        raise ValueError("num_queries 少于单图实例数量")
    _validated_model_config({
        "num_classes": num_classes,
        "num_queries": num_queries,
        "image_size": image_size,
    })
    _validate_training_shape(len(dataset["images"]), image_size)
    data_manifest = _data_manifest(dataset)
    data_manifest_sha256 = canonical_sha256(data_manifest)
    images, targets = _training_tensors(dataset, image_size)
    images = images.to(device=torch_device)
    targets = [
        {
            key: value.to(device=torch_device)
            for key, value in target.items()
        }
        for target in targets
    ]
    model = build_model(num_classes, num_queries, image_size).to(
        device=torch_device
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    optimizer_steps = 0
    model.train()
    for _ in range(epochs):
        optimizer.zero_grad(set_to_none=True)
        loss = _match_loss(model(images), targets)
        if not bool(torch.isfinite(loss)):
            raise ValueError("训练 loss 出现 NaN/Inf")
        loss.backward()
        optimizer.step()
        optimizer_steps += 1
    output = prepare_output_directory(output_dir)
    write_json(output / "data-manifest.json", data_manifest)
    model_config = {
        "num_classes": num_classes,
        "num_queries": num_queries,
        "image_size": image_size,
    }
    effective_config = dict(config)
    effective_config["device"] = str(device)
    resolved_config_sha256 = (
        config_sha256 or canonical_sha256(effective_config)
    )
    checkpoint_payload = {
        "schema": CHECKPOINT_SCHEMA,
        "direction": "instseg",
        "model_state_dict": model.state_dict(),
        "model_config": model_config,
        "seed": seed,
        "run_id": run_id,
        "config_sha256": resolved_config_sha256,
        "data_manifest_sha256": data_manifest_sha256,
        "optimizer_steps": optimizer_steps,
    }
    _save_checkpoint(output / "checkpoint.pt", checkpoint_payload)
    loaded, reloaded_model = load_checkpoint(output / "checkpoint.pt")
    reloaded_model = reloaded_model.to(device=torch_device)
    inputs = _save_inputs(output, dataset)
    predictions, masks = _predict(
        reloaded_model,
        dataset,
        image_size,
        output,
        inputs,
        device=str(device),
    )
    synthetic = mode == "synthetic_debug"
    metrics = (
        _debug_metrics(predictions, masks, dataset)
        if synthetic
        else _formal_metrics(output, dataset, predictions, masks)
    )
    write_json(
        output / "evaluation.json",
        {
            "schema": EVALUATION_SCHEMA,
            "mode": mode,
            "backend": (
                "debug_mask_metrics"
                if synthetic
                else "pycocotools==2.0.11"
            ),
            "iou_type": "segm",
            "metrics": metrics,
        },
    )
    write_json(
        output / "predictions.json",
        {
            "schema": PREDICTIONS_SCHEMA,
            "mode": mode,
            "predictions": predictions,
        },
    )
    _exclusive_bytes(
        output / "raw.log",
        (
            "PACK-INSTSEG-V1.0.0\n"
            f"mode={mode}\noptimizer_steps={optimizer_steps}\n"
            "checkpoint_reloaded=true\n"
        ).encode("utf-8"),
    )
    relatives = [
        "checkpoint.pt",
        "data-manifest.json",
        "evaluation.json",
        "predictions.json",
        "raw.log",
        *(item["path"] for item in inputs),
        *(item["mask_path"] for item in predictions),
    ]
    if not synthetic:
        relatives.append("coco-ground-truth.json")
    artifacts = _artifact_hashes(output, relatives)
    record = {
        "schema": RUN_SCHEMA,
        "phase": "train_evaluate_infer",
        "mode": mode,
        "direction": "instseg",
        "run_kind": (
            "synthetic_debug_only"
            if synthetic
            else "local_coco_baseline"
        ),
        "paper_eligible": False,
        "run_id": run_id,
        "config_sha256": resolved_config_sha256,
        "seed": seed,
        "device": str(device),
        "sample_count": len(dataset["images"]),
        "optimizer_steps": optimizer_steps,
        "checkpoint_reloaded": loaded["run_id"] == run_id,
        "model_config": model_config,
        "data_manifest_sha256": data_manifest_sha256,
        "artifact_sha256": artifacts,
    }
    write_json(output / "run.json", record)
    return record


def _dataset_from_checkpoint(
    checkpoint: dict[str, Any],
    mode: str,
    data_root: Path | None,
    annotation_file: Path | None,
) -> dict[str, Any]:
    config = checkpoint["model_config"]
    if mode == "synthetic_debug":
        return synthetic_dataset(
            checkpoint["seed"],
            6,
            config["image_size"],
            config["num_classes"],
        )
    if mode == "local_coco" and data_root is not None and annotation_file is not None:
        return load_coco_dataset(data_root, annotation_file)
    raise ValueError("local_coco 需要 data_root 和 annotation_file")


def run_evaluation(
    checkpoint_path: Path,
    output_dir: Path,
    *,
    mode: str,
    data_root: Path | None = None,
    annotation_file: Path | None = None,
    device: str = "cpu",
) -> dict[str, Any]:
    if mode not in {"synthetic_debug", "local_coco"}:
        raise ValueError("mode 无效")
    torch_device = require_device(
        device,
        synthetic=mode == "synthetic_debug",
    )
    checkpoint, model = load_checkpoint(checkpoint_path)
    set_deterministic(
        checkpoint["seed"],
        device,
        synthetic=mode == "synthetic_debug",
    )
    model = model.to(device=torch_device)
    if mode == "local_coco":
        require_pycocotools()
    dataset = _dataset_from_checkpoint(
        checkpoint,
        mode,
        data_root,
        annotation_file,
    )
    output = prepare_output_directory(output_dir)
    inputs = _save_inputs(output, dataset)
    predictions, masks = _predict(
        model,
        dataset,
        checkpoint["model_config"]["image_size"],
        output,
        inputs,
        device=device,
    )
    synthetic = mode == "synthetic_debug"
    metrics = (
        _debug_metrics(predictions, masks, dataset)
        if synthetic
        else _formal_metrics(output, dataset, predictions, masks)
    )
    result = {
        "schema": EVALUATION_SCHEMA,
        "mode": mode,
        "backend": (
            "debug_mask_metrics"
            if synthetic
            else "pycocotools==2.0.11"
        ),
        "iou_type": "segm",
        "metrics": metrics,
    }
    write_json(output / "evaluation.json", result)
    write_json(
        output / "predictions.json",
        {
            "schema": PREDICTIONS_SCHEMA,
            "mode": mode,
            "predictions": predictions,
        },
    )
    return result


def run_inference(
    checkpoint_path: Path,
    output_dir: Path,
    *,
    mode: str,
    data_root: Path | None = None,
    annotation_file: Path | None = None,
    device: str = "cpu",
) -> dict[str, Any]:
    if mode not in {"synthetic_debug", "local_coco"}:
        raise ValueError("mode 无效")
    torch_device = require_device(
        device,
        synthetic=mode == "synthetic_debug",
    )
    checkpoint, model = load_checkpoint(checkpoint_path)
    set_deterministic(
        checkpoint["seed"],
        device,
        synthetic=mode == "synthetic_debug",
    )
    model = model.to(device=torch_device)
    dataset = _dataset_from_checkpoint(
        checkpoint,
        mode,
        data_root,
        annotation_file,
    )
    output = prepare_output_directory(output_dir)
    inputs = _save_inputs(output, dataset)
    predictions, _ = _predict(
        model,
        dataset,
        checkpoint["model_config"]["image_size"],
        output,
        inputs,
        device=device,
    )
    result = {
        "schema": PREDICTIONS_SCHEMA,
        "mode": mode,
        "predictions": predictions,
    }
    write_json(output / "predictions.json", result)
    return result


def validate_workflow_output(
    output_dir: Path,
    *,
    mode: str,
    run_id: str,
    config_sha256: str,
    seed: int,
    expected_data_manifest_sha256: str | None = None,
    expected_device: str = "cpu",
) -> tuple[dict[str, Any], dict[str, float], list[str]]:
    if expected_device not in {"cpu", "cuda"}:
        raise ValueError("expected_device 只允许 cpu 或 cuda")
    if mode == "synthetic_debug" and expected_device != "cpu":
        raise ValueError("synthetic_debug 输出只能声明 CPU")
    output = require_plain_directory(output_dir, "Run 输出")
    observed_files, observed_directories = _enumerate_output_tree(output)
    record = _read_json(output / "run.json", "run.json")
    run_fields = {
        "schema",
        "phase",
        "mode",
        "direction",
        "run_kind",
        "paper_eligible",
        "run_id",
        "config_sha256",
        "seed",
        "device",
        "sample_count",
        "optimizer_steps",
        "checkpoint_reloaded",
        "model_config",
        "data_manifest_sha256",
        "artifact_sha256",
    }
    if (
        set(record) != run_fields
        or record.get("schema") != RUN_SCHEMA
        or record.get("phase") != "train_evaluate_infer"
        or record.get("mode") != mode
        or record.get("direction") != "instseg"
        or record.get("run_id") != run_id
        or record.get("config_sha256") != config_sha256
        or record.get("seed") != seed
        or record.get("device") != expected_device
        or record.get("checkpoint_reloaded") is not True
        or isinstance(record.get("optimizer_steps"), bool)
        or not isinstance(record.get("optimizer_steps"), int)
        or not 1 <= record["optimizer_steps"] <= MAX_EPOCHS
        or type(record.get("sample_count")) is not int
        or not 1 <= record["sample_count"] <= MAX_IMAGES
    ):
        raise ValueError("run.json 顶层字段、身份或训练记录不严格")
    synthetic = mode == "synthetic_debug"
    if (
        record.get("run_kind")
        != ("synthetic_debug_only" if synthetic else "local_coco_baseline")
        or record.get("paper_eligible") is not False
    ):
        raise ValueError("run_kind/paper_eligible 被篡改")
    if (
        expected_data_manifest_sha256 is not None
        and record.get("data_manifest_sha256")
        != expected_data_manifest_sha256
    ):
        raise ValueError("run.json data manifest 与冻结数据身份不一致")
    checkpoint, _ = load_checkpoint(output / "checkpoint.pt")
    for key in (
        "run_id",
        "config_sha256",
        "seed",
        "data_manifest_sha256",
        "optimizer_steps",
        "model_config",
    ):
        if checkpoint.get(key) != record.get(key):
            raise ValueError(f"checkpoint 与 run.json 的 {key} 身份不一致")
    data_manifest = _read_json(
        output / "data-manifest.json",
        "data-manifest.json",
    )
    if canonical_sha256(data_manifest) != record["data_manifest_sha256"]:
        raise ValueError("data-manifest.json 与冻结数据 manifest hash 不一致")
    expected_samples, category_ids = _validated_data_manifest(
        data_manifest,
        sample_count=record["sample_count"],
        num_classes=record["model_config"]["num_classes"],
    )
    evaluation = _read_json(output / "evaluation.json", "evaluation.json")
    if (
        set(evaluation) != {"schema", "mode", "backend", "iou_type", "metrics"}
        or evaluation.get("schema") != EVALUATION_SCHEMA
        or evaluation.get("mode") != mode
        or evaluation.get("iou_type") != "segm"
        or evaluation.get("backend")
        != ("debug_mask_metrics" if synthetic else "pycocotools==2.0.11")
        or not isinstance(evaluation.get("metrics"), dict)
    ):
        raise ValueError("evaluation.json 顶层字段、schema 或后端身份无效")
    _validate_metrics(evaluation["metrics"], synthetic=synthetic)
    predictions = _read_json(output / "predictions.json", "predictions.json")
    if (
        set(predictions) != {"schema", "mode", "predictions"}
        or predictions.get("schema") != PREDICTIONS_SCHEMA
        or predictions.get("mode") != mode
        or not isinstance(predictions.get("predictions"), list)
        or len(predictions["predictions"]) != record.get("sample_count")
    ):
        raise ValueError("predictions.json 顶层字段、schema 或数量无效")
    _, numpy, Image, _ = require_runtime()
    expected_artifacts = {
        "checkpoint.pt",
        "data-manifest.json",
        "evaluation.json",
        "predictions.json",
        "raw.log",
    }
    observed_inputs: set[str] = set()
    observed_masks: set[str] = set()
    observed_image_ids: set[int] = set()
    output_total_pixels = 0
    output_total_mask_pixels = 0
    for item in predictions["predictions"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "image_id",
                "category_id",
                "score",
                "input_path",
                "input_sha256",
                "source_sha256",
                "mask_path",
                "mask_sha256",
            }
            or isinstance(item["image_id"], bool)
            or not isinstance(item["image_id"], int)
            or item["image_id"] <= 0
            or item["image_id"] in observed_image_ids
            or isinstance(item["category_id"], bool)
            or not isinstance(item["category_id"], int)
            or item["category_id"] <= 0
            or type(item["score"]) is not float
            or not math.isfinite(item["score"])
            or not 0.0 <= item["score"] <= 1.0
            or SHA256.fullmatch(item.get("input_sha256", "")) is None
            or SHA256.fullmatch(item.get("source_sha256", "")) is None
            or SHA256.fullmatch(item.get("mask_sha256", "")) is None
        ):
            raise ValueError("predictions.json image/category 身份无效或重复")
        if (
            expected_samples.get(item["image_id"]) != item["source_sha256"]
            or item["category_id"] not in category_ids
        ):
            raise ValueError("prediction 未绑定冻结数据 manifest 的样本或类别合同")
        observed_image_ids.add(item["image_id"])
        input_path = safe_output_relative(item.get("input_path"))
        mask_path = safe_output_relative(item.get("mask_path"))
        if (
            len(PurePosixPath(input_path).parts) != 2
            or PurePosixPath(input_path).parts[0] != "inputs"
            or input_path in observed_inputs
            or len(PurePosixPath(mask_path).parts) != 2
            or PurePosixPath(mask_path).parts[0] != "masks"
            or mask_path in observed_masks
        ):
            raise ValueError("predictions input/mask path 无效或重复")
        observed_inputs.add(input_path)
        observed_masks.add(mask_path)
        expected_artifacts.update({input_path, mask_path})
        input_size: tuple[int, int] | None = None
        for field, hash_field, label in (
            ("input_path", "input_sha256", "输入"),
            ("mask_path", "mask_sha256", "mask"),
        ):
            relative = input_path if field == "input_path" else mask_path
            content = stable_read_bytes(
                output / relative,
                f"预测{label} sidecar",
                MAX_FILE_BYTES,
            )
            if f"sha256:{hashlib.sha256(content).hexdigest()}" != item[hash_field]:
                raise ValueError(f"预测{label} SHA-256 hash 被篡改")
            if field == "input_path":
                try:
                    with Image.open(io.BytesIO(content)) as image:
                        if image.mode != "RGB":
                            raise ValueError("输入 PNG 必须是 RGB")
                        input_size = image.size
                        width, height = input_size
                        if (
                            not 1 <= width <= MAX_IMAGE_DIMENSION
                            or not 1 <= height <= MAX_IMAGE_DIMENSION
                        ):
                            raise ValueError("输入 PNG 尺寸超过上限")
                        output_total_pixels += width * height
                        if output_total_pixels > MAX_TOTAL_PIXELS:
                            raise ValueError("输入 PNG 总像素数超过上限")
                        image.load()
                except OSError as error:
                    raise ValueError("输入 PNG 无法读取") from error
            else:
                try:
                    with Image.open(io.BytesIO(content)) as image:
                        if input_size is None or image.size != input_size:
                            raise ValueError("mask PNG 尺寸与输入不一致")
                        width, height = image.size
                        if (
                            not 1 <= width <= MAX_IMAGE_DIMENSION
                            or not 1 <= height <= MAX_IMAGE_DIMENSION
                        ):
                            raise ValueError("mask PNG 尺寸超过上限")
                        output_total_mask_pixels += width * height
                        if output_total_mask_pixels > MAX_TOTAL_MASK_PIXELS:
                            raise ValueError("mask PNG 总像素数超过上限")
                        if (
                            image.mode != "L"
                        ):
                            raise ValueError("mask PNG 必须是二值 L 模式")
                        image.load()
                        array = numpy.asarray(image)
                        if (
                            not set(numpy.unique(array)).issubset({0, 255})
                            or 255 not in set(numpy.unique(array))
                        ):
                            raise ValueError("mask PNG 必须是二值 L 模式")
                except OSError as error:
                    raise ValueError("mask PNG 无法读取") from error
    if observed_image_ids != set(expected_samples):
        raise ValueError("prediction image_id 未与冻结数据 manifest 一一对应")
    hashes = record.get("artifact_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("run.json 缺少 artifact SHA-256")
    if not synthetic:
        expected_artifacts.add("coco-ground-truth.json")
    if set(hashes) != expected_artifacts:
        raise ValueError("run.json artifact path 清单不完整或含未知路径")
    expected_files = {*hashes, "run.json"}
    expected_directories: set[str] = set()
    for relative in expected_files:
        parts = PurePosixPath(relative).parts[:-1]
        for length in range(1, len(parts) + 1):
            expected_directories.add(PurePosixPath(*parts[:length]).as_posix())
    if (
        observed_files != expected_files
        or observed_directories != expected_directories
    ):
        raise ValueError("Run 输出含未登记文件、目录或清单缺项")
    output_total_bytes = len(
        stable_read_bytes(
            output / "run.json",
            "run.json",
            MAX_FILE_BYTES,
        )
    )
    if output_total_bytes > MAX_OUTPUT_BYTES:
        raise ValueError("Run 输出总字节数超过 64 MiB 上限")
    for relative, expected in hashes.items():
        normalized_relative = safe_output_relative(relative)
        if (
            normalized_relative != relative
            or SHA256.fullmatch(expected) is None
        ):
            raise ValueError("artifact SHA-256 清单无效")
        content = stable_read_bytes(output / relative, "Run artifact", MAX_FILE_BYTES)
        output_total_bytes += len(content)
        if output_total_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("Run 输出总字节数超过 64 MiB 上限")
        if f"sha256:{hashlib.sha256(content).hexdigest()}" != expected:
            raise ValueError("Run artifact SHA-256 hash 被篡改")
    final_files, final_directories = _enumerate_output_tree(output)
    if final_files != expected_files or final_directories != expected_directories:
        raise ValueError("Run 输出在验证期间变化或含未登记项")
    return record, dict(evaluation["metrics"]), sorted([*hashes, "run.json"])


def _read_json(path: Path, label: str) -> dict[str, Any]:
    content = stable_read_bytes(path, label, MAX_FILE_BYTES)
    try:
        payload = json.loads(
            content.decode("utf-8"),
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} 不是严格 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 必须是 JSON object")
    return payload


def _reject_constant(value: str) -> None:
    raise ValueError(f"禁止非有限数字：{value}")
