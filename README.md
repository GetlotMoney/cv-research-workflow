# 智能双流系统：通用科研工作流

当前交付是可直接安装和验证的通用科研模板，不含任何个人实例。论文工作流保留为下一阶段，本阶段不会自动启动或修改论文。

## 唯一入口

首次安装或换机器时运行：

```powershell
.\tools\install-research-workflow.ps1
.\tools\install-research-workflow.ps1 -Check
```

日常双击：

```text
D:\智能双流系统\启动工作流.bat
```

页面地址：

```text
http://127.0.0.1:18082/
```

没有个人实例时，页面会以“通用模板”模式正常打开，不会偷偷创建用户目录。

## 当前能做什么

- 六个研究方向都有统一身份和状态。
- GZSL（广义零样本学习）已开放，可创建完整 Git 仓库。
- 图像分类、目标检测、实例分割、语义分割和超分辨率显示为待做，不伪装成可运行。
- GZSL 支持复现、调参、消融、创新四类实验。
- 每个实验使用独立分支和 Worktree（同一仓库的独立工作目录）。
- 运行固定使用 `dvsr_gpu` 和 CUDA，不提供 CPU 退路。
- 外来 GZSL 代码可先扫描，再由用户校正组件对应关系；一个组件可以对应多个文件。
- 数据集原文件留在用户指定的位置，Git 只保存来源、许可证、哈希和内容清单。
- debug 和 evidence 必须使用同一份配置与随机种子，避免账本与真实运行不一致。
- 只有正式 GPU Run 经过人工确认后，才允许生成科研交付包。

## 三条重要安全边界

- 外来代码的“信任确认”只表示你已经人工看过代码，不是操作系统沙箱。来源不明的代码不要运行。
- 外来框架要通过模板原版测试、中央 CUDA 设备检查和两人复核的等价结果；系统会在临时 Git 快照中重跑结果命令，手写两组相同成绩不能登记为稳定框架。
- 合成小数据只用于证明工作流能跑通，不能当成论文成绩。

## 以后怎样建立自己的个人工作流

通用模板验收完成后，由你主动执行：

```powershell
.\tools\instantiate-workspace.ps1 `
  -DisplayName "我的 CV 科研工作流" `
  -Slug "my-workspace"

.\tools\activate-workspace.ps1 -Slug "my-workspace"
```

这时才会在 `users\my-workspace\` 创建个人目录和个人 Skill；不会覆盖已有实例，也不会提前创建五个空方向仓库。随后在页面选择 GZSL，系统才创建该方向 Git 仓库。

## 科研与论文的边界

```text
代码 Framework → 四类实验 → GPU Run → 人工确认
                                      ↓ 用户主动生成
                                  科研交付包
                                      ↓ 下一阶段
                                   PaperFlow
```

PaperFlow 不直接读取正在变化的实验仓库。没有用户主动生成的交付包，科研不会触发写论文。

## 说明文档

- [日常使用](docs/USAGE.md)
- [最终目录结构](docs/architecture/LATEST_STRUCTURE.md)
- [后续工作](docs/ROADMAP.md)
- [技术版本记录](docs/TECH_STACK_HISTORY.md)
- [最终架构图](docs/architecture/RESEARCH_WORKFLOW_FINAL.html)
- [最终测试与审核记录](docs/reviews/RESEARCH_V2_VALIDATION.md)

## 许可证

通用科研代码使用 [MIT License](LICENSE)。外来代码、数据集和权重仍各自遵守其原始许可证，不能因为接入本工作流就自动变成 MIT。
