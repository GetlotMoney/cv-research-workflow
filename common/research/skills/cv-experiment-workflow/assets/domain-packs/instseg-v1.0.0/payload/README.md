# PACK-INSTSEG-V1.0.0

这是独立的实例分割仓库模板。它不引用任何其他方向包，内部带一个很小的
fixed-query（固定数量实例候选）PyTorch 模型，可以真正完成训练、磁盘
checkpoint 重载、评估和推理，并把每个预测掩码写成真实灰度 PNG。合成
冒烟固定使用 CPU；真实数据 baseline 默认使用 CUDA。`local_coco` 可以显式
选择 `cpu` 做兼容 debug，但正式 evidence 必须选择 `cuda`。选择
`cuda` 前，用户必须预先安装可用的 CUDA 版 PyTorch；模板不会自动安装，
也不会偷偷降级到 CPU。

## 两种数据模式

- `synthetic_debug`：内置小图与矩形实例，只验证代码链路。输出永久是
  `synthetic_debug_only`、`paper_eligible=false`，指标只能叫
  `debug_mask_mean_iou` 和 `debug_precision_at_mask_iou_0_5`。
- `local_coco`：用户提供严格 COCO polygon 数据。V1 只接受非 crowd 的多边形；
  RLE、退化多边形、越界坐标、未知图片或类别都会直接拒绝。

正式 `segm` 评估只使用精确 `pycocotools==2.0.11` 的 COCOeval。缺依赖时
明确返回 `OPTIONAL_NOT_INSTALLED`，不会把自制 mask IoU 冒充 COCO AP。

## 最短调试链路

```powershell
python -m src.instseg.train --config configs/smoke.json --mode synthetic_debug --output-dir outputs/train
python -m src.instseg.evaluate --checkpoint outputs/train/checkpoint.pt --mode synthetic_debug --output-dir outputs/evaluate
python -m src.instseg.infer --checkpoint outputs/train/checkpoint.pt --mode synthetic_debug --output-dir outputs/infer
```

所有输出目录必须全新；工具不覆盖、不递归清理。真实数据不进入模板，只保存
用户明确的数据集名称、版本、公开 URI、划分和逐文件内容清单哈希。

## 结果身份与资源上限

每次训练都会生成 `data-manifest.json`，冻结真实逐文件清单、每个
`image_id` 对应的原图 SHA-256，以及允许的 `category_id` 集合。结果复核要求
每张预测和掩码都与这份清单一一对应，不能用格式正确但身份错误的 JSON 混入。

首版上限为：类别数和查询数各 256，训练尺寸不超过 256，epoch 不超过 100，
样本数不超过 128；训练总像素以及 `num_queries × image_size²` 的 mask head
规模还有组合上限。配置和 checkpoint 会在创建数据张量或模型之前检查。

## 外部来源

本模板不打包或自动下载外部源码。torchvision 固定到
`78839c2b06c83c6cfb5c4da692ffb331bbd4c4cc`，pycocotools 固定到
`ac87f5077ad6b8864c2dc5e93d14cae62d1db05a`；地址和许可证记录在
`domain-pack.json` 与 `DOWNLOADS.json`。
