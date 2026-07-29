# 从零理解并使用工作流

## v2 人话入口

v2 不要求每次从 Idea 开始。用户直接说论文、代码、调参、消融、复现、创新、状态或继续，Coordinator 先用 LLM（大模型）归到固定路线；LLM 不可用时才用关键词降级。给 LLM 的内容只有项目标准摘要、当前 Version、未完 Task 和固定路线表，不读取整仓源码、日志或大文件。

| 用户说法 | 系统理解 | 当前 v2 能否写入 |
|---|---|---|
| `读这篇论文`、`整理创新点` | 论文整理；与创新实验分开 | 否，只说明所需输入 |
| `整理代码模板`、`接入模块` | 首次提炼并建立自有模板，或向已有模板接入单个模块 | 否，只说明所需输入 |
| `调参` | 调参实验 | 可建议现有 `start-task`，不自动创建 |
| `做消融` | 消融实验 | 可以；锁定 Module、关闭行为和启用侧 Run 后创建 |
| `复现这个结果` | 复现实验 | 可以；锁定 Source、来源 Run、目标、容差、代码和数据标准后创建 |
| `试这个 Idea`、`做创新实验` | 创新实验；使用已整理 Idea、Template 和 Module | 可以；三者对齐后用 `start-task --route innovation` 创建，不自动运行 |
| `查状态` | 只读状态 | 是，运行 `workflow-status --project` |
| `导出单条 Run 快照` | 仅兼容未绑定 V2 包的旧项目或旧接收端，不是当前 V2 建议 | 是，运行 `export-paperflow --project --run --out` |
| `核验单条 Run 快照` | 仅核对旧 v1 快照，不是当前 V2 建议 | 是，只读运行 `verify-paperflow-handoff --project --handoff --expected-run --expected-payload-sha256` |
| `导出论文材料包` | 把已封存的 `PKG-xxxx` 交给论文系统 | 是，运行 `export-paper-package --project --package --mode --out` |
| `核验论文材料包` | 不依赖科研项目核对 V2 目录 | 是，只读运行 `verify-paper-package --package-dir` |
| `继续` | 继续唯一未完 Task | 只建议，不自动执行 |

每次先回答同一组五句话：`我理解成什么`、`准备基于什么`、`现在能不能开始`、`真正缺什么`、`下一步做什么`。有歧义时只问一个会改变结果的问题。状态和规划只读；只有原 Task 已保存预算、执行授权且计划未变化，才可建议 `run-task`，解释过程本身不会开训练。

### 统一科研入口

在源码根运行 `tools/start_unified_workflow.ps1`，传入科研项目、PaperFlow 和知识库路径。`UI-CONSOLE-V2.0.0` 仍用“开始与设置、关系图、执行确认”三个页面，但已接入受控写动作：

- “开始与设置”先说明当前路线一定要提供什么、系统已经发现什么、还缺什么。
- “关系图”只画账本和项目设置里真实存在的对象；未登记 Codebase 时不补造编号。
- Codebase（代码库身份）用 `CB-xxxx` 记录一个真实 Git 顶层和它登记时的方向、模板来源、分支、commit 与可选 Tag。`register-codebase` 要求 tracked 文件全部干净；外部仓库没有账本豁免，同仓也只豁免严格白名单里的未跟踪账本文件，任何被 Git 忽略的可执行文件都拒绝登记。`verify-codebase` 默认只核对 Codebase 基线身份，也就是仓库、初始 commit 和可选 Tag 仍然存在，不要求当前 checkout 停在初始分支或 commit，也不默认检查工作树是否 clean。正式 Run 必须传入 `expected_git`，并设置 `require_clean=true`，这时才会精确检查当前 branch、commit、tag、普通脏文件和被 Git 忽略的可执行文件。所有检查都只读，不切分支、不打 Tag、不 push。实验进度继续由 Task 表示，不另建第二套 `EXP-*` 账本。
- “执行确认”解释当前门槛和下一步；已登记 Codebase 时允许继续建 Task，但正式 Run 仍会重新核对 Git 现场。

服务只监听 `127.0.0.1`。页面写入只允许新建/打开项目、Brief、Source、Idea、Codebase、Task、Run、Evidence 和 Research Package 这些固定动作；没有任意命令、删除、push、发布或投稿接口。页面打不开时，使用 `intake-check` 和 `console-state` 可得到同源只读结果。

### v2 只读任务清单

项目总清单和每个实验的清单不是第二套账本，也不新增 `checklist` 持久化字段。它们每次都从 Task、Run 和 Evidence 自动算出，所以 Task 阶段、真实 Run 或证据变化后，下一次读取会直接反映新事实，不会维护出两份互相打架的进度。

- `workflow-status --project <项目路径>`：查看项目总览，其中的全局 `task_list` 就是项目总清单。
- `task-list --project <项目路径>`：只看项目总清单。
- `task-list --project <项目路径> --task TASK-0001`：只看一个实验的六步清单。

Research Brief（科研摘要）用
`save-research-brief --project <项目路径> --manifest brief.json` 保存。它记录研究背景、
问题、目标、研究问题、可证伪假设、范围、术语、计划贡献和用户确认。首次保存生成
`BRIEF-0001`，后续保存只会生成新的连续 ID 和修订号，不覆盖旧文件。
`workflow-status` 顶层的 `research_briefs` 会显示总数和最新一份的简要信息。
`paper_inspired`（受论文启发）和 `adapted`（基于来源改写）的计划贡献必须引用已经
登记的 `SRC-xxxx`；`original`（项目原创）允许没有来源引用。

科研实验和证据整理完成后，当前科研 → 论文唯一主接口是 V2 目录包。先运行
`seal-paper-package --project <项目路径> --brief BRIEF-0001 --selection selection.json`
生成不可覆盖的内部 `PKG-xxxx`。然后运行：

```text
export-paper-package --project <项目路径> --package PKG-0001 --mode hybrid --out <交付父目录>/PKG-0001
verify-paper-package --package-dir <交付父目录>/PKG-0001
```

第一条把 `cv-research-handoff/v2` 目录完整建在同级 staging 中，自检通过后才发布，
绝不覆盖已有目标。hybrid 只复制写作必需且许可、隐私、可用性都允许的资产；full
再复制其他同时满足 available、允许复制、许可已知和隐私允许的资产；同一内容
有多条声明时整组采用最严格决定，不是无条件复制全部 available 资产。目录固定包含
`manifest.json`、六个科学事实文件、
`checksums.sha256` 和 `assets/included/<sha>/<filename>`。第二条只读取交付目录，
即使原科研项目已经移动或归档也能独立核验。用户必须在本机显式选择具体
`PKG-xxxx` 目录，PaperFlow 接收端才会导入并绑定；科研端不会自动发送或替论文系统
批准正文。SHA-256、checksum 和 producer 只能证明所选目录的字节与声明一致，不是
数字签名，不能证明作者、生成工具或来源身份；能改写整包的人也能重算摘要。以后若
接收不受信任的人或网络传来的整包，需要另加数字签名。

当前可落盘的四条实验路线都显示目标条件、开跑检查、最小 debug（调试）、正式证据、分析审核、收尾，并使用各自的中文说明：

| 路线 | 1. 目标条件 | 2. 开跑检查 | 3. 最小 debug（调试） | 4. 正式证据 | 5. 分析审核 | 6. 收尾 |
|---|---|---|---|---|---|---|
| 调参 | 明确调参目标、候选范围与停止条件 | 通过调参开跑检查 | 完成最小调试运行 | 完成正式调参证据运行 | 分析指标并审核调参结论 | 收尾并记录最佳配置 |
| 消融 | 明确消融模块、关闭行为与启用侧对照 | 通过消融实验开跑检查 | 完成关闭侧最小调试运行 | 完成正式消融证据运行 | 核对启用/关闭差异并审核结论 | 收尾并记录模块贡献 |
| 复现 | 锁定来源、目标、容差、代码和数据标准 | 通过复现实验开跑检查 | 完成最小复现调试运行 | 完成正式复现证据运行 | 记录真实差异并审核是否落入容差 | 收尾并记录复现结果 |
| 创新 | 明确创新假设、对照条件与停止条件 | 通过创新实验开跑检查 | 完成最小调试运行 | 完成正式创新证据运行 | 分析结果并审核创新结论 | 收尾并记录创新结论 |

这些差异只影响前置输入、候选和比较规则；四条路线共用同一套 Task → Run → Evidence 循环、状态和证据，不复制执行流程。调参检测到代码行为变化时会阻止运行并建议转创新；消融必须让项目 Adapter 核对 Module 登记的 `disabled_behavior`；复现只能记录实际差异和是否落入预先容差，不能在看完结果后改目标。`done` / `reviewing` 的 Task 如果缺少要求的 debug 或正式证据，对应步骤会明确显示 `blocked`，不会把缺证据说成完成。当前能力边界仍以本页开头表格为准。

`max_runs` 是执行时强制检查的运行次数上限，不是仅供显示的备注。达到上限后，`run-task` 拒绝创建新 Run，状态页把原因写成 `run_budget_exhausted`，`plan-next` 只建议 `new-task`。停止条件若为 `{"type":"single_debug","after_runs":1}`，一次可信 debug Run 达标后，原 Task 会以 `scope=debug_only` 收尾；若旧版本恰好在 Run 已关闭、Task 尚未收尾时中断，再次执行同一个 debug 会复用原 Run 完成收尾，不创建 RUN-0002。六步清单会明确提示“正式证据需要新建 Task”；debug 的局部涨跌可以记录，但正式科学假设仍应标为 `inconclusive`（证据不足，暂不能下结论）。

创新 Task 的目标必须至少包含一个 `ready` Idea、一个 v2 Template 和一个 `ready research` Module。目标 Module 必须明确引用目标 Idea，并挂到目标 Template 的 attachment；Source 或本地代码快照不能冒充这三类目标。创新还必须写明有限的比较基线 `baseline` 和主指标 `primary_metric`，并自动记为会改变代码行为。合法创新与调参共用同一个 Runner、状态和收尾：成功 debug Run 只记 `debug` 证据，Run 关闭后 Task 回到 `preparing`，不会被误写为正式单次结果。

代码路线只有两个动作。第一次是“建立基础模板”：比较多份参考代码并归纳共同接口、训练规范、评估标准和模块落点，再独立编写本项目的可运行模板；不得把参考仓库直接拼起来，也不得随意指定其中一份作为永久主干。以后是“接入单个模块”：保持已确认的数据、训练和评估规则不变，只加入模块、配置和测试。模块若必须改变固定规则，应明确升级 Template，而不是把基础模板的变化藏在模块接入中。模块关闭后回到模板基线，是指训练与评估路径、配置和口径恢复，不要求随机训练得到的指标逐位相同。

CLI 的 `interpret-request --project --request` 只运行关键词降级并明确返回 `source=fallback`，不会伪装成 LLM。下面的 Idea、Trial、Attempt 主链是 v1 兼容说明；v2 主线是“人话 → Task → Run → Evidence（证据）→ 收尾”。

### 从内置方向包创建代码仓库

`list-domain-packs` 只读列出随当前 Skill 发布并通过文件大小、SHA-256 和来源清单核验的方向包。`create-domain-repo --project <科研项目> --pack cls --destination <新仓库目录> --name <仓库名>` 从选定包建立一个新的 `main` Git 仓库、创建固定初始 Tag，并把它登记为当前科研项目的 Codebase；命令不接受任意外部资源根，不创建远端，也不会覆盖已有目录。

新仓库里的 `python -m cls.smoke --work-dir runs/smoke --device cpu` 使用固定 seed 在 CPU 上真实训练和推理，但输出始终写明 `synthetic_debug_only` 与 `paper_eligible=false`。这句话的意思很直接：smoke 只能检查代码有没有跑通，不能证明方法有效，也不能进入论文成绩句。真实训练读取用户自己准备的数据；大型数据放在仓库已忽略的 `datasets/` 或 `data/` 下，配置使用相对路径，例如 `datasets/my-dataset`。模板和科研交付包都不复制大数据或权重，只保存固定下载地址、版本、划分和文件清单摘要。

正式真实实验默认使用 CUDA，只接受六个受信任的内置方向模板。中央执行层不相信 Adapter 自报的 `device=cuda`，而是核对模板版本、Adapter SHA-256 和 Python SHA-256，并在同一个环境中亲自在 CUDA 上完成 `2×2` 矩阵运算。GPU 名称、CUDA 版本、设备编号、计算能力和运算结果进入冻结环境、环境指纹和输出封存；自定义 Adapter 只能做 debug。六个方向的正式指标定义也由模板固定注入，用户不能在 Task 配置里临时换口径，冻结后删改增都会失败。

每个方向包的 `DOWNLOADS.json` 必须明确写成“不自动下载、没有捆绑大文件”，并为每项可选资源记录固定版本、固定修订、HTTPS 页面和许可证来源。页面地址的 `sha256`（文件摘要）必须写成 `null`；只有直接指向单个下载制品时才允许填写并强制核对 64 位 SHA-256。`latest`、`main`、`master` 等会漂移的写法会在建仓前被拒绝，下载清单本身也不得超过 512 KiB。这个合同核对的是清单声明，不把它夸成操作系统级断网；候选发布还要另跑离线 smoke，检查常用 Python API 的联网或拉起子进程尝试。许可证不清楚时直接在 `license` 中原样写明“官方页未声明，使用前需核验”，并把官方说明页登记为 `license_url`，不另造含义重复的布尔字段，也不能替数据集编一个许可证。

### 旧 v1 单 Run 兼容入口

下面两条命令只给尚未绑定 V2 论文包的既有项目，或仍只接受
`cv-research-handoff/v1` 单 Run JSON 的旧接收端。它们继续可用，但不是当前 V2
科研 → 论文操作建议，也不能替代上面的目录包主路。

`export-paperflow --project <项目路径> --run RUN-0001 --out handoff.json` 会生成 `cv-research-handoff/v1` 本地快照。`single_run` 只能成为 `candidate`（候选），`confirmed` 且代码版本、配置、随机种子、数据身份、环境、指标定义、指标值和结果产物齐全时才能成为 `result_verified`（已验证结果）。`--dry-run` 只打印、不写文件；正式导出原子创建目标，绝不覆盖。

`verify-paperflow-handoff --project <项目路径> --handoff handoff.json --expected-run RUN-0001 --expected-payload-sha256 sha256:<64hex>` 是只读现场核验。两个期望身份由 PaperFlow 从已绑定的原始交接记录传入，先阻止同项目合法 Run 被调包。核验严格检查交接包自摘要和 producer（生成工具版本），再在科研项目同一把受控锁内重新读取当前 Task、Run、Evidence 与产物文件，调用同一套交接内容构建逻辑；所有文件哈希结束后会再次统一核验身份，最后同时比较完整摘要和规范 JSON 正文字节。完全一致输出固定八字段的 `status=pass` 并返回 0；参数错误、自签名伪造、字段或产物漂移、Evidence 撤销、项目不可用和坏包都会只向标准输出写稳定 JSON、返回非零值。

## 它解决什么问题

做实验最怕三件事：想法与代码对不上、运行条件无法复现、好结果不知道凭什么进入主版本。本工作流把最少必要事实写进本地账本，让你能回答“为什么做、基于什么代码、实际跑了什么、得到什么、能否晋级”。它不负责写论文，也不替代训练框架。

## 安装、本地项目与 GitHub

Skill（技能包）是可复用工具；每篇论文或每个研究项目是独立目标项目。安装 Skill 后，不要在 Skill 源仓库里继续写自己的实验。进入目标项目，让 Codex 响应“初始化科研工作流”，其实际调用为：

```powershell
python <Skill绝对路径>/scripts/rw.py init --path <项目路径> --name <项目名>
```

`init` 会在项目根生成项目专属 `SKILL.md`，并在 `project.json` 保存 `project_skill` 身份。接着必须显式调用：

```powershell
python <Skill绝对路径>/scripts/rw.py install-project-skill --project <项目路径> --skills-root <Codex Skills目录>
```

项目专属 Skill 是该项目的唯一总协调入口，固定项目 ID、路径提示、仓库读写边界和 Agent 协作入口；共享路线、状态机和 CLI 继续委托给通用 Skill，不复制核心代码或账本。新入口名使用可读目录名加项目 UUID 短后缀，避免不同磁盘下的同名项目冲突。既有项目缺少入口时，先运行 `init-project-skill --project <项目路径>`。项目搬家后运行 `rebind-project-skill --project <项目路径>`，它只刷新可证明为旧路径下自动生成的源文件；随后重新安装。安装目录的机器清单只允许更新同一项目且未被人工改写的旧副本。生成并安装专属 Skill 是每次实例化工作流的必经步骤；安装动作显式写外部目录，因此不藏在 `init` 中。

初始化只建立当前本地项目账本，不会自动新建 GitHub 仓库。账本仓库与实验代码仓库可以同仓或分仓：同仓适合代码和账本共同回滚；分仓时，Version 用仓库 URL、精确 commit 和相对路径引用外部实验代码。若你想让 GitHub 只保存账本与规范，可以新建一个独立项目仓库，再在其中初始化；只有用户明确授权后才能创建远端或推送。

Git 适合版本化 `.experiment-workflow/`、`AGENTS.md`、`WORKFLOW.md` 和小型代码。论文 PDF、数据集、日志、checkpoint、模型权重或其他本地大文件不进入 `.experiment-workflow/` 和 Git；它们放在项目约定的外部存储中，账本只记路径与可选 SHA-256 摘要。

## v1 兼容主链

v1.5.0 的 v1 兼容主链仍是：Idea → Trial → CodeAsset → Attempt → Result → Confirmation → Promotion → Version。

Idea 可以独立存在：Idea 可以独立创建、澄清和激活，不需要预先归属某个 Version；进入 Trial 时才选择基础 Version。`new-trial` 随 Trial 创建 CodeAsset。Result 内嵌在 Attempt 中，没有独立 ID；Confirmation 也不是独立对象，而是以已完成 Attempt 为 target 的新 Attempt。Promotion 消费 accepted Attempt 并生成新的 Version；若项目要求独立确认，以 completed Attempt 为 target 创建 Confirmation Attempt，并把确认结果纳入 promotion。CLI 不强制 Confirmation。

## 最小对象

v1.5.0 继续包含 Template（项目模板）的轻量登记与正式组合。

| 对象 | ID / 位置 | 用途 |
|---|---|---|
| Project | `project.json` 中的 UUID | 标识当前项目 |
| Idea（想法） | `IDEA-0001`；`ideas/IDEA-0001.json` | 保存问题线索、来源与假设 |
| Version（代码版本） | `VER-0001`；`versions/VER-0001.json` | 锁定基础代码仓库、commit、路径 |
| Template（项目模板） | `TPL-0001`；`templates/TPL-0001.json` | 登记外部 Git commit 中的轻量模板事实，不复制来源代码 |
| Trial（创新试验方案） | `TRIAL-0001`；`trials/TRIAL-0001/trial.json` | 连接 active Idea、基础 Version 和一次创新分支 |
| CodeAsset（代码资产） | `CODE-0001`；`code-assets/CODE-0001/` | 新 Trial 的小型可插拔代码、契约、出处和框架视图 |
| Attempt（一次冻结运行） | `ATTEMPT-0001`；`attempts/ATTEMPT-0001.json` | 冻结一次实际可运行实验及其内嵌 Result |

`promote` 只生成 draft（草稿）`VER-xxxx`。用户把已接受资产集成到真实代码仓库并提交后，`activate-version --project P --version VER-xxxx --repo-url URL --commit 40HEX --code-path REL` 才把它原子登记为 active（激活）Version；命令只登记最终 commit，不 clone、不创建 worktree、不执行代码。

新 Result 用双轴分开记录实现可信度与科学结论：`implementation_status`（`valid/invalid/uncertain`）与 `hypothesis_status`（`supported/not_supported/inconclusive/not_evaluated`）。`decision` 由两轴派生：只有 `valid + supported` 为 `accept`，`valid + not_supported` 为 `reject`，其余为 `inconclusive`。旧 Result 的单一 `decision` 继续可读并标为 legacy evidence（旧证据），不会被自动解释成双轴结论。

常见字段：`status`（状态）表示 draft（草稿）、active（可用）、planned（计划）或 completed（完成）；`target`（目标对象）记录 Attempt 基于哪个 Version、Trial 或已完成 Attempt；`seed`（随机种子）固定一次运行；`metrics`（指标集合）是 Result 中的 JSON 数值与描述。

Idea 来源不建立硬分类，只用 `label`、`locator`、`note` 标清论文、个人观察、讨论或其他来源。普通 `new-idea` 继续生成 Idea v1；只有传入 `--parent-idea` 或可重复的 `--related-idea` 时才直接生成 Idea v2，关系只允许引用已存在 Idea。active Idea 可用 `revise-idea` 修订科学字段：证据必须引用现有账本对象且至少包含一个带严格 Result 的 completed Attempt；旧版本保存在同一 JSON 的 `revision_history`。问题或机制变化会让旧 `implementation_mapping` 失效。

项目已有可复用母版时，可准备严格 manifest（清单）并调用 `register-template`。登记要求 `code-root` 是 manifest 所指 commit 的干净 Git worktree（工作树）顶层；脚本只对 manifest 列出的普通文件做有界、身份稳定读取，并以 commit 中的 Git blob SHA-256 作为权威摘要。登记结果是单个 `TPL-xxxx.json`，不会 clone、复制或执行来源代码。`render-template` 可把其中的框架事实派生为确定性、离线 HTML；输出必须位于 `.experiment-workflow/` 之外且拒绝覆盖。

正式 Trial 使用 `new-trial --template TPL-xxxx`，只引用登记的 Template，并在 CodeAsset 中保存小型 `composition.json`；它不复制母版代码或创建 payload。创建前必须用 `set-implementation-mapping` 把 Idea 的当前问题与机制映射到 Template 的 attachment、目标路径、验证命令和 disabled behavior（关闭行为）；Trial 用 `idea_revision` 与 `implementation_mapping_sha256` 冻结当时依据，后续合法修订从 Idea 历史复核。完整旧 formal Trial 字段集保持只读兼容，不自动迁移；新旧字段不得混装。映射不一致时 Trial 与 CodeAsset 均不落盘。首次组合要求 Base Version 的主 commit 等于 Template commit。来源分析、clone、worktree 准备和集成真实代码是需用户授权的 Agent 步骤；manifest 登记、引用校验、账本写入和 `activate-version` 是确定性脚本步骤。Agent 完成实现后，用 `sync-code-asset --code-root ... --relative-path ...` 登记 `external_code_ref`；未绑定的新资产或继承资产不能创建 Attempt。演示路径继续使用 `--template-family`，不要求 implementation mapping。

## 完整流程

1. **初始化**：运行 `init`。若已存在控制目录，命令拒绝覆盖。
2. **记录 Idea**：用 `new-idea` 写标题、笔记和可选来源。粗糙想法就是 draft Idea，不存在 Inbox；只有要表达继承或相关关系时才传 `--parent-idea` / `--related-idea`。
3. **澄清与修订 Idea**：用 `activate-idea` 写问题、机制和可证伪假设；实验暴露变化后，用 `revise-idea` 携带原因、真实 evidence ID 和实际变化字段修订。
4. **登记 Version**：用 `register-version` 保存基础仓库 URL、40 位 commit 和相对代码路径。
5. **可选登记 Template**：对已准备的干净 Git worktree，调用 `register-template` 登记 manifest；需要查看框架时另用 `render-template` 派生 HTML。
6. **创建 Trial**：正式路径先用 `set-implementation-mapping` 固定实现落点，再用 `new-trial --template` 原子生成 Trial、CodeAsset 与 composition；演示路径用 `--template-family`。
7. **实现代码**：正式实现留在外部代码仓库，提交后用成对的 `--code-root`、`--relative-path` 绑定；演示资产仍可直接同步骨架摘要和 `framework.html`。
8. **计划 Attempt**：选择 `innovation`、`tune`、`ablation` 或 `reproduction`，用 `new-attempt` 固化 target、一个 seed、命令与 JSON 配置。
9. **外部运行**：训练进程仍由项目自己的环境执行；不要把大输出塞进控制目录。
10. **记录 Result**：新格式成对提供 `--implementation-status` 与 `--hypothesis-status`；旧脚本可继续只传 `--decision`，两种格式不得混用。completed Attempt 不得改成另一份结果。
11. **Confirmation**：默认门槛是 1 个 accepted Attempt 与 1 个不同 seed。需要更强确认时用 `set-confirmation-policy` 提高到 1..100；它只按同一根 Trial（Attempt 链会追到根）、同一基础 Version 统计 completed 且合格的 Attempt，不发明论文指标阈值。
12. **Promotion**：先 `promotion-check`；数量或 seed 不足时返回结构化原因。`promote` 使用相同选择规则，并把当时全部合格证据 ID 稳定排序后冻结进 draft Version。
13. **激活 Version**：经授权完成真实代码集成并取得最终 40 位 commit 后运行 `activate-version`；同参重试幂等，异参或无效 lineage（谱系）拒绝。
14. **更新角色记忆**：Coordinator 审核可复用经验，把 UTF-8 Markdown 写到普通 `content-file`，再调用 `update-role-memory --project <项目> --role <六个固定角色之一> --content-file <文件>`。其他角色不得直接写 memory。
15. **校验与回滚**：每次写入后运行 `validate --project <项目路径>`。CLI 写入具备锁和原子性；Git 提交由用户决定，出现不满意改动时用项目自己的 Git 历史回滚。

v2 新 Idea 使用 `cv-experiment-workflow.catalog-idea.v1`，根 Idea 固定为 revision 1；`revise-catalog-idea` 每次创建新 ID，并保存 `parent_idea_ref` 与修订原因，父文件不改。每个 `source_ref` 都必须至少有一条 `source_links`，写清来源 ID、论文页码或代码位置、支持的问题/机制/假设字段和具体论断；科学字段完全没变时拒绝制造空修订。`evidence_refs` 只接受已登记 Source 或已关闭 Run。历史 `cv-experiment-workflow.idea.v2` 只读兼容，不自动改写。

用 `check-workflow-drift --project <项目> --source-skill <源码 Skill>` 可只读比较源码、当前运行 Skill 和实例锁。旧 `workflow-lock.v1` 会报告 `legacy_unpinned`；确认源码与运行版完全一致后，才可显式执行 `upgrade-workflow-lock`。任何检查都不会静默改锁。

v1 可用 `status --project <项目路径>` 查看对象计数；v2 使用 `workflow-status --project <项目路径>` 查看 Task、Run、Evidence 和全局 `task_list`，或用 `task-list --project <项目路径>` / `task-list --project <项目路径> --task TASK-0001` 单独查看清单。`plan-next --project <项目路径>` 会按项目 schema 自动选择 v1 或 v2 建议。它们都从严格校验的单一快照派生，不创建 TASK、不生成 ID、不执行训练或晋级，也不修改账本。

## 当前版本边界

v1.5.0 包含项目专属总协调 Skill、统一本机入口、六方向 Codebase 模板、Task → Run → Evidence、不可覆盖 Research Package 和 PaperFlow V2 交付。六份真实 baseline 默认使用 CUDA；CPU 只用于 smoke 或兼容调试，所有真实正式 evidence 必须在配置和环境里同时明确使用 CUDA。PaperFlow V1 保留兼容入口，新的科研到论文主链只使用 `cv-research-handoff/v2` 目录包。对象 schema 的兼容格式继续由校验器识别；旧锁可读，受信任旧 v2 锁可显式升级，新锁必须匹配当前运行 Skill。长期助手、云端多用户、自动资源调度和自动投稿不在本轮。

## 控制目录

```text
.experiment-workflow/
├─ project.json              # 项目身份
├─ workflow.lock.json        # Skill 协议版本
├─ adapter.json              # 外部代码能力声明；按需保存 Confirmation policy
├─ ideas/                    # Idea JSON
├─ versions/                 # Version JSON
├─ templates/                # 单文件 TPL-xxxx.json；不复制来源代码
├─ trials/                   # Trial 目录
├─ code-assets/              # 小型骨架、composition 与派生 HTML
├─ attempts/                 # Attempt + 内嵌 Result
├─ agents/                   # 仅由 update-role-memory 写入的六种长期角色记忆
└─ artifacts/index.json      # 仅引用；目录内不放真实大制品
```

不要手改对象 JSON、复制编号或手写 HTML。遇到错误先保留现场，运行 `validate` 并报告原始错误。
