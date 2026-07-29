# 通用 CV 科研工作流

这里保存所有用户共用的科研规则、Skill 和方向模板，不保存任何人的真实实验历史。

## 它在完整系统中的位置

```text
common/research/                         通用科研模板
        ↓ 实例化
users/<user>/                            某个人的工作流
        ↓ 新建方向仓库
users/<user>/repositories/<repository>/ 一个独立 Git 代码仓库
```

统一入口位于整个系统根目录：

```text
<统一系统根目录>\启动工作流.bat
```

用户也可以直接在 Codex 中描述要做的事情，不必手填网页。

## 当前完成范围

当前只完整实现 GZSL（广义零样本学习）。图像分类、目标检测、实例分割、语义分割和超分辨率保留在系统根目录 `docs/ROADMAP.md`，不能冒充已经可用。六方向状态只读取系统根目录的 `config/directions/catalog.json`。

每个 GZSL 仓库包含：

- 完整代码框架；
- 数据、训练、评估、推理和 `S/U/H` 指标接口；
- 复现、调参、消融和创新四类实验；
- Git 分支、Worktree 和稳定框架 Tag；
- GPU Run、原始日志、指标、产物哈希和人工确认；
- 用户主动生成的 PaperFlow 科研交付包。

## Framework、Idea 和实验的关系

Framework 是一套能独立运行的完整代码，不依赖 Idea 才存在。

- 复现：证明某个框架可以按既定配置重现。
- 调参：改变学习率、批量大小等参数，不产生新框架。
- 消融：关闭或替换已有部分，不产生新框架。
- 创新：把一个 Idea 落到代码中；只有验证成功后，才可以产生子 Framework。

Idea 记录问题、机制、来源、父子关系、改了哪个 Framework、在哪些实验中使用、结果状态和下一步。用户界面不再单独维护 Codebase、Module、Trial 或 Attempt 账本。

## 外来代码怎么接入

外来论文代码或用户自己的代码先克隆进一个独立仓库，再由用户和 AI 一起解释其结构。工作流不要求“一个模块只能有一个 Python 文件”，而是要求能明确找到：

- 数据与已见类/未见类划分；
- 模型主体及模块关系；
- 一个或多个损失；
- 训练入口；
- 评估与推理入口；
- `S/U/H` 指标；
- 配置入口；
- 连接工作流的 Adapter。

整理前后必须使用相同数据划分，并用实际输出核对 `S/U/H`。核对不通过时只能继续整理，不能登记成稳定 Framework。

## Git 规则

- 一个研究方向可以使用一个仓库管理多个完整 Framework。
- 新实验从所选 Framework 的稳定 Tag 建分支，并使用独立 Worktree。
- 复现、调参和消融只记录实验分支与 Run，不生成子 Framework。
- 创新实验成功晋级后才创建子 Framework 和稳定 Tag。
- 普通 Run 不打 Tag。
- 工作流不会自动 `push`、发布或创建远端仓库。

## GPU 与数据

科研运行固定使用本机生成的 `config/environment.local.json` 所记录的 Conda 环境 `dvsr_gpu`，不提供 CPU 退路。大型数据和权重不随模板分发，只记录下载地址、版本、许可证和 SHA-256。GZSL 原始 NPZ 留在仓库外，单文件当前上限为 1 GiB，并保留压缩成员数与展开总量限制。

外来代码会真实执行测试和 CUDA 探针，人工确认信任不是操作系统沙箱。只有已经审读、来源可信的代码才可执行；稳定登记还要绑定同一数据清单、运行命令、代码提交、输出哈希和两名不同复核人。

## 什么时候进入 PaperFlow

科研和论文是两个独立板块。只有用户决定写论文时，才从一个已人工确认的 Run 主动生成 `cv-research-handoff/v2` 科研交付包。PaperFlow 接收该包后，再选择方法卡、参考论文和证据进行六章写作。

没有人工确认的结果不能进入交付包；外部论文也不能替当前研究证明方法或实验数字。

## 主要位置

- 通用 Skill：`skills/cv-experiment-workflow/`
- GZSL 方向模板：`skills/cv-experiment-workflow/assets/domain-packs/gzsl-v1.1.1/`
- 通用工具：`tools/`
- 自动测试：`tests/`
- 个人实例和实际仓库：系统根目录 `users/`

技术版本和历史决定继续保存在 `docs/TECH_STACK_HISTORY.md`，旧编号只用于回查，不代表当前还有第二套活动系统。
