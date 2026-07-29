# 广义零样本学习方向仓库

这是 `PACK-GZSL-V1.1.1` 的自包含基线。它使用线性视觉到属性映射，真实执行前向、MSE、反向传播和参数更新；评估与推理分别从磁盘重载 `state_dict`。本仓库固定使用 Conda 环境 `dvsr_gpu` 和 CUDA，合成调试与真实数据实验都在 GPU 上运行。它不联网、不自动下载数据，也不做 calibrated stacking（校准堆叠）或其他分数校准。

## 最小检查

```powershell
<GPU_PYTHON> -m gzsl.smoke --work-dir runs/smoke --device cuda
```

合成结果始终是 `synthetic_debug_only`、`paper_eligible=false`，只输出三个 `debug_*` 指标，不能变成论文成绩。

## 严格 NPZ 合同

NPZ 必须在 `allow_pickle=False` 下读取，并精确包含：

- `features`: float32 `[N（样本数量）, D（视觉特征维度）]`
- `labels`: int64 `[N（样本数量）]`
- `attributes`: float32 `[C（类别数量）, A（语义属性维度）]`
- `class_ids`: int64 `[C（类别数量）]`
- `seen_class_ids`、`unseen_class_ids`: 互斥且完整覆盖 `class_ids`
- `train_indices`、`test_seen_indices`、`test_unseen_indices`: 互斥且覆盖全部样本

类别与属性行的唯一映射是：`class_ids[index]` 对应 `attributes[index]`。训练划分只能含 seen 类；seen/unseen 测试划分必须分别覆盖所有声明类别。object dtype、pickle、NaN/Inf、重复 ID、缺失类别、越界或重复索引全部拒绝。正式运行只读取一次 NPZ，并让清单、训练和评估共享同一份冻结 bytes；在 `numpy.load` 前先检查 ZIP 成员、NPY header、压缩比以及单项和总展开体积，拒绝压缩炸弹与超预算数组。

正式分类候选是 `seen ∪ unseen`，用余弦相似度且不校准。`S` 和 `U` 都先逐类算准确率再做类别平均，`H=2*S*U/(S+U)`；当 `S+U=0` 时明确定义 `H=0`。

```powershell
python -m gzsl.train --config configs/baseline.json --data-path <本地数据文件> --output-dir runs\baseline --dataset-id my-gzsl --dataset-version 1 --source-uri https://example.org/my-gzsl --manifest-sha256 <真实清单哈希>
python -m gzsl.evaluate --checkpoint runs\baseline\checkpoint.pt --data-path <本地数据文件> --output-dir runs\evaluate --device cuda
python -m gzsl.infer --checkpoint runs\baseline\checkpoint.pt --data-path <本地数据文件> --output-dir runs\infer --device cuda
```

`DOWNLOADS.json` 只给 AwA2 地址和许可证复核提醒，不打包大数据。方向包不会自行批准论文结果。
