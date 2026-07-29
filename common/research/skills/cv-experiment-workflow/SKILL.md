---
name: cv-experiment-workflow
description: "Use when 用户要求初始化科研或计算机视觉实验工作流、记录想法或实验、规划下一轮实验、管理创新/调参/消融/复现、检查 promotion、追踪代码模板与论文出处、导出论文证据，或协调多 Agent 完成可复现实验。"
---

# CV 实验工作流

## 当前唯一主路

先从当前统一系统根目录读取 `config/system.json`。没有激活个人实例时只检查通用模板，不创建用户目录；用户以后主动实例化并激活后，才读取对应 `workspace.json` 并进入方向仓库。不要扫描其他工作流目录，也不要自行创建平行副本。

当前只开放 GZSL（广义零样本学习）。一个 GZSL 仓库可以包含多个完整代码 Framework（框架）；每个 Framework 下面固定分为：

- `reproduction`：复现；
- `tuning`：调参；
- `ablation`：消融；
- `innovation`：创新。

只有创新实验可以绑定 Idea。创新经正式 Run、人工确认和 Git 提交后，才可用 `promote-framework` 晋级为带父子关系的稳定子 Framework，并只在该创新实验目录生成一个 `framework.html`。复现、调参和消融不产生新 Framework，也不画框架图。

当前人话对象只有：方向仓库 → 完整代码 Framework → 四类实验 → Run → 已确认结果。Idea 只服务于创新实验。旧的 Codebase、Module、Trial、Attempt 和 Version 命令只属于底层兼容代码，当前用户入口不得把它们展示成用户需要管理的对象。

当前 GZSL 命令：

| 目的 | 命令 |
|---|---|
| 新建 GZSL 仓库 | `create-gzsl-repository --destination --name --environment dvsr_gpu` |
| 接入已标准化外来框架 | `import-standardized-gzsl-framework --repository --source-repository --framework --title --version --component-map --equivalence --i-trust-this-code`；最后一个开关表示用户已人工审读外来代码 |
| 创建 Idea | `create-framework-idea --repository --title --problem --mechanism --hypothesis --source-notes [--parent-idea]` |
| 创建四类实验 | `create-framework-experiment --repository --framework --route --slug --title --summary --route-contract [--idea]`；页面用普通输入框生成比较规则 |
| 建立独立实验工作区 | `prepare-framework-worktree --repository --experiment` |
| 登记外置数据 | `register-gzsl-dataset --repository --experiment --worktree --source --slug --dataset-id --version --source-uri --license`；原始 NPZ 不进入 Git |
| GPU 运行 | `run-framework-experiment --repository --experiment --worktree --config-json --seed --purpose` |
| 人工确认正式结果 | `confirm-framework-run --repository --experiment --run --reason --proposed-by --checked-by [--innovation-outcome accepted\|rejected --innovation-conclusion]` |
| 主动生成论文交付包 | `export-framework-paper-package --repository --experiment --run --research-brief-json --claim --confirmed-by [--destination-root]` |
| 创新晋级成子框架 | `promote-framework --repository --experiment --worktree --child-slug --title --version --framework-view` |
| 检查整个仓库 | `validate-framework-workspace --repository` |

科研不会自动触发论文写作。只有用户明确说要交付时，才生成一次论文交付包。

## v2 人话入口

用户可以直接说：`读论文`、`整理代码模板`、`调参`、`做消融`、`复现结果`、`试这个创新`、`查状态`或`继续`。先由 Coordinator 用 LLM（大模型）归到八种固定意图；只给它项目标准摘要、当前 Version、未完 Task 和固定路线表。LLM 不可用时才用最小关键词表；一句话命中多条路线就问一个问题，不猜。

在写文件或耗资源前，先用五句人话解释，再决定是否调用 CLI：

```text
我理解成什么
准备基于什么
现在能不能开始
真正缺什么
下一步做什么
```

`workflow-status --project`、`task-list --project`、`console-state --project --intent` 与 `plan-next --project` 都只读。`console-state` 只为本地界面一次返回已验证的项目、关系、启动检查和阻塞条件，不会创建 Task 或执行实验。`interpret-request --project --request` 只是明确标为 `fallback` 的离线关键词降级，不会假装调用 LLM。`继续`也只给建议；只有原 Task 已保存预算、执行授权且计划未变，才可建议现有 `run-task`，仍不在解释阶段自动运行。

要打开 `UI-CONSOLE-V2.0.0` 统一入口，优先在源码根运行 `tools/start_unified_workflow.ps1`，同时传入科研项目、PaperFlow 和知识库路径。界面、CLI 和 Agent 共用 `console-state` 的核心结果；写操作只能走固定白名单，包括新建/打开项目、保存 Brief/Source/Idea、创建 Codebase/Task/Run 和冻结交付包。页面没有任意命令、删除、push、发布或高成本训练快捷入口。

当前可写的 v2 最小闭环是 `init --layout v2`、Source / Idea / Template / Module / Codebase 登记、`start-task` 和 `run-task`。`verify-codebase` 默认只核对 Codebase 基线身份；单独做严格现场检查时仍可传 `expected_git` 并设 `require_clean=true`。正式 Task 在 `--route-options` 里保存精确 `code_binding`：`codebase_id`、branch、40 位小写 commit 和可选 tag；旧 Task 不带此字段仍可读取。运行前会现场核对这四项，正式证据还要求工作树干净；debug（调试）可在脏工作树运行，但结果不能成为论文证据。绑定后不会直接在现场仓库运行，而是把该 commit 的普通文件复制到当前 Run 独有的只读执行快照，只开放快照内固定的 `.cv-workflow-output/` 写结果；每次 Adapter 调用前后都重新核对快照和数据身份。即使直接调用底层 Run 写入口，系统也会从登记的 Codebase 重新核对仓库路径、Git、clean 状态和 Adapter 摘要，不接受调用者自报“已验证”。整个过程不会创建分支、push 或修改仓库。身份复核失败时先用内存中已经载入的同一 Adapter 请求停止；停止不能确认就保留未完成 Run，并记录 `cleanup_pending` 或 `cleanup_failure`，不产生正向论文证据。当前正式支持范围仅为 Windows 本地单用户；Linux 全链兼容仍在 `docs/ROADMAP.md`，不得对外声称已经支持。调参、消融、复现和创新继续共用 Task → Run → Evidence；四条路线只在前置输入、候选生成和比较规则上不同。下面的 Idea → Trial 等命令属于 v1 兼容说明。

新建方向仓库时，先读取系统根目录 `config/directions/catalog.json`。`list-domain-packs` 只列出内置历史包和各自的 `availability`；`pending` 只表示保留兼容材料，不能创建仓库或运行实验。当前只有 GZSL 是 `ready`，因此只允许 `create-domain-repo --pack gzsl`；另外五个方向会在底层复制前被拒绝。更推荐使用本页上方的 `create-gzsl-repository` 新主路。模板不附带数据集、权重或自动下载行为。真实 NPZ 留在用户指定的本机位置，Git 只提交下载地址、版本、许可证、文件 SHA-256 和内容清单；科研交付包会把本机绝对路径换成数据哈希身份。

当前只有 GZSL 提供真实 baseline，且固定使用 CUDA，不提供 CPU 退路。其余五个历史包只用于静态兼容检查，不能据此声称方向已经可运行。GZSL 正式 evidence 除了在配置和环境里同时明确使用 CUDA，还必须通过中央 CUDA 小算子证明；证明随冻结环境和输出封存一起保存。自定义 Adapter 不能自报 GPU 后取得正式资格。架构不兼容会在创建输出前停止，绝不静默退回 CPU。

外来 Framework 只能从个人 `inbox` 中已经扫描并复制的标准化草稿导入。系统会运行草稿代码，因此必须先人工审读并明确确认信任；这项确认不是操作系统沙箱，未知或来源不明的代码不得执行。标准测试和 CUDA 探针必须与当前 GZSL 通用模板逐字一致，CUDA 由中央可信探针实际计算。两份等价结果还要绑定可重跑的 Python 命令、代码 commit、同一数据清单和输出文件 SHA-256；系统会在临时 Git 快照中删除旧结果后重新执行命令，手写两份相同 S/U/H 不算证明。

CLS（图像分类）使用真实本地 ImageFolder 时，配置除 `mode` 和 `data_root` 外还要填写 `dataset_id`、`version`、`source_uri`；`source_uri` 是可追溯来源地址，不能写本机绝对路径。Adapter 会现场读取 `train` 和 `val` 文件并生成内容清单摘要，正式 Run 冻结代码、配置、数据、评估和环境五类身份。可信运行先返回 `artifact_seal_pending`（结果已跑完、正等待产物封存）；调用 `seal-run-outputs` 后，系统把固定输出目录复制到项目 `.cv-workflow-seals/<RUN-ID>`，再次执行同一 Task 才登记 `single_run` 并关闭 Run。没有权威封存副本时不会产生正向论文证据。

论文写作前，先用 `save-research-brief --project --manifest` 保存不可覆盖的科研摘要，再用 `seal-paper-package --project --brief --selection` 把已确认结果、创新边界、外部来源用途和素材清单封成项目内部 `PKG-xxxx`。封存只接受严格 JSON，结果数字会与当前 Run 和 Evidence 核对。历史包仍可如实保留 `incomplete`；新 1.5 正式证据包必须是 `paper_ready`，缺少任何必需素材就拒绝，不能降级冒充可写论文。

内部包完成后，用 `export-paper-package --project --package --mode --out` 导出 `cv-research-handoff/v2` 独立目录，再用 `verify-paper-package --package-dir` 独立核验；这是当前科研 → 论文唯一主接口。`--out` 必须直接指向同名 `PKG-xxxx`，`--mode` 必须等于内部包已冻结的 hybrid 或 full。1.5 导出会在同一项目锁中重验 Run、Evidence、执行快照和权威输出，included 的 Run 文件只从 `.cv-workflow-seals/<RUN-ID>` 复制，再用 `manifest.reverification` 绑定有序交付字节。目录固定包含 manifest、六个科学文件、checksum 和允许复制的 `assets/included/<sha>/<filename>`。核验不回读科研项目；科研端不会自动发送。用户必须在本机显式选择具体 `PKG-xxxx` 目录，PaperFlow 才能通过自己的接收核验后绑定。SHA-256、checksum 和 producer 只检查所选包的字节与声明是否一致，不是数字签名，不能证明作者、生成工具或来源身份；能改写整包的人也能重算摘要。若以后要接收不受信任的人或网络传来的整包，需要另加数字签名，本地核验不能冒充这项能力。

`export-paperflow --project --run --out` 与 `verify-paperflow-handoff --project --handoff --expected-run --expected-payload-sha256` 只保留给旧接收端读取 `cv-research-handoff/v1` 单 Run JSON。当前通用入口不得建议使用这条兼容接口。

项目初始化的必经步骤是生成项目专属 Skill，并显式安装后才算完成。项目专属 Skill 是该项目的唯一总协调入口：固定项目 ID、路径提示和仓库边界，再委托本通用 Skill 选择角色、调用 `rw.py`；不得复制状态机或另建第二套账本。新入口名带项目 UUID 短后缀避免同名冲突；项目搬家后用 `rebind-project-skill` 安全刷新路径。

### 只读任务清单

项目总清单和每个实验的六步清单不是第二套账本，也不新增 `checklist` 持久化字段；它们每次都从 Task、Run 和 Evidence 自动算出，所以不会与执行事实不同步：

- `workflow-status --project <项目路径>`：看项目总览，其中的全局 `task_list` 就是项目总清单。
- `task-list --project <项目路径>`：只看项目总清单。
- `task-list --project <项目路径> --task TASK-0001`：只看一个实验的清单。

四条实验路线各有贴合本路线的中文清单，但都只有六个位置：目标条件、开跑检查、最小 debug（调试）、正式证据、分析审核、收尾；它们共用同一套 Task → Run → Evidence 循环，不复制执行流程。`done` / `reviewing` 的 Task 如果缺少要求的 debug 或正式证据，对应位置会明确显示 `blocked`，不会把缺证据说成完成。具体中文步骤见 `references/workflow.md`。

运行预算是硬上限：已达到 `max_runs` 的 Task 不会再创建 Run，`继续`只会建议新建 Task。若停止条件明确写成 `single_debug`（只跑一次调试）且一次可信 debug 已完成，本 Task 会按这个有限范围收尾；这只说明“最小链路跑通”，不能冒充正式证据或论文结论。要做正式实验，必须另建预算和停止条件都适合正式证据的新 Task。

### 代码路线的两种动作

`整理代码模板`与`接入模块`属于同一条代码路线，但必须分开处理：

- **建立基础模板**：比较多份参考代码并归纳共同规则，包括数据接口、训练步骤、评估口径和真实模块插入位置，再独立编写项目自己的可运行模板。参考项目是规则与思路来源，不选其中一份冒充永久主干，也不把多份仓库拼接成模板。
- **接入单个模块**：复用已经确认的基础模板，只增加当前模块、插入位置、配置和测试。默认不得改动公共训练循环、评估口径或其他模块；确实必须改变这些固定部分时，应建立新的 Template 修订，不能伪装成普通模块接入。

基础模板必须在关闭研究模块时独立运行。模块必须声明输入、输出、插入位置和关闭行为；关闭后应回到模板基线，即训练与评估路径、配置和口径恢复，不要求随机训练得到的指标逐位相同。参考实现只用于 `inspiration`（思路参考）或经许可证允许的明确重实现/改写，详细出处边界见 `references/code-and-provenance.md`。

仓库 `assets/templates/` 下的内置四组代码骨架只用于演示 attachment（插入位置）、关闭行为和契约测试；它们启用研究分支时会抛出 `NotImplementedError`。它们不是可直接训练的完整 CV 项目模板，也不能替代实例根据真实数据、模型、训练和评估规则独立建立的项目 Template。

## 核心原则

把自然语言意图翻译成少量、可验证的实验对象。科学判断由 Agent（智能体）完成；确定性写入、编号、冻结与校验一律调用 `scripts/rw.py`，不要手改结构化账本。

v2 主路由是“人话 → 原 Task → Run → Evidence（证据）”；调参、消融、复现和创新共用这一条执行循环。调参不得夹带代码行为变化；消融要锁定 Module、关闭行为和启用侧 Run；复现要锁定 Source、来源 Run、目标、容差、代码和数据标准；创新继续锁定 Idea、Template、Module 和比较基线。v1 的 Idea（想法）→ Trial（创新试验方案）→ CodeAsset（代码资产）→ Attempt（一次运行）仍保持兼容。对象主链、生命周期与当前能力边界以 `references/workflow.md` 为权威说明。

核心字段：`status`（状态）区分草稿、可用、计划和完成；`target`（目标对象）说明实验基于谁；`seed`（随机种子）固定一次运行；`metrics`（指标集合）保存结构化结果。

## 执行入口

先把 Skill 根目录记为 `SKILL_DIR`，用当前 Python 执行：

```text
python SKILL_DIR/scripts/rw.py <command> ...
```

先运行对应命令的 `--help`，再按真实参数调用。下表主要是 v1 兼容命令；v2 先走上面的人话入口。

| 用户意图 | 路由 |
|---|---|
| 初始化 v2 当前项目 | `init --path --name --layout v2` |
| 为兼容实例补建专属 Skill | `init-project-skill --project` |
| 项目搬家后刷新专属 Skill | `rebind-project-skill --project` |
| 安装项目专属 Skill | `install-project-skill --project --skills-root` |
| 离线降级解释人话 | `interpret-request --project --request`；输出 `source=fallback` |
| v2 总览（含全局清单） | `workflow-status --project <项目路径>` |
| v2 只看项目总清单 | `task-list --project <项目路径>` |
| v2 只看一个实验清单 | `task-list --project <项目路径> --task TASK-0001` |
| v2 本地界面统一只读数据 | `console-state --project <项目路径> --intent status` |
| 打开 v2 三页只读界面 | `python SKILL_DIR/scripts/console_server.py --project <项目路径> --port 8765` |
| v2 登记来源 | `register-source --project --manifest --source-path` |
| v2 登记代码库身份 | `register-codebase --project --manifest --repo-root` |
| v2 现场复查代码库 | `verify-codebase --project --codebase [--expected-git JSON]`；默认只查基线，正式 Run 的 `expected_git` 必须设置 `require_clean=true` |
| 查看内置方向仓库材料与开放状态 | `list-domain-packs`；状态读取系统根目录 `config/directions/catalog.json` |
| 新建当前唯一开放的方向仓库 | `create-domain-repo --project --pack gzsl --destination <新目录> --name <仓库名>`；五个 pending 方向在写入前拒绝 |
| v2 保存 Research Brief（科研摘要） | `save-research-brief --project --manifest` |
| v2 封存内部论文包 | `seal-paper-package --project --brief BRIEF-xxxx --selection <严格 JSON 文件>` |
| v2 导出独立论文交付包 | `export-paper-package --project --package PKG-xxxx --mode hybrid\|full --out <父目录>/PKG-xxxx` |
| v2 独立核验论文交付包 | `verify-paper-package --package-dir <父目录>/PKG-xxxx` |
| v2 保存 Idea | `save-idea --project --manifest` |
| v2 修订 Idea（新建记录，不覆盖父记录） | `revise-catalog-idea --project --idea --manifest --reason` |
| v2 登记 research Module | `register-module --project --manifest --code-root` |
| v2 创建调参 Task | `start-task --project ... --route-options '{"code_binding":{"codebase_id":"CB-0001","branch":"main","commit":"<40位小写提交>","tag":null}}'`；只创建，不自动运行 |
| v2 创建消融 Task | `start-task --route ablation ... --route-options '{"module_ref":"MOD-...","baseline_run_ref":"RUN-...","disabled_behavior":"..."}'`；只创建，不自动运行 |
| v2 创建复现 Task | `start-task --route reproduction --target-ref SRC-... --target-ref RUN-... --baseline 0.0 ... --route-options '{"source_ref":"SRC-...","source_run_ref":"RUN-...","tolerance":0.0,"code_standard":{},"data_standard":{}}'`；只创建，不自动运行 |
| v2 创建创新 Task | `start-task --route innovation --target-ref IDEA-... --target-ref TPL-... --target-ref MOD-... --primary-metric ... --baseline ...`；只创建，不自动运行 |
| v2 执行已授权 Task | `run-task --project --task ...` |
| v2 封存正式 Run 输出 | `seal-run-outputs --project --run ...`；只接受成功关闭、正式数据且可写论文的 evidence Run |
| 旧 v1 单 Run 兼容导出（不是当前 V2 建议） | `export-paperflow --project --run --out [--dry-run]` |
| 旧 v1 单 Run 兼容核验（不是当前 V2 建议） | `verify-paperflow-handoff --project --handoff --expected-run --expected-payload-sha256` |
| 初始化当前项目 | `init --path --name` |
| 记下粗糙想法 | `new-idea --project --title --note`，来源用可选 `--source-*` |
| 把想法变成可证伪 Idea | `activate-idea --project --idea --problem --mechanism --hypothesis` |
| 修订 active Idea | `revise-idea --project --idea --reason [--problem/--mechanism/--hypothesis] --evidence JSON_ARRAY` |
| 绑定 Idea 与代码落点 | `set-implementation-mapping --project --idea --mapping JSON_OBJECT` |
| 登记可复现基础代码 | `register-version --project --name --repo-url --commit [--code-path]` |
| 登记项目 Template 元数据 | `register-template --project --manifest --code-root` |
| 派生离线 Template 框架视图 | `render-template --project --template --output` |
| 创建 Trial 与 CodeAsset | `new-trial ... --template-family FAMILY`（演示）或 `new-trial ... --template TPL-xxxx`（正式），二选一 |
| 同步摘要或绑定外部实现 | `sync-code-asset --project --asset [--code-root PATH --relative-path PATH]` |
| 固化一次创新/调参/消融/复现 | `new-attempt --project --target --type --seed --command [--config]` |
| 记录实验结果 | 新格式：`record-result ... --implementation-status --hypothesis-status --conclusion`；旧格式仍可用 `--decision`，不得混用 |
| 设置确认门槛 | `set-confirmation-policy --project --minimum-accepted-attempts --minimum-distinct-seeds` |
| 检查或执行晋级 | `promotion-check --project --attempt`；`promote --project --attempt ...` |
| 登记已集成的晋级版本 | `activate-version --project --version --repo-url --commit --code-path` |
| 更新经审核的长期角色记忆 | `update-role-memory --project --role --content-file` |
| 只读核对源码、运行版和实例锁 | `check-workflow-drift --project --source-skill` |
| 显式把旧实例锁升级为代码指纹锁 | `upgrade-workflow-lock --project --source-skill` |
| 只读查看账本事实 | `status --project` |
| 只读派生下一步建议 | `plan-next --project`；不创建 TASK、不执行建议 |
| 检查全账本 | `validate --project` |

“规划下一轮”先做科学判断，用户确认运行配置后才写 `new-attempt`。不要发明 `Inbox`、独立 Result、独立 Confirmation 或不存在的 CLI 命令。

v1.5.0 中，“查看状态/规划下一轮”保持只读路由；需要协作时只启动六个固定角色中的最小组合。统一入口的写动作仍调用同一生产函数，不复制状态机。旧工作流锁继续可读，升级必须显式执行；科研交付以不可覆盖的 `cv-research-handoff/v2` 目录作为与 PaperFlow 的唯一通用接口。这里的“唯一”指当前主路只交换这一种目录合同，不表示删除 V2 历史 producer 的精确解析兼容，也不删除 v1 单 Run 兼容命令。

## 写入与安全边界

1. 初始化只写目标项目并生成项目专属 `SKILL.md`；安装到 Codex Skills 目录必须显式调用 `install-project-skill`。已有不同 `SKILL.md` 时拒绝覆盖；后续更新只接受安装清单证明属于同一项目、且未被人工改写的副本。
2. 用 CLI（命令行接口）写 JSON（结构化数据）。角色记忆只能由 Coordinator 审核内容后调用 `update-role-memory` 写入；禁止手写 `.experiment-workflow/agents/`。
3. 每次写入后运行 `validate`。失败时报告原始错误，不猜测修复账本。
4. 不覆盖 completed Attempt，不修改被 Attempt 冻结的 CodeAsset，不手改 `framework.html`。
5. 数据集、论文 PDF、日志、checkpoint（训练检查点）、权重和大制品留在账本外；Result 只记相对路径和可选摘要。
6. 未获用户明确授权，不创建 GitHub 远端、不推送、不发布。Git 用于用户项目回滚，不由 Skill 擅自操作。
7. 缺少真实实验事实时，只在对话中列出所需字段，不新建临时计划、审核或确认 Markdown。除 CLI 管理的对象和用户明确要求的输出外，不新建文档。
8. `register-template` 只核验 manifest 明确列出的来源文件并登记单个 JSON；不复制模板代码。`render-template` 的 HTML 必须输出到 `.experiment-workflow` 之外。
9. 绑定执行的 Adapter 固定为 Codebase Git 顶层的 `workflow_adapter.py`；它必须是普通 Python 文件，当前 bytes 必须与 Task 绑定 commit 中的普通 blob 完全一致。真正运行时只使用当前 Run 的 commit 快照，不从现场仓库或其他 Python 路径补文件；原始日志和制品路径只能落在该快照的 `.cv-workflow-output/`。正式 evidence 工作树必须干净；debug 即使允许脏工作树，也会冻结 `worktree_clean=false` 并禁止论文资格。

## 按需加载

- 初次使用、对象/ID/目录、Idea 与 Version 时序、主链、仓库和版本能力边界：读 `references/workflow.md`。
- 选择模板、理解内置模板定位、写创新代码、论文或官方代码出处：读 `references/code-and-provenance.md`。
- 选择角色、多 Agent 通信、审核强度与长期记忆：读 `references/agents.md`，再只加载所选 `assets/roles/*.md`。
- 只有用户明确做广义零样本学习时，读 `references/gzsl.md` 和 `assets/reference-packs/gzsl.json`。

## 端到端例子

用户说：“在当前项目记录一个来自论文的特征适配想法，用基础提交做首次创新实验，结果好再确认并检查 promotion。”

执行：

1. 若未初始化，调用 `init`；用 `new-idea` 保存来源标签和定位信息，再用 `activate-idea` 写问题、机制、可证伪假设。
2. 用精确 40 位提交调用 `register-version`。正式 Template 路径先用 `set-implementation-mapping` 固定问题、机制、attachment、目标路径、验证命令和关闭行为；再实际打开并核验出处，调用 `new-trial --template`。演示路径可直接使用 `--template-family`。
3. Implementer 只改新 CodeAsset 的 `module.py` 和契约；调用 `sync-code-asset`，运行契约与 `validate`。
4. 用 `new-attempt --type innovation` 固化命令、配置和一个 seed；外部运行后用 Result 双轴分别记录实现可信度与假设结论。
5. 若需确认，用上一 completed Attempt 作为 `new-attempt --target`，并从 `innovation/tune/ablation/reproduction` 四种固定类型中选择；通常使用 `reproduction`。记录结果并重新校验。
6. 仅对 completed 且 accepted、冻结事实未漂移的 Attempt 调用 `promotion-check`；用户同意后才 `promote`。经授权集成真实代码并提交后，用 `activate-version` 登记最终 commit。
