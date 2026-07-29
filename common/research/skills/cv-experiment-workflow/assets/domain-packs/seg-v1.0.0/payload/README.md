# 语义分割方向仓库

这是 `PACK-SEG-V1.0.0` 的自包含 CPU 最小基线。它不会联网或自动下载数据，也不随模板携带数据集和权重。

## 先验证代码链

```powershell
python -m seg.smoke --work-dir runs/smoke --device cpu
```

这条命令会真实执行前向计算、反向传播和参数更新，再从磁盘重新载入 `state_dict` 做评估和推理。输出包含 checkpoint、原始日志、评估 JSON、真实 PNG mask、Run 记录和 SHA-256 清单。合成结果始终标成 `synthetic_debug_only`、`paper_eligible=false`，指标名也只有 `debug_mean_iou`，不能写进论文。

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

## 本地数据格式

```text
数据根/
├─ train/
│  ├─ images/<id>.png
│  └─ masks/<id>.png
└─ val/
   ├─ images/<id>.png
   └─ masks/<id>.png
```

图片必须是 RGB PNG；mask 必须是 P/L PNG，类别 ID 为 `0..C-1`，忽略像素固定为 `255`。图片与 mask 的相对 ID、原始尺寸必须完全匹配；mask 缩放只使用最近邻。数据身份使用 `cv-experiment-workflow.dataset-identity.v1`，下载来源必须是 HTTPS URL，真实文件清单会重新计算 SHA-256。正式运行会把通过检查的 PNG 冻结为同一份内存快照，清单和训练只使用这份快照；解码前先检查图片尺寸、单图及整套像素量和压缩比，拒绝超预算输入。

正式评估的 `mean_iou` 从整套验证集累计混淆矩阵后计算。某类别 union 为 0 时写 `null` 并从平均值省略；只在预测或只在真值出现的类别仍计入。全是 ignore 的验证集直接报错。

```powershell
python -m seg.train --config configs/baseline.json --data-root D:\seg-data --output-dir runs\baseline --dataset-id my-seg --dataset-version 1 --source-uri https://example.org/my-seg --manifest-sha256 <真实清单哈希> --device cuda
python -m seg.evaluate --checkpoint runs\baseline\checkpoint.pt --data-root D:\seg-data --output-dir runs\evaluate --device cuda
python -m seg.infer --checkpoint runs\baseline\checkpoint.pt --data-root D:\seg-data --split val --output-dir runs\infer --device cuda
```

正式科研证据是否可进入论文，仍由外层科研工作流根据 Git、环境和复核记录判断；方向包自身不会批准论文结果。
