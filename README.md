# 智能双流系统：科研工作流 V1.0

这是当前唯一的通用 CV 科研工作流。它不含个人实例，安装后先显示通用模板；只有你主动实例化，系统才创建个人工作区。

## 现在从哪里开始

第一次安装或换机器：

```powershell
Set-Location 'D:\智能双流系统'
.\tools\install-research-workflow.ps1
.\tools\install-research-workflow.ps1 -Check
```

日常使用只需双击：

```text
D:\智能双流系统\启动工作流.bat
```

页面地址：

```text
http://127.0.0.1:18082/
```

## 当前能做什么

- 六个研究方向都有固定身份。
- 只有 GZSL（广义零样本学习）已经开放。
- 图像分类、目标检测、实例分割、语义分割和超分辨率尚未开放。
- 一个 GZSL 仓库可以管理多个完整 Framework（代码框架）。
- 每个 Framework 支持复现、调参、消融和创新四类实验。
- 每个实验使用自己的 Git 分支和 Worktree（独立工作目录）。
- 调试和正式运行都必须使用 `dvsr_gpu` 与 CUDA，没有 CPU 退路。
- 正式结果必须经过人工确认。

## 建立自己的工作区

你决定开始个性化后再运行：

```powershell
.\tools\instantiate-workspace.ps1 `
  -DisplayName "我的 CV 科研工作流" `
  -Slug "my-workspace"

.\tools\activate-workspace.ps1 -Slug "my-workspace"
```

激活后重新启动工作流。个人目录只会建立在：

```text
D:\智能双流系统\users\my-workspace
```

随后在页面选择 GZSL 并填写仓库名，系统才会创建第一个方向仓库。

## 当前唯一科研主线

```text
个人工作区
→ GZSL 仓库
→ Framework
→ 复现 / 调参 / 消融 / 创新
→ GPU Run
→ 人工确认
```

本仓库到已确认科研结果为止，不启动论文写作。

## 当前文档

- [完整使用说明](docs/USAGE.md)
- [当前目录结构](docs/architecture/LATEST_STRUCTURE.md)
- [当前状态与后续工作](docs/ROADMAP.md)
- [当前测试结果](docs/reviews/CURRENT_VALIDATION.md)
- [最终架构图](docs/architecture/RESEARCH_WORKFLOW_FINAL.html)

过去的修改记录统一从[历史入口](docs/HISTORY.md)查看，历史文件不能作为当前操作说明。

## 许可证

通用科研代码使用 [MIT License](LICENSE)。外来代码、数据集和权重继续遵守各自许可证。
