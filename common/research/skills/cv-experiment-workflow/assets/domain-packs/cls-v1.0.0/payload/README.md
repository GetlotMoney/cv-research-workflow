# 图像分类方向仓库

这是 `PACK-CLS-V1.0.0` 的可运行分类基线。它只读取本地图片，不会联网、不会自动下载数据，也不会把数据集或权重塞进 Git。

## 先跑最小检查

当前 Python 环境已经安装 `torch`、`numpy` 和 `Pillow` 后，在仓库根目录运行：

```powershell
python -m cls.smoke --work-dir runs/smoke --device cpu
```

它会真实训练一个很小的卷积网络，并生成 `checkpoint.pt`、`metrics.json`、`predictions.json` 和 `run.json`。这批内置数据只用来证明代码能跑，`run.json` 固定写明 `synthetic_debug_only` 和 `paper_eligible=false`，不能拿去写论文成绩。

## Windows 正式实验使用 GPU

真实数据 baseline 默认使用 CUDA；需要低成本兼容检查时，仍可显式传入
`--device cpu`，但这种 CPU Run 只能用于 debug，不能创建正式 evidence。
合成 smoke 始终固定使用 CPU。

模板不捆绑体积很大的 CUDA 版 PyTorch，也不会替你联网安装。先打开
[PyTorch 官方安装选择器](https://pytorch.org/get-started/locally/)，选择
Windows、Pip、Python 和本机驱动支持的 CUDA 版本，然后执行页面给出的官方
命令。`requirements/windows-cpu.lock.txt` 只用于 CPU 小样例；装好 GPU 版后
不要再用它覆盖 PyTorch。

如需只使用第 0 张可见显卡，可先在 PowerShell 设置：

```powershell
$env:CUDA_VISIBLE_DEVICES="0"
```

工作流会把 `CUDA_VISIBLE_DEVICES` 原样传给训练进程，并在真正创建输出前设置
`CUBLAS_WORKSPACE_CONFIG=:4096:8`，再执行一个很小的 CUDA 运算。驱动、PyTorch
CUDA 构建或显卡架构不兼容时会直接停止，不留下半成品。

## 准备自己的图片

按类别建立子目录：

```text
本地数据/
├─ train/
│  ├─ cat/
│  └─ dog/
└─ val/
   ├─ cat/
   └─ dog/
```

然后依次运行：

```powershell
python -m cls.train --config configs/baseline.json --data-root D:\my-data --output-dir runs/baseline --device cuda
python -m cls.evaluate --checkpoint runs/baseline/checkpoint.pt --data-root D:\my-data --split val --output-dir runs/evaluate --device cuda
python -m cls.infer --checkpoint runs/baseline/checkpoint.pt --input D:\my-data\val\cat\001.jpg --output runs/predictions.json --device cuda
```

指标固定为 `top1_accuracy`（第一候选正确率）、`top5_accuracy`（最多取五个候选；类别少于五个时按真实类别数计算）和 `macro_f1`（各类别 F1 的平均值）。正式科研证据仍要交给外层工作流冻结 Git、配置、数据身份、环境和输出摘要，本仓库不会自行宣布结果可用于论文。

## 大文件和下载

数据、预训练权重和训练输出都不随模板分发。可选公开数据与依赖只在
`DOWNLOADS.json` 记录固定版本、固定发布页和许可证来源；模板不会自动联网，
是否下载由用户自己决定。CIFAR-10 官方页面没有声明统一许可证，使用前仍需
人工核验数据条款；页面类地址的 `sha256`（文件摘要）固定为 `null`，只有将来
列出可直接下载的单个制品时才允许填写并强制核对 64 位 SHA-256。
