# 科研工作流 V1.0 当前目录结构

## 唯一总目录

```text
D:\智能双流系统\
├─ README.md
├─ 启动工作流.bat
├─ 停止工作流.bat
├─ app\                    本机科研页面和服务
├─ common\research\        通用科研能力
├─ config\                 当前方向、环境和激活状态
├─ docs\                   当前说明、验证和历史入口
├─ tests\                  系统测试
├─ tools\                  安装、实例化和启停脚本
└─ users\                  用户主动创建的个人工作区
```

## 三层关系

```text
通用系统
└─ 个人工作区
   └─ GZSL 方向仓库
```

通用系统不包含个人数据。个人工作区不会在安装时自动创建。方向仓库只在用户选择已开放方向后创建。

## 个人工作区

```text
users\<slug>\
├─ workspace.json
├─ REPOSITORY_INDEX.json
├─ SKILL.md
├─ repositories\
├─ deliveries\
├─ inbox\
├─ materials\
└─ .runtime\
```

个人论文目录和文献库不在科研阶段预建。

## GZSL 仓库

```text
repositories\<repo>\
├─ .git\
├─ SKILL.md
├─ workflow_adapter.py
├─ gzsl\
├─ configs\
├─ tests\
└─ .experiment-workflow\
   ├─ repository.json
   ├─ frameworks\
   ├─ ideas\
   ├─ runs\
   └─ worktrees\
```

一个仓库可以包含多个完整 Framework。每个 Framework 下面可以创建四类实验。

## 实验与 Git

```text
Framework
├─ reproduction   复现
├─ tuning         调参
├─ ablation       消融
└─ innovation     创新
```

- 每个实验有独立编号、分支和 Worktree。
- 普通 Run 不打 Tag。
- 稳定基础 Framework、通过接入的外来 Framework、晋级后的创新 Framework 才打 Tag。
- 复现、调参和消融不产生子 Framework。
- 只有被接受并提交的创新实验可以晋级。

## 数据与运行

- 原始数据和大型权重留在仓库外。
- Git 保存来源、许可证、版本、哈希和内容清单。
- 所有运行固定使用 `dvsr_gpu` 与 CUDA。
- 正式结果保存代码提交、配置、随机种子、数据身份、日志、指标和产物哈希。
- 没有人工确认的结果不能成为正式科研交付。

## 阶段边界

当前目录结构只负责科研。论文写作系统是独立阶段，不读取正在变化的实验仓库。
