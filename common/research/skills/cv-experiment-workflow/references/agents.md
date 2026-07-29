# 多 Agent 编排

## 身份长期、实例临时

角色是长期身份与知识边界，定义在 `assets/roles/`；Agent（智能体）是为当前任务临时启动的实例。实例完成即关闭，但角色文件和经审核的滚动 memory（记忆）长期保留。启动时只加载所需角色与该角色 memory，结束时先审核新增经验，再由 Coordinator 调用 `update-role-memory` 原子更新；任何角色都不得手写 `.experiment-workflow/agents/`。

memory 仅在完成真实任务后更新，候选经验必须同时满足：新出现且已验证、项目特定、模板中不存在、对未来有复用价值。初始化、no-op（无操作）、阻塞、仅复述 Skill 规则或通用角色职责时不得写 memory；真实实验事实仍写 Idea/Trial/Attempt/Result，不在 memory 复制结论。一次只更新实际产生新经验的最小角色集合：先经 Reviewer 审核，再由 Coordinator 调用 `update-role-memory`；未经审核的猜测、长对话和临时日志不沉淀。

长期角色不等于永久运行的聊天线程：角色定义长期存在于 `assets/roles/*.md`，审核后的项目经验长期存在于 `.experiment-workflow/agents/*.md`；Agent 实例只为当前任务临时创建，证据交接完成后关闭。Idea、Trial、Attempt、Result 是长期实验事实，不复制进 memory。

`rw.py` 只负责确定性账本、编号、冻结和校验，不负责创建 Agent。宿主环境支持真实多 Agent 时，Coordinator 启动所需的最小临时组合；不支持时可以按角色边界串行完成普通工作。需要独立审核时优先使用不同模型；某个模型超时、额度不足或失败，就换成当前环境可用的临时只读 Agent。失败调用不算有效审核，但不能只因为某个审核工具不可用而卡住整个任务；所有独立 Agent 都不可用时，由主 Agent 隔离上下文做反方自审、重跑机器验证并明确记录限制。主 Agent 自审不算独立审核轮次，也不能单独放行需要独立 Reviewer 的 promotion 或重要结论。

## 六个角色

| 角色 | 责任 |
|---|---|
| Coordinator | 承担 Promotion Gatekeeper（晋级门控）；机器、Reviewer、用户三项许可齐全后协调 CLI |
| Idea Scientist | 维护 Idea Tree（创意树）的关系、revision（修订版本）与来源 |
| Implementer | 承担 Template Architect（模板架构），按实现映射从干净模板派生代码 |
| Runner | 做资源前置检查，准备并核对冻结 Attempt，由 Coordinator 写账本 |
| Analyst | 用实现/假设双轴解释 Result，并按确认策略说明尚缺的独立证据 |
| Reviewer | 承担 Source Curator（来源整理/出处审核）与 promotion 独立只读审核 |

## 共享执行骨架

四类实验共用一条主链：Coordinator 分类与选角 → 前置角色准备 → Coordinator 冻结 Attempt → Runner 忠实执行 → Analyst 分析 Result → Reviewer 按风险介入 → Coordinator 写账本并运行 `validate` → 审核并按需更新 memory → 关闭临时 Agent。

- Coordinator 是唯一账本协调者。
- Implementer 只能在 Attempt 冻结前修改代码；冻结后若机制变化，重新创建 Trial/Attempt。
- Runner 只执行冻结事实，不边跑边改命令、配置或 seed。
- Analyst 不把实现失败解释成假设失败，也不用单次结果冒充稳定结论。
- Reviewer 默认独立只读，不能同时审核自己的实现结论。
- 没有新的、已验证的项目特定经验，就不更新角色 memory。

## 按实验类型选组合

| 类型 | 最小角色组合与升级条件 | 核心差异 | 重新分类条件 |
|---|---|---|---|
| 创新 | Coordinator + Idea Scientist + Implementer + Runner + Analyst + Reviewer | 新 Trial，通常修改结构代码；严格审核 | 不适用 |
| 调参 | Coordinator + Runner + Analyst；涉及 promotion 或异常时加 Reviewer | 结构不变，每组参数使用独立 Attempt | 改变结构、损失机制或评估语义时改为创新 |
| 消融 | Coordinator + Runner + Analyst + Reviewer；缺少开关时加 Implementer | 只关闭或替换一个成分，其他关键条件保持可比 | 需要重新设计结构时改为创新 |
| 复现/确认 | Coordinator + Runner + Analyst + Reviewer；需要代码适配时加 Implementer | 忠实复用已冻结的代码和条件 | 改 seed、配置或机制后不再是严格复现 |

这是选择表，不要求每次启动全部角色。简单调参不做沉重文档审核；改变创新代码、出处、评估语义或 promotion 时必须升级审核。

## 通信协议

临时 Agent 可以相互通信，传递对象 ID、只读文件路径、假设、diff、命令和机器结果。不要只说“看起来没问题”。每条交接至少包含：

1. 当前目标和角色边界；
2. 输入对象 ID 与读取的 commit/文件；
3. 做了什么或建议什么；
4. 原始验证命令、退出码和关键输出；
5. 未决问题、风险与不得声称的结论。

Coordinator 是唯一账本协调者：收集各角色输出，解决冲突，再调用 `rw.py`。更新 memory 时，先把审核后全文写入普通 UTF-8 Markdown（不超过 64 KiB），再执行 `update-role-memory --project ... --role ... --content-file ...`。其他角色不并发手改 JSON 或 memory，不自行 promotion，不擅自创建远端。若通信结论冲突，保留分歧并交 Reviewer；证据不足就记 `inconclusive`，不强行统一。

## 轻重审核

- **一轮**：文字、小范围测试维护、局部配置或其他小改动，而且不改变运行、数据、评估或验收含义。完成机器检查后，由一个独立 Reviewer 核对改动与结论。
- **两轮**：普通新模块、局部行为变化、消融、复现、常规缺陷修复，或不支撑重要结论、promotion 和论文主张的普通新结果。第一轮查实现和边界，修复后第二轮核对当前 diff 与证据。
- **三轮**：跨多个模块、改变模板或状态机、训练入口、数据划分、评估含义、重要实验结论、重要 promotion 或论文主张的大改动。第三轮只做收口，确认前两轮问题已经关闭且没有新问题。

主 Agent 在开始前说明选择一、二或三轮的理由；审查中发现影响扩大时再升级。优先使用不同模型，但不绑定任何厂商或工具。机器验证永远不能被 Agent 意见替代。

审核发现问题时，由原执行角色修复，重新跑机器验证，再请 Reviewer 复核。审核只读，除非用户明确授权角色切换。
