---
name: cv-experiment-workflow
description: "Use when 用户要建立或继续 CV 科研工作区、管理 GZSL Framework、运行复现/调参/消融/创新实验、接入外来 GZSL 代码或检查 GPU 科研结果。"
---

# 通用 CV 科研工作流

## 当前范围

先读取系统根目录 `config/system.json` 和 `config/directions/catalog.json`。没有激活个人工作区时只检查通用模板，不创建用户目录。当前只有 GZSL 已开放，另外五个方向必须拒绝创建仓库或运行实验。

如果用户明确决定开始个性化，才从系统根目录依次运行：

```powershell
.\tools\instantiate-workspace.ps1 -DisplayName "显示名称" -Slug "英文短名"
.\tools\activate-workspace.ps1 -Slug "英文短名"
```

当前唯一对象链：

```text
个人工作区 → GZSL 仓库 → Framework → 四类实验 → GPU Run → 人工确认
```

一个 GZSL 仓库可以包含多个完整 Framework。四类实验是：

- `reproduction`：复现；
- `tuning`：调参；
- `ablation`：消融；
- `innovation`：创新。

只有创新实验绑定 Idea。复现、调参和消融不产生子 Framework；创新只有在正式 Run 被人工确认、结论被接受并完成 Git 提交后，才能晋级为稳定子 Framework。

## 开始操作前

先用五句人话说明：

```text
我理解成什么
准备基于哪个 Framework
现在能不能开始
真正缺什么
下一步做什么
```

用户未确认运行条件时只检查状态，不创建实验或启动 GPU。

## 当前命令

下面的命令入口位于本 Skill 目录的 `scripts/rw.py`。先把工作目录切换到本 `SKILL.md` 所在目录，再运行 `python scripts/rw.py <命令> --help`；不要在系统根目录直接假定存在 `scripts/rw.py`。

| 目的 | 命令 |
|---|---|
| 创建 GZSL 仓库 | `create-gzsl-repository` |
| 创建 Idea | `create-framework-idea` |
| 创建四类实验 | `create-framework-experiment` |
| 建立独立 Worktree | `prepare-framework-worktree` |
| 登记外置数据 | `register-gzsl-dataset` |
| 执行 GPU Run | `run-framework-experiment` |
| 人工确认结果 | `confirm-framework-run` |
| 接入外来 Framework | `import-standardized-gzsl-framework` |
| 创新晋级 | `promote-framework` |
| 检查仓库 | `validate-framework-workspace` |

创建实验时必须填写 `--route-contract`，也就是先把“这次允许改什么、怎样比较才算公平”写清楚：

| 实验类型 | 必须写清的比较条件 |
|---|---|
| 复现 | 目标结果、允许误差、相同数据和评估规则 |
| 调参 | 允许改变哪些参数、搜索范围和停止条件 |
| 消融 | 关闭哪个组成部分、关闭后的行为和比较指标 |
| 创新 | 绑定的 Idea、真实基线和最低提升要求 |

## 不可绕过的规则

- 调试和正式运行都固定使用 `dvsr_gpu` 与 CUDA，不提供 CPU 退路。
- 每个实验使用独立分支和 Worktree。
- 实验编号在整个仓库中唯一；首次运行后配置与随机种子冻结，系统拒绝中途改条件。
- 原始 NPZ 不进入 Git，只登记来源、版本、许可证、SHA-256 和内容清单；当前单个 NPZ 上限为 1 GiB。
- 合成小数据只验证链路，不能当正式成绩。
- 外来代码必须先进入个人 `inbox`，完成人工审读后才由用户明确给出 `--i-trust-this-code`。这个确认不是操作系统沙箱，来源不明的代码不能运行。
- 外来 Framework 必须用相同数据和划分真正重跑；不能靠手写两份相同成绩冒充等价检查。登记前要核对命令、输出文件哈希，并由两名不同复核人确认。
- 创新结果必须用 `--innovation-outcome accepted|rejected` 明确选择接受或拒绝；只有接受、人工确认并完成 Git 提交后，才能晋级成稳定子 Framework。
- 普通 Run 不打 Tag，只有稳定 Framework 才打 Tag。
- 不自动 push、发布、下载大型数据或运行高成本任务。
- 当前工作在人工确认科研结果处结束，不启动论文写作。

需要详细步骤时读取 `references/workflow.md`；只在处理 GZSL 数据和指标时读取 `references/gzsl.md`。
