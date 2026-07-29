# 通用 CV 科研能力

这是科研工作流 V1.0 的共用源码。用户平时从系统根目录 `README.md` 和本机页面进入，不需要直接操作本目录。

## 当前唯一对象

```text
GZSL 仓库
→ Framework
→ 复现 / 调参 / 消融 / 创新
→ GPU Run
→ 人工确认
```

一个 GZSL 仓库可以管理多个完整 Framework。只有创新实验可以绑定 Idea，只有被接受、确认并提交的创新可以晋级为稳定子 Framework。

## 当前方向

- GZSL：已开放。
- 图像分类：尚未开放。
- 目标检测：尚未开放。
- 实例分割：尚未开放。
- 语义分割：尚未开放。
- 超分辨率：尚未开放。

方向状态以系统根目录 `config/directions/catalog.json` 为准。

## 主要入口

- `skills/cv-experiment-workflow/SKILL.md`：当前通用 Skill。
- `skills/cv-experiment-workflow/scripts/rw.py`：确定性命令入口。
- `skills/cv-experiment-workflow/assets/domain-packs/`：方向材料。
- `tests/`：科研能力测试。

## 固定边界

- 使用 `dvsr_gpu` 和 CUDA，没有 CPU 退路。
- 原始数据、大型权重和正式日志不放进通用模板。
- 外来代码必须先人工审读，再执行标准化和等价检查。
- 不自动 push、发布、下载大型数据或执行高成本训练。
- 当前阶段不启动论文写作。

当前用法见系统根目录 `docs/USAGE.md`。历史记录见系统根目录 `docs/HISTORY.md`。
