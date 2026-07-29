# 代码模板与出处规范

## 先分清两类模板

系统内置的模板分成两层，用途不同：

| 位置 | 是什么 | 能不能直接跑实验 |
|---|---|---|
| `assets/templates/` | 四类研究模块小骨架，用来说明一个新机制应从哪里接入、关闭后怎样回到基线 | 不能；它们不是完整训练项目 |
| `assets/domain-packs/` | 六套可新建 Git 仓库的方向包：图像分类、目标检测、广义零样本、实例分割、语义分割和超分辨率 | 能；每套都带训练、评估、推理、配置、测试和工作流 Adapter |

六套方向包遵守同一组使用边界：

- 真实数据的 baseline（基础实验配置）默认使用 CUDA，也就是 NVIDIA GPU；如果 GPU 不可用就明确报错，不会偷偷退回 CPU。
- CPU 只跑内置合成小样例或低成本兼容调试，用于检查 `train → evaluate → infer` 能否接通；所有真实正式 evidence 都必须在配置和环境里同时明确使用 CUDA，CPU Run 不能当论文成绩。
- 大型数据集和预训练权重不打进模板包，只在仓库根目录的 `DOWNLOADS.json` 中给出下载地址、许可证和人工准备步骤。
- 方向包里的开源来源、许可证和本项目新增代码边界记录在各仓库根目录的 `LICENSE`、README 与 manifest 中；来源不清或许可证不兼容时不能复制代码。

实际开始一个方向项目时，先用对应的 `domain-pack` 新建仓库；需要加入自己的创新机制时，再参考 `assets/templates/` 的小骨架确定接入点。比如做目标检测时，先从 `det` 方向包得到可运行仓库，再把新的特征融合模块接到模型内部，而不是拿 `fusion-gate` 小骨架冒充完整检测项目。

## 内置代码骨架不是训练项目

`assets/templates/` 下的内置四组代码骨架只演示 feature adapter（特征适配）、fusion gate（融合门）、auxiliary loss（辅助损失）和 sampler/data view（采样/数据视图）怎样声明接口、关闭行为与契约。启用尚未实现的研究分支时，它们会抛出 `NotImplementedError`。

因此，这些骨架不是可直接训练的完整 CV 项目模板。真实实例必须根据自己的数据接口、模型、训练步骤、评估口径、checkpoint 和指标独立建立项目 Template，再接入一个研究 Module；不得把结构骨架登记成已经可训练的科学基线。

## 为什么从干净模板开始

创新实验会改变机制。如果一直在旧 Trial（创新试验方案）上叠加修改，代码会混入历史残留，无法判断结果来自哪一项变化。因此每个新创新都必须调用 `new-trial`，从经过摘要校验的干净模板派生新的 Trial + CodeAsset（代码资产）；不要复制旧 Trial，也不要在冻结后的 CodeAsset 上继续改。

调参、消融和复现不必机械创建新代码：

| 实验类型 | 典型 target（目标对象） | 代码原则 |
|---|---|---|
| `innovation`（创新） | 新 Trial | 从干净模板派生，只实现当前机制 |
| `tune`（调参） | active Version、Trial 或已完成 Attempt | 固化配置差异，通常不改结构代码 |
| `ablation`（消融） | Trial 或已完成 Attempt | 关闭/替换一个成分，保持其他条件可比 |
| `reproduction`（复现/确认） | Version、Trial 或已完成 Attempt | 忠实复用已声明代码与条件，不偷加改进 |

## 从参考代码建立基础模板

用户提供多份论文开源代码并要求“构造模板”时，默认目标不是合并仓库，也不是选一份代码改名后充当模板。先比较它们的数据输入、训练循环、评估口径、配置方式和模块插入位置，提炼共同规则；再独立编写本项目自己的基础模板。

基础模板固定数据划分、训练/验证/测试含义、seed（随机种子）、checkpoint（训练检查点）选择、指标计算、日志和结果格式。研究机制放进少量真实插入位置，例如数据视图、特征处理、视觉语义交互、训练目标或预测校准。不要为了假想中的未来模块预埋大量通用 hook（钩子，即运行到某一步时调用额外代码的入口）。

模板定型后，代码路线切换为“接入单个模块”：

1. 默认只新增当前模块、配置映射和测试；简单模块优先一个 `.py`，复杂模块可用小目录，但只暴露一个入口。
2. 模块必须声明输入、输出、插入位置和 disabled behavior（关闭行为）；关闭后应回到基础模板基线，即训练与评估路径、配置和口径恢复，不要求随机训练得到的指标逐位相同。
3. 默认不得顺手改公共训练循环、评估指标或其他模块。确实需要改变固定部分时，建立新的 Template 修订并重新跑基线。
4. 参考仓库、论文和具体符号仍要登记出处。只提炼规则时记为 `inspiration`；复制或改写实现时必须满足后文许可证要求并写清边界。

第一版只证明“基础模板可以独立运行”和“一个测试模块可以插入、关闭、替换”，不同时接入多篇论文的真实模块。

## 模板怎么找

项目已有真实母版时，先在已锁定 commit 的干净本地 Git 仓库中准备严格 manifest，再调用 `register-template --project --manifest --code-root`。来源分析、clone、worktree 准备和真实代码集成是需用户授权的 Agent 步骤；manifest 登记、引用校验、账本写入和 `activate-version` 是确定性脚本步骤。脚本核对 HEAD、工作树状态和最多 64 个列出文件的 SHA-256，只原子登记 `TPL-xxxx.json`，不会 clone、递归复制或执行来源代码。需要框架图时调用 `render-template --project --template --output`，输出放在控制面之外。

manifest 必须如实记录来源身份、定位、commit、许可证、文件、symbols（符号）和使用方式。`copied`/`adapted` 只接受下文兼容许可证；`inspiration`/`reimplementation` 也必须写明许可证与说明。没有公开代码时明确记录论文重实现或原创实现，不得伪造公开仓库。

`feature-adapter`、`fusion-gate`、`auxiliary-loss`、`sampler-data-view` 四个内置 family 仅用于演示/自测，以及项目没有自己的母版时的兼容回退；它们不是固定科学分类，Idea 不必属于其中任何一类。下表描述的是随 Skill 发布的模板资产，不是研究问题的本体分类。

使用内置模板时，先读每个 `assets/templates/*/template.json`（模板 manifest，即机器清单），再按 Idea 的实际接入位置选择：

| family | attachment point | 适用变化 |
|---|---|---|
| `feature-adapter` | `feature-output` | 特征输出后的轻量变换 |
| `fusion-gate` | `fusion-input` | 多路信息融合前的门控 |
| `auxiliary-loss` | `training-objective` | 训练目标中的辅助损失 |
| `sampler-data-view` | `data-loader` | 采样或数据视图变化 |

`new-trial` 会按 family、接入点、`adapter.json` 能力、接口/依赖/契约存在性和 provenance（出处记录）状态做确定性筛选，并记录模板 manifest 摘要与选择原因。项目已有母版或专用模板时，应以项目真实接口和科学机制为准；当前内置 family 找不到精确匹配就停止，不要为迁就四类回退模板而扭曲 Idea，也不要拿“最像的”模板冒充。

演示 family 仍提供可直接编辑的 `module.py`、`test_contract.py`。正式 `--template TPL-xxxx` 只生成默认关闭的通用小骨架和 `composition.json`，不复制母版；真实实现留在外部代码仓库，提交后用 `sync-code-asset --code-root ... --relative-path ...` 绑定精确 commit、路径和摘要。该命令不创建 worktree、不复制或执行代码；控制目录内 `framework.html` 是 CodeAsset 账本骨架/边界派生图，即使绑定 `external_code_ref` 也不代表外部真实实现的代码结构，且禁止手改。

## 论文、官方代码与 license（许可证）

论文提供科学机制证据，代码仓库提供实现证据，两者不能互相代替。

- 论文：记录题名、作者、年份、venue（发表场所）、URL、具体 locator（页码/章节/公式/图）和它支持的 claim（主张）。`paper-derived` 必须至少有一篇已核验的 primary mechanism（主要机制）论文。
- 官方代码：记录仓库 URL、精确 40 位 commit、文件、symbols（符号/函数/类）、使用方式与 license。
- `copied` 或 `adapted` 只接受当前 allowlist：MIT、Apache-2.0、BSD-2-Clause、BSD-3-Clause、ISC，并要求 `license_compatibility: compatible`。未知或冲突时停止复制，保留 unresolved question（未决问题）。
- `inspiration` 表示只参考思路，不能声称复制了实现。
- `what_is_copied`、`what_is_adapted`、`what_is_new` 要分别写清边界；不要把自己的改动归给论文，也不要把论文机制冒充原创。

需要论文或外部代码时，准备 provenance JSON，再将其传给 `new-trial --provenance-file`。字段结构以 `scripts/workflow_core/templates.py` 的校验器为准。下面只展示 `paper-derived` 字段布局，`papers` 为空，因此**不可提交**：

```json
{
  "schema": "cv-experiment-workflow.provenance.v1",
  "scientific_claim_scope": "paper-derived",
  "papers": [],
  "code_sources": [],
  "what_is_copied": [],
  "what_is_adapted": [],
  "what_is_new": [],
  "unresolved_questions": []
}
```

不要为了通过校验伪造 `verification_status: verified`、locator 或 supported claim。用户必须实际打开论文核验；`paper-derived` 至少填写一篇真实、已核验的 primary paper 后才能提交。

下面是可由真实校验器接受的 `original-hypothesis` 最小示例。它只声明“用户原创、尚待实验验证”，没有冒充论文或代码来源：

```json
{
  "schema": "cv-experiment-workflow.provenance.v1",
  "scientific_claim_scope": "original-hypothesis",
  "papers": [],
  "code_sources": [],
  "what_is_copied": [],
  "what_is_adapted": [],
  "what_is_new": ["用户提出的特征适配假设，尚待实验验证"],
  "unresolved_questions": []
}
```

三种 claim scope（主张范围）：

- `none`：纯结构模板，不声称科学机制来源。
- `paper-derived`：机制来自论文，必须补全已核验主要论文。
- `original-hypothesis`：用户原创假设，必须明确 `what_is_new`，仍要标注参考与代码来源。

## 冻结与检查

在 `new-attempt` 前完成代码、契约、`sync-code-asset` 和 `validate`。Attempt（一次冻结运行）会冻结 target 事实、基础 Version、CodeAsset 顺序、命令、配置与一个 seed（随机种子）。冻结后若要再创新，创建新的 Trial；若只是运行同一冻结方案，创建新的 Attempt。Result（结果）仅引用账本外制品，不把 checkpoint、日志或数据复制进 `.experiment-workflow/`。
