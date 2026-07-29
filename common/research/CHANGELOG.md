# 变更日志

## [1.5.0] - 2026-07-27（未安装的开发候选）

- 增加 `UI-CONSOLE-V2.0.0` 统一入口，可在同一页面新建/打开科研项目、登记来源与 Idea、创建六方向 Codebase、创建 Task/Run、冻结 Research Package，并在包完成后打开 PaperFlow。
- 增加 DET、CLS、SEG、INSTSEG、SR、GZSL 六套独立仓库模板；内置合成样例和低成本兼容检查可使用 CPU，六份真实 baseline 默认使用 CUDA，所有真实正式 evidence 强制使用 CUDA。
- CUDA 入口不只检查“能看到显卡”，还会在创建输出前真实执行极小矩阵运算并同步；PyTorch 与显卡架构不兼容时零产物失败，不会偷偷退回 CPU。
- 正式 evidence 只接受六个受信任的内置方向模板；中央执行层把模板版本、Adapter/Python 摘要、CUDA 设备和真实算子结果写入冻结环境、指纹与输出封存。自定义 Adapter 不能靠自报 `cuda` 获得论文资格。
- 六个方向的正式指标定义由模板固定注入并冻结，用户配置不能覆盖；缺少、增加或篡改定义都会拒绝。每个正式 Run 还必须封存可核对的原始日志与全部结果文件。
- 已在 RTX 5070 Ti、`torch 2.11.0+cu128` 下完成 DET、CLS、SEG、INSTSEG、SR、GZSL 六个方向的真实 CUDA 训练、评估、推理和 checkpoint 重载；DET、INSTSEG 的精确 `pycocotools==2.0.11` 只安装在可删除的隔离临时环境，未污染原 Conda 环境。
- 所有本轮 GPU 真机验证只使用微型本地数据并固定 `paper_eligible=false`，不能作为论文成绩或模型效果结论。
- 正式 Run 绑定 Codebase 的 branch、commit、可选 tag、配置、数据和评估身份；debug Run 不能晋级为论文证据。
- 增加不可覆盖的 Research Brief、内部 `PKG-xxxx` 封存和 `cv-research-handoff/v2` 独立交付目录；大数据放在仓库已忽略的 `datasets/` 或 `data/` 相对路径，权重和受限材料只记录下载地址或引用，不打进发布物。
- 统一启动器在任何写入前核对科研项目、PaperFlow 接收 profile 和 producer 精确版本；自动选择已安装的 PaperFlow 专用 Python，服务异常时只回收自己创建的进程。
- 新增真实 Edge 浏览器验收和科研到 PaperFlow 的低成本闭环；候选稿始终标记 `not_for_submission`，不批准、不发布、不关机。
- `1.4.0 / SYS-V2.12.0` 保留为精确旧锁，可查看并显式升级；本候选不会自动安装到正式 Skill 或覆盖旧 既有项目。

## [1.4.0] - 2026-07-26（未安装的开发候选）

- 增加 `intake-check` 和三页控制台共用的启动必填检查；缺项时只返回报告，不创建 Task、Run 或训练。
- 增加 `console-state`、`UI-CONSOLE-V1.0.0` 三页原生界面和只绑定 `127.0.0.1` 的本地只读服务；界面只允许读取状态和预览 `intent/provided`，执行入口固定禁用。
- 关系图只使用真实 Source、Idea、Template、Module、Task、Run 和 Evidence；没有 Codebase 账本时不伪造 `CB-*`。
- PaperFlow V1 现场核验精确兼容已发布的 1.2.2、1.2.3、历史 1.3.0 和当前 1.4.0 producer 组合；未知或错配组合继续拒绝。
- 在不改变 V1 外层字段集合的前提下，把主指标、完整 Run 分析和 Evidence 审核字段放入 `reproducibility.config` 的保留命名空间并纳入摘要。
- 导出目标位于项目 `.experiment-workflow` 控制目录或经链接解析后落入该目录时，正式导出和 `--dry-run` 都会在写入前拒绝；允许的目标会固定解析时的真实位置，并在创建期间持有实际父目录锚点，避免链接或真实路径祖先随后换绑。
- 所有结果文件完成首轮哈希后，再逐文件完整读取并比较 SHA-256，关闭同尺寸替换并恢复时间戳的长窗口反例。
- 本候选不会自动安装或升级实例；本机安装版仍是 `SKILL-RELEASE-V1.2.3 / SYS-V2.10.3`，旧个人实例锁仍是 `SKILL-RELEASE-V1.2.1 / SYS-V2.10.1`。

## [1.2.1] - 2026-07-24

- 为项目专属 Skill 补齐普通校验、同名项目 UUID 后缀、搬家重绑定和可核验安装清单。
- 拒绝假项目、超长或多行项目名、链接/reparse 路径、竞争占位和无法证明归属的安装覆盖。
- 历史受信任锁可先显式升级再补建项目 Skill；安装读取使用同一项目快照。
- 为漂移检查补充可定位的 `skill_path` 约定，并把最终修复版独立编号为 `SYS-V2.10.1`。

## [1.2.0] - 2026-07-24

- 把“生成项目专属 Skill”设为项目初始化的必经步骤；新项目同时写入 `SKILL.md` 和机器可读 `project_skill` 身份。
- 增加 `init-project-skill`，为历史实例安全补建总协调入口；已有外来 `SKILL.md` 时拒绝覆盖。
- 增加 `install-project-skill`，把项目内正式副本显式安装到指定 Codex Skills 目录；同内容重试幂等，不同内容拒绝覆盖。
- 项目专属 Skill 只固定项目 ID、路径提示、仓库边界和 Agent 协作入口，核心状态机继续唯一委托给通用 `cv-experiment-workflow`。

## [1.1.2] - 2026-07-24

- 锁升级在写锁内再次读取源码 Skill 摘要；源码在首次检查后发生变化时拒绝写入。
- 将精确的本机已验证 1.1.1 锁加入受信任旧 v2 清单，实例可安全升级到本补丁。

## [1.1.1] - 2026-07-24

- 锁升级前先验证项目身份和完整账本；缺 `project.json`、账本损坏或未知 v2 锁都会原子拒绝。
- 漂移检查在读取项目身份和锁时使用项目快照锁，避免与并发升级读出混合状态。
- 维护精确的受信任旧 v2 锁身份，支持 v2→下一发布升级，同时拒绝伪造摘要。
- 每个 Idea `source_ref` 现在都必须至少有一条具体来源定位；完全相同的科学内容不能制造空修订。
- 增加论文页码、代码符号、未关闭 Run、非法证据、旧 Idea→新格式修订等拒绝与兼容测试。

## [1.1.0] - 2026-07-24

- 新项目使用 `workflow-lock.v2`，把 `SKILL-RELEASE-V1.1.0`、`SYS-V2.9` 和当前 Skill 正式文件摘要写进实例。
- 新增只读 `check-workflow-drift`，区分源码/安装版不一致、旧锁未绑定和实例锁漂移。
- 新增显式 `upgrade-workflow-lock`；旧 `workflow-lock.v1` 继续可读，绝不静默迁移。
- v2 新 Idea 改用独立 `catalog-idea.v1` schema，解决与旧 Idea 同名但字段含义不同的问题。
- 新增不可变 `revise-catalog-idea`：修订生成新 ID，保留父 Idea 原文件，并记录父引用、修订原因和递增版本。
- 新 Idea 必须记录来源定位与支持的科学字段；证据只接受已登记 Source 或已关闭 Run。
- 四组内置代码明确标为最小骨架，不再表述为可直接训练的完整 CV 项目。

## [1.0.0] - 2026-07-16

- 首次公开发布可安装的计算机视觉实验工作流 Codex Skill。
- 支持 Idea → Trial → CodeAsset → Attempt → Result → Confirmation → Promotion → Version 主链。
- 支持创新、调参、消融和复现实验的确定性记录、校验与晋级。
- 支持项目 Template 元数据、代码来源、离线 HTML 框架视图和外部代码引用。
- 增加只读派生下一步规划，并补全现有六角色的 Idea（创意）树、模板、资源、结果双轴、来源和晋级门控能力；不新增角色身份。
- Result v2 分开记录实现可信度与假设结论，`decision` 由双轴唯一派生；旧 Result 继续兼容。
- adapter 可按需升级为 v2，保存最少 accepted Attempt 数和不同 seed 数；默认 1/1 保持旧行为。
- promotion 按同一根 Trial（或 Version）与基础 Version 选择合格证据，并冻结稳定排序的 Attempt ID。
- 新增项目级轻量 Template（模板）元数据登记，只保存严格 `TPL-xxxx.json`，不复制来源代码。
- 新增确定性的离线 HTML 框架视图；视图位于控制面之外，不作为权威事实。
- 明确 Idea、实验主链、模板定位、仓库与本地大文件边界。
- 明确当前 promotion、decision 和 v2 能力边界。
- 新项目使用 workflow lock（工作流锁）1.0.0。
