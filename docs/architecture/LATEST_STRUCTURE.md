# 通用科研工作流最终目录结构

## 唯一总目录

```text
D:\智能双流系统\
├─ 启动工作流.bat
├─ 停止工作流.bat
├─ README.md
├─ AGENTS.md
├─ app\                         科研页面后台
├─ web\                         科研页面
├─ config\
│  ├─ system.json              可上传的通用配置
│  ├─ environment.example.json 环境示例
│  └─ directions\catalog.json  六方向唯一状态源
├─ tools\                       安装、启动、实例化和激活
├─ tests\                       根入口验收
├─ docs\                        正式说明、架构和审核记录
├─ common\
│  ├─ research\                通用科研源码和方向包
│  └─ paperflow\               下一阶段使用，本阶段不修改
├─ users\
│  └─ .gitkeep                 当前没有正式个人实例
└─ .runtime\                   测试和运行时临时文件
```

本机私有文件 `config/environment.local.json` 和以后产生的 `users\<slug>\` 不进入 GitHub。

## 六方向唯一状态

```text
config\directions\catalog.json
├─ 图像分类      pending
├─ 目标检测      pending
├─ 实例分割      pending
├─ 语义分割      pending
├─ 超分辨率      pending
└─ GZSL          ready
```

配置、页面、实例化工具和服务都读取这一份文件，不再各写一套方向名单。

## 从通用模板到个人仓库

```text
通用模板
  ↓ 用户主动实例化
users\<slug>\
  ↓ 用户选择 GZSL
repositories\<repo>\
  ↓ 四类实验
独立分支 + 独立 Worktree + GPU Run
  ↓ 人工确认
科研交付包
```

个人实例不复制通用代码，只记录个人身份、仓库索引和本地材料。未选择方向时不创建仓库。

## GZSL 仓库内部

```text
repositories\<repo>\
├─ .git\
├─ configs\
├─ gzsl\
├─ tests\
├─ workflow_adapter.py
└─ .experiment-workflow\
   ├─ repository.json
   ├─ idea-tree\
   └─ frameworks\
      └─ gzsl-base\
         ├─ framework.json
         └─ experiments\
            ├─ reproduction\
            ├─ tuning\
            ├─ ablation\
            └─ innovation\
```

普通 Run 不打 Tag。稳定基础框架、验证通过的外来框架、创新晋级后的子 Framework 才打 Tag；只有创新晋级会生成 `framework.html`。

四类实验的编号在整个仓库内分别递增，不按 Framework 重新从 001 开始。debug 第一次运行时会冻结配置和随机种子，后续 evidence 必须保持一致。

## 数据与外来代码

```text
真实 NPZ（仓库外）
  ↓ 内容检查
来源 + 版本 + 许可证 + SHA-256 + 内容清单（进入 Git）
  ↓ GPU Run
日志 + S/U/H + 产物哈希（进入实验账本）
```

外来代码只允许从当前个人实例的 `inbox\frameworks\` 草稿进入稳定 Framework。验证会真实执行草稿代码，因此信任勾选不是沙箱；它只适用于已经人工审读的可信代码。稳定登记还要绑定代码提交、同一数据清单、运行命令、输出文件哈希和两名复核人。

## 与 PaperFlow 的唯一连接

```text
已确认 Run → 用户主动生成科研交付包 → PaperFlow 下一阶段导入
```

PaperFlow 不读取活动实验目录，科研服务也不创建论文。
