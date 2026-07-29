# x2 超分辨率方向仓库

这是 `PACK-SR-V1.0.0` 的自包含 CPU 最小基线。它不会联网、不会自动下载数据，也不把数据集或权重打进模板。

## 最小真实检查

```powershell
python -m sr.smoke --work-dir runs/smoke --device cpu
```

命令会用 TinyPixelShuffle 网络真实执行前向、L1 损失、反向传播和参数更新，然后分别从磁盘重载 `state_dict` 做评估与推理。它会输出 checkpoint、原始日志、评估 JSON、真实 RGB PNG、Run 记录和逐文件 SHA-256。合成结果固定为 `synthetic_debug_only`、`paper_eligible=false`，唯一指标名是 `debug_psnr_rgb_x2`。

正式指标 `psnr_rgb_x2` 使用本模板自定义的“完整 RGB、uint8、累计 SSE、不裁边”口径。它不是超分论文常见的 Y 通道加边界裁剪协议，未经按目标论文协议重新计算，不得直接与采用 Y-channel + border-shave 的公开结果比较。

## Windows 正式实验使用 GPU

真实数据 baseline 默认使用 CUDA；需要低成本兼容检查时，仍可显式传入
`--device cpu`，但这种 CPU Run 只能用于 debug，不能创建正式 evidence。
合成 smoke 始终固定使用 CPU。

模板不捆绑体积很大的 CUDA 版 PyTorch，也不会自动联网安装。先打开
[PyTorch 官方安装选择器](https://pytorch.org/get-started/locally/)，选择
Windows、Pip、Python 和本机驱动支持的 CUDA 版本，再执行页面给出的官方
命令。`requirements/windows-cpu.lock.txt` 只用于 CPU 小样例，不要在装好
GPU 版后用它覆盖 PyTorch。

```powershell
$env:CUDA_VISIBLE_DEVICES="0"
```

工作流会保留这个显卡选择，设置
`CUBLAS_WORKSPACE_CONFIG=:4096:8`，并在创建输出目录前真实执行一个很小的
CUDA 运算；驱动、PyTorch CUDA 构建或显卡架构不兼容时会直接停止且不生成
半成品。合成 smoke 仍只允许 CPU，正式本地数据运行才使用 `--device cuda`。

## 本地数据

```text
数据根/
├─ train/
│  ├─ HR/<id>.png
│  └─ LR/x2/<id>x2.png
└─ val/
   ├─ HR/<id>.png
   └─ LR/x2/<id>x2.png
```

所有图片必须是 RGB PNG，HR 宽高必须精确等于 LR 的两倍，ID 必须严格配对。正式运行会把通过检查的 PNG 冻结为同一份内存快照，清单、训练和评估只使用这份快照；解码前先检查图片尺寸、单图及整套像素量和压缩比。正式 PSNR 使用完整 RGB uint8，不裁边、不转 Y 通道；先在整套验证集累计 SSE 和通道值数量，再计算一次 MSE/PSNR，不能平均每张图片的 PSNR。MSE 为 0 时直接报错，不输出无穷值。

```powershell
python -m sr.train --config configs/baseline.json --data-root D:\sr-data --output-dir runs\baseline --dataset-id my-sr --dataset-version 1 --source-uri https://example.org/my-sr --manifest-sha256 <真实清单哈希> --device cuda
python -m sr.evaluate --checkpoint runs\baseline\checkpoint.pt --data-root D:\sr-data --output-dir runs\evaluate --device cuda
python -m sr.infer --checkpoint runs\baseline\checkpoint.pt --data-root D:\sr-data --split val --output-dir runs\infer --device cuda
```

`DOWNLOADS.json` 只记录 DIV2K 地址和许可证复核提醒，不携带大数据。方向包本身不会批准论文成绩；是否晋级仍由外层工作流冻结 Git、数据、环境和复核记录后判断。
