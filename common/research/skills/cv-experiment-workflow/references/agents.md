# 当前 Agent 协作规则

多个 Agent 只用于能够独立调查、实现或复核的工作。它们共享同一个仓库事实，但不能同时修改同一批文件。

## 六个角色

| 角色 | 负责什么 |
|---|---|
| Coordinator | 确认目标、选择最小协作组合、汇总最终结果 |
| Idea Scientist | 把创新想法整理成可验证假设 |
| Implementer | 在指定 Worktree 内实现代码 |
| Runner | 检查配置、数据和 GPU 后执行 Run |
| Analyst | 比较指标、日志和失败模式 |
| Reviewer | 独立检查证据、边界和晋级条件 |

## 按实验选择角色

- 复现：Coordinator、Runner、Analyst、Reviewer。
- 调参：Coordinator、Runner、Analyst。
- 消融：Coordinator、Implementer、Runner、Analyst、Reviewer。
- 创新：Coordinator、Idea Scientist、Implementer、Runner、Analyst、Reviewer。

简单任务不为凑人数启动全部角色。

## 共享信息

Agent 之间只传递：

- 仓库和 Framework 身份；
- 实验编号与路线；
- 当前分支、提交和 Worktree；
- 数据清单身份；
- 配置与随机种子；
- Run 状态与可复核产物；
- 明确的阻塞条件。

不得用聊天结论替代仓库记录，也不得编造未执行的结果。

## 审核强度

- 低风险文档或提示修改：一轮。
- 会改变实验行为的修改：两轮。
- 跨模块、证据门禁或晋级规则修改：三轮。

机器测试不能被 Agent 审核替代。
