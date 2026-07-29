# 技术演进历史

> 历史记录，不能作为当前操作说明。当前公开版本统一称为“科研工作流 V1.0”。

本文件按版本追加，保留正式 V1.0 发布前的科研引擎技术选择、替换和放弃记录。当前操作请读取系统根目录 `README.md` 和当前 Skill。

<details>
<summary>展开查看正式 V1.0 发布前的内部开发记录</summary>

## 版本类型说明

- `SYS-V*`：科研实验工作流的系统能力与源码基线。
- `SKILL-RELEASE-V*`：Codex Skill 的入口与发布身份；在工作流锁中保存为不带 `SKILL-RELEASE-V` 前缀的 `release_version`。
- `UI-CONSOLE-V*`：本机统一页面及其交互版本，不代表系统内核同步升级。
- `UI-V*`：流程图或其他视觉文档版本。
- `PACK-*-V*`：六个方向的代码模板、数据合同、指标口径和运行入口。
- `DATA-RESEARCH-HANDOFF-*`：科研结果交接的数据格式与安全规则。
- `DATA-RESEARCH-BRIEF-*`：科研摘要账本的数据格式、来源规则与不可覆盖语义。
- `DATA-RESEARCH-PACKAGE-SEAL-*`：科研端内部论文封存包的数据格式、证据门槛与不可覆盖语义。
- `DATA-RESEARCH-PAPER-EXPORT-*`：目录型论文交付包的导出、素材复制与独立核验规则。
- `DOC-*`：项目内的说明文档版本，不代表系统功能已经上线。

## 当前版本一览

- 当前系统源码候选：`SYS-V2.13.0`；当前工作流锁：`SKILL-RELEASE-V1.5.0 / SYS-V2.13.0`；当前统一页面：`UI-CONSOLE-V2.0.0`。它们均未自动安装到本机正式 Skill，本机安装版仍为 `SYS-V2.10.3 / SKILL-RELEASE-V1.2.3`。
- 当前科研摘要规则：`DATA-RESEARCH-BRIEF-V1`，schema 为 `cv-experiment-workflow.research-brief.v1`。
- 当前内部论文封存规则：`DATA-RESEARCH-PACKAGE-SEAL-V1.0.1`，package schema 仍为 `cv-experiment-workflow.paper-package.v1`。它表示项目内部的 `PKG-xxxx` 不可覆盖封存包已可用，不表示 PaperFlow V2 接收已经完成。
- 当前数据交接规则：单 Run 旧入口为 `DATA-RESEARCH-HANDOFF-V1.1.1`（schema 为 `cv-research-handoff/v1`）；目录型论文交付候选为 `DATA-RESEARCH-PAPER-EXPORT-V2.1.2`（schema 仍为 `cv-research-handoff/v2`）。1.5 producer 只接受绑定正式证据；1.2.3/1.3.0 继续作为历史正文结构解析兼容标签，不是签名或来源可信证明，旧包仍要通过全部事实、文件和路径检查。旧包中的 result 导入后固定为 `candidate`、`allowed_sections=[]`，且不生成 `contains_number` 或 `numeric_claims`，因此只能查看历史，不能进入任何论文章节；只有 1.5 绑定证据能成为 `result_verified`。真实最小 1.5 包已由 PaperFlow 接收候选直接验收。
- 当前源码候选构建规则：`SYS-RELEASE-V1.1.1`。正常发布使用操作系统提供的原子“不覆盖改名”；构建失败只保留并报告 `.release-staging-*` 现场，不再按路径自动递归删除。验证器 `1.2.0` 会在测试前冻结科研与 PaperFlow 两棵源码树，并在每组测试后同时核对文件、空目录、Git HEAD 和索引；外部 `GIT_*` 变量不能把检查重定向到别处。`1.1.1` 还明确禁止把 PaperFlow 的受保护策略文件放进候选，并同时用字面路径和解析后的真实路径检查本机目录泄漏。
- 当前方向包：`PACK-GZSL-V1.1.1`；其余 `PACK-CLS/DET/SEG/INSTSEG/SR-V1.0.0` 保留为历史候选，本轮不继续实施。GZSL 调试和正式运行均固定使用 CUDA，不提供 CPU fallback；RTX 5070 Ti 上真实 `train → evaluate → infer` 已通过。微型验证仍为 `paper_eligible=false`，不构成论文成绩。
- 历史记录中的 `v2.1`、`v2.2`、`v2.3` 是旧的混用编号，保留原样以便追溯；它们不是当前 `SYS-V2.10.3` 的可比较版本号。此前写作的 `SYS-RESEARCH-WORKFLOW-V1.2.1` 误把发布号 `1.2.1` 当成系统号，现映射为“系统 `SYS-V2.10.1`、发布 `1.2.1`”，不删除旧名称以便回查。

本文件按版本追加，保留通用工作流技术方案的采用、替换和放弃记录。

## SYS-V2.13.0 / SKILL-RELEASE-V1.5.0 / UI-CONSOLE-V2.0.0：统一科研入口与论文交付接口

- 日期：2026-07-27
- 状态：候选版验证通过；代码、定向测试、六方向 CUDA 真机验证、真实浏览器检查、最终干净代码快照的科研/PaperFlow 全量、三轮独立核心审核和最终成品独立复核均已通过。第一次命名候选虽被旧检查器误判为通过，但被独立复核否决并保留为 `REJECTED`；使用 `SYS-RELEASE-V1.1.1` 重建的最终目录已通过 13 项清洁检查，独立成品 Reviewer 结论为 `APPROVE`。它仍未安装、push 或发布。
- 本次改的是哪个对象：科研系统、工作流锁、统一页面、六方向仓库模板、Research Package producer 和 PaperFlow 统一启动边界；论文写作内核保持在独立 PaperFlow worktree。
- 目标问题：原科研账本、代码模板和 PaperFlow 分开启动；页面只能看不能做；六方向模板没有统一 GPU 规则；科研完成后缺少一个能被论文系统独立核验的正式交付包。
- 采用技术：Python 标准库本地服务与固定动作白名单；原生 HTML/CSS/JavaScript；Codebase/Task/Run/Evidence 单一账本；六套自包含 PyTorch 模板；中央 CUDA 真小算子证明；六方向固定指标定义；SHA-256 不可覆盖目录包；PowerShell 5.1 启动器；PaperFlow 精确 producer profile；Python 3.10 使用条件依赖 `tomli` 读取 TOML，3.11 及以上使用标准库 `tomllib`。
- 替换了什么：把 `UI-CONSOLE-V1.0.0` 的只读页面替换为 `UI-CONSOLE-V2.0.0` 受控入口；把“科研目录与论文目录靠人工解释连接”替换为 `cv-research-handoff/v2` 交付包；不替换旧 既有项目，不覆盖正式 Skill。
- 实际可见效果：用户可在一个页面新建/打开科研项目、创建六方向仓库并沿 Source → Idea → Codebase → Task → Run → Evidence 工作；科研结束后通过 `export-paper-package → verify-paper-package` 生成并核对 V2 目录包，这是当前科研到论文的唯一主接口，再由 PaperFlow 在用户显式选择该本地包后接收、检索证据和写六章候选稿。`export-paperflow / verify-paperflow-handoff` 只保留为旧未绑定项目或旧接收端的 v1 单 Run 兼容命令，不是当前 V2 操作建议。正式真实 evidence 只接受六个受信任内置方向模板，中央执行层核对模板与 Adapter/Python 摘要并亲自在 CUDA 上运行；自定义 Adapter 只能做 debug。固定指标定义、原始日志和全部结果文件随 Run 一起封存。
- 选择原因：两个系统只共享一个清楚、可搬移、可复核的包，比共享内部数据库或把两个状态机揉在一起更简单，也更不容易把外部论文当成本论文实验事实。
- 已知限制：当前正式支持 Windows 本机单用户；不捆绑数据集、权重、虚拟环境或 CUDA PyTorch。大型数据放在仓库已忽略的 `datasets/` 或 `data/` 相对路径，交付包只保存来源 URL、版本、划分和清单摘要。SHA-256、checksum 和 producer 不是数字签名；用户显式选择本地包后，系统只能核对包内字节与声明一致，不能识别有写权限者改写整包并重算摘要。未来若接收不受信任的人或网络传来的整包，需要另加数字签名。Windows 按路径递归删除存在目录被调包后误删的风险，因此发布验证器完成测试后不自动删除短路径沙箱，而是保留 `.owner` 和全部内容并在 JSON、终端中明确报告位置。已在 RTX 5070 Ti、`torch 2.11.0+cu128`、CUDA build 12.8、compute capability 12.0 下完成六方向真实 CUDA `train → evaluate → infer`，请求 CUDA 时不会退回 CPU。SEG 保持确定性算法开启，模型、输入、mask、logits、loss 和 gradient 均在 `cuda:0`；DET、INSTSEG 的精确 `pycocotools==2.0.11` 只放在隔离临时环境，原 Conda 环境未改。这些运行只使用本地微型数据并固定 `paper_eligible=false`，不能作为论文成绩。
- 素材位置：`tools/start_unified_workflow.ps1`、`skills/cv-experiment-workflow/assets/console/`、`skills/cv-experiment-workflow/assets/domain-packs/`、`skills/cv-experiment-workflow/scripts/workflow_core/paper_package*.py`、`docs/superpowers/plans/2026-07-27-lean-complete-cv-workflow.md`。
- 验证命令与结果：六方向真实 CUDA `train → evaluate → infer` 全部通过且均为 `paper_eligible=false`；科研到 PaperFlow 完整闭环为 `4 passed in 58.94s`，正式 Git / Run 身份门禁为 `41 passed, 55 subtests`，科研 `1.5` 生产包专项为 `19 passed, 3 skipped, 2 subtests`，PaperFlow `1.5` 接收专项为 `84 passed`；真实 Edge 检查通过。验证器 `1.2.0` 在全新干净代码快照上得到科研 `1128 passed, 1 allowed skip, 1487 subtests passed`、PaperFlow `1013 passed, 1 warning, 0 skip`，两组进程树清理均为 `PASS`，两棵源码树的组前、组后和最终摘要完全相同，验证摘要整体 `PASS`、`source_roots_unchanged=true`、`power_action=none`。第一次命名候选的旧 13 项结果已经作废；补丁采用失败先行测试后，科研发布专项为 `108 passed, 54 subtests`，PaperFlow 相关专项为 `213 passed`。重新构建的最终目录包含 476 个内容文件，payload 为 7,369,259 bytes；加两个控制清单后实际为 478 个文件、7,513,667 bytes，13 项检查全部 `PASS`，测试收集为科研 1132 个、PaperFlow 1014 个。最终报告为 `final-clean-candidate-build-result.json` 和 `final-clean-candidate-release-check.json`，不能沿用作废候选报告。
- 回退方式：回退本条候选分支即可恢复 `1.4.0 / SYS-V2.12.0` 源码；已生成的科研项目、Git 仓库和 `PKG-xxxx` 不自动删除或改写。

## SYS-RELEASE-V1.1.1：受保护文件排除与真实路径泄漏检查

- 日期：2026-07-28
- 状态：已完成失败先行测试、最小修复、双端定向回归和最终干净候选重建；第一次命名候选已保留为 `REJECTED`。新候选 13 项自动检查全部通过，独立成品 Reviewer 结论为 `APPROVE`。
- 本次改的是哪个对象：`tools/build_release_staging.py`、`tools/run_release_checks.py` 及其发布测试；PaperFlow 候选侧只把解析器提示和测试夹具中的本机示例改成可搬移写法。它不改变训练、评估、科研事实、正式数据库、旧 既有项目 或正式安装。
- 目标问题：旧白名单把 PaperFlow 顶层策略文件当成普通说明文件带进候选；绝对路径扫描只比较部分字面父目录，在 Windows 短文件名或路径别名下可能漏掉真实工作区路径；另有两处示例直接写入生成机器的目录和用户名。旧检查因此出现“13 项全过，但候选仍不能交付”的假阳性。
- 采用技术：在读取文件正文前按规范相对路径明确排除 `paperflow/SECURITY.md`；发布复查同时明确拒绝该路径。隐私扫描同时使用输出目录父级的字面绝对路径和 `resolve()` 后真实路径；移除对具体用户名路径的豁免。PaperFlow 运行时提示改用 `%USERPROFILE%`，测试夹具改用中性占位符。
- 替换了什么：替换“顶层 Markdown 默认可发”和“只比较一种父目录拼写”的规则；不读取、复制或改写被保护文件。
- 实际可见效果：受保护策略文件不会进入 manifest，手工塞回候选会被发布检查拒绝；测试夹具即便记录真实工作区路径、Windows 8.3 短路径与长路径拼写不同，也不能绕过扫描；普通合成测试路径仍可使用。
- 选择原因：发布检查的结论必须和实际交付边界一致；安全策略文件和个人目录都不属于他人拿到候选后运行系统所必需的内容。
- 已知限制：字符串扫描只能发现规则覆盖的路径表达，不能证明任意编码或加密内容中绝无隐私；因此仍保留文件白名单、禁带类型、manifest、独立复核和不覆盖发布多层检查。此前完整双端全量覆盖应用代码基线，晚期补丁只由发布工具与路径文本专项覆盖，不能伪称旧全量报告已经覆盖新字节。
- 素材位置：`tools/build_release_staging.py`、`tools/run_release_checks.py`、`tests/test_release_staging.py`、`tests/test_release_checks.py`、`docs/reviews/2026-07-28-rejected-candidate-security-path-leak.md`。
- 验证命令与结果：新增反例先分别暴露受保护文件被打包、真实工作区路径未拦截和具体用户名提示三个问题；修复后科研发布组合为 `108 passed, 54 subtests`，PaperFlow 的 V2/V1.5 接收与解析器相关组合在短临时根下为 `213 passed`。最终候选为 476 个内容文件、478 个实际文件、7,513,667 bytes；13/13 `PASS`，科研/PaperFlow 分别收集 1132/1014 个测试；受保护策略文件不存在，两个真实个人路径标记直接扫描均为 0。manifest SHA-256 为 `7751F7A814CFAE487D4A78F6632CE6097E1F25EA9403BFBD70167E79C1FE1E3E`，排除清单 SHA-256 为 `49C50BC4CEA4FA870C20A8BC251F871FD47D40BF6EDDD1702B83A0411FD3EE67`。新构建报告 SHA-256 为 `E595E463E9C59E23D6D10754E0E5877AD3E28D2200D1E1817EB8911BAABA4356`，新检查报告 SHA-256 为 `FE8839A13B984534E2B9C92B1E4EEDDF715B5AE6E79C51A7A5200620EC5D9111`。独立成品 Reviewer 重新计算全部文件后确认缺失、多余、大小不符和哈希不符均为 0，并复核无链接、硬链接、数据库、权重、数据集、缓存或运行产物，最终结论为 `APPROVE`。第一次候选的两个结果 JSON 作为否决证据保留，不得当作最终报告。
- 回退方式：不建议回退；回退会再次允许受保护文件或个人路径进入一个表面通过的候选。已经作废的候选只用于审计，不自动删除或覆盖。

## SYS-RELEASE-V1.1.0：双源码冻结、Git 隔离与测试写入边界

- 日期：2026-07-28
- 状态：已完成实现、定向回归、三轮独立审核和最终双端全量验证；本版本第一次发布目录检查后来被人工反方复核发现覆盖缺口，发布结论由 `SYS-RELEASE-V1.1.1` 取代。
- 本次改的是哪个对象：`tools/validate_release.py`、`tools/build_release_staging.py`、`tools/run_release_checks.py` 及其测试；PaperFlow 候选侧另把工作台方法卡测试搬到临时工作区。它不改变训练、评估、论文事实、正式数据库或旧 既有项目。
- 目标问题：早期全量测试虽然通过，但 PaperFlow 工作台测试会刷新正式源码目录里的方法目录时间；旧验证器只在每组开始时记录该组自己的源码，不能及时发现科研测试改写 PaperFlow；外部 `GIT_DIR`、`GIT_WORK_TREE`、`GIT_INDEX_FILE` 等变量也可能把 Git 核对引到别处。
- 采用技术：验证开始前冻结两棵源码树；每组结束后同时比较全部源码根，并在最后再比较一次。快照覆盖普通文件 SHA-256、空目录、worktree `.git` 文件、真实 HEAD 和 `git ls-files --stage -v` 索引语义；时间戳不参与判定。源码树内的符号链接、目录联接和 Windows reparse point 在测试前失败关闭。所有 Git 子进程清除大小写不敏感的外部 `GIT_*`，再注入只读、无交互、无全局配置的固定环境；构建器还要求 Git 报告的仓库根正好等于所选源码根。报告 schema 保持 `cv-research-paperflow.release-validation.v1`，验证器版本升级为 `1.2.0`。
- 替换了什么：把“每组只核对自己那棵源码”和“继承调用者 Git 环境”替换为“双源码全程冻结”和“受控 Git 环境”；把 PaperFlow 测试直接刷新源码方法目录替换为复制到短路径临时工作区后重建 13 张方法卡、3 套组合。
- 实际可见效果：任一测试新增、删除或修改另一棵源码中的文件、空目录、Git HEAD 或索引，当前组立即失败，后续组不再执行；即使后来删除整个源码根，也会留下结构化失败 JSON。PaperFlow 方法目录测试结束后，源码 `catalog.json` 的字节、SHA-256 和修改时间均不变；候选包即使不带生成态 catalog，也能在临时工作区重建出 13/3 目录。
- 选择原因：发布验证不仅要证明测试通过，还要证明测试没有顺手改掉被测源码；Git 环境必须由验证器自己固定，不能相信启动它的终端没有残留重定向变量。
- 已知限制：快照不比较时间戳，因此“只改时间”或把文件重写成完全相同字节不会失败；普通 `.git` 目录不会逐字节递归哈希，而是核对 HEAD、索引和工作树内容。SHA-256 仍不是数字签名，不能证明外部来源身份。Windows 安全规则要求测试沙箱保留而不自动递归删除。
- 素材位置：`tools/validate_release.py`、`tools/build_release_staging.py`、`tools/run_release_checks.py`、`tests/test_validation_summary.py`、`tests/test_release_staging.py`、`tests/test_release_checks.py`、PaperFlow 候选的 `tests_v2/test_workbench_api.py`。
- 验证命令与结果：关键组合回归为 `105 passed, 54 subtests`；验证器专项为 `32 passed, 16 subtests`；PaperFlow 工作台为 `34 passed`；发布暂存专项为 `23 passed, 33 subtests`。最终全新快照全量为科研 `1128 passed, 1 allowed skip, 1487 subtests passed`、PaperFlow `1013 passed, 1 warning, 0 skip`。科研源码摘要在组前、组后和最终均为 `afa198c7810eebac7b1f4f984a7c51f6e3394d2fb1f11b45934419c8b8f49b96`，PaperFlow 均为 `f61bda88ee68ea001fee773abcf0767d2246acaed6395cfe61c457a6abc138e6`；`changed_path_count=0`、`source_roots_unchanged=true`。总报告文件 SHA-256 为 `f2930073b5c300fe98e1431730e2ccc9eddcc78b1056c8cecad25ff41c8ce412`，规范 JSON 完整性值为 `a9676f129aefeb63faec019bb49aaa7e328b740f93cca9b456c68dfbdb95cdc6`，两位独立 Reviewer 最终均为 `APPROVE`。历史发布预检曾记录 477 个文件、7,512,858 bytes 和 13 项 `PASS`，但该候选后来发现误带受保护文件和两处本机路径，因此这组发布数字及其两个结果 JSON 已作废；应用全量结果仍只证明当时应用代码快照，不能替代 `1.1.1` 的新发布边界检查。
- 回退方式：不建议单独回退；回退会重新允许测试在“全部通过”的同时改写另一棵源码，或让外部 Git 环境改变验证对象。失败现场和测试沙箱继续保留，不能自动删除。

## SYS-RELEASE-V1.0.1：候选目录失败保留与原子不覆盖发布

- 日期：2026-07-28
- 状态：已完成定向实现、独立审核和最终全量复跑。
- 本次改的是哪个对象：`tools/build_release_staging.py` 的候选目录失败处理和最后发布动作；不改训练、评估、论文事实、正式数据库或旧 既有项目。
- 目标问题：旧实现先核对临时目录路径再递归删除，核对与删除之间若目录被调包，可能删到外部位置；最后发布又使用“先检查、再 `os.replace`”，不能原子证明并发出现的用户目录绝不被替换。
- 采用技术：失败时只用不跟随链接的字面绝对路径记录 `retained_staging_path`、`retention_status` 和提示，不执行 `resolve`、`rmtree`、`unlink`、`remove`、`rmdir` 或 `chmod`。成功发布在 Windows 使用 `os.rename`，Linux 使用 `renameat2(RENAME_NOREPLACE)`，macOS 使用 `renameatx_np(RENAME_EXCL)`；目标在最后一瞬间出现时原子失败并保留双方目录。
- 替换了什么：移除失败路径的 `shutil.rmtree` 和成功路径的 `os.replace`；不改变成功候选包的文件白名单、50 MiB 上限或最终目录布局。
- 实际可见效果：正常构建仍只出现完整候选目录；失败时不会发布半成品，而是在同级留下 `.release-staging-*` 供人工检查。并发创建的目标目录及其中用户文件保持原样，失败现场也不被自动删除。
- 选择原因：第一版宁可留下几十 MB 以内的可见现场，也不能为自动收尾承担误删用户目录的风险；原子不覆盖改名把“目标是否已经存在”的判断交给一次操作完成。
- 已知限制：失败现场会占用磁盘，需要用户看清路径后另行授权清理；当前正式支持范围仍为 Windows，Linux 和 macOS 分支只做代码与单元契约兼容，需在真实机器上再验。
- 素材位置：`tools/build_release_staging.py`、`tests/test_release_staging.py`、`docs/ROADMAP.md`。
- 验证命令与结果：TDD 红测先证明旧实现会调用递归删除、解析被调包根并继续使用可覆盖发布；修复并补外部 Git 环境回归后，`tests/test_release_staging.py` 为 `23 passed, 33 subtests passed`。新增测试分别证明五种破坏性 API 零调用、失败根不解析/跟随、目标最后瞬间出现时用户标记文件和失败 staging 均保留，以及外部 `GIT_*` 不能重定向构建；独立安全复核结论为 `APPROVE`。
- 回退方式：只回退本条代码会重新引入误删和覆盖竞争风险，不建议单独回退；已经留下的失败 staging 继续保留，回退动作也不得自动删除。

## SYS-V2.11.0 / DATA-RESEARCH-PAPER-EXPORT-V2.0.3：科学语义重算收口

- 日期：2026-07-26
- 状态：已完成。
- 本次改的是哪个对象：`cv-research-handoff/v2` 独立核验器的 Brief、Claim comparison、Run 原始日志、Source 身份和 manifest 标量规则；不改发布身份。
- 目标问题：协调改写科学文件、manifest 和摘要后，旧核验器可能接受删空实验资产却仍为 `paper_ready`、自造 comparison 数字、paper_scope 脱离 Brief、Source 身份字段改成容器类型，以及 bool/UUID/替代编号边界绕过。
- 采用技术：从包内 Brief 重新绑定 paper_scope；复用 Seal 的 result Claim 解析逻辑，从 Task/Run 快照重算指标、comparison 和中英文数字；Run 快照显式保存并与 live 账本绑定 `artifact_paths`，再用 raw_log、artifact_paths、Run.files 和 Asset 完整重算 readiness/missing；Source 只接受 paper 且身份字段复用上游文本规范；UUID、`layout_revision` 和 supersedes 使用类型敏感与单向编号检查。external/restricted 的已选 Run 文件保留空内容身份的 Asset 锚点。
- 替换了什么：替换“相信包内 comparisons 数字”“只检查 readiness 自洽”“Source 元数据只透传”和 Python `True == 1` 可通过布局修订的行为；不修改 schema、CLI、producer、SKILL 或 SYS 发布身份。
- 实际可见效果：删空 raw_log/Asset、伪造 99%、scope 与 Brief 脱钩、Source dict/list、非规范 UUID、自指或未来替代编号即使重算全部摘要仍会被拒绝；合法 external/restricted 资产仍可作为 incomplete 引用包独立核验。
- 选择原因：checksum 只能证明字节彼此对得上，论文系统还需要能从同一包内的更底层事实重新推出写作范围、数字和证据是否齐全。
- 已知限制：包仍没有密码学签名，独立核验不能证明作者或外部来源真实性；完全未在科学文件中留下任何锚点的历史外部事实不能仅靠目录恢复。当前源码仍是未发布开发态；总计划 Task 6 将同时升级 SKILL/SYS、producer 精确兼容白名单和 golden。
- 素材位置：`skills/cv-experiment-workflow/scripts/workflow_core/paper_package.py`、`skills/cv-experiment-workflow/scripts/workflow_core/paper_package_delivery.py`、`tests/test_paper_package_export_v2.py`、`docs/integration/research_paper_package_v2.md`、`docs/reviews/2026-07-26-paper-package-export-v2-review.md`。
- 验证命令与结果：语义攻击及缺失 artifact 兼容定向集为 `8 passed, 10 subtests passed`；导出专项为 `29 passed, 21 subtests passed`；内部封存专项为 `53 passed, 55 subtests passed`；其余 Brief、Task/Run/Evidence、v2 项目、Skill 文档和 v1 交接五文件为 `119 passed, 3 skipped, 87 subtests passed`。当前七文件集合合计 `201 passed, 3 skipped, 163 subtests passed`；3 个 skip 仍是既有 Windows 符号链接权限分支。Python 编译与 `git diff --check` 通过。
- 回退方式：回退本条语义收紧可恢复旧独立验证行为；已导出的目录不得自动删除或改写。

## SYS-V2.11.0 / DATA-RESEARCH-PAPER-EXPORT-V2.0.2：独立审核文件系统收口

- 日期：2026-07-26
- 状态：已完成。
- 本次改的是哪个对象：独立核验的初始文件身份绑定、英文结果数字、Windows 暂存路径、异常清理和 rename 提交判定。
- 目标问题：控制文件或 included 资产可在读取时被替换再恢复；英文结果陈述可协调重签名后夹带伪造数字；mkdir 与 rename 的中断窗口会留下残目录或含糊提交结果；最终可用路径会被更长的暂存名撑爆。
- 采用技术：通用有界读取增加 `_expected_before`，verify 把首次枚举的 `stat` 传入每次读取与哈希；非空 `statement_en` 复用 Seal 数字核对；staging 使用与 `PKG-xxxx` 等长的 8 字符随机名，并在私有目录内直接 `xb + flush + fsync`；无 identity 时只安全 `rmdir` 空普通自有目录；rename 异常后以目标目录身份和完整独立 verify 判定是否已提交。
- 替换了什么：替换“只在最后二次枚举发现变化”、长 UUID staging、私有 staging 内重复文件级事务临时名，以及 rename 异常后一律报失败的行为；不修改 schema、CLI 和正式目录名。
- 实际可见效果：替换—读取/哈希—恢复原文件不能通过；英文 `Accuracy reaches 99%.` 不能冒充现场 `0.75`；Windows 上 248 字符最终文件路径能完成导出；mkdir 成功即中断不留空目录；rename 成功即中断会安全返回 `exported`。
- 选择原因：目录整体发布已经提供原子边界，私有 staging 内无需再增加更长的文件级临时名；提交恢复只有同时证明目录身份和完整内容才不会把竞争者目标误认成本次结果。
- 已知限制：短 staging 不扩大操作系统对最终路径本身的支持范围，只保证暂存过程不比同一最终路径更长；checksum 和 producer 仍不是签名。
- 素材位置：`skills/cv-experiment-workflow/scripts/workflow_core/io.py`、`skills/cv-experiment-workflow/scripts/workflow_core/paper_package_delivery.py`、`tests/test_paper_package_export_v2.py`、`docs/integration/research_paper_package_v2.md`、`docs/reviews/2026-07-26-paper-package-export-v2-review.md`。
- 验证命令与结果：五类问题的 6 个定向测试全部通过；导出专项为 `22 passed, 11 subtests passed`；与内部封存组合回归为 `75 passed, 66 subtests passed`；封存、导出、Brief、Task/Run/Evidence、v2 项目、Skill 文档和 v1 交接的最终七文件组合回归为 `194 passed, 3 skipped, 153 subtests passed`。3 个 skip 仍是既有 Windows 符号链接权限分支。Python 编译与 `git diff --check` 通过。
- 回退方式：回退本条独立审核修复可恢复旧暂存和核验行为；已导出的最终目录不得自动删除或改写。

## SYS-V2.11.0 / DATA-RESEARCH-PACKAGE-SEAL-V1.0.1：独立复审收口

- 日期：2026-07-26
- 状态：已完成。
- 本次改的是哪个对象：内部封存发布后的现场复核、Claim 数字扫描、external/restricted Asset 的读取边界。
- 目标问题：发布后的最终复核可能命中发布前缓存；Unicode 兼容数字和残缺科学计数可能绕过指标核对；声明为 external/restricted 的本地路径仍会被打开和哈希。
- 采用技术：最终复核新建独立 `_LiveFileHashSession`；数字先经过 NFKC 与 Unicode 十进制归一化，再用完整 token 覆盖检查拒绝残片；external/restricted 在文件存在性检查之前直接生成引用行，大小和 SHA-256 固定为空。
- 替换了什么：替换最终复核复用旧缓存、只扫描常规数字形式，以及“路径存在就读取”的外部资产行为；不改变 schema 和正常 available Asset 的现场核验。
- 实际可见效果：同 inode、同大小、同修改时间的发布瞬间原位改写仍会被最后复核发现；上标、圈号、全角数字和 `0.75e+` 不能进入已确认结果 Claim；external/restricted 即使本机有文件也零读取、零哈希且不占现场哈希预算。
- 选择原因：外部/受限是明确的“不读取内容”边界，最终复核也必须真正独立，不能让性能缓存改变封存结论。
- 已知限制：NFKC 只用于识别可疑数字写法，不把兼容形式静默改写进 Claim；内部包仍没有密码学签名。
- 素材位置：`skills/cv-experiment-workflow/scripts/workflow_core/paper_package.py`、`tests/test_paper_package_v2.py`、`docs/integration/research_paper_package_v2.md`、`docs/reviews/2026-07-26-paper-package-seal-v1-review.md`。
- 验证命令与结果：三项定向测试及四个数字反例全部通过；与目录导出组合回归为 `69 passed, 66 subtests passed`；封存、导出、Brief、Task/Run/Evidence、v2 项目、Skill 文档和 v1 交接的最终七文件组合回归为 `188 passed, 3 skipped, 153 subtests passed`。3 个 skip 仍是原有 Windows 符号链接权限分支。Python 编译与 `git diff --check` 通过。
- 回退方式：回退本次独立复审修复可恢复旧行为；已生成的 `PKG-xxxx` 不得自动删除或重写。

## SYS-V2.11.0 / DATA-RESEARCH-PAPER-EXPORT-V2.0.1：未读取资产身份收紧

- 日期：2026-07-26
- 状态：已完成。
- 本次改的是哪个对象：`cv-research-handoff/v2` 独立核验器对非 available Asset 的内在身份规则。
- 目标问题：协调修改科学文件与摘要后，独立交付包可能给 external/restricted Asset 填入未经读取的 `size_bytes/sha256`，最后只在交付策略层报笼统不一致。
- 采用技术：独立内在校验明确要求所有非 available Asset 的 `size_bytes` 与 `sha256` 同时为 `null`，并给出稳定错误。
- 替换了什么：把“非 available Asset 可同时携带大小与摘要”收紧为“未读取就不得声明内容身份”；不改变 included/referenced/omitted 的交付决定。
- 实际可见效果：即使攻击者同时重算科学内容摘要、manifest 和 checksum，独立核验也会直接拒绝未读取资产的伪造身份。
- 选择原因：科研端和论文接收端必须对同一字段表达相同语义，避免摘要看起来完整却没有任何现场读取证据。
- 已知限制：独立核验仍只能证明交付目录的字节和内在关系一致，不能证明作者或外部引用内容。
- 素材位置：`skills/cv-experiment-workflow/scripts/workflow_core/paper_package_delivery.py`、`tests/test_paper_package_export_v2.py`、`docs/integration/research_paper_package_v2.md`。
- 验证命令与结果：协调篡改反例先因旧错误而红，收紧后按明确的未读取资产错误转绿；封存与导出组合回归为 `69 passed, 66 subtests passed`；最终七文件组合回归为 `188 passed, 3 skipped, 153 subtests passed`。
- 回退方式：回退本次独立核验收紧即可恢复旧的内在字段容忍；已经导出的目录不自动删除或改写。

## SYS-V2.11.0 / DATA-RESEARCH-PAPER-EXPORT-V2：目录交付与独立核验

- 日期：2026-07-26
- 状态：已完成（科研端导出和独立核验；PaperFlow 接收仍待后续任务）。
- 本次改的是哪个对象：内部 `PKG-xxxx` 到 `cv-research-handoff/v2` 外部目录的映射、资产复制策略、原子发布与无项目依赖核验器。
- 目标问题：内部封存包只固定科研事实，没有形成论文系统能直接接收、归档后仍可核验、又不会误复制受限材料的正式交付目录。
- 采用技术：新增 `paper_package_delivery.py`、`export-paper-package --project --package --mode --out` 和 `verify-paper-package --package-dir`。外部目录固定包含 `manifest.json`、六个原始科学文件、`checksums.sha256` 与 `assets/included/<sha>/<filename>`；manifest 保留内部包全部身份字段，并增加文件和资产交付清单。hybrid / full、许可、隐私、可用性和同 SHA-256 最严格规则由一个纯函数同时供导出与验证使用。复制在同一源文件句柄上边写边算摘要，前后检查身份、大小和时间；整个目录先在同级 UUID staging 构建并独立验证，再无覆盖发布。
- 替换了什么：没有替换 v1 单 Run `export-paperflow` / `verify-paperflow-handoff`，也不改内部八文件封存包；只把过去“内部包仍需人工拷贝”的缺口替换为正式 V2 目录导出。PaperFlow 接收仍保持独立后续模块。
- 实际可见效果：用户把 `PKG-0001` 导出到同名最终目录后，可在删除或移动科研项目后单独运行 verify。`paper_ready` 的必需资产必须实际 included；`incomplete` 可以如实交付但不会伪装完整。许可、隐私或重复 SHA 声明不能靠改 manifest 和重算 checksum 绕过。路径、链接、预算、未知文件、读取期间新增/替换、并发同目标、源文件 TOCTOU 与 `KeyboardInterrupt` 都会失败关闭且不留下可见半包。
- 选择原因：交付目录是科研与论文系统唯一通用接口；先完成“内部事实包 → 独立目录 → 独立核验”的最短闭环，避免把 PaperFlow 接收状态反写进科研账本。
- 已知限制：SHA-256、checksum 和 producer 不是签名，不能证明作者、生成程序或来源身份；独立核验只能证明目录字节、声明和内在关系一致。当前不自动发送目录，不创建 PaperFlow 论文，不替论文系统选择方法卡或批准正文。
- 素材位置：`skills/cv-experiment-workflow/scripts/workflow_core/paper_package_delivery.py`、`skills/cv-experiment-workflow/scripts/workflow_core/paper_package.py`、`skills/cv-experiment-workflow/scripts/rw.py`、`tests/test_paper_package_export_v2.py`、`docs/integration/research_paper_package_v2.md`、`docs/reviews/2026-07-26-paper-package-export-v2-review.md`。
- 验证命令与结果：TDD 首轮 3 个测试因 direct API 与 CLI 均不存在按预期失败；最小闭环转绿后，安全矩阵首轮 14 项中 6 项按预期暴露状态降级、最终目录漂移、隐私优先级、Windows hardlink 识别和测试现场恢复问题。修复并扩展内在科学校验后，导出专项为 `15 passed, 11 subtests passed`；与内部封存组合回归为 `65 passed, 62 subtests passed`；导出、封存、Brief、Task/Run/Evidence、v2 项目、v1 交接和 Skill 文档组合回归为 `184 passed, 3 skipped, 149 subtests passed`。3 个 skip 仍是原有 Windows 符号链接权限分支。Python 编译与 `git diff --check` 通过；独立审核由总任务后续统一执行并回填审核记录。
- 回退方式：回退本次提交可移除 V2 导出与独立验证命令；内部 Research Brief 和八文件 `PKG-xxxx` 不受影响。已经导出的外部目录属于用户交付物，不得由回退自动删除。

## SYS-V2.11.0 / DATA-RESEARCH-PACKAGE-SEAL-V1：不可覆盖的内部论文封存包

- 日期：2026-07-26
- 状态：已完成（仅科研项目内部封存；对外导出、独立验证和 PaperFlow V2 接收仍待后续任务）。
- 本次改的是哪个对象：科研端 `PKG-xxxx` 内部封存目录、严格 selection schema、证据和素材门槛、并发发布以及只读状态摘要。
- 目标问题：Research Brief 只有研究意图，还缺一份把真实结果、创新边界、外部论文用途、实验关系和写作素材冻结在一起，并且后续修订不会覆盖历史的论文包。
- 采用技术：继续使用 Python 标准库、规范 JSON / JSONL 与 SHA-256；新增 `seal-paper-package --project --brief --selection`。系统从当前项目账本重建 Run、Evidence、Task、Idea、Module 与 Source 事实，不相信 selection 自报的结果数字、文件大小、文件摘要或可用状态。包固定生成八个文件；七个科学正文文件按文件名排序写入 `checksums.sha256`，六个科学内容文件再用带长度边界的字节序列计算内容 SHA-256。首次和后续发布都先在项目根 UUID staging 中完整构建、复核，再用不覆盖发布；进程锁保证两个真实命令行进程竞争时只产生连续且完整的包。严格读取复用生成端六类规范化器，嵌套 JSON 按类型比较，避免 `false == 0`；完整 v2 快照只读一次，并按规范路径和文件身份去重现场哈希。一次快照最多读取 512 MiB，完整数据集、checkpoint 和整仓代码应使用 external/referenced。
- 替换了什么：没有替换 v1 `cv-research-handoff/v1`、Research Brief、Task → Run → Evidence 主链或现有五元内部状态返回值；只把过去需要人工拼接的论文事实选择，替换为严格、可重放的内部封存步骤。
- 实际可见效果：用户给出严格 selection 后得到 `PKG-0001 / package_revision=1 / status=sealed`。至少有一个 confirmed result 且没有缺项时为 `paper_ready`；只有存在 innovation Claim 才要求完整创新来源链，没有 innovation Claim 时不额外要求创新来源链。必需素材缺失、不可复制、受限，或漏选 live Run 的 `raw_log` / artifact 时仍可封存，但明确标成 `incomplete` 并列出稳定缺项。`hybrid` 与 `full` 对同一科学事实得到相同内容摘要。显式 `supersedes_package_id` 只能单向替代同项目未被直接替代的旧包；旧包字节保持不变，状态现场给出反向替代与来源撤销提示。
- 选择原因：先跑通“真实账本事实 → 严格选择 → 不可覆盖封存 → 状态可见”的最短闭环，避免在科研事实尚未冻结前提前加入素材复制、对外传输或 PaperFlow 接收逻辑。
- 已知限制：内部包没有密码学签名，producer 精确白名单、`checksums.sha256` 与 `content_sha256` 都不是签名，不能证明生成工具或作者身份；它们的价值是规范字节、交叉校验和项目账本约束。当前不复制素材，不生成 `cv-research-handoff/v2` 外部目录，也不提供 Task 3 的独立验证器或 PaperFlow V2 接收端。
- 素材位置：`skills/cv-experiment-workflow/scripts/workflow_core/paper_package.py`、`skills/cv-experiment-workflow/scripts/workflow_core/planning.py`、`skills/cv-experiment-workflow/scripts/workflow_core/validation.py`、`skills/cv-experiment-workflow/scripts/rw.py`、`skills/cv-experiment-workflow/SKILL.md`、`tests/test_paper_package_v2.py`、`tests/test_skill_docs.py`、`docs/integration/research_paper_package_v2.md`。
- 验证命令与结果：TDD 首轮红测因 CLI 尚无 `seal-paper-package` 按预期失败；实现后的专项测试覆盖真实完整项目、八类 Claim、中英文结果数字与现场指标不一致、未确认/失败/撤销证据、创新来源链、外部论文变更、资产许可与可用性、hybrid/full 内容一致、替代关系、重算全部摘要后的正文篡改、合法 Run/Task 状态前进、首次与后续发布故障，以及线程和两个真实操作系统进程竞争。提交后正式规格复核又用红测补齐 live Brief scope、Asset/Experiment 现场身份与完整运行文件清单、真实 log/metrics/Module/Source 漂移、协调删除后伪装就绪、裸数字与半角/全角百分号区分、中文紧贴数字、前导点/尾点小数、完整科学计数 token、未绑定正负号、result/innovation 共用唯一 tokenizer、selection 枚举错误类型，以及最终质量复核的 staging 中断、producer 白名单、统一回读、单次状态快照、512 MiB 总预算、类型敏感比较、多角色 Source、2048 上限和 PKG ID 耗尽。最终专项结果为 `50 passed, 51 subtests passed`；与 Research Brief、v2 项目、Task/Run/Evidence、Skill 文档和 v1 PaperFlow 交接的组合回归为 `168 passed, 3 skipped, 138 subtests passed`。创新夹具补齐初始化锁和 bootstrap 临时文件忽略规则后，目标用例连续运行 5 次全部通过。Python 编译与 `git diff --check` 通过；完整审核与修复回应见 `docs/reviews/2026-07-26-paper-package-seal-v1-review.md`。
- 回退方式：回退本次提交可移除 Seal Package 命令、严格包校验和状态摘要；已经生成的 `paper-packages/packages/PKG-xxxx` 必须由用户自行保留或迁移，不能由回退流程自动删除。

## SYS-V2.11.0 / DATA-RESEARCH-BRIEF-V1：不可覆盖的 Research Brief

- 日期：2026-07-25
- 状态：已完成（仅 Research Brief；V2 正式包仍在计划中）。
- 本次改的是哪个对象：科研端 Research Brief 的 CLI、账本 schema、严格校验和只读状态摘要。
- 目标问题：研究进入实验前缺少一份由用户明确确认、能区分原创与论文启发贡献、后续修订又不会覆盖历史的结构化摘要。
- 采用技术：新增 `workflow_core/paper_package.py` 和 `save-research-brief --project --manifest`；首次写入先在项目根 UUID staging（暂存）目录中完整生成并校验 `briefs/BRIEF-0001.json`，再一次发布为 `paper-packages`，异常与 `KeyboardInterrupt` 清理本次暂存目录，目标竞争时拒绝覆盖。后续使用 `BRIEF-xxxx.json` 原子新建不可覆盖记录。旧 v2 项目不要求补目录；目录一旦存在就严格校验非空账本、字段、连续 ID、修订号、项目 UUID、合法 RFC3339 时间与 Source 引用。`terminology` 至少一项。`workflow-status` 在同一项目锁快照里给出数量和最新摘要，现有 5 元内部返回值与四类 catalog 计数保持不变。
- 替换了什么：没有替换 v1 `cv-research-handoff/v1`、`paper_handoff.py` 或既有 Task → Run → Evidence 主链；只新增 Research Brief 前置账本。
- 实际可见效果：用户保存完整 manifest 后得到 `BRIEF-0001 / revision=1`；再次保存得到新文件和新修订号，旧文件字节不变。论文启发或改写贡献只有引用当前存在的 `SRC-xxxx` 才能落盘；旧项目不产生目录也能继续校验和查看状态。真实旧 `1.2.3 / SYS-V2.10.3` 的 57 文件精确锁可显式升级到当前未发布开发源码，摘要改一位仍拒绝；正式版本号留到总计划 Task 6 统一升级。
- 选择原因：第一版只跑通“用户确认摘要 → 不可覆盖落盘 → 状态可见”最小闭环，把未来 Claim、Experiment、Seal Package 和 PaperFlow V2 留在独立后续任务，避免提前堆抽象。
- 已知限制：当前没有创建 `PKG-xxxx`、没有 Seal Package、没有 V2 导出或接收端；Research Brief 也不代表实验结论已经被 Run 与 Evidence 验证。
- 素材位置：`skills/cv-experiment-workflow/scripts/workflow_core/paper_package.py`、`skills/cv-experiment-workflow/scripts/rw.py`、`tests/test_research_brief.py`、`docs/integration/research_paper_package_v2.md`。
- 验证命令与结果：TDD 首轮红测因 CLI 尚无 `save-research-brief` 按预期失败；审核补充红测分别证明非法 `+00:60`、空术语、首次中断清理、空目标竞争、空 Brief 账本、未知 POSIX 平台失败关闭和真实旧 1.2.3 锁升级在修复前失败。修复后 Research Brief 专项为 `13 passed, 5 subtests passed`；Brief、旧锁、v1/v2 兼容、状态、规划、文档和初始化组合回归为 `145 passed, 8 skipped, 66 subtests passed`。21 个测试文件的可运行部分合计 `438 passed, 39 skipped`；另有三个原有测试分支因当前 Windows 账户没有创建符号链接的权限而无法完成。Python 编译、个人路径检查和 `git diff --check` 通过，完整证据见本次审核记录。
- 回退方式：回退本次提交可移除 Research Brief 入口和可选目录识别；已经生成的 `paper-packages/briefs` 必须先由用户自行保留或迁移，不能由回退流程自动删除。

## DATA-RESEARCH-HANDOFF-V1.1.1：producer 兼容语义勘误

- `SYS-*`：状态机、命令行、执行器、账本和四条实验路线等底层功能。
- `SKILL-*`：用户与 Agent 使用的工作流入口、角色分工和协作规范；正式发布号写成 `SKILL-RELEASE-*`。
- `UI-*`：流程图和其他可视化页面。
- `PACK-*`：某一个科研方向的可运行代码模板、指标合同和仓库初始化素材。
- `DOC-*`：说明文档、审核记录和其他交付文件。

历史上曾把不同对象混在同一条 `v2.x` 编号中，旧标题和文件名保留以便回查；从本次整理起按下面的名字理解：

| 原历史编号 | 新分类版本 | 实际对象 |
| --- | --- | --- |
| `v2.1`、`v2.2`、`v2.3`、`v2.4`、`v2.7`、`v2.8` | 对应 `SYS-V2.1` 至 `SYS-V2.8` | 系统功能、隔离边界和执行闭环 |
| `v2.5` | `DOC-ARCHIVE-V1` | 项目文档归档规则 |
| `v2.6` | `UI-V2.0` | 严格框架图的编辑排版版本 |
| `SKILL-V2.9` 候选 | `SKILL-POLICY-V2.9` | 固定两轮审核草案，最终未启用 |

## 2026-07-27 收口前规划快照（历史）

下面保留开工与分支集成阶段的版本快照，只用于回查当时的计划和依赖；当前状态一律以文件顶部“当前版本一览”为准。

- 当前精简完整系统计划：`SYS-V2.13.0`，状态为计划中；目标是六方向代码仓库、Codebase/Git 规则、Research Package V2 和统一入口。
- 当前发布暂存复查工具：`SYS-RELEASE-V1.0.0`，状态为已完成候选；发布树采用文件与目录双快照，Windows 子进程先挂起并加入 Job 后才运行。
- 当前精简入口计划：`UI-CONSOLE-V2.0.0`，状态为计划中；把现有三页只读控制台收成科研到 PaperFlow 的单入口。
- 当前六个方向包候选：`PACK-CLS/DET/SEG/INSTSEG/SR/GZSL-V1.0.0`；均可创建独立仓库并运行合成冒烟与对应的本地小型数据基线，尚未安装到正式目录，依赖和大数据不打包。
- 当前六方向整合合同：`PACK-INTEGRATION-SIX-V1.0.0`，状态为已完成候选；六包统一使用固定 Run 输出根和同一份八字段下载资源格式，等待晚增长加固合入后的整组复测。
- 当前科研论文交付候选：`DATA-RESEARCH-PAPER-EXPORT-V2.1.2`，producer 为 `cv-experiment-workflow / 1.5.0 / SYS-V2.13.0`。它只导出已绑定 Codebase、正式数据、可写论文、成功关闭、四项质量全 valid、权威输出已封存且 Evidence 已走完 `single_run → confirmed` 的 Run；真实最小包已由 PaperFlow `PF-SYS-V5.1` 候选接收器直接验收，尚未安装或发布。
- 当前三包第二轮复核加固：`PACK-HARDENING-SEG-SR-GZSL-V1.0.2`；已补齐清单自身字节的总量计算，并关闭“安全读取返回后文件继续增长”的竞态，等待下一轮独立 Reviewer 复核。
- 当前实施计划文档：`DOC-LEAN-EXECUTION-PLAN-V1.0.0`；暂缓能力统一记录在 `docs/ROADMAP.md`。
- 当前正式运行绑定组件：`SYS-FORMAL-RUN-V1.0.2`，状态为已完成候选；绑定 Run 从精确 commit 的独立只读快照执行，冻结 Adapter 的句柄换绑和子进程临时目录边界已经两轮独立复核，完整正式 Run 回归仍在最终复跑。
- 当前公平比较门禁：`SYS-FAIR-COMPARISON-V1.1.0`，状态为已完成候选并通过独立复核；执行器已接通两条 Run 的现场输出封存复核，比较规则版本随 Run 冻结并进入输出封存链。
- 当前方向包下载清单合同：`SYS-DOWNLOAD-CONTRACT-V1.0.0`，状态为已完成候选、待独立审核；六类模板只登记固定发布页或带摘要的直接制品，不自动下载、不捆绑大文件。
- 当前系统源码候选：`SYS-V2.12.0`，完成 PaperFlow V1 加固、启动必填检查和三页控制台共用的只读数据；尚未安装。
- 当前 Skill 源码候选：`SKILL-RELEASE-V1.4.0`，本机安装版仍是 `SKILL-RELEASE-V1.2.3`，旧个人实例锁仍是 `SKILL-RELEASE-V1.2.1`。
- 当前本地控制台版本：`UI-CONSOLE-V1.0.0`，提供三个只读页面；尚未安装到本机 Skill，也未接入写入或训练。
- 当前框架图版本：`UI-V3.1`，V3 页面已把旧“版本号锁”文字同步为代码指纹锁；V2 原版保留不改。
- 当前旧个人实例人工审核包版本：`DOC-AUDIT-V1.0.0`，实例内保存展开目录，模板仓库内保存同字节 ZIP 备份。

## SYS-FORMAL-RUN-V1.0.2 / PACK-BOUND-COMPAT-V1.0.0：冻结执行护栏与五方向绑定接入

- 日期：2026-07-27
- 状态：已完成候选；机器回归和两类独立审核通过，尚未安装。
- 本次改的是哪个对象：冻结 Adapter 的 Python 文件写入与子进程边界、可选依赖警告，以及 DET、SEG、INSTSEG、SR、GZSL 五个方向模板读取中央绑定 Run 的兼容层；不改变训练目标和指标定义，也不修改正式目录、个人实例、PaperFlow 或旧 既有项目。
- 目标问题：原五个方向模板主要按各自直接运行格式读取 frozen，不能完整消费中央绑定 Run；冻结 Adapter 的 `os.open → os.fdopen` 许可还可能被文件描述符换绑绕过，子进程临时根被搬走时父进程会静默跳过清理。
- 采用技术：方向模板同时严格识别旧 direct frozen 与中央 bound frozen，并核对代码/环境声明、clean、`run_kind`、`paper_eligible` 和中央数据身份。冻结 Adapter 给写文件描述符绑定设备号、文件号和普通文件类型，消费一次性令牌，并在受控调用中拒绝 `os.dup/os.dup2`；子进程七类临时/缓存环境统一指向 `.adapter-child-*`，child policy 保护该根，父进程按目录身份清理。合成调试的可选依赖缺失作为显式 warning 返回，正式模式继续阻塞。
- 替换了什么：替换五个模板只能读取旧 frozen 的行为；替换“只在 fdopen 当下检查”和“临时目录原路径消失就当作已清理”的旧边界。Adapter 修改外部数据不再等事后漂移检查，而是在写入处直接失败。
- 实际可见效果：五个方向可从中央绑定快照运行，也继续兼容旧直接测试；句柄在 fdopen 前后换绑、关闭后编号复用和 child 临时根搬移都不能形成成功结果；Adapter 异常只暴露原始请求命令；合成调试缺少可选包时页面能看到警告，正式实验不会误放行。
- 选择原因：中央系统和方向模板各自只核对自己掌握的事实，避免把领域 Adapter 变成第二套总账；对 Windows 协作型 Python Adapter 直接禁止描述符复制，比维护一个可被缓冲写绕过的文件对象代理更小、更容易验证。
- 已知限制：这不是操作系统沙箱，不承诺阻止 `ctypes`、C 扩展、原生恶意代码或同账户外部进程；五个自包含包有少量重复边界代码。中央正式 evidence 真数据闭环将在总集成验收统一运行。
- 素材位置：实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/adapters.py`、`engine.py` 和五个方向包；测试位于 `tests/test_adapter_snapshot_isolation.py`、`tests/test_domain_pack_bound_compat.py`、五个方向测试及 `tests/test_formal_run_git_gate.py`；中文审核记录为 `docs/reviews/2026-07-27-bound-adapter-and-domain-pack-integration.md`。
- 验证命令与结果：五方向完整回归 `66 passed, 59 subtests passed`；绑定兼容修复后 `4 passed, 43 subtests passed`；五种正式本地小数据 `5 passed, 11 subtests passed`；DET/INSTSEG 修复后完整回归 `30 passed, 55 subtests passed`；中央 DET 快照闭环通过；最终冻结后的 Adapter 与正式 Run 全组 `69 passed, 1 skipped, 55 subtests passed`。独立复核额外验证 `os/nt dup2`、replace、move、copy 和数据身份伪造；`py_compile` 与 `git diff --check` 通过。
- 回退方式：回退本条对应的适配器、五方向兼容、测试和文档提交即可；不需要迁移旧 Run，不会触碰正式工作流、个人实例、PaperFlow 或旧 既有项目。

## SYS-FAIR-COMPARISON-V1.1.0：现场封存复核与比较规则冻结

- 日期：2026-07-27
- 状态：已完成候选；78 项机器回归通过，三轮独立反方复核最终为 `APPROVE`，尚未安装到正式目录。
- 本次改的是哪个对象：四条实验路线在执行器收尾时生成永久比较结论的方式、离线项目校验和历史结论兼容；不改训练算法、原始指标、六方向模板、PaperFlow、个人实例或旧 既有项目。
- 目标问题：`V1.0.0` 已有公平比较门禁，但执行器尚未把真实 `output_seal` 现场复核器传进去；离线校验一度还会根据可改写的比较结果外形猜新旧版本，使新正式结论能整体伪装成旧格式，并和被改写的 baseline 一起制造错误差值。
- 采用技术：正式收尾先重新读取来源 Run 与候选 Run，再分别调用 `verify_run_output_seal(project, run_id)`；任一现场副本漂移都让 Task 留在 `reviewing` 且不写结论。新 Run 的冻结环境增加固定 `workflow_comparison_policy`，该标记进入环境指纹、整份 `frozen_digest` 和 `output_seal.frozen_digest`。离线校验只按这个冻结标记选择新算法；没有标记的历史 Run 只接受最早的 pre-fair 形状，未发布的过渡形状不再兼容。
- 替换了什么：替换“根据 comparison 有没有某些字段来猜年代”和“执行器收尾仍使用没有现场复核器的比较”两条旧路径；历史 pre-fair 原始记录继续可读，不能被静默升级为正式结论。
- 实际可见效果：两条合法、已关闭且现场封存仍一致的正式 Run 才能产生 `formal` 差值；修改差值、范围、阻断原因、整段换成旧形状、同时修改 baseline，或动过任一侧封存文件，都会失败关闭。新旧 debug Run 从中断状态恢复后也能通过完整项目校验。
- 选择原因：比较结论会直接进入论文主张，规则身份必须在实验开始前锁住，不能由结论文件自己声明；复用 Run 冻结摘要和现有输出封存，比新增第三本账更小也更容易回查。
- 已知限制：没有冻结标记的真正历史记录只能按旧算法做一致性核对，不能补做当年的现场复核；普通本地 SHA-256 仍不是独立数字签名。当前候选尚未安装，完整系统全量与科研包到 PaperFlow 的总闭环仍在后续步骤。
- 素材位置：`skills/cv-experiment-workflow/scripts/workflow_core/engine.py`、`run_identity.py`、`runs.py`；测试位于 `tests/test_output_seal.py`、`tests/test_experiment_routes.py`、`tests/test_engine_tune.py`、`tests/test_formal_run_git_gate.py`；独立记录为 `docs/reviews/2026-07-27-fair-live-comparison-integration.md`。
- 验证命令与结果：`python -m unittest tests.test_fair_comparison tests.test_experiment_routes tests.test_output_seal tests.test_engine_tune -v` 为 `78 passed`；关键正式双 Run、三类降级、两侧封存漂移和环境冻结为 7 项通过；恢复后完整校验为 1 项通过；`py_compile` 与 `git diff --check` 通过。独立 Reviewer 又执行旧记录恢复、删除规则标记后重签、未知算法标记和 baseline 联动攻击，最终结论为 `APPROVE`。
- 回退方式：回退本条对应代码、测试和文档即可恢复 `V1.0.0` 的只注入接口状态；不删除既有 Task、Run、Evidence、封存输出或用户数据。

## SYS-FAIR-COMPARISON-V1.0.0：正式差值与模块效果中央门禁

- 日期：2026-07-27
- 状态：已完成候选；机器专项与旧执行器回归通过，尚未安装到正式目录，等待产物封存组件合入后接通现场复核器并执行最终独立审核。
- 本次改的是哪个对象：四条实验路线共用的比较结果生成、Innovation 在启动检查与 Task 中的可选来源 Run 声明，以及对应回归测试；不改训练算法、原始指标、封存算法、PaperFlow 或旧 既有项目。
- 目标问题：旧逻辑只要拿到 `baseline` 和候选指标就会直接计算 `delta`、`module_effect`、`difference` 与 `within_tolerance`，调试运行、未封存运行、数据集不同或评估口径不同也可能留下看似正式的提升结论。
- 采用技术：新增一个纯中央门禁，调用者必须同时交付来源 Run、候选 Run 和只接收 Run ID 的现场封存复核函数。两个 Run 都要绑定代码身份、属于真实实验、允许写入论文、成功且质量检查全部有效、已关闭并有非空封存记录；门禁还会严格比较 `data` 与 `evaluation` 两个指纹。现场复核函数必须为两个 Run 各返回与当前比较输入完全相同的封存记录，异常、空返回、返回别的记录或缺少函数都按失败关闭。Innovation 可以显式声明 `source_run_ref`，但没有合法来源时仍可运行和查看数字，不能形成正式差值。
- 替换了什么：把“看到两个数就直接相减”替换为“先保存原始来源值与候选值，再由中央门禁决定是否填写正式差值”。没有复制产物封存和文件哈希逻辑，后续只需把 `verify_run_output_seal(project, run_id)` 包成单参数函数注入。
- 实际可见效果：门禁通过时四条路线可产生正式 `delta`，消融可产生 `module_effect`，复现可产生 `difference` 和布尔型 `within_tolerance`；任一条件不满足时 `comparison_scope` 为 `side_by_side_only`，这些正式字段统一为 `null`，`blockers` 用稳定编号和中文原因说明缺什么，原始 `baseline/source/candidate` 仍保留供人工查看。
- 选择原因：比较含义直接影响论文主张，必须由一处代码统一判断；单参数复核接口既能真实复核现场文件，也让本组件在尚无 `output_seal.py` 的基础分支上独立测试，不会形成第二套封存实现。
- 已知限制：本候选分支只有可注入接口，不包含产物封存组件；当前执行器没有传入复核函数时会一律只给原始并排结果，这是有意的失败关闭。与产物封存提交合入后，主集成分支仍需把项目路径绑定进复核函数，并重跑真实双 Run 正向闭环。持久化结论的离线回放与现场重新晋级要继续分开：离线校验不能假装做过现场文件复核。
- 素材位置：实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/fair_comparison.py`、`routes.py`、`tasking.py` 和 `intake.py`；专项测试位于 `tests/test_fair_comparison.py`，验证记录位于 `docs/reviews/2026-07-27-fair-comparison-gate-validation.md`。
- 验证命令与结果：旧执行器完整回归为 `48 passed, 29 subtests passed`；专项门禁、Task/Run/Evidence、启动输入、四路线合同和 Innovation 旧调试闭环组合为 `75 passed, 87 subtests passed`。Python 编译与 `git diff --check` 通过；新增行没有引入本机绝对路径，正式命令与限制见独立验证记录。
- 回退方式：回退本条对应提交即可恢复旧比较行为；不会删除或迁移既有 Task、Run、Evidence、项目数据、正式工作流、个人实例、PaperFlow 或旧 既有项目。
## SYS-V2.13.0 / DATA-RESEARCH-PAPER-EXPORT-V2.1.0：绑定证据交付包

- 日期：2026-07-27
- 状态：已完成候选实现与机器验证，等待独立 Reviewer；没有安装、发布或覆盖正式目录。
- 本次改的是哪个对象：Research Package V2 的 `1.5.0 / SYS-V2.13.0` producer、正式 Run 现场复核、可携带 Evidence 投影、权威输出复制和 PaperFlow 接收合同；旧 `1.2.3 / SYS-V2.10.3`、`1.3.0 / SYS-V2.11.0` 只保留独立解析兼容，不被重新标成 1.5。
- 目标问题：旧目录包只保存普通 Run 快照，Run 文件仍可能从项目根或执行快照的可变输出读取；它没有把 Codebase 绑定、执行快照、权威输出封存和确认事件一起交给 PaperFlow，也无法证明交付素材来自封存副本。
- 采用技术：在同一项目快照锁中重新核验 Run、Evidence、执行快照 manifest 和 `.cv-workflow-seals/<RUN-ID>` 权威副本；`experiments.json` 增加 bound frozen、execution snapshot、portable output seal 和精确两事件 Evidence binding。交付资产只从权威副本复制，固定使用 `cv-research-handoff/package-payload/v1\0` 域分隔符按 `delivery.files` 顺序摘要；`manifest.reverification` 再绑定 payload、frozen、snapshot、portable seal 和 Evidence 摘要。已知 Source 本机定位会投影成 `source://SRC-xxxx`；所有复制资产都尝试严格 UTF-8 解码，能解码的内容不论扩展名都拒绝本机绝对路径，不能解码的二进制资产继续按原有身份检查复制。
- 替换了什么：替换“相信内部包旧 Run 快照并从可变 source 复制”的 1.5 导出路径；不改变旧包的正文解析语义、不删除旧交付目录，也不修改 v1 单 Run 交接。
- 实际可见效果：一个本地最小 Git Codebase 的正式 Run 能经过执行、封存、`single_run`、Reviewer 确认、内部 PKG 和 1.5 导出后，直接通过 PaperFlow `paperflow_v2.research_package.validate_research_package`。未封存、未 confirmed、debug、权威 seal 漂移、execution snapshot 漂移和交付文件篡改都会失败；执行快照内可变输出即使后来改变，交付字节仍来自权威副本。
- 选择原因：科研与论文仍是两套独立流程，唯一接口必须是一份离开科研项目后仍能解释、核对并追到正式 Run 的包；把复核摘要和实际 included 字节绑定，才能阻止只改 manifest 后自签名。
- 已知限制：SHA-256、checksum 和 producer 只证明包内字节与声明之间的一致性，不是作者或来源签名；同一账户中拥有目录写权限的人可以改写内容后重算这些摘要。这是同一台 Windows 机器、同一用户权限下的合作式防漂移边界，不是针对恶意原生进程的操作系统沙箱。自动 `single_run` 事件原账本没有 Reviewer，portable 投影会明确写为 `not-applicable:single-run-automatic-quality-gate`；最终 `confirmed` 事件仍必须有真实非空 `checked_by`。旧 output seal 时间只有秒精度，portable 投影只把其下界提升到 Run 完成时间，避免微秒截断制造假倒序。
- 素材位置：`skills/cv-experiment-workflow/scripts/workflow_core/paper_package.py`、`skills/cv-experiment-workflow/scripts/workflow_core/paper_package_delivery.py`、`tests/test_paper_package_bound_v15.py`、`docs/integration/research_paper_package_v2.md`、`docs/reviews/2026-07-27-paper-package-bound-v15-validation.md`。
- 验证命令与结果：真实 producer→PaperFlow 1.5 专项为 `9 passed in 97.18s`；底层输出封存回归为 `10 passed in 82.79s`；内部旧包回归为 `53 passed, 55 subtests passed`；旧独立导出回归为 `29 passed, 21 subtests passed`；Skill 文档兼容检查为 `23 passed`。Python 编译和 `git diff --check` 通过。最终组合回归与独立 Reviewer 结论在候选合入前补记。
- 回退方式：回退本条 producer/profile 分支和新测试即可；已经存在的旧包保持原字节，正式目录、已安装 Skill、个人实例与 既有项目 均未修改。

## DATA-RESEARCH-PAPER-EXPORT-V2.1.1：复制资产路径泄露修复

- 日期：2026-07-27
- 状态：历史候选已完成，随后发现非法 UTF-8 后半段、UTF-16 / UTF-32 和扫描后换档缺口，由 `V2.1.2` 继续修复；未安装或发布。
- 本次改的是哪个对象：Research Package producer 的 1.5 复制资产本机路径检查、对应回归测试和交接说明。
- 目标问题：旧规则只按文件扩展名识别文本，攻击者可以把 UTF-8 日志命名为 `.py`、无扩展名或未知扩展名，把本机绝对路径带进交付包。
- 采用技术：对每一个实际复制的资产进行流式严格 UTF-8 解码；解码成功即扫描盘符、UNC、`file://`、`~/` 和 POSIX 绝对路径，不信任文件扩展名。解码失败的二进制文件继续按原有大小、SHA-256、普通文件、封存副本和复制期身份检查处理。
- 替换了什么：替换只扫描 `.log`、`.json`、`.txt` 等少数后缀的策略；不改变资产许可、隐私、交付目录布局或 PaperFlow 接收端。
- 实际可见效果：`.py`、无扩展名、未知扩展名的 UTF-8 资产含 `C:\Users\...` 时导出失败；含无效 UTF-8 字节的 `.bin` 资产仍能成功导出并保持字节一致。
- 选择原因：真实日志和数据文件的名称不能作为安全依据；流式读取避免把允许的 512 MiB 资产一次性装入内存。
- 已知限制：SHA-256、checksum 和 producer 仍只证明包内字节与声明一致，不证明作者或来源身份；同一账户中拥有目录写权限的人仍能改写后重算。恶意本机进程的操作系统权限隔离不在此生产端边界内。
- 素材位置：实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/paper_package_delivery.py`；回归位于 `tests/test_paper_package_bound_v15.py`；交接说明位于 `docs/integration/research_paper_package_v2.md`。
- 验证命令与结果：四个定向攻击/兼容测试为 `Ran 4 tests, OK`；既有 producer→PaperFlow 接收测试为 `Ran 1 test, OK`；`py_compile` 与 `git diff --check` 通过。
- 回退方式：回退本条对应代码、测试和文档改动即可恢复 `DATA-RESEARCH-PAPER-EXPORT-V2.1.0` 的后缀白名单行为；不删除任何已交付包。

## DATA-RESEARCH-PAPER-EXPORT-V2.1.2：稳定便携性扫描与独立验包对称

- 日期：2026-07-27
- 状态：实现、机器验证和两轮独立审核已完成；合同审核与修复后的安全复审均为 `APPROVE`。仍未安装、发布或修改正式目录。
- 本次改的是哪个对象：1.5 producer 的资产便携性扫描、扫描到复制的文件身份绑定、独立 `verify-paper-package` 最终目录检查及对应文档和测试。
- 目标问题：非法 UTF-8 字节会让旧扫描器放弃后半段；带 BOM 的 UTF-16 / UTF-32 文本未覆盖；`\Users\...` 当前盘根路径未显式拒绝；扫描与复制分别读取，攻击者可以在两步之间换文件；独立验包没有对科学内容和全部 included 资产执行同一便携性合同。
- 采用技术：所有复制资产使用一个稳定文件句柄同步执行流式路径检测、大小计数和 SHA-256；规范 UTF-8、UTF-16 / UTF-32 BOM 文本做 Unicode 检查，任意字节流继续检查高置信 ASCII 路径标记。扫描结果保存文件身份、解析后路径、大小和摘要，复制前逐项复核。独立验包直接在首次目录枚举身份上使用同一扫描器，六个科学 JSON / JSONL 使用同一递归字符串规则，导出 staging 只有通过完整独立验包才发布。
- 替换了什么：替换“非法 UTF-8 即停止文本检查”和“扫描、哈希、复制各自只凭相同期望摘要”的实现；不改变交付 schema、许可/隐私规则、目录布局或历史 producer 解析。
- 实际可见效果：非法 UTF-8 伪装二进制后追加本机路径、UTF-16 / UTF-32、`\Users\...`、扫描后同字节换文件和重新计算摘要后的科学 JSON 路径都会失败；不含路径的普通二进制继续按原字节导出。独立验包可证明每个 included 资产都经过同一次路径、大小和 SHA-256 检查。独立复核发现并修掉高熵二进制误报后，固定 200 个 PNG 风格随机样例和额外 1200 个压力样例均为 0 误报。
- 选择原因：科研端导出和 PaperFlow 接收必须对同一字节给出同一答案；一次稳定读取和文件身份延续能缩短竞态窗口，也避免把 512 MiB 资产整体载入内存。
- 已知限制：高置信二进制扫描不是恶意本机进程的操作系统沙箱；SHA-256、checksum 和 producer 仍不是作者或来源签名。同账户写入者可以整体改写后重算摘要，但独立验包仍会拒绝合同明确禁止的本机路径。
- 素材位置：实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/paper_package_delivery.py`；测试位于 `tests/test_paper_package_bound_v15.py`；说明位于 `docs/integration/research_paper_package_v2.md` 和 `docs/reviews/2026-07-27-paper-package-bound-v15-validation.md`。
- 验证命令与结果：包含真实 PaperFlow 接收器的 1.5 producer 完整专项为 `22 passed, 2 subtests passed in 247.51s`；定向编码、根路径、换档、standalone verify、真实 PNG 和高熵二进制组合为 `7 passed, 2 subtests passed`。额外 1000 个固定随机及 200 个系统随机 PNG 风格样例为 0 误报。最终代码下，旧内部包与旧目录导出同次回归为 `82 passed, 76 subtests passed in 240.87s`；Skill 文档为 `23 passed in 0.23s`。生产端实现和新增测试通过 `py_compile`，全部已跟踪差异通过 `git diff --check`。独立合同审核为 `APPROVE`；安全审核修复高熵二进制误报后复审为 `APPROVE`，Reviewer 独立跑 8 个关键回归 `8/8 OK in 87.944s`、1200 个高熵样例 `0/1200` 误拒，并确认审核前后实现与测试摘要不变。
- 回退方式：回退 `V2.1.2` 对应代码、测试和文档即可回到 `V2.1.1`；不会删除内部包、既有交付包、正式工作流、个人实例或 既有项目。
## PACK-INTEGRATION-SIX-V1.0.0：六方向输出与下载合同统一

- 日期：2026-07-27
- 状态：已完成候选；静态清单、逐文件摘要和定向测试通过，等待三包晚增长补丁合入后再跑六包整组回归。
- 本次改的是哪个对象：`PACK-DET/SEG/INSTSEG/SR/GZSL-V1.0.0` 的固定输出目录、五份下载清单、六方向注册表断言，以及六包共用的下载字段测试；`PACK-CLS-V1.0.0` 继续作为已符合新合同的基准。
- 目标问题：各方向包曾使用不同的运行输出目录，下载清单也残留方向私有字段，导致正式 Run 快照无法用同一套边界封存六类实验。
- 采用技术：五个方向统一把 Adapter 输出写到当前执行快照的 `.cv-workflow-output/`；下载清单顶层固定为 `schema/automatic_download/bundled_large_files/optional_resources`，每个资源严格使用 `name/kind/url/version/revision/license/license_url/sha256` 八个字段；发布页的摘要为 `null`，直接制品才允许固定 SHA-256。
- 替换了什么：替换 `runs/workflow/RUN-*` 等包内私有输出位置，删除 `license_review_required`、实现备注等含义重复的清单字段；许可风险继续写在 `license` 文本和官方 `license_url`，没有丢失用户需要知道的信息。
- 实际可见效果：六个方向使用同一个输出根，模板仓库不会接收运行产物；六份清单可以由同一校验器读取，大型数据仍只登记固定页面或带摘要的直接下载地址，不随模板打包。
- 选择原因：先统一最小公共接口，后续结果封存、科研交付包和统一界面只需处理一套结构，不再为六个方向分别写分支逻辑。
- 已知限制：本条只统一接口，不替代各方向的训练和指标正确性审核；SEG/SR/GZSL 的晚增长竞态在独立提交中修复，本条提交不冒充该加固已经完成。
- 素材位置：五包位于 `skills/cv-experiment-workflow/assets/domain-packs/`；共用合同测试位于 `tests/test_download_manifest_contract.py`，方向回归位于对应的 `tests/test_*_domain_pack.py`。
- 验证命令与结果：独立只读复核中，SEG/SR/GZSL 为 `29 passed, 4 subtests passed`，CLS/DET/INSTSEG/下载合同为 `54 passed, 123 subtests passed` 后仅剩两条旧测试断言；两条断言修复后定向 `2 passed`。六个 `pack.json` 全部通过校验，131 个 payload 文件逐项复算大小与 SHA-256 零不一致，`git diff --check` 通过。
- 回退方式：回退本条对应提交即可恢复各包原有输出位置和下载声明；不会删除用户数据、旧 既有项目、正式工作流、本机 Skill、个人实例或 PaperFlow。

## SYS-FORMAL-RUN-V1.0.1：精确 commit 的独立执行快照

- 日期：2026-07-27
- 状态：已完成候选，机器定向验证通过，等待修复后独立复核；不代表 `SYS-V2.13.0` 或产物封存已经完成。
- 本次改的是哪个对象：绑定 Run 的代码执行根、Git 读取边界、运行中身份复核、异常停止和输出路径；同时收紧新证据与 PaperFlow 导出的门禁。
- 目标问题：`SYS-FORMAL-RUN-V1.0.0` 虽然锁住 commit 和 Adapter 字节，运行时仍可能读取现场仓库；Git replace、额外目录、Python namespace 回退、启动后漂移和停止失败也没有形成一条完整的失败关闭路径。
- 采用技术：用受输出上限保护且禁用 replace 的 Git 命令读取精确 commit，拒绝 symlink、submodule、特殊对象、Windows 路径冲突和超预算树；每个 Run 原子创建独立 `execution_snapshot`，源码与目录设为只读，只允许 `.cv-workflow-output/` 写入。Adapter 只从快照根和受信 Python 运行库导入，子进程 Git 被固定到不存在的快照内 `GIT_DIR`，不会向上找到现场仓库。快照会在加载前后、启动前后、状态读取前后和结果收集前后重新核对；数据配置与评估口径也在启动前和收集后重建比较。
- 替换了什么：把“核对 commit 后仍以现场 Codebase 为执行根”替换为“现场仓库只负责提供已核对的 Git 对象，训练只在当前 Run 快照运行”；把启动异常后再猜进程状态替换为先使用内存中已经载入的同一个 Adapter 请求停止。
- 实际可见效果：源码发生持久变化、出现额外空目录或导入试图回退到现场路径时会失败关闭。`raw_log` 和 artifacts 只能引用当前 Run 的固定输出目录。停止未确认时 Run 保持未完成并记录 `cleanup_pending`；停止调用失败时记录 `cleanup_failure`，两者都不会产生正向 Evidence。
- 选择原因：执行根与现场仓库分开后，“当时到底跑了哪份代码”可以由 commit、manifest 和文件摘要共同回答；保留原 Task → Run → Evidence 状态机，不新建第二套账本。
- 已知限制：当前正式支持和实测范围仅为 Windows 本地单用户；Linux 正式兼容仍在 `docs/ROADMAP.md`。只防止并检测调用边界之间保留下来的持久篡改，不宣称阻止同一账户在单次调用内部完成“改动后恢复”的瞬时攻击。Phase B 产物封存未完成，因此绑定 Run 即使成功也停在 `artifact_seal_pending`，不能导出成 PaperFlow 证据。
- 素材位置：核心实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/execution_snapshot.py`、`git_safe.py`、`adapters.py`、`engine.py`、`runs.py`、`runner.py` 和 `run_identity.py`；专项测试位于 `tests/test_formal_run_git_gate.py`。
- 验证命令与结果：`tests/test_formal_run_git_gate.py` 为 `40 passed, 55 subtests passed`；旧生命周期回放 `tests/test_task_run_evidence.py` 为 `44 passed, 48 subtests passed`；PaperFlow 历史兼容 `tests/test_paper_handoff.py` 为 `39 passed, 52 subtests passed`。这些是本次定向结果，不冒充完整测试；完整回归仍沿用上一提交已记录的 `684 passed, 1068 subtests passed` 基线。
- 回退方式：回退本修复提交即可恢复 `SYS-FORMAL-RUN-V1.0.0`；旧 Task、旧正向 Evidence 和既有交付包保持可读，不需要迁移或删除。正式目录、本机安装 Skill、个人实例、PaperFlow 和旧 既有项目 均未修改。

## SYS-FORMAL-RUN-V1.0.0：Task 到真实代码仓库的可信运行绑定

- 日期：2026-07-27
- 状态：已完成候选，机器验证全部通过，待独立审核；不代表 `SYS-V2.13.0` 六方向完整闭环已经完成。
- 本次改的是哪个对象：Task 的 Codebase 绑定、正式运行 Git 门禁、固定 Adapter 执行根、Run 五类身份，以及 CLS 真实数据清单；不包含产物封存、PaperFlow V2 或其余五个方向包。
- 目标问题：旧执行器可以在账本项目里调用 Adapter，却没有把一张 Task 锁到唯一代码仓库、分支、提交和 Tag；真实数据也没有统一的来源与内容身份，因此运行结果不能可靠说明“哪份代码、哪批数据、什么评估口径跑出来的”。
- 采用技术：`code_binding` 精确保存 Codebase ID、branch、40 位小写 commit 和可选 tag；运行前后用 Git 核对 checkout 与工作树，固定加载 Codebase 根目录的普通文件 `workflow_adapter.py`，并把字节与绑定 commit 的 blob 比较。`create_run` 和 `claim_run` 在项目写锁内复用无嵌套锁的 live gate，从 Codebase 登记重建仓库路径、Git、clean 状态和 Adapter SHA-256，不接受调用者传入“已验证”开关；Git 禁用可选锁，固定 15 秒超时和输出上限。Run 继续保留原外层和五个 FROZEN 字段，在字段内部统一冻结代码、原始配置、数据、Adapter 评估、实时环境及各自 SHA-256。真实数据使用严格 `dataset_identity`；CLS 对 `train`、`val` 每个文件做链接检查、打开前后身份复核和内容摘要，防止读取中途被替换。
- 替换了什么：替换“运行时临时传一份 Git 预期值、默认从账本项目加载 Adapter、数据来源只靠自由文本”的做法；旧 Task 不带 `code_binding` 时仍按原路径兼容，不迁移或重写历史账本。
- 实际可见效果：绑定 Task 只会在指定 Codebase 上运行。正式 evidence 要求干净工作树；debug 可保留脏状态，但会冻结 `worktree_clean=false` 且不能成为论文证据。可信运行成功后明确返回 `artifact_seal_pending`，保持 Run `finished`、Task `reviewing`、Evidence 为空，等后续产物封存完成后再进入证据审核。
- 选择原因：先可信地回答“跑的是哪份代码和哪批数据”，再封存结果文件，可以把安全边界拆成能单独验证的小步；沿用现有 Task → Run → Evidence 账本和五个冻结字段，避免新造第二套状态机。
- 已知限制：Phase A 不封存 checkpoint、指标文件和预测文件，因此不会创建 Evidence，也不能导出 PaperFlow；文件系统竞争只能在现有操作系统可观察身份范围内检测。大型数据仍只保存来源和摘要，不复制进工作流。
- 素材位置：核心实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/codebases.py`、`engine.py`、`run_identity.py`；CLS 数据清单位于 `skills/cv-experiment-workflow/assets/domain-packs/cls-v1.0.0/payload/workflow_adapter.py`；测试位于 `tests/test_formal_run_git_gate.py` 和 `tests/test_cls_domain_pack.py`。
- 验证命令与结果：正式运行绑定专项为 `15 passed, 54 subtests passed`；Codebase 与文档为 `34 passed, 11 subtests passed`；Task/Run/Evidence 为 `44 passed, 48 subtests passed`；旧调参执行为 `48 passed, 29 subtests passed`；CLS 方向包为 `20 passed, 43 subtests passed`。文档预算为 `SKILL=161/220, references=365/800`，个人路径扫描和 `git diff --check` 通过；最终 `python -m pytest -q` 为 `684 passed, 1068 subtests passed`，耗时 933.39 秒。独立审核将在本地提交后执行。
- 回退方式：回退本组件实现、专项测试和本条记录即可；不需要迁移旧 Task。正式目录、本机安装 Skill、个人实例、PaperFlow 和旧 既有项目 均未修改。

## SYS-DOWNLOAD-CONTRACT-V1.0.0：六方向统一下载清单

- 日期：2026-07-27
- 状态：已完成候选，机器验证通过，待独立审核；尚未合并其余五个方向包，也未安装到正式目录。
- 本次改的是哪个对象：方向包的 `DOWNLOADS.json` 校验规则、分类包下载清单和配套说明；不下载数据、不训练正式模型，也不修改科研项目账本。
- 目标问题：旧方向包工厂只核对文件摘要，不理解下载清单内容，因而无法拒绝“自动下载、捆绑大文件、漂移版本、缺失许可证来源或没有摘要的直接制品”等不合格声明进入新仓库。
- 采用技术：方向包校验器对下载清单做严格 UTF-8 JSON 解析，拒绝重复键、未知字段和超过 512 KiB 的清单；顶层固定为 `schema/automatic_download/bundled_large_files/optional_resources`。资源固定记录名称、类型、HTTPS 地址、版本、修订、许可证、许可证地址和可空 SHA-256；发布页必须把摘要写成 `null`，直接制品必须给 64 位小写 SHA-256。`latest/main/master/current` 等独立词或文件名组成部分、泛化的 `downloads` 末级页面、查询参数和百分号编码主机都会在建仓前失败。
- 替换了什么：替换“`DOWNLOADS.json` 只是随包复制的一段自由文本”的做法；分类包的依赖地址改为 PyTorch `v2.12.0` 固定发布页，CIFAR-10 保留官方页面并如实标明官方页未声明统一许可证。
- 实际可见效果：大型数据和权重不会随模板分发；清单明确声明模板不自动下载，用户看到的是能回查版本与许可证来源的地址。如果以后登记单个可下载文件，缺摘要会直接阻止建仓。清单校验不冒充系统级断网，候选发布另用离线 smoke 检查常用 Python API 尝试。
- 选择原因：六个方向共用一条小而明确的下载合同，比每个模板各写一套临时规则更容易核验，也直接落实“发布体积最多几十 MB、大型数据只给地址”的要求。
- 已知限制：清单只能证明模板声明是否完整，不能替用户接受第三方条款，也不代替下载后的再次哈希核验，更不是操作系统级网络沙箱；官方页面没有统一许可证时仍需人工确认。许可证未知状态直接写入 `license` 文本并指向官方页，刻意不保留含义重复的额外布尔字段。当前提交只规范分类包，其余五包会在集成后按同一合同机械整理。
- 素材位置：校验器位于 `skills/cv-experiment-workflow/scripts/workflow_core/domain_packs.py`；分类清单与说明位于 `skills/cv-experiment-workflow/assets/domain-packs/cls-v1.0.0/payload/`；回归测试位于 `tests/test_download_manifest_contract.py` 和 `tests/test_domain_pack_factory.py`。
- 验证命令与结果：修复独立审核发现的漂移地址与清单体积问题后，下载合同、工厂和分类方向组合为 `43 passed, 73 subtests passed`；其中下载合同与工厂为 `23 passed, 30 subtests passed`，分类方向完整回归为 `20 passed, 43 subtests passed`。Python 编译、正式分类包复核、JSON 解析、`git diff --check` 和新增行隐私路径扫描均通过；其余五包兼容仍必须在集成分支完成后复审。
- 回退方式：回退校验器、测试、分类包下载清单、重建后的 `pack.json` 和本条记录；不会影响旧 既有项目、正式工作流、本机安装 Skill、个人实例或 PaperFlow。

## SYS-RELEASE-V1.0.0：发布暂存与受控复查加固

- 日期：2026-07-27
- 状态：已完成，属于隔离 worktree 中尚未正式安装的开发候选。
- 本次改的是哪个对象：科研与 PaperFlow 的发布暂存构建、发布树复查和方向包运行环境检查；不改训练、评估、论文事实或旧 既有项目。
- 目标问题：原测试中的机器路径攻击样例会让真实仓库扫描到测试源码后自我拒绝；目录快照漏掉空目录污染；Windows 子进程可能在加入 Job 前先执行用户代码；正常结束的父进程仍可能留下后代；启动或回收异常也没有统一返回可读结果。
- 采用技术：攻击路径改为测试运行时拼装，生产扫描器和普通测试文件仍完整扫描；发布树和方向包快照同时记录文件与目录；Windows 使用 `CREATE_SUSPENDED → Job Object 绑定 → 恢复执行`，绑定或恢复失败立即拒绝，POSIX 继续使用独立进程组；无论父进程超时还是正常结束都按已绑定所有权回收整棵进程树，不在运行阶段退回裸 PID 终止。运行环境报告只声称检测审计钩子与 monkeypatch 能覆盖的常用 Python API 尝试。
- 替换了什么：替换“先启动再尽快绑定”“只比较文件”“父进程退出便不再回收”和“把 API 守卫描述成完整离线保证”的旧实现；没有放宽绝对路径、安全链接、体积或 Manifest 扫描。
- 实际可见效果：收集测试若导入模块并在发布树新建空目录，会被前后快照发现；Windows 用户代码在 Job 绑定前不会开始；父进程正常退出后仍存活的子进程会被回收；启动、绑定、恢复和回收错误会进入结构化失败结果，而不是冒泡中断或静默降级。
- 选择原因：发布检查必须先保证被检查代码处于可控进程边界，再谈超时和清理；同时把守卫能力写窄，避免把常用 API 拦截误说成操作系统级完全断网。
- 已知限制：`pytest --collect-only` 会导入测试模块并执行模块级代码；文件与目录快照只能发现发布树内可见污染，不能证明系统其他位置零副作用。Python API 守卫不是硬隔离，不能覆盖 `os.startfile`、`ctypes` 直接系统调用或受测代码自行改写环境变量。真实生产构建目前仍会按设计拒绝仓库原有文档中的机器绝对路径，首个已知位置是 `README.md` 的 Windows 示例；本轮没有通过弱化扫描器掩盖这项旧债。
- 素材位置：实现位于 `tools/build_release_staging.py`、`tools/run_release_checks.py` 和 `tools/check_runtime_environment.py`；回归测试位于 `tests/test_release_staging.py`、`tests/test_release_checks.py` 和 `tests/test_runtime_environment.py`。
- 验证命令与结果：发布工具三文件组合为 `43 passed, 34 subtests passed`；9 个新增回归场景全部通过；两个工具及三个测试文件通过 `py_compile` 和 `git diff --check`；五个本轮修改源码/测试文件均通过生产私有绝对路径与源码根标记扫描。整仓 `python -m pytest -q` 在 300 秒上限内未结束，没有形成全量通过结论；真实生产构建明确失败于上述历史文档路径，而不是本轮攻击夹具。
- 回退方式：回退本条对应候选提交即可恢复旧发布检查行为；正式目录、已安装 Skill、个人实例和旧 既有项目 均未修改。

### 2026-07-27 补充加固

- 状态：已完成候选，仍未正式安装。
- 本次改的是哪个对象：继续收紧 `SYS-RELEASE-V1.0.0` 的输入边界，不改变系统、界面或论文内容版本。
- 目标问题：首轮候选仍会接受未登记空目录、hardlink、缺字段控制清单、重复 JSON 字段和带可变入口的方向包；超限源码与测试收集输出也可能在判定失败前占用过多内存。
- 采用技术：发布目录必须完全由清单文件的父目录推导；普通文件拒绝 hardlink；JSON 使用拒绝重复键与 `NaN/Infinity` 的严格解析；稳定读取和源文件复制在读取前与读取中执行字节上限；测试收集和方向包 smoke 输出均使用 2 MiB 有界缓冲；运行环境在 smoke 前复算方向包逐文件大小和 SHA-256，并要求四个入口精确指向本方向模块；Python 依赖发现和导入使用 `-I -B` 隔离模式。
- 实际可见效果：预先放入空目录、hardlink、篡改后自洽的内层入口、缺字段清单或无限输出都不能再得到 PASS；被篡改方向包不会进入执行阶段。
- 已知限制：测试收集仍会导入模块；常用 Python API 守卫仍不是操作系统沙箱。首轮记录中提到的历史绝对路径旧债仍需在最终候选集成时按“当前生产文件改成占位路径、历史记录明确排除并留原因”的方式处理，不能靠放松扫描器解决。
- 素材位置：仍为 `tools/build_release_staging.py`、`tools/run_release_checks.py`、`tools/check_runtime_environment.py` 及对应三个测试文件。
- 验证命令与结果：`python -m pytest -q tests/test_release_checks.py tests/test_release_staging.py tests/test_runtime_environment.py` 为 `51 passed, 36 subtests passed`，无 warning；新增下载 URL 查询参数回归在独立下载清单分支通过 `22 passed, 26 subtests passed`。
- 回退方式：回退补充加固提交即可回到首轮候选；不涉及正式目录、用户数据、旧 既有项目 或已安装 Skill。

### 2026-07-27 独立复核后的第二次加固

- 状态：修复候选已完成机器验证，等待发现问题的同一独立 Reviewer 复审；尚未正式安装。
- 本次改的是哪个对象：继续收紧 `SYS-RELEASE-V1.0.0` 的发布复查 API、方向包已验证字节快照和 smoke 守卫报告；不改变训练、评估或论文结论。
- 目标问题：独立复核发现，程序直接调用复查 API 时可禁用必需测试收集却得到 PASS；发布目录会在检查总量前把所有文件读入内存；方向包验证与实际 smoke 读取的是两份不同时刻的内容；smoke 守卫报告未拒绝重复 JSON 字段；清单计数字段和 hardlink 边界仍不够严格。
- 采用技术：核心 API 自身把“必需测试收集关闭”记录为 FAIL；发布文件先用元数据累计总量，超限时不读取正文，稳定读取阶段继续传递剩余预算，并限制最多 100,000 个文件；方向包合同返回本次已经核验的字节快照，smoke 只复制并执行这份快照，结束后再与来源比较；payload 与 `pack.json` 都拒绝 hardlink；守卫报告使用拒绝重复键和 `NaN/Infinity` 的严格 JSON 解析，子进程在运行受测模块前保存低层写入函数并用独占新建方式写报告。
- 替换了什么：替换“CLI 才补做不收集失败”“读完全部文件再算总量”“先验一份、运行前再读一份”和“普通 `json.loads` 接受矛盾字段”的旧行为。
- 实际可见效果：结构诊断仍可查看，但不能冒充正式发布 PASS；超限发布树会在零正文读取时退出；方向包在验证后被改动时，smoke 执行原核验字节并最终因来源变化拒绝；重复状态字段、非有限数字和 hardlink 都不能通过。
- 选择原因：发布结论必须由同一个核心 API 给出，且真正执行的代码字节必须就是刚刚核验的字节；两条规则能直接消除调用方式和检查—执行时间差造成的假绿灯。
- 已知限制：常用 Python API 守卫仍不是操作系统级沙箱；`pytest --collect-only` 仍会导入测试模块。100,000 文件上限保护内容快照，目录枚举本身仍由操作系统先返回目录项。
- 素材位置：`tools/run_release_checks.py`、`tools/check_runtime_environment.py`、`tests/test_release_checks.py` 和 `tests/test_runtime_environment.py`。
- 验证命令与结果：六个新增回归先得到 `6 failed, 1 passed, 1 subtests passed`，修复后为 `6 passed, 2 subtests passed`；三文件完整回归为 `56 passed, 38 subtests passed in 16.63s`；`git diff --check` 通过。
- 回退方式：回退本补充提交即可回到上一候选；正式目录、旧 既有项目、个人实例和已安装 Skill 均未修改。

## SYS-V2.13.0 / UI-CONSOLE-V2.0.0 / PACK-*-V1.0.0：精简完整闭环（历史计划基线）

- 日期：2026-07-27
- 状态：历史计划记录；实际实施状态已由文件顶部同版本收口条目承接。
- 本次改的是哪个对象：科研系统内核、本地入口和六个方向模板包；PaperFlow 的接收兼容作为独立项目版本记录，不与本编号混用。
- 目标问题：现有系统已有实验账本、只读控制台和论文交接的分散能力，但没有六个可直接创建的方向仓库、正式 Codebase/Git 门禁，也没有一条从科研交付包进入 PaperFlow 的简单可见入口。
- 采用技术：继续使用 Python 3.10+ 标准库、JSON/JSONL、Git 和 SHA-256；新建方向仓库使用 PyTorch，但依赖不打包；页面继续使用本地原生 HTML/CSS/JavaScript；科研和论文之间只交换不可覆盖的 Research Package V2。
- 替换了什么：把“科研和论文连成一条自动研究流水线”的旧误解，替换为两个独立工作流加一个明确交付包；把八页管理中心和过早扩展收成一条主链和一个入口；不删除任何历史方案。
- 实际可见效果：完成后用户可从同一入口选择方向、创建仓库、记录实验、冻结交付包并进入 PaperFlow；六方向真实 baseline 默认使用 CUDA，CPU 合成小样例仍可跑通 `train/evaluate/infer`，但调试结果明确禁止成为论文成绩。
- 选择原因：先跑通真实最短闭环，再按重复问题扩展；统一包接口比合并两套状态机更清楚，也更容易核验论文来源。
- 已知限制：第一版只正式支持 Windows 本地单用户；不含大数据、权重、虚拟环境、云端、多用户或高成本训练；正式目录安装与本机 Skill 升级仍需单独授权。
- 素材位置：实施计划位于 `docs/superpowers/plans/2026-07-27-lean-complete-cv-workflow.md`，暂缓项位于 `docs/ROADMAP.md`；实现将落在 `skills/cv-experiment-workflow/` 和隔离 PaperFlow worktree。
- 验证命令与结果：计划阶段尚未执行；完成后必须记录六方向冒烟、完整科研到 PaperFlow 闭环、全量测试、体积检查、浏览器检查和三轮有效审核。
- 回退方式：每项使用独立小提交；失败只回退当前任务。两个正式项目、个人实例、本机安装 Skill 和六个旧 既有项目 根均不在本轮直接修改范围。

## PACK-HARDENING-SEG-SR-GZSL-V1.0.2：关闭产物验收末尾增长竞态

- 日期：2026-07-27
- 状态：已完成实现与机器验证，等待下一轮独立 Reviewer 复核。
- 本次改的是哪个对象：`PACK-SEG-V1.0.0`、`PACK-SR-V1.0.0` 和 `PACK-GZSL-V1.0.0` 的产物清单安全读取、验收末尾复扫和清单写出成功条件，以及 SEG 的中央工作流 Adapter；没有修改下载清单、训练与指标语义、正式安装目录、旧 既有项目 或 PaperFlow。
- 目标问题：`V1.0.1` 已把 `artifacts.json` 自身字节计入总量，但独立 Reviewer 证明，清单或普通产物仍可在第一次安全读取返回后继续增长。旧验收器会沿用第一次读到的清单长度，三个写出函数也会在普通产物第一次读取后直接返回成功，因此存在“验收结束时实际目录已经漂移或超限”的窗口。
- 采用技术：三个合同和 SEG Adapter 增加 JSON 快照读取，把解析 object、原始 bytes 与 `device/inode/size/mtime/ctime` 文件身份绑定；读取前后身份、原始长度和链接状态不一致时立即拒绝。验收器先完整核对清单路径、大小、SHA-256、实际目录集合和总字节，再重读清单比较身份与 bytes，随后重新扫描整棵产物目录，最后再次重读清单，确保末尾复扫使用的仍是同一份清单。三个写出函数落盘后调用同一严格验收器，只有最终复扫通过才返回成功。
- 替换了什么：替换“清单只读一次”和“写出端只根据第一次读取结果生成清单”的旧行为；保留原 `read_json_with_size`、`read_json` 和三个写出函数的对外返回类型。
- 实际可见效果：清单在初次读取返回后追加字节会被 SEG 合同、SEG Adapter、SR 合同和 GZSL 合同拒绝；普通产物在写出端第一次读取返回后继续增长时，SEG、SR、GZSL 三个写出函数都不会错误返回成功。正常合成冒烟、本地小数据、指标计算和已有安全边界保持不变。
- 选择原因：单次安全读取只能证明“这次读取期间”文件稳定，不能证明后续验收阶段仍未变化；把清单原始 bytes 与文件身份冻结，并在末尾重新累计精确目录，是不引入平台锁和新依赖时能完成的最小闭环。
- 已知限制：本规则保证验收执行期间发现身份或内容漂移，并保证成功返回前最后一轮检查仍满足总量与精确目录要求；外部进程在函数已经返回后再次修改目录，仍需由上层目录权限、冻结或发布机制约束。当前仍是隔离 Windows 候选，未安装到正式 Skill。
- 素材位置：实现位于 `skills/cv-experiment-workflow/assets/domain-packs/{seg,sr,gzsl}-v1.0.0/`；回归测试位于 `tests/test_seg_domain_pack.py`、`tests/test_sr_domain_pack.py` 和 `tests/test_gzsl_domain_pack.py`；中文审核记录位于 `docs/reviews/2026-07-27-seg-sr-gzsl-artifact-budget-review.md`。
- 验证命令与结果：7 个 late-growth 回归在旧实现上得到预期 `7 failed in 33.02s`，把清单探针收紧到“整个 JSON 初读已返回后才增长”并完成最小修复后最终为 `7 passed in 34.02s`。三包合并完整测试为 `36 passed, 4 subtests passed in 202.93s`；拆分结果为 SEG `14 passed, 4 subtests passed in 75.13s`、SR `11 passed in 72.62s`、GZSL `11 passed in 69.32s`。4 个实现文件和 3 个测试文件共 7 个 Python 文件通过 AST 解析，SEG/SR/GZSL 分别以 23/24/24 个文件通过 `validate_domain_pack`，三个 `pack.json` 已按最终 payload 机械重建。
- 回退方式：回退本补漏提交即可恢复 `PACK-HARDENING-SEG-SR-GZSL-V1.0.1`；正式目录、下载合同分支和其他方向包不需要迁移。

## PACK-HARDENING-SEG-SR-GZSL-V1.0.1：清单自身字节计入产物总量

- 日期：2026-07-27
- 状态：已完成实现与机器验证；后续独立 Reviewer 发现读取后增长竞态，因此由 `V1.0.2` 继续补漏，本版未单独放行。
- 本次改的是哪个对象：`PACK-SEG-V1.0.0`、`PACK-SR-V1.0.0` 和 `PACK-GZSL-V1.0.0` 的产物清单写出与验收总量边界，以及 SEG 的中央工作流 Adapter 复查；没有修改下载清单、训练目标、指标定义、科研账本或正式安装目录。
- 目标问题：上一版虽然会重新累计清单列出的产物和实际目录文件，却把 `artifacts.json` 自身排除在 64 MiB（SEG/SR）或 128 MiB（GZSL）总上限之外。合法 JSON 可以追加尾随空白，因此普通产物刚好未超限时，清单仍可额外占用最多一个单文件上限；三个写出函数也可能生成随后被严格验收器拒绝的目录。
- 采用技术：三个合同增加 `read_json_with_size`，在同一次有界、身份稳定读取中同时返回解析后的 object 与原始字节数；清单列出路径复查和实际目录复查都从该字节数开始累计。三包写出端先生成规范 JSON bytes，再用“普通产物总量 + 清单 bytes”做最终判断，通过后才以同一份 bytes 落盘。SEG Adapter 使用同样的 `_read_json_with_size` 规则。
- 替换了什么：把两条验收总量的初值从 `0` 改为清单原始字节数，并替换“普通产物未超限就直接写清单”的旧行为；原 `read_json` 返回值和调用方式保持不变。
- 实际可见效果：普通产物本身未超限、`artifacts.json` 尾随空白使整体超限时，SEG Adapter 与三包合同都会拒绝；三包写出端也不会再制造整体超限的清单目录。正常合成冒烟、本地小数据、mIoU、PSNR、GZSL 指标和 PNG/NPZ 安全规则保持不变。
- 选择原因：总量上限保护的是整个 Run 输出目录，清单也是目录中的真实文件；从同一次安全读取取原始长度，既能计算尾随空白，也避免“统计一次、解析另一次”的替换窗口。
- 已知限制：本次只修 `artifacts.json` 总量边界；`DOWNLOADS.json` 的统一字段整理仍由下载合同集成分支完成。当前仍是隔离 Windows 候选，未安装到正式 Skill，也没有修改旧 既有项目、个人实例或 PaperFlow。
- 素材位置：实现位于 `skills/cv-experiment-workflow/assets/domain-packs/{seg,sr,gzsl}-v1.0.0/`；回归测试位于 `tests/test_seg_domain_pack.py`、`tests/test_sr_domain_pack.py` 和 `tests/test_gzsl_domain_pack.py`；中文验证记录位于 `docs/reviews/2026-07-27-seg-sr-gzsl-artifact-budget-review.md`。
- 验证命令与结果：验收端首次 RED 为 `4 failed, 1 passed in 15.81s`，其中 SEG 的 Adapter 与合同分别形成两个子失败；补写出端后再次 RED 为 `3 failed, 2 subtests passed in 14.84s`。最小修复后定向节点为 `3 passed, 2 subtests passed in 15.03s`，三包全量为 `29 passed, 4 subtests passed in 163.01s`；7 个改动 Python 文件通过 AST 解析，SEG/SR/GZSL 正式包分别以 23/24/24 个文件通过 `validate_domain_pack`，三个 `pack.json` 均由项目工具按最终 payload 机械重建，最终 `git diff --check` 通过。
- 回退方式：回退本补漏提交即可恢复 `PACK-HARDENING-SEG-SR-GZSL-V1.0.0`；正式目录、下载合同分支和其他方向包不需要迁移。

## PACK-HARDENING-SEG-SR-GZSL-V1.0.0：第二轮产物复核加固

- 日期：2026-07-27
- 状态：已完成实现与机器验证，等待独立 Reviewer 最终放行。
- 本次改的是哪个对象：`PACK-SEG-V1.0.0`、`PACK-SR-V1.0.0` 和 `PACK-GZSL-V1.0.0` 的输出验收器、回归测试和来源说明；没有改变训练目标、正式指标名字、中央下载合同或工作流账本。
- 目标问题：第一轮已限制单文件和输入数据体量，但验收阶段没有重新累计全部产物字节；SEG 只核对 mIoU 各字段彼此平均一致，没有从混淆矩阵重新计算，也没有拒绝预测 mask 中超出类别范围的像素；SEG/SR 对被篡改的输出 PNG 会先交给 Pillow；SR 的最终摘要没有直接提醒自定义 PSNR 不能和常见标准口径比较；DIV2K 的许可提醒不够完整。
- 采用技术：SEG 从整数混淆矩阵逐类重算交集、并集、`per_class_iou`、`included_classes` 和 `mean_iou`；预测 mask 解码后用 L 模式像素极值拒绝 `0..num_classes-1` 之外的任何值。SEG/SR 输出 PNG 在 Pillow 前复用各自数据模块的 IHDR 尺寸、单图像素、累计像素和压缩比检查。三包在清单读取和实际目录复查两遍都重新累计产物 bytes，并沿用原有 64 MiB（SEG/SR）或 128 MiB（GZSL）总上限。SR 的 `inspect` 与 `parse_result` 直接返回 `protocol=custom_full_rgb_no_shave` 和 `comparable_to_standard=false`；DIV2K 说明改为“仅供学术研究，图片版权归原权利人，使用前核验条款”，官方页面链接保持不变。
- 替换了什么：替换“只相信产物自报字段彼此一致”“总量只在写出时检查”和“输出图片先解码再看预算”的旧验收行为；不修改后续中央下载合约计划中的 `implementation_note` 或额外 resource 字段，避免与集成分支重复改同一接口。
- 实际可见效果：即使攻击者同步重签 `artifacts.json`，伪造 mIoU 分解、越界类别 mask、压缩炸弹 PNG 或总体积超限的输出仍会被拒绝；正常 SEG/SR/GZSL 合成与本地小数据链路保持原行为，SR 摘要会直接提示结果不可和标准协议比较。
- 选择原因：这些检查都位于论文证据入口之前，必须由可重算事实决定，不能只验证同一份输出中的自报数字；复用已有 PNG 预算函数可避免维护两套安全口径。
- 已知限制：当前只冻结 Windows 主闭环；大型数据仍不打包。中央 frozen V2 与未来下载资源字段调整由集成分支统一处理，本次没有提前扩展接口。
- 素材位置：实现位于 `skills/cv-experiment-workflow/assets/domain-packs/{seg,sr,gzsl}-v1.0.0/`，回归测试位于 `tests/test_seg_domain_pack.py`、`tests/test_sr_domain_pack.py` 和 `tests/test_gzsl_domain_pack.py`。
- 验证命令与结果：所有新测试先在旧实现上得到预期 RED，包括矛盾 mIoU、越界 mask、未重算总字节、Pillow 前置预算缺失、累计像素缺失、SR 摘要缺项和 DIV2K 文案不符；修复后 `python -B -m pytest -q tests/test_seg_domain_pack.py tests/test_sr_domain_pack.py tests/test_gzsl_domain_pack.py` 为 `26 passed, 2 subtests passed in 175.30s`。三个 `pack.json` 均由 `tools/rebuild_domain_pack_manifests.py` 按最终 payload bytes 机械重建。
- 回退方式：回退本加固提交即可恢复三包上一版候选；正式目录、个人实例、PaperFlow 和旧 既有项目 均未修改。

## PACK-CLS-V1.0.0：可运行图像分类方向包

- 日期：2026-07-27
- 状态：已完成，属于隔离 worktree 中尚未正式安装的开发候选。
- 本次改的是哪个对象：单标签图像分类方向的仓库模板、指标合同、项目 Adapter、Manifest 和自动测试；没有把 `SYS-V2.13.0` 整体计划标为完成。
- 目标问题：六方向计划只有方向包工厂和合同，没有一套可以现场创建仓库、运行训练、评估、推理和合成冒烟的真实分类模板，也缺少 Run 产物与科研证据之间的严格边界。
- 采用技术：使用 PyTorch CPU 小模型和只保存 `state_dict` 的 checkpoint；本地数据采用 ImageFolder 的 `train/val/类别/图片` 结构；指标固定为 top-1 accuracy、top-5 accuracy 和 macro-F1；合成数据按固定 seed 运行。Adapter 把每个 `RUN-*` 绑定到唯一目录，对 run、metrics、predictions 和 checkpoint 做严格身份与有限数字检查。本地训练通过同父目录随机 staging 产出，验证通过后原子发布；Manifest 由 registry、包内 provenance、文件大小和 SHA-256 机械重建。
- 替换了什么：把“CLS 方向包仍只是计划”的状态替换为一个已验证候选；同时替换早期候选中的共享输出目录、30 秒本地训练超时、弱 checkpoint 校验、硬编码来源和失败递归清理。其他五个方向包仍保持计划中。
- 实际可见效果：用户可用 `list-domain-packs` 查看 CLS，用 `create-domain-repo` 创建带固定 `main` 首提交和 `domain-pack/cls/v1.0.0` Tag 的独立仓库；仓库可运行 `cls.smoke`、`cls.train`、`cls.evaluate` 和 `cls.infer`。合成运行明确写成 `synthetic_debug_only` 和 `paper_eligible=false`。
- 选择原因：先用一个方向跑通“方向包清单 → 建仓库 → 真实训练代码 → 严格 Run 产物”的最短闭环，可以在复制到其余方向前发现接口、安全和证据边界问题；PyTorch、ImageFolder 和三个分类指标也是最小且可现场验证的分类基线。
- 已知限制：只提供轻量 CPU 候选，不打包大数据、预训练权重或虚拟环境；没有做大型 ImageFolder 性能基准。成功运行会保留空 staging，失败运行会保留完整失败 staging，避免路径替换下的误删。任何结果要成为正式论文证据，仍需后续中心 Git、Codebase 和 Run 门禁核对代码、数据、配置和环境身份。
- 素材位置：方向包位于 `skills/cv-experiment-workflow/assets/domain-packs/cls-v1.0.0/`；工厂实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/domain_packs.py`；Manifest 工具为 `tools/rebuild_domain_pack_manifests.py`；测试为 `tests/test_cls_domain_pack.py` 和 `tests/test_domain_pack_factory.py`；审核记录为 `docs/reviews/2026-07-27-cls-domain-pack-review.md`。
- 验证命令与结果：`python -B -m pytest -q tests/test_cls_domain_pack.py` 为 `19 passed, 43 subtests passed`；方向包工厂与 Skill 文档组合为 `37 passed, 11 subtests passed`；文档预算为 `SKILL=159/220, references=365/800`；正式 Manifest 校验通过并精确覆盖 22 个文件；`git diff --check` 通过；payload 内 `.pyc` 为 0。两轮有效审核最终均为 PASS。
- 回退方式：回退本方向包素材、CLI 接线、测试、Manifest 工具和本条记录即可；保留 `SYS-V2.13.0` 与其余五方向的计划状态。正式目录、已安装 Skill、个人实例和 既有项目 未被修改，不需要迁移或回退。

## PACK-DET-V1.0.0：可运行目标检测方向包

- 日期：2026-07-27
- 状态：已完成开发候选，位于隔离 worktree，待主分支复核和合并；没有安装到正式目录。
- 本次改的是哪个对象：目标检测方向的仓库模板、COCO bbox 数据合同、评估合同、项目 Adapter、Manifest 和自动测试；没有把 `SYS-V2.13.0` 整体计划标为完成。
- 目标问题：方向包工厂此前只有分类包，目标检测仍没有一套能现场建仓库并跑通训练、评估、推理和合成调试的真实模板，也没有把正式 COCO 数据身份和调试结果边界固定下来。
- 采用技术：使用 PyTorch CPU 固定查询小模型完成真实前向、反向传播、Adam 更新和 `state_dict` checkpoint；正式数据采用经过严格字段、图片和边界检查的本地 COCO bbox；正式指标固定为 COCO bbox AP、AP50、AP75，并只接受精确版本 `pycocotools==2.0.11`。Adapter 的准备结果严格只有 `code/config/seed/data/environment` 五个顶层字段，评估合同放在 `data.evaluation`，数据内容单独以 SHA-256 绑定。
- 替换了什么：把“DET 仍只是计划”的状态替换为可验证开发候选；同时禁止缺少正式评估依赖时静默改用近似指标，也禁止把合成调试分数写成 COCO 指标或论文成绩。
- 实际可见效果：用户可用方向包工厂创建目标检测仓库，运行 `det.smoke`、`det.train`、`det.evaluate` 和 `det.infer`。每次运行写入唯一 `runs/workflow/RUN-*`，结果、预测、输入和 checkpoint 均有内容哈希；合成运行只输出 `debug_bbox_mean_iou` 和 `debug_precision_at_iou_0_5`，并固定为 `synthetic_debug_only`、`paper_eligible=false`。
- 选择原因：固定查询的小模型足以现场证明训练链真实运行，又不会把大型框架、权重或数据塞进通用模板；正式评估直接使用 COCO 官方工具，避免自制“差不多的 AP”造成结论混淆。
- 已知限制：包内不含大型数据、预训练权重、虚拟环境或 `pycocotools` 本体，只记录来源、固定版本和安装要求；当前机器没有 `pycocotools==2.0.11`，所以正式 COCO 评估由受控替身测试验证调用合同，真实正式评估仍需用户准备依赖和本地数据后再跑。合成结果永远不能成为论文数字。
- 素材位置：方向包位于 `skills/cv-experiment-workflow/assets/domain-packs/det-v1.0.0/`；登记入口位于 `skills/cv-experiment-workflow/assets/domain-packs/registry.json`；测试位于 `tests/test_det_domain_pack.py`、`tests/test_domain_pack_factory.py` 和 `tests/test_cls_domain_pack.py`；验证记录位于 `docs/reviews/2026-07-27-det-instseg-domain-pack-validation.md`。
- 验证命令与结果：五字段接口测试先得到 2 个预期失败，修正后与 INSTSEG 组合为 `4 passed, 5 subtests passed`；DET 与 INSTSEG 完整测试为 `18 passed, 5 subtests passed`；方向包工厂与既有 CLS 回归为 `37 passed, 54 subtests passed`；Codebase 与 Skill 文档回归为 `34 passed, 11 subtests passed`。Manifest 使用 `tools/rebuild_domain_pack_manifests.py --pack det` 机械重建，连续重建 SHA-256 不变并通过正式校验；`git diff --check` 和零缓存检查通过。独立审核仍由主任务在合并前完成。
- 回退方式：回退 DET 方向包目录、registry 条目、对应测试和本条记录即可；分类包、其他方向计划、正式目录、已安装 Skill、个人实例和旧 既有项目 不需要迁移或回退。

## PACK-INSTSEG-V1.0.0：可运行实例分割方向包

- 日期：2026-07-27
- 状态：已完成开发候选，位于隔离 worktree，待主分支复核和合并；没有安装到正式目录。
- 本次改的是哪个对象：实例分割方向的仓库模板、COCO polygon 数据合同、二值掩码产物、评估合同、项目 Adapter、Manifest 和自动测试；没有把 `SYS-V2.13.0` 整体计划标为完成。
- 目标问题：实例分割方向此前没有可运行仓库模板，也没有明确限定 polygon、二值 PNG 掩码、COCO segm 正式指标和运行产物之间的对应关系。
- 采用技术：使用 PyTorch CPU 固定查询小模型完成真实训练和 checkpoint 重载；正式数据采用本地 COCO polygon，仅接受非 crowd 的有效多边形，不接受 RLE；推理输出真实单通道二值 PNG 掩码并登记 SHA-256。正式指标固定为 COCO segm AP、AP50、AP75，并只接受精确版本 `pycocotools==2.0.11`。Adapter 同样使用五字段顶层交接，评估合同放在 `data.evaluation`。
- 替换了什么：把“INSTSEG 仍只是计划”的状态替换为可验证开发候选；删除任何在缺依赖时伪装成 COCO segm AP 的降级路线，并把预测图片身份、掩码文件和输出哈希逐项绑定。
- 实际可见效果：用户可用方向包工厂创建实例分割仓库，运行 `instseg.smoke`、`instseg.train`、`instseg.evaluate` 和 `instseg.infer`。合成运行只输出 `debug_mask_mean_iou` 和 `debug_precision_at_mask_iou_0_5`，固定为 `synthetic_debug_only`、`paper_eligible=false`；正式运行必须重新确认数据内容没有在准备后被替换。
- 选择原因：polygon-only 是首版最小且容易严格核验的正式输入；真实 PNG 掩码比只写 JSON 坐标更能证明推理产物存在，官方 COCO 工具则保证正式指标语义不被模板自行改写。
- 已知限制：首版不接受 RLE 和 crowd 标注，不包含大型数据、权重、虚拟环境或 `pycocotools` 本体；当前机器缺少精确版本的正式评估依赖，因此真实 COCO segm 正式评估还需在用户准备依赖和数据后执行。合成结果永远不能成为论文数字。
- 素材位置：方向包位于 `skills/cv-experiment-workflow/assets/domain-packs/instseg-v1.0.0/`；登记入口位于 `skills/cv-experiment-workflow/assets/domain-packs/registry.json`；测试位于 `tests/test_instseg_domain_pack.py`、`tests/test_domain_pack_factory.py` 和 `tests/test_cls_domain_pack.py`；验证记录位于 `docs/reviews/2026-07-27-det-instseg-domain-pack-validation.md`。
- 验证命令与结果：五字段接口测试先得到 2 个预期失败，修正后与 DET 组合为 `4 passed, 5 subtests passed`；DET 与 INSTSEG 完整测试为 `18 passed, 5 subtests passed`；方向包工厂与既有 CLS 回归为 `37 passed, 54 subtests passed`；Codebase 与 Skill 文档回归为 `34 passed, 11 subtests passed`。Manifest 使用 `tools/rebuild_domain_pack_manifests.py --pack instseg` 机械重建，连续重建 SHA-256 不变并通过正式校验；`git diff --check` 和零缓存检查通过。独立审核仍由主任务在合并前完成。
- 回退方式：回退 INSTSEG 方向包目录、registry 条目、对应测试和本条记录即可；分类包、其他方向计划、正式目录、已安装 Skill、个人实例和旧 既有项目 不需要迁移或回退。

## PACK-DET/INSTSEG-V1.0.0：第二轮身份与资源边界加固

- 日期：2026-07-27
- 状态：实现和本分支机器验证已完成；仍是隔离 worktree 内的开发候选，等待主任务独立复核与集成。
- 本次改的是哪个对象：`PACK-DET-V1.0.0` 和 `PACK-INSTSEG-V1.0.0` 的包内数据清单、预测结果复核、配置与 checkpoint 资源边界、来源 commit、README、Manifest 和回归测试；不改中央 frozen v2 schema，也不改系统版本。
- 目标问题：旧复核只能发现预测格式错误和重复 image ID，不能证明 `image_id`、`source_sha256` 和 `category_id` 来自本次冻结数据；配置和 checkpoint 的大整数会在拒绝前进入数据或模型创建；两个外部源码 commit 需要改成最终核验值。
- 采用技术：每个训练 Run 写入严格的 `data-manifest.json`，用 SHA-256 同时绑定逐文件清单、真实样本映射和类别集合；输出复核逐项比对预测。配置和 checkpoint 共用带上下限的整数检查，并对训练总像素、检测 head 与实例分割 mask head 增加组合预算。外部来源只更新固定 commit 和许可证链接，继续保持 `automatic_download=false`。
- 替换了什么：用真实数据清单的一一对应检查替换“只看字段格式和是否重复”；用模型创建前的单项、组合上限替换“只有正整数下限”；用 torchvision `78839c2b06c83c6cfb5c4da692ffb331bbd4c4cc` 和 pycocotools `ac87f5077ad6b8864c2dc5e93d14cae62d1db05a` 替换此前未最终核验的 commit。
- 实际可见效果：篡改为不存在的 image ID、另一张图的原图哈希或合同外类别都会被拒绝；十亿级配置和小 checkpoint 攻击会在 synthetic dataset 或 `build_model` 之前失败；用户仍只看到下载地址，不会触发联网下载。
- 选择原因：预测身份是结果能否成为证据的基础，不能由输出 JSON 自我声明；对单字段和组合规模同时限额，才能避免“每个数字看似合法、乘起来耗尽内存”；包内清单可以先独立修复，避免猜测尚未确定的中央 schema。
- 已知限制：中央 frozen v2 还没有接入 `data-manifest.json` 正文，本轮保留明确待办，等 Phase A 最终 schema 后一次适配；正式 COCO 评估仍需要用户自行准备精确 `pycocotools==2.0.11` 与本地数据。
- 素材位置：实现位于两个方向包各自的 `payload/src/*/runtime.py`；来源记录位于 `domain-pack.json`、`DOWNLOADS.json` 和 README；测试位于 `tests/test_det_domain_pack.py`、`tests/test_instseg_domain_pack.py`；详细验证记录位于 `docs/reviews/2026-07-27-det-instseg-domain-pack-validation.md`。
- 验证命令与结果：TDD RED 为四个定向节点退出码 `1`、`22 failed, 4 passed`；最小修复后的同一命令为 `4 passed, 22 subtests passed`，提交前攻击节点复跑仍为 `4 passed, 22 subtests passed`。两包完整测试为 `28 passed, 55 subtests passed`。DET 与 INSTSEG Manifest 连续机械重建后 SHA-256 分别稳定为 `8B630861D10EF62ED90EDF4C1987240AC0B08FB0E4CC87965973C8935FD1D519` 和 `1B001AD4950C1FDA3774D30BFD98AF76AD1DB3402ACD67CBB49AB43AE9BC15E4`，正式校验各覆盖 19 个 payload 文件；18 个 Python 文件通过 AST，旧 revision 搜索为 0，`git diff --check` 通过。
- 回退方式：回退本轮追加提交即可恢复第二轮复核前候选；本轮没有写入中央 frozen v2、正式目录、个人实例或旧 既有项目，也没有 push、发布或自动下载。

## PACK-DET/INSTSEG-V1.0.0：累计输出总字节加固

- 日期：2026-07-27
- 状态：实现和本分支机器验证已完成；等待同一独立 Reviewer 复审，仍未合并或安装。
- 本次改的是哪个对象：`PACK-DET-V1.0.0` 和 `PACK-INSTSEG-V1.0.0` 的 Run 输出资源上限、回归测试、Manifest 和验证记录；不改变模型、指标、数据合同或中央工作流 schema。
- 目标问题：旧输出复核只把每个文件限制为 8 MiB，没有累计一个 Run 的全部文件。攻击者可以给多张仍可正常解码的 PNG 追加尾字节，再同步重签预测与 `run.json` 的所有 SHA-256，使 DET 或 INSTSEG 约 80 MiB 的输出通过复核。
- 采用技术：新增每个 Run 64 MiB 总输出上限。首次安全枚举目录时累计所有普通文件的逻辑大小，先于 JSON、checkpoint 和 PNG 解码快速拒绝；完成严格路径、结构和哈希检查时，再对 `run.json` 以及清单内每个产物的稳定读取字节逐项累计，防止只依赖可竞争的早期文件大小。精确输出树保证统计覆盖数据清单、评估、预测、日志、checkpoint、输入 PNG、实例 mask PNG 和正式 COCO 边车。
- 替换了什么：用“单文件 8 MiB + 整个 Run 64 MiB”替换只有单文件上限的做法；不减少已有图片像素、模型规模、文件身份或哈希检查。
- 实际可见效果：正常合成输出继续通过；在 PNG 尾部、JSON 尾部和日志中分散填充字节并重签全部哈希后，只要一个 Run 的所有普通文件合计超过 64 MiB，就会在复核阶段失败。
- 选择原因：用户交付体量只需几十 MiB；64 MiB 与现有数据总量上限一致，并允许正常轻量基线输出，同时阻止用许多低于 8 MiB 的文件绕过总量约束。
- 已知限制：上限统计普通文件的逻辑字节数，不等于文件系统实际占用空间；正式 COCO 评估仍需要用户准备本地数据和精确 `pycocotools==2.0.11`。
- 素材位置：两包的 `payload/src/det/runtime.py`、`payload/src/instseg/runtime.py`，对应测试文件和 `docs/reviews/2026-07-27-det-instseg-domain-pack-validation.md`。
- 验证命令与结果：TDD RED 为两个定向节点退出码 `1`、`2 failed`，原因均是累计超限没有抛错；最小实现后同两节点为 `2 passed`。两包完整回归为 `30 passed, 55 subtests passed in 199.17s`。Manifest 连续机械重建后 SHA-256 稳定为 DET `F6389486A0A15E50E236918DF1031D23CEF1E67B6DA5AB38B56D60F847779D54`、INSTSEG `369CFDB47D490BAC6D8D02606CA1A2D2A2DF8479605A07123F7BB0A17B15917A`，正式校验各覆盖 19 个 payload 文件。
- 回退方式：回退本次追加提交即可恢复上一候选；不需要迁移实验账本，也不会影响正式目录、个人实例、PaperFlow 或旧 既有项目。

## PACK-SEG-V1.0.0：可运行语义分割方向包

- 日期：2026-07-27
- 状态：已完成，属于隔离 worktree 中尚未正式安装的开发候选。
- 本次改的是哪个对象：语义分割仓库模板、数据与指标合同、项目 Adapter、Manifest 和自动测试；没有修改工作流内核或旧项目。
- 目标问题：补齐能现场建仓库并走通训练、评估、推理的最小语义分割模板，同时把像素标签、忽略值与 mIoU 计算口径固定下来。
- 采用技术：使用 PyTorch CPU 小模型 `TinySegmenter-v1`、成对 RGB PNG 与索引 mask PNG；mask 只允许类别 `0..C-1` 和忽略值 `255`，缩放固定最近邻；mIoU 由全验证集混淆矩阵一次计算，零并集类别记为 `null` 且不参与均值。正式运行先把全部 PNG 安全读取成一份有界内存快照，清单、样本 ID 和训练消费同一份 bytes，不再二次扫描源目录；Pillow 解码前先读 IHDR，限制尺寸、单图/累计像素与压缩比。checkpoint 只保存 `state_dict`，所有产物记录大小与 SHA-256；`DOWNLOADS.json` 固定 PyTorch 发布 tag、版本、许可证及许可证页面，不自动下载。
- 替换了什么：把 SEG 从计划条目替换为可运行候选；没有替换共享内核、CLS 或其他方向包。
- 实际可见效果：创建出的仓库可运行 `seg.smoke`、`seg.train`、`seg.evaluate` 和 `seg.infer`；合成运行只输出 `debug_mean_iou`，正式本地数据才输出 `mean_iou`，两者不会混成论文成绩。冻结后即使源 PNG 被删除，当前运行仍只使用已核验快照；伪造超大 IHDR 或超预算压缩输入会在 Pillow 解码前失败。
- 选择原因：像素级任务最容易因 mask 插值、忽略标签和逐批平均产生假指标，因此第一版直接固定最小但完整的数据与计算合同。
- 已知限制：只提供轻量 CPU 基线，不打包大型数据、预训练权重或环境；快照方案以受限内存换取同一运行内的数据一致性，不适合未经拆分的大型数据集。尚未做大型分割数据集性能基准，正式论文结论仍需中心 Run 与证据门禁确认。
- 素材位置：`skills/cv-experiment-workflow/assets/domain-packs/seg-v1.0.0/`；回归测试为 `tests/test_seg_domain_pack.py`。
- 验证命令与结果：历史首次三个新方向组合回归为 `18 passed`；本次一致性与资源预算加固先用三个精确攻击节点验证为 `3 passed`，加固初版三文件组合回归为 `21 passed`。最后一次有界内存收尾后，组合命令受并行负载影响在 180 秒到时且没有产生断言失败，因此拆分重跑：SEG、SR、GZSL 各 `7 passed`，合计 21 项全部通过。Manifest 已在 README、代码和下载元数据定稿后按 payload 字节机械重建。
- 回退方式：回退 SEG 目录、注册表条目、测试和本条记录即可；正式目录、个人实例、PaperFlow 与 既有项目 无需迁移或回退。

## PACK-SR-V1.0.0：可运行二倍超分辨率方向包

- 日期：2026-07-27
- 状态：已完成，属于隔离 worktree 中尚未正式安装的开发候选。
- 本次改的是哪个对象：二倍单图超分辨率仓库模板、数据与指标合同、项目 Adapter、Manifest 和自动测试；没有修改共享训练内核。
- 目标问题：提供能现场运行的最小超分辨率仓库，并明确低分辨率与高分辨率配对、放大倍数和 PSNR 计算口径。
- 采用技术：使用 PyTorch CPU 小模型 `TinyPixelShuffle-v1` 与 L1 损失；数据固定为 RGB PNG，`LR/x2` 的宽高必须恰好是 HR 的一半；正式运行先把全部 PNG 安全读取成一份有界内存快照，清单、配对 ID 和训练/评估消费同一份 bytes，Pillow 解码前限制 IHDR 尺寸、单图/累计像素与压缩比。PSNR 在完整验证集的 RGB uint8 像素上累计平方误差与元素数后统一计算，拒绝零 MSE 的无限值，并在合同和产物中明确标为 `custom_full_rgb_no_shave`，未经重算不得与常见 Y 通道加裁边结果比较。`DOWNLOADS.json` 固定 PyTorch tag；DIV2K 只记录 2017 官方发布页，并明确官方页未声明统一许可、使用前人工核验。
- 替换了什么：把 SR 从计划条目替换为可运行候选；没有抽出跨方向公共模型层，也没有给其他包增加依赖。
- 实际可见效果：创建出的仓库可运行 `sr.smoke`、`sr.train`、`sr.evaluate` 和 `sr.infer`；合成运行只输出 `debug_psnr_rgb_x2`，正式本地数据才输出 `psnr_rgb_x2`。冻结后即使源 PNG 被删除，当前运行仍只使用已核验快照；结果文件会直接说明该 PSNR 是自定义口径，避免被当成公开基准的标准口径。
- 选择原因：把倍率、颜色空间和累计方式写死在合同中，能避免不同实验用不同 PSNR 定义却被误当成同一成绩。
- 已知限制：第一版只支持二倍 RGB 超分辨率和轻量 CPU 基线，不含感知损失、预训练权重、大型数据或高分辨率性能测试；快照方案不面向未经拆分的大型图像集。当前 PSNR 不是常见 Y 通道加边界裁剪协议，对外比较前必须按目标论文协议重算。
- 素材位置：`skills/cv-experiment-workflow/assets/domain-packs/sr-v1.0.0/`；回归测试为 `tests/test_sr_domain_pack.py`。
- 验证命令与结果：测试先因方向包不存在得到 `6 failed`；历史首次三个新方向组合回归为 `18 passed`。本次一致性、资源预算和指标口径加固的三个精确节点为 `3 passed`；加固初版三文件组合回归为 `21 passed`。最后一次有界内存收尾后，组合命令受并行负载影响在 180 秒到时且没有产生断言失败，随后拆分运行 SEG、SR、GZSL，三者各 `7 passed`。Manifest 已在 payload 定稿后机械重建。
- 回退方式：回退 SR 目录、注册表条目、测试和本条记录即可；其他方向包、正式目录、个人实例与旧工作流不受影响。

## PACK-GZSL-V1.0.0：可运行广义零样本学习方向包

- 日期：2026-07-27
- 状态：已完成，属于隔离 worktree 中尚未正式安装的开发候选。
- 本次改的是哪个对象：广义零样本学习仓库模板、严格 NPZ 数据合同、指标合同、项目 Adapter、Manifest 和自动测试；没有修改 Idea 树或共享账本。
- 目标问题：补齐从已见类训练、同时在已见类与未见类测试的最小 GZSL 路线，并防止数据划分泄漏或类别语义行错位。
- 采用技术：使用 PyTorch CPU 线性视觉到属性映射与 MSE 损失；NPZ 只允许九个固定数组，禁用 pickle/object，严格检查 dtype、有限值、索引互斥、类别覆盖和 `class_ids[index] → attributes[index]` 映射。正式运行只安全读取一次 NPZ，清单与训练/评估共享同一份冻结 bytes；在 `numpy.load` 前先检查 ZIP 成员、压缩方法、压缩比、单项/总展开体积，并安全解析 NPY header 的版本、dtype、shape 与声明字节数。推理在已见与未见类别并集上做余弦最近邻，不做隐藏校准；正式指标固定为按类平均的 Seen、Unseen 与调和平均 H。`DOWNLOADS.json` 固定 PyTorch tag，并记录 AwA2 官方发布页和逐图许可证复核要求。
- 替换了什么：把 GZSL 从计划条目替换为可运行候选；没有把外部 CADA-VAE 代码或数据复制进模板，只记录固定的官方依赖与数据来源元数据。
- 实际可见效果：创建出的仓库可运行 `gzsl.smoke`、`gzsl.train`、`gzsl.evaluate` 和 `gzsl.infer`；合成指标使用长名称明确标成 debug，正式运行才输出 `seen_class_average_accuracy`、`unseen_class_average_accuracy` 和 `harmonic_mean`。冻结后即使源 NPZ 被改写，当前运行仍消费已核验 bytes；高压缩比或伪造巨型 NPY header 会在数组物化前失败。
- 选择原因：GZSL 的关键不是堆模型，而是先把已见/未见划分、类别属性对应和 S/U/H 统计方式锁清楚，避免泄漏后仍产生看似合理的数字。
- 已知限制：只提供无校准的线性 CPU 基线，不打包 AwA2 等大型数据，也不代表 CADA-VAE 复现；当前快照方案只面向用户约定的几十 MB 输入，不适合直接装载大型原始图像集合。论文结论仍需真实数据、冻结环境与证据门禁。
- 素材位置：`skills/cv-experiment-workflow/assets/domain-packs/gzsl-v1.0.0/`；回归测试为 `tests/test_gzsl_domain_pack.py`。
- 验证命令与结果：测试先因方向包不存在得到 `6 failed`；历史首次三个新方向组合回归为 `18 passed`。本次一致性和压缩炸弹加固的三个精确节点为 `3 passed`；加固初版三文件组合回归为 `21 passed`。最后一次有界内存收尾后，组合命令受并行负载影响在 180 秒到时且没有产生断言失败，随后拆分运行 SEG、SR、GZSL，三者各 `7 passed`。Manifest 已在 payload 定稿后机械重建。
- 回退方式：回退 GZSL 目录、注册表条目、测试和本条记录即可；其他方向包、正式目录、个人实例、PaperFlow 与 既有项目 均不受影响。

## SYS-V2.12.0 / SKILL-RELEASE-V1.4.0：PaperFlow V1 与只读入口

- 日期：2026-07-26
- 状态：已完成，属于尚未安装的开发候选。
- 本次改的是哪个对象：PaperFlow V1 导出和现场核验、启动必填检查、统一只读页面数据、本地服务以及正式源码说明；不改 Task、Run、Evidence 的持久化字段，也不安装 Skill 或升级实例锁。
- 目标问题：旧交接包不能按原 producer 和原正文形状重建，结果摘要与文件复查也不够完整；同时工作流启动前没有一份明确的必填清单，界面、CLI 和 Agent 容易各自解释项目状态。
- 采用技术：按精确 producer 版本核验 PaperFlow V1，并对结果文件做二次完整 SHA-256；新增按实验路线区分的零写入启动检查；一次读取形成项目、真实关系、缺项和阻塞条件；本地服务只绑定 `127.0.0.1`，只开放固定静态文件与只读状态预览，并对来源、Host、请求体、并发和读取时间设上限。
- 替换了什么：替换“现场核验只接受当前 producer、遗漏事实不进摘要、最终只看文件时间”的实现，也替换 CLI、界面和 Agent 分别拼装启动判断的做法；保留 PaperFlow V1 顶层字段和现有实验账本格式。
- 实际可见效果：1.2.2、1.2.3、历史 1.3.0 与当前 1.4.0 的 V1 包可按各自正文规则核验；`intake-check` 会直接列出必须提供和仍缺少的内容；`console-state` 与本地三页界面读取同一份真实关系和执行阻塞原因。
- 选择原因：先把已有论文交接能力放回正式源码，再建立一个零写入、可直接观察的最小入口；这样能验证规则是否同源，又不会提前引入 Codebase 写入、Git 自动化或第二套账本。
- 已知限制：二次完整哈希不宣称消除文件系统上的全部理论竞争；历史 1.3.0 只兼容其 V1 正文，不加入锁升级白名单；第一阶段不登记 Codebase、不操作 Git、不创建 Task 或 Run，也不训练。
- 素材位置：核心实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/`，本地服务为 `skills/cv-experiment-workflow/scripts/console_server.py`；测试位于 `tests/test_paper_handoff.py`、`tests/test_intake.py`、`tests/test_ui_service.py`、`tests/test_console_server.py` 和发布、文档测试。
- 验证命令与结果：默认 Python 与 `dvsr_gpu` 的全量测试均为 616/616；本地服务专项均为 29/29，初始化专项均为 53/53。两套 `compileall`、JavaScript 语法检查、`git diff --check`、真实浏览器宽窄屏和实例零写入摘要检查均通过；细节见 `docs/reviews/2026-07-26-three-page-readonly-console-review.md`。
- 回退方式：回退本版本源码与文档提交；本机安装版和旧个人实例锁未改，无需回退安装或实例数据。

## UI-CONSOLE-V1.0.0：三页本地只读控制台

- 日期：2026-07-26
- 状态：已完成，随未安装的源码候选交付。
- 本次改的是哪个对象：工作流的人机入口和本地可视化页面；不改变科学实验、账本事实或 Git 历史。
- 目标问题：用户启动后应立即看懂“要给什么、项目里有什么关系、为什么现在不能执行”，而不是先读 JSON 或长命令说明。
- 采用技术：原生 HTML、CSS、JavaScript 加 Python 标准库服务；三个页面共用 `console-state` 的 Python 结果；关系图使用原生 SVG，并保留可读的对象和关系文字清单；不增加 Node 运行依赖、云服务、数据库或账号系统。
- 替换了什么：把旧八页控制台设想收成“开始与设置、关系图、执行确认”三页；命令行和 Agent 入口继续保留，不另建规则。
- 实际可见效果：宽屏和 390 像素窄屏都能查看真实项目快照；启动表单会显示全部缺项；关系图中的对象和关系均使用中文；错误时保留上一次结果并给出可操作提示；执行按钮固定禁用。
- 选择原因：三页已经覆盖当前真实使用问题，且可以随时关闭并退回 CLI，认知和维护成本较低。
- 已知限制：页面只读；没有 Codebase 账本时只显示已知仓库事实，不生成假的 `CB-*`；尚未实现仓库登记、分支、worktree、Tag、训练日志或 PaperFlow V2。
- 素材位置：`skills/cv-experiment-workflow/assets/console/index.html`、`styles.css` 和 `app.js`。
- 验证命令与结果：两套 Python 的静态页面测试通过，`node --check` 通过；Chrome 实测宽屏、390 像素窄屏、键盘导航、空缺项、确认页和错误状态，控制台为 0 个错误、0 个警告。只访问固定页面资源与 `/api/state`，实例控制目录 25 个文件和 Git 索引在浏览器前后摘要、字节与时间均未变化。
- 回退方式：停止本地服务并回退本节对应源码提交；CLI、Agent、实验账本和已安装旧 Skill 不受影响。

## DOC-AUDIT-V1.0.0：旧个人实例完整人工审核包

- 日期：2026-07-24
- 状态：已完成。
- 本次改的是哪个对象：审核包构建工具和项目内交付副本；不修改通用状态机、训练、评估、数据或历史账本。
- 目标问题：用户需要拿到能逐文件审核的真实文档和代码包，不能只依赖聊天说明或 C 盘临时文件。
- 采用技术：新增标准库脚本 `tools/build_audit_package.py`，按明确白名单复制通用模板、项目实例和机器账本的当前工作树字节，生成原路径映射、SHA-256、Python 语法检查和 ZIP 自解压复核。正式展开目录留在实例项目，模板仓库只保留同字节 ZIP，避免把实例账本当成通用模板源码。
- 替换了什么：没有替换工作流技术；只新增可重复生成的审核交付路线。
- 实际可见效果：用户可从旧个人实例 `docs/audit-package/00_先看这里.md` 开始逐步审核，也可使用两个仓库 `docs/deliverables/` 下的同名 ZIP。代码快照注明来自当前工作树，未把它冒充成纯净提交。
- 选择原因：项目文档要跟实例走，但构建方法属于通用模板；这能同时保证好找、可复查和两个仓库边界清楚。
- 已知限制：审核包主动排除 Git 内部对象、数据集、模型权重、运行大产物、外部环境和缓存；不能仅凭审核包复现高成本训练。
- 素材位置：构建脚本为 `tools/build_audit_package.py`；模板仓库交付副本为 `docs/deliverables/legacy-user-cv-workflow-audit-package-DOC-AUDIT-V1.0.0.zip`；正式展开目录位于同级实例仓库。
- 验证命令与结果：初次构建纳入 192 个源文件；补入构建脚本并完成修复后，最终登记 193 个源文件、211 个受校验文件和 104 个 Python 文件。ZIP 通过 CRC、自解压和解压后逐文件 SHA-256 复核，两个仓库副本逐字节一致。事实审核初审发现 2 个 Important，修复后为 0/0/0 PASS；完整性审核为 0/0/0 PASS。最终证据位于实例审核包的 `verification/AUDIT_RESULT.md`。
- 回退方式：回退新增脚本、技术记录和交付 ZIP；实例源代码和历史账本不需要回退。删除交付文件仍需用户另行明确授权。

## SYS-V2.10.1 / SKILL-RELEASE-V1.2.1：项目专属总协调入口

- 日期：2026-07-24
- 状态：已完成。
- 本次改的是哪个对象：项目初始化、项目身份、Skill 安装命令和 Agent 总协调入口；不改 Task、Run、Evidence、训练、数据或评估含义。
- 目标问题：只有通用 Skill 时，Agent 知道“实验工作流怎么运行”，却不能仅凭一个稳定入口知道“这次是哪一个项目、仓库在哪里、哪些目录只能读”。每次换会话都靠聊天重新说明，容易写错仓库。
- 采用技术：`init` 在项目根生成薄的 `SKILL.md`，并把带 UUID 短后缀的 `skill_id`、入口和 schema 写入 `.experiment-workflow/project.json`；既有项目保留已经登记的稳定 ID，使用 `init-project-skill` 补建。`install-project-skill` 通过安装清单显式复制或安全更新，`rebind-project-skill` 只刷新可证明为旧自动生成内容的搬家路径。项目专属 Skill 固定项目 UUID、路径提示、仓库边界和 Agent 协作规则，所有状态机操作继续委托给通用 `cv-experiment-workflow`。
- 替换了什么：替换“只有一份全局 Skill，再靠对话临时选项目”的做法；没有为每个项目复制一套通用脚本。
- 实际可见效果：每个新项目都会拥有一个可单独调用且不与同名项目冲突的 Skill 名；同一个项目 Skill 的正式源文件跟项目一起走。普通 `validate` 会检查入口缺失或错绑；假项目、外来 `SKILL.md`、超长或多行项目名、项目 UUID 不匹配、未知安装内容、竞争占位或链接/reparse 路径都会拒绝，不会静默覆盖。
- 选择原因：把项目边界固化为可审查文件，同时避免通用规则出现多份副本。用户说“继续演示项目”时先进入 `demo-user-cv-lab`，再由它调度 Idea Scientist、Implementer、Runner、Analyst 和 Reviewer。
- 已知限制：Skill 的路径只是移动后重新定位的提示，真正身份以项目 UUID 为准；搬家后必须显式执行重绑定和安装；Skill 只规定协作方式，真正执行仍依赖 Agent 宿主和可用环境。
- 素材位置：核心实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/project_skills.py`、`project.py` 和 `scripts/rw.py`；回归测试位于 `tests/test_project_skill.py`；入口规范位于 `README.md`、Skill 根 `SKILL.md` 和 `references/workflow.md`。
- 验证命令与结果：项目 Skill 专项为 `Ran 14 tests, OK`，系统 Python 与 `dvsr_gpu` 的项目 Skill、初始化、发布和文档组合各 `Ran 97 tests, OK`；两套环境的通用全量测试各 `Ran 510 tests, OK`，旧个人实例在 `dvsr_gpu` 下 `Ran 109 tests, OK`。源码、安装版和实例锁的 56 个正式文件摘要均为 `sha256:b17499136cefeab72995f498a430e0afddf9ddb79d703beac0a2349118c96fdc`，`check-workflow-drift=in_sync`。演示项目入口源文件与安装副本均通过 Skill 格式校验，SHA-256 同为 `5FC1AC5EC949FA433D055FA355BD00731047B5452C8135C0CDD833D0083A30D3`。三轮独立审核最终均为 0 Critical、0 Important、0 Minor、PASS。
- 回退方式：回退本版本代码和说明；新项目根 `SKILL.md` 与 `project_skill` 身份是可识别的新字段，旧运行版不会据此写账本。已安装的项目 Skill 副本需由用户确认后删除，本次不自动删除任何文件。

## SYS-V2.9.2 / SKILL-RELEASE-V1.1.2：锁升级的最终源码复核

- 日期：2026-07-24
- 状态：已完成。
- 本次改的是哪个对象：工作流锁升级时的源码一致性检查；不改 Idea、Task、Run、Evidence、训练或评估含义。
- 目标问题：升级命令最初只在进入写锁前读取一次源码摘要；若源码恰好在升级过程中变化，可能把过时摘要写入实例锁。
- 采用技术：先保存进入升级时看到的源码摘要，再在项目写锁内重新计算源码摘要，并同时要求它等于初始摘要和当前运行版摘要；任何一项不同都原子拒绝写锁。
- 替换了什么：加固 `SYS-V2.9.1 / SKILL-RELEASE-V1.1.1` 的锁升级实现，并把精确的已验证 v1.1.1 锁加入受信任旧锁清单。
- 实际可见效果：即使源码在升级过程中从 A 变成 B，实例锁也不会错误记录 A；用户修复同步关系后可重新显式升级。
- 选择原因：独立 Reviewer 用竞态场景发现了这个时间窗口；在写锁内二次计算是改动最小、结果最直接的封口方式。
- 已知限制：文件摘要只能证明三处正式 Skill 文件相同，不能证明 GitHub 已发布，也不能替代机器测试和科学审核。
- 素材位置：核心实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/releases.py`；回归测试位于 `tests/test_workflow_release.py`。
- 验证命令与结果：定向版本锁测试 `Ran 11 tests, OK`，发布、文档和初始化组合测试在两套 Python 中各 `Ran 82 tests, OK`。公共工作流在系统 Python 3.14 与 `dvsr_gpu` Python 3.10 下各 `Ran 495 tests, OK`；旧个人实例在 `dvsr_gpu` 下 `Ran 108 tests, OK`。源码与安装版 55 个正式文件 0 个不同，源码入口和安装版入口的 `check-workflow-drift` 都为 `in_sync`，三处摘要均为 `sha256:200971d3078c27e82228a8bfcc62730105134a79a38264ccee5be616cf48140a`；实例 `validate` 通过，`task-list` 为 4/4 done。三位独立 Reviewer 分别检查实例代沟、Idea/论文依据和发布同步，修复后均明确 PASS；发布同步终审为 0 Critical、0 Important、0 Minor。
- 回退方式：同时恢复 v1.1.1 的源码、安装版和实例锁；不得只手改锁摘要。历史实验账本不需要回退。

## SYS-V2.9.1 / SKILL-RELEASE-V1.1.1：升级安全与 Idea 追踪加固

- 日期：2026-07-24
- 状态：已完成。
- 本次改的是哪个对象：版本锁升级边界、只读漂移快照、Idea 来源完整性和修订真实性；不改四条实验路线的训练与比较语义。
- 目标问题：初版升级命令能改写只有锁文件的假项目，未知 v2 锁没有安全升级路线；Idea 允许部分来源没有定位，也允许内容完全相同的空修订。
- 采用技术：写锁内先按项目 schema 校验 UUID、名称、固定目录和完整账本；只读检查用项目快照锁。受信任旧 v2 锁使用精确完整字典登记，未知摘要拒绝。Idea 要求 `source_refs` 与 `source_links` 覆盖集合一致，并比较全部科学字段拒绝空修订。
- 替换了什么：加固 `SKILL-RELEASE-V1.1.0 / SYS-V2.9` 的初版实现；保留旧 v1 锁和精确受信任 v2 锁的显式升级能力。
- 实际可见效果：假项目、损坏账本、未知 v2 锁和升级过程中的源码变化都不会写锁；每个 Idea 来源都有论文页码、章节、公式或代码符号等具体定位；没有实际变化就不会增加版本号。
- 选择原因：把独立 Reviewer 实证出的失败路径直接固化为机器拒绝规则，避免“有命令但不验证对象”和“有来源 ID 但没有具体依据”。
- 已知限制：受信任旧 v2 锁需要每次正式发布时显式追加，不能靠版本号猜测；论文/代码定位内容是否真的正确仍需 Reviewer 阅读原文，脚本只保证非空、归属和完整覆盖。
- 素材位置：核心实现位于 `workflow_core/releases.py`、`records.py`、`v2_catalog.py`；回归测试位于 `tests/test_workflow_release.py`、`tests/test_v2_catalog.py`。
- 验证命令与结果：定向版本锁 10 项与新增 Idea 场景 4 项已通过；随后 Reviewer 发现升级过程中的源码变化窗口，由 `SYS-V2.9.2` 继续修复并新增第 11 项版本锁测试。
- 回退方式：回退本补丁并恢复上一版源码、安装版与实例锁；不得只手改锁摘要。历史 Idea、Task、Run 和 Evidence 不需要删除。

## UI-V3.1：当前框架图同步代码指纹锁

- 日期：2026-07-24
- 状态：已完成。
- 本次改的是哪个对象：当前 V3 HTML 的版本锁说明文字；不改 V2 原版、页面结构、样式、交互或系统行为。
- 目标问题：当前技术版本已使用 v2 指纹锁，但 V3 页面仍写“Skill 版本 1.0.0”。
- 采用技术：只把一行目录注释改为“v2 代码指纹锁（发布号 + 文件摘要）”。
- 替换了什么：替换 V3 的过期事实文字，不替换视觉方案。
- 实际可见效果：用户从当前框架图看到的锁含义与真实实例一致。
- 选择原因：避免“当前页面”继续展示已淘汰的版本号锁。
- 已知限制：页面仍是静态事实图，后续对象数量和实验进度不会自动更新。
- 素材位置：`docs/diagrams/cv_research_workflow_current_framework_strict_editorial_v3.html`。
- 验证命令与结果：HTML 原文件存在；新文字恰好 1 处，旧“Skill 版本 1.0.0”文字为 0 处；`git diff --check` 通过，仅提示工作区行尾将在 Git 下次处理时标准化。
- 回退方式：恢复 V3 这一行旧文字；V2 原版不受影响。

## SYS-V2.9 / SKILL-RELEASE-V1.1.0：代码指纹锁与不可变 Idea 修订

- 日期：2026-07-24
- 状态：已完成。
- 本次改的是哪个对象：系统的版本识别、实例锁和 v2 Idea 记录；同时更新 Skill 发布说明。不改四条实验路线的训练、比较和 Evidence 历史。
- 目标问题：旧实例锁只写 `1.0.0`，不能证明源码与安装版相同；v2 Idea 与另一种 Idea 共用 schema 名称，修订也会丢失父版本和论文具体出处。
- 采用技术：继续使用 Python 标准库、JSON、SHA-256、项目写锁和原子写入。新锁记录 Skill 正式文件的路径+字节摘要；旧锁保持可读，升级必须显式执行。新 Idea 使用独立 schema，修订总是创建新 ID，并校验来源定位和证据引用。
- 替换了什么：新项目从只含版本号的 `workflow-lock.v1` 改为可核验的 `workflow-lock.v2`；新 v2 Idea 从含义冲突的 `idea.v2` 改为 `catalog-idea.v1`。历史锁和历史 Idea 不删除、不覆盖。
- 实际可见效果：用户可以只读看到源码、运行版、实例三者是否一致；源码不一致时升级会拒绝写入。Idea 修订后父文件字节不变，新文件明确指向父 Idea、修订原因、论文页码或代码位置。
- 选择原因：先让版本和科学依据可追溯，再做更大的 Adapter 拆分，能用最小改动解决最容易误判的两个根因。
- 已知限制：代码指纹说明“文件相同”，不等于 GitHub 已发布或已打标签；Reviewer 结论仍保存在审核文档，尚未成为机器状态机里的独立 Review 对象。
- 素材位置：实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/releases.py`、`v2_catalog.py` 和 `scripts/rw.py`；测试位于 `tests/test_workflow_release.py`、`tests/test_v2_catalog.py`。
- 验证命令与结果：定向版本锁测试 `Ran 7 tests, OK`；v2 Catalog 测试 `Ran 30 tests, OK`。公共工作流在系统 Python 3.14 与 `dvsr_gpu` Python 3.10 下各 `Ran 488 tests, OK`；旧个人实例 `Ran 108 tests, OK`；安装版 `validate` 通过，`task-list` 为 4/4 done，`check-workflow-drift` 为 `in_sync`。
- 回退方式：回退本版本代码和文档；已经升级为 v2 锁的实例需使用与锁内摘要一致的 Skill 运行，不能直接伪造回旧锁。历史 Idea 文件不需要回退。

## SKILL-POLICY-V2.9：固定两轮审核草案（原候选编号 `SKILL-V2.9`）

- 日期：2026-07-23
- 状态：已放弃，未启用。2026-07-24 根据项目现行规则完成清理；源码、安装版和测试都继续使用按真实影响选择一至三轮的政策。
- 本次改的是哪个对象：通用工作流的审核政策、Idea 到代码的角色交接和文档测试；不改 Task → Run → Evidence 状态机，不改已经完成的历史审核记录。
- 目标问题：此前高影响改动会使用第三轮审核，耗时偏长；Idea、伪代码、正式模块和实验证据之间的两道责任边界也没有在人话入口中说清。
- 采用技术：草案曾设计固定两轮独立审核。第一轮在正式编码前检查 Idea、可证伪假设、伪代码、Template 接入点和关闭行为；第二轮检查真实 `.py`、diff、契约测试、运行配置、Result 和结论边界。最终没有把该草案写入活动规范。
- 替换了什么：没有替换当前政策；历史三轮审核文档原样保留，只作为当时事实。
- 实际可见效果：没有活动效果。工作流仍根据真实影响选择一、二或三轮，小改不被强制做两轮，重要训练、评估和论文结论也不会被固定两轮封顶。
- 选择原因：放弃固定轮数，是因为审核强度应跟影响范围相称；固定两轮会让低风险修改多做无效等待，也可能让高风险改动少一次必要的收口检查。
- 已知限制：审核轮数本身不能替代机器测试；promotion、论文主张、删除、push 和发布仍保留原安全与授权门槛。
- 素材位置：草案核验和放弃原因保存在 Git 历史；历史测试样本现为 `tests/skill_scenarios/red-attempt.txt`，不能作为当前操作说明。
- 验证命令与结果：草案阶段的定向检查曾通过；放弃草案后重新运行文档测试、场景证据检查、补丁格式检查和源码/安装版一致性检查，结果见对应审核记录。
- 回退方式：本条只保留历史说明，不提供重新启用固定两轮的自动回退；如未来确有新证据支持，应建立新的 `SKILL-POLICY-*` 候选并重新审核，而不是复活旧草案。

## UI-V3.0：与当前四条实验事实同步的严格框架图

- 日期：2026-07-23
- 状态：已完成。
- 本次改的是哪个对象：界面与流程图事实层；不改系统代码、Skill 入口、实例账本和旧版 V2 页面。
- 目标问题：`editorial_v2` 的视觉风格已经稳定，但正文仍混有“尚未绑定、只有规划”等旧快照，容易让人把历史设计误认为当前进度。
- 采用技术：复制 V2 单文件 HTML，以同一套纯 CSS、内嵌 SVG 和六页签交互更新事实层；不新增网络资源、构建工具或新框架，继续沿用 V2 已有的 unpkg 脚本。
- 替换了什么：不替换、不覆盖 V2，只新增独立的 `editorial_v3` 入口。
- 实际可见效果：图中能看到 innovation、tune、ablation、reproduction 四条共享闭环、4 个已完成 Task/Run/Evidence、当前模板与实例路径，以及旧 既有项目 只读边界。
- 选择原因：保留 V2 便于历史对照，同时让日常打开的 V3 与真实代码、账本和实验进度一致。
- 已知限制：这是 2026-07-23 的静态事实图，后续新增正式规模实验时仍需人工更新；不同电脑的中文字体回退可能略有差异。
- 素材位置：正式页面为 `docs/diagrams/cv_research_workflow_current_framework_strict_editorial_v3.html`；三种尺寸截图和复查脚本位于 `docs/superpowers/reviews/artifacts/2026-07-23-v3/`。
- 验证命令与结果：Edge/Playwright 在 `1440×2200`、`813×1066`、`390×844` 下逐一切换 6 个页签；每次都是 1 个页签被选中、1 个面板可见，外层和内层横向溢出均为 0，控制台错误与警告均为 0。V2 文件的 Git object 仍为 `b25690e1862a5e7039fcdaac1b91e4715a83e105`，与 `HEAD` 一致。
- 回退方式：停止使用或回退 V3 文件和本条记录即可；V2 原版及其历史入口不受影响。

## SYS-V2.8：四类实验共享执行闭环（原历史编号 `v2.8`）

- 日期：2026-07-23
- 状态：已完成。
- 本次改的是哪个对象：系统底层的四路线任务创建、执行、比较和证据闭环；不改界面视觉和旧 既有项目。
- 目标问题：v2 此前只有调参和创新能创建、执行 Task，消融与复现虽然能被自然语言识别，却不能进入 Task → Run → Evidence；实例 Adapter 也只接受创新。
- 采用技术：继续使用唯一 `execute_task()`、现有 Task/Run/Evidence 状态机和项目五接口 Adapter；将四条路线差异限制在 `requires / build_variants / compare / mutation_class`。CLI 用 `--route-options` 接收经过严格 schema 复核的路线专属 JSON，不新增第二套账本或 Runner。
- 替换了什么：把“消融、复现只识别”的旧边界替换为四路线可执行边界；不替换现有创新链、崩溃恢复、预算和证据分级。
- 实际可见效果：`start-task --route` 接受 tune、ablation、reproduction、innovation；四条路线共用同一 Task → Run → Evidence。旧个人实例的 4 个 Task、4 个真实 GPU Run 和 4 条 debug Evidence 均已完成，调参负结果、消融差异和零容差精确复现都按原值留账。
- 选择原因：先复用已完成 `RUN-0001` 作为启用侧和复现来源，每条新 Task 只跑一个真实候选，可以最小成本证明四路线共用一条闭环，同时不提前扩建多候选调度系统。
- 已知限制：本版本只验收四条最小真实 debug，不声称找到最佳参数、证明模块贡献或形成论文结论；同一 Task 的多候选自动调度仍未实现。
- 素材位置：公共实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/` 与 `scripts/rw.py`；路线测试位于 `tests/test_experiment_routes.py`、`tests/test_task_run_evidence.py`；实例配置、Adapter、账本和结果说明位于同级 `legacy-user-cv-lab` 仓库；审核记录位于 `docs/superpowers/reviews/2026-07-23-four-route-real-loops-review.md`。
- 验证命令与结果：系统 Python 3.14 执行全量 `unittest discover` 为 `Ran 476 tests, OK`；`dvsr_gpu` Python 3.10 执行同一套测试为 `Ran 476 tests, OK`；实例为 `Ran 106 tests, OK`。安装版 `validate` 通过并识别 9 个 Source、1 个 Idea、1 个 Template、1 个 Module，4 个 Task 全部完成。隔离安全、科学协议、文档账本三位独立 Reviewer 在修复后均明确 PASS；文档账本终审为 0 Critical、0 Important、0 Minor。
- 回退方式：回退本版本功能提交并恢复安装版 Skill；实例新增 Task/Run 作为历史保留，不改写或删除旧 `RUN-0001`。旧 既有项目 全程只读，不参与回退。

## v2.7：可核验资料目录与 innovation 最小执行闭环

- 日期：2026-07-23
- 状态：已完成。
- 本次改的是哪个对象：系统底层的 Source、Idea、Template、Module 目录和 innovation 执行路线；新分类对应 `SYS-V2.7`。
- 目标问题：新版此前只有调参执行路线，无法把论文、非 Git 参考代码、新想法、可运行模板和研究模块连成可核验关系，也不能从 `innovation` Task 安全启动项目自己的真实 Adapter。
- 采用技术：继续使用 Python 标准库、JSON / JSONL、Git 和 `argparse`；新增 `Source / Idea / Template / Module` v2 目录，非 Git 代码使用有限关键文件 SHA-256 快照，运行代码必须来自 Git Source；项目 Adapter 绑定固定提交和原始 bytes。Windows 运行器使用 Job Object 管理整个进程树，POSIX 使用进程组，并统一限制时间和日志缓冲；Task 的 `max_runs` 在创建 Run 前强制检查，`single_debug` 达标后只按“调试范围”收尾。
- 替换了什么：把“论文、代码、创新入口尚未完成”的 v2.1 计划状态替换为可写、可校验的资料目录和 `innovation` 实际执行路线；调参路线保持不变，消融与复现仍只展示清单、不开放执行。
- 实际可见效果：旧个人实例已登记 9 个 Source、1 个 Idea、1 个 Template、1 个 Module，并由 `TASK-0001` 生成成功关闭的 `RUN-0001` 与 debug Evidence；一次调试预算用完后不会出现“还可继续跑”的假入口。外部 `code_snapshot` 只能说明参考来源，不能冒充可运行模板。
- 选择原因：可运行代码必须能追到固定 Git 提交，参考快照则只需证明“看过哪些明确文件”；两种用途分开，既不把外部目录拼进新项目，也能把研究来源和真实实现讲清楚。
- 已知限制：当前只开放 `tune` 与 `innovation` 两条写入/执行路线；首次闭环是每类一张图片的 debug，不能支持论文结论；运行产物仍由具体项目自行决定是否上传或长期保存。
- 素材位置：目录实现位于 `skills/cv-experiment-workflow/scripts/workflow_core/v2_catalog.py`；任务关系校验位于 `tasking.py`；Adapter 与有界运行位于 `adapters.py`、`policy.py` 和项目 `workflow_adapter.py`；终审记录位于 `docs/superpowers/reviews/2026-07-23-v2-catalog-and-innovation-loop-review.md`。
- 验证命令与结果：Python 3.14 与 Python 3.10 对 `test_v2_catalog`、`test_engine_tune`、`test_task_checklists`、`test_task_run_evidence`、`test_v2_project`、`test_skill_docs` 均为 `Ran 165 tests, OK`；运行 Adapter 的私有 dataclass、重复加载、异常回滚、并发加载和 `single_debug` 旧状态恢复通过；旧个人实例为 `Ran 101 tests, OK (skipped=1)`，真实 `RUN-0001` 退出码为 0。
- 回退方式：回退本版本从 `dcde418a8cd106486ddc23b9d21dd22213dc1b79` 之后的功能提交；实例账本可以保留为历史，但旧版工作流和 既有项目 不需要迁移或修改。

## v2.6：严格框架图“科研档案册”视觉新版

- 日期：2026-07-23
- 状态：已完成。
- 本次改的是哪个对象：严格框架图的页面视觉和排版；新分类对应 `UI-V2.0`，不代表系统功能升级。
- 目标问题：原严格框架图信息完整，但画布偏窄、蓝色后台卡片感较强，长流程的层级、阅读节奏和项目辨识度不足。
- 采用技术：保留单文件 HTML、原有内嵌 SVG、六页签交互和全部历史内容；新增纯 CSS 编辑排版层，使用暖米白纸张、深墨绿主色、中文衬线大标题、编号目录、深色数据横条和响应式布局，不引入网络字体或新依赖。
- 替换了什么：不替换原版，只新增独立视觉版本；参考 `<只读 PaperFlow 项目>/docs/cv_paper_workflow_final.html` 的研究手册风格，但不复制其业务内容。
- 实际可见效果：新版形成更宽的科研档案册式页面，首页先讲主链和读图规则，桌面端使用左侧编号目录切换六个主题，窄屏改为横向目录；流程节点、表格、进度和历史边界使用统一的绿、赭、灰语义。状态页原先贴边的 `TASK / RUN / EVIDENCE` 标签已在视觉复核中修正。
- 选择原因：参考页的编辑排版感适合承载高密度研究流程；保留原版可确保用户随时对照或回退。
- 已知限制：新版仍是 2026-07-16 历史设计快照的视觉重排，不会自动把旧进度更新为当前实现；页面使用本机中文字体回退链，不同电脑的字形可能略有差异。
- 素材位置：原版为 `docs/diagrams/cv_research_workflow_current_framework_strict.html`；新版为 `docs/diagrams/cv_research_workflow_current_framework_strict_editorial_v2.html`；参考风格来自项目外只读文件 `<只读 PaperFlow 项目>/docs/cv_paper_workflow_final.html`。
- 验证命令与结果：Edge 在 `1440×2200`、`813×1066` 和 `390×844` 三种尺寸下完成真实渲染和截图复核；Playwright 进入内嵌页面逐一切换 6 个页签，桌面与窄屏均无页面或面板横向溢出，3 张 SVG 中无文字越界和节点重叠，浏览器无 JavaScript 或控制台错误。流程图审核脚本能渲染外层页面，但因 SVG 位于 `iframe srcdoc` 内而报告“未发现 SVG”，所以改用同一浏览器直接进入 iframe 完成节点级检查。原版 SHA-256 保持 `52B47037B79F541859D45591ED1766B4A5EEFB68D9A0E5AFE825842509FB9E56`；`python -m pytest -q tests/test_scenario_evidence.py tests/test_skill_docs.py` 为 `22 passed`，`git diff --check` 通过。审核记录位于 `docs/superpowers/reviews/2026-07-23-editorial-framework-diagram-review.md`。
- 回退方式：删除或回退新增的视觉新版和本条记录即可；原版文件不修改。

## v2.5：HTML 流程图归档到项目

- 日期：2026-07-21
- 状态：已完成。
- 本次改的是哪个对象：项目文档的保存位置和归档规则；新分类对应 `DOC-ARCHIVE-V1`，不改工作流执行逻辑。
- 目标问题：此前由 Codex 生成的流程图只保存在 C 盘临时目录，脱离项目后不容易找到，也不能随仓库一起维护。
- 采用技术：继续使用可离线打开的单文件 HTML，不新增框架或依赖；正式文件统一保存到 `docs/diagrams/`，并在新工作流总目录和模板仓库的 `AGENTS.md` 中固定保存规则，使模板与实例共同继承。
- 替换了什么：用项目内可追踪目录替换 `%USERPROFILE%\.codex\visualizations\` 作为正式交付位置；C 盘原文件只保留为生成缓存，不删除。
- 实际可见效果：项目内现有总链路图、严格完整框架图和工作流地图三份 HTML，可以直接从仓库打开；历史图顶部注明生成和归档日期，旧工作树链接已改为项目内相对链接。
- 选择原因：流程图属于项目资料，和代码、规范放在一起最容易查找、迁移和版本追踪；单文件 HTML 也符合本项目本地直接打开的要求。
- 已知限制：三份图反映的是 2026-07-16 至 2026-07-17 的设计快照，不代表所有规划功能已经实现；当前事实仍以代码、测试和本文件最新记录为准。
- 素材位置：`docs/diagrams/cv_research_workflow_chinese_v1.html`、`docs/diagrams/cv_research_workflow_current_framework_strict.html`、`docs/diagrams/cv_research_workflow_map.html`。
- 验证命令与结果：3 份 HTML 均通过文件存在性、标题、可见历史提示、旧工作树路径和个人绝对路径检查，3 个项目内链接目标均存在；`python -m pytest -q tests/test_scenario_evidence.py tests/test_skill_docs.py` 为 `22 passed`，`git diff --check` 通过。全量测试分别用 Python 3.14 的 pytest 和 Python 3.10 的 unittest 尝试，两次都在 120 秒上限内未结束；其中 Python 3.14 在超时后的输出流收尾另报 `OSError: [Errno 22]`，没有出现与本次改动相关的断言失败。
- 回退方式：回退本次提交即可移除项目副本和保存规则；C 盘原始生成文件未删除，仍可恢复。

## v2.4：新工作区迁移与双向只读索引

- 日期：2026-07-20
- 状态：已完成。
- 本次改的是哪个对象：系统仓库布局、迁移位置和新旧仓库只读索引；新分类对应 `SYS-V2.4`。
- 目标问题：把通用模板与初始化实例迁入同一工作总目录，并让新版与旧 既有项目 能互相定位、读取仓库文件，同时继续阻止自动同步和跨仓库误写。
- 采用技术：为模板、实例和旧 既有项目 分别维护 `REPOSITORY_INDEX.json` 与人话说明；新模板和实例使用自包含 Git 仓库，不使用链接或共享对象库。
- 替换了什么：替换依赖旧 `.worktrees/v2` 路径的入口；不恢复旧迁移命令，也不合并新旧仓库。
- 实际可见效果：在 PyCharm 打开新总目录即可看到模板和实例；两套工作流可按索引读取对方文件，跨仓库写入仍需用户明确授权。
- 选择原因：自包含仓库搬家后不会依赖旧 Git 管理目录；只读索引满足相互查阅需求，又不破坏此前建立的独立边界。
- 已知限制：索引使用本机绝对路径，只适用于当前电脑；它不负责监控文件变化或自动同步。
- 素材位置：新总目录为当前科研工作流工作区；旧 既有项目 继续位于原目录，不随本次迁移移动或删除。
- 验证命令与结果：两个新仓库执行 `git fsck --full --no-dangling` 通过；四处 JSON 索引均能解析并定位三个真实 Git 仓库；实例 `validate`、`workflow-status`、`plan-next` 通过；旧 既有项目 `validate` 与 `audit-boundary` 通过；新版 `python -m pytest -q` 为 `422 passed, 318 subtests passed`。
- 回退方式：旧模板与旧实例位置先保留不删除；新目录验证失败时继续使用旧位置。

## v2.3：新版与旧项目彻底隔离

- 日期：2026-07-20
- 状态：已完成。
- 本次改的是哪个对象：系统隔离边界、测试入口和安装目录；新分类对应 `SYS-V2.3`。
- 目标问题：新版虽然已有独立目录，但仍保留旧项目迁移入口、真实旧项目测试依赖、旧 Adapter worktree 和混乱的安装入口，无法保证两个工作流互不影响。
- 采用技术：保留 Python 标准库与现有 Task → Run → Evidence 内核；删除 v2 活跃迁移/旧账读取模块，新增隔离边界测试，并用 pytest `testpaths` 限定正式测试目录。
- 替换了什么：替换“把旧 既有项目 迁移为新版首个实例”的旧路线，改为新版工具、新版实例、旧 既有项目 三边完全分开。
- 实际可见效果：新版 CLI 不再提供旧项目迁移命令，测试不再要求真实 既有项目，新旧 Skill 分目录安装；旧 Adapter 开发分身已退出活跃 worktree，并保留为可独立恢复的 Git 归档。
- 选择原因：用户明确要求两个工作流彻底独立；继续保留迁移和切换能力会让误写旧项目始终存在。
- 已知限制：历史 Git 提交和旧审核文档会继续保留相关名称，作为可追溯历史，不代表当前活跃依赖。
- 素材位置：新版源码位于当前 v2 worktree；旧项目与历史归档保持在各自目录。
- 验证命令与结果：新增隔离测试先按预期失败；修复后 `python -m pytest -q` 为 `422 passed, 318 subtests passed`，Python 3.10 的 `python -m unittest discover -s tests -v` 为 `Ran 422 tests, OK`。旧 Adapter 归档执行 `git fsck --full --no-reflogs` 通过，目标提交与原分支 tree 完全一致；新版三个修复文件与独立安装 Skill 的 SHA-256 完全一致。
- 回退方式：所有代码删除均由 Git 保存；旧 Adapter worktree 转独立归档前先建立自包含克隆并核对提交与对象完整性。

## v2.1：独立实例初始化与代码路线

- 日期：2026-07-19
- 状态：计划中
- 本次改的是哪个对象：系统的独立实例初始化和代码路线计划；新分类对应 `SYS-V2.1`。
- 目标问题：让独立 CV 项目在安装工作流后，能通过人话完成一次性项目配置、基础模板建立、单模块接入和本地验证，同时不接管 既有项目。
- 采用技术：通用工作流继续使用 Python 3.10+ 标准库、JSON/JSONL、`argparse`、`unittest` 和 Git；新增项目标准、Source、Template、Module 与代码 Task 的轻量记录，不在通用仓库引入 PyTorch。
- 替换了什么：计划替换旧设计中的“迁移并切换 既有项目 写入口”，以及 Template 维护固定科学插口的描述。
- 实际可见效果：计划完成后，用户说“把这些代码整理成模板”或“把这个 Idea 接进去”时，工作流能建立对应 Task、检查缺失输入并追踪模板或模块登记；当前尚未实施。
- 选择原因：把通用协调能力和具体训练代码分开，既保持新实例独立，也避免把通用工具做成沉重的训练框架。
- 已知限制：当前 v2 仍只有调参执行路线；论文、代码、消融、复现和创新写入口尚未完成。审核改为按风险选一至三轮，Claude Code 不可用时由临时只读 Agent 接替，不再等待单一工具额度恢复。
- 素材位置：完整实施规划位于 `docs/superpowers/plans/2026-07-17-cv-research-workflow-implementation.md`；真实训练代码计划放在独立 `legacy-user-cv-lab` 仓库。
- 验证命令与结果：规划阶段将运行文档规则测试、文档预算和 `git diff --check`；技术实现尚未开始，不能记录为功能通过。
- 回退方式：每个实施 Task 独立提交；任一阶段失败只回退当前小提交。既有项目 不在本次修改范围内，不需要迁移回退。

## PACK-GZSL-V1.1.0 / SYS-CORE-V1.0.0：GPU-only Framework 工作区

- 日期：2026-07-29
- 状态：已完成并通过 GZSL 专项与科研到 PaperFlow 的真实端到端验证；当前仍位于 C 盘隔离候选，不自动安装、不 push。
- 本次改的是哪个对象：GZSL 方向包、Framework/Idea/实验/Run 账本、外来 GZSL 框架接入和科研交付包入口。
- 目标问题：旧页面暴露 Codebase、Module、Trial、Attempt 等内部名词；GZSL 仍有 CPU 调试路径；代码框架、四类实验和 Idea 子框架的关系没有按用户理解落到同一个仓库。
- 采用技术：`dvsr_gpu`、PyTorch 2.11.0+cu128、Git 分支/Worktree/Tag、仓库内 JSON 账本、局部可读实验编号、固定 `S/U/H`、NPZ 数据清单、SHA-256 和输出密封。
- 替换了什么：当前 GZSL 使用 `PACK-GZSL-V1.1.0` 替代 `PACK-GZSL-V1.0.0`；旧方向包目录在 Git 历史中保留。用户入口不再暴露 Codebase、Module、Trial、Attempt 和旧 Version 账本，底层兼容对象只用于复用已验证执行引擎。
- 实际可见效果：每个稳定 Framework 都有复现、调参、消融和创新四类实验；只有创新绑定 Idea 并产生子 Framework 与一张 HTML；外来标准化框架可以成为同级顶层 Framework；所有 GZSL Run 固定使用 GPU。
- 选择原因：代码框架是实验的根，Idea 只是可能改变框架的一类创新；这一结构既兼容用户已有框架，也避免每个小实验新建仓库。
- 已知限制：只完成 GZSL；大型数据和权重不打包，内置小型 NPZ 登记上限为 64 MiB；任意外来仓库仍需要用户或 AI 先解释和标准化代码，系统不会猜测语义。
- 素材位置：`skills/cv-experiment-workflow/assets/domain-packs/gzsl-v1.1.0/`、`scripts/workflow_core/framework_workspace.py`、`tests/test_framework_workspace.py`。
- 验证命令与结果：`tests.test_gzsl_domain_pack` 为 16 项通过；`tests.test_domain_pack_bound_compat` 为 3 项通过；Framework 专项覆盖四类实验、Idea 子框架、GPU Run、交付包、外来框架接入与完整性检查；统一系统真实端到端测试通过。
- 回退方式：停止使用 C 盘统一候选；旧 既有项目、旧科研工作流和旧目录均未删除或覆盖。

## v2.2：只读任务清单

- 日期：2026-07-19
- 状态：已完成
- 本次改的是哪个对象：系统的只读任务清单派生视图；新分类对应 `SYS-V2.2`。
- 目标问题：让用户既能看整个项目还有多少任务，也能只看某个实验走到哪一步，同时避免再维护一份容易过期的清单。
- 采用技术：没有更换技术栈；在现有 Python 3.10+ 标准库、JSON/JSONL 和 `argparse` 上增加纯 Python 只读派生视图。项目总清单和单实验六步清单每次从 Task、Run、Evidence 计算，不新增 `checklist` 持久化字段。
- 替换了什么：没有替换框架或核心依赖；只替代“另存一份 checklist 再人工同步”的做法，复用现有 Task 状态、Run 结果和 Evidence 资格。
- 实际可见效果：`workflow-status --project` 的总览内含全局 `task_list`；`task-list --project` 只看总清单；加 `--task TASK-0001` 只看一个实验。调参、消融、复现和创新显示各自的六步中文清单；`done` / `reviewing` 缺 debug 或正式证据时明确显示 `blocked`。
- 选择原因：同一事实只保存一次，清单随底层事实现场计算，既不会不同步，也不复制四套执行流程。
- 已知限制：只支持 v2 项目；这是只读查看能力，不会创建 Task、运行实验或补证据。四类路线都有清单，不代表四类写入和执行入口都已开放；当前仍只有调参执行路线。
- 素材位置：核心派生逻辑在 `skills/cv-experiment-workflow/scripts/workflow_core/checklists.py`，命令接线在 `scripts/rw.py` 与 `workflow_core/planning.py`，回归测试在 `tests/test_task_checklists.py` 和 `tests/test_skill_docs.py`，审核证据在 `docs/superpowers/reviews/2026-07-19-derived-task-lists-review.md`。
- 验证命令与结果：实现阶段运行全量 `unittest`，结果为 `Ran 458, OK (skipped=5)`；本次运行 `tests.test_task_checklists` 为 `Ran 10, OK`，`tests.test_skill_docs` 为 `Ran 17, OK`，文档预算为 `SKILL=125/220, references=320/800`，`git diff --check` 无输出并通过。
- 回退方式：先回退本次文档提交；若连功能一起撤销，再依次回退修复提交 `f1c1baef` 和功能提交 `b3d7c486`。清单从未落盘，因此不需要迁移或清理用户数据。

</details>
