# PACK-DET-V1.0.0

这是一个可直接实例化的目标检测代码仓库模板。它自带一个很小的
fixed-query（固定数量候选框）PyTorch 检测器，能完成真正的前向传播、
反向传播、优化器更新、磁盘 checkpoint 重载、评估和推理。合成冒烟固定
使用 CPU；真实数据 baseline 默认使用 CUDA。`local_coco` 可以显式选择
`cpu` 做兼容 debug，但正式 evidence 必须选择 `cuda`。选择 `cuda`
前，用户必须预先安装可用的 CUDA 版 PyTorch；模板不会自动安装，也不会
偷偷降级到 CPU。

## 两种数据模式

- `synthetic_debug`：模板自己生成小图片，只用于证明代码能跑。所有结果永久标为
  `synthetic_debug_only` 与 `paper_eligible=false`，指标名带 `debug_`，
  不能作为论文成绩。
- `local_coco`：读取用户自己准备的严格 COCO bbox 数据。模板不打包也不下载大数据，
  只在配置中保存数据集名称、版本、公开来源地址和内容清单哈希。

正式 COCO bbox 评估只允许精确的 `pycocotools==2.0.11`。没有安装时，
预检会返回 `OPTIONAL_NOT_INSTALLED`，不会悄悄换成近似 AP。

## 最短调试链路

```powershell
python -m src.det.train --config configs/smoke.json --mode synthetic_debug --output-dir outputs/train
python -m src.det.evaluate --checkpoint outputs/train/checkpoint.pt --mode synthetic_debug --output-dir outputs/evaluate
python -m src.det.infer --checkpoint outputs/train/checkpoint.pt --mode synthetic_debug --output-dir outputs/infer
```

每次输出目录必须全新，工具不会覆盖或递归清理已有目录。

## 正式数据最少信息

用户需要明确提供 `data_root`、`annotation_file`、`dataset_id`、`version`、
`source_uri` 和 `split`。`source_uri` 必须是公开 URI，不能拿本机路径冒充。
图片必须是普通 RGB 文件；目录、父目录和文件不能是 symlink、junction 或
reparse point。读取时会在打开前后核对文件身份，防止读取过程中被替换。

## 结果身份与资源上限

每次训练都会生成 `data-manifest.json`。它把本次真实使用的逐文件清单、
每个 `image_id` 对应的原图 SHA-256，以及允许的 `category_id` 集合一起冻结。
结果复核要求预测与这些样本一一对应，不能只让 JSON 格式看起来正确。

首版上限为：类别数和查询数各 256，训练尺寸不超过 1024，epoch 不超过 100，
样本数不超过 128；训练总像素和模型输出还有组合上限。配置和 checkpoint
会在创建数据张量或模型之前执行同一套检查。

## 外部来源

本模板没有复制 torchvision 或 pycocotools 源码。固定来源、commit 与许可证
写在 `domain-pack.json` 和 `DOWNLOADS.json`。当前固定 torchvision commit 为
`78839c2b06c83c6cfb5c4da692ffb331bbd4c4cc`，pycocotools commit 为
`ac87f5077ad6b8864c2dc5e93d14cae62d1db05a`；这里只保留地址，不自动下载。
