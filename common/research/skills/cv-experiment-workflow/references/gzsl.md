# GZSL 参考包

Generalized Zero-Shot Learning（广义零样本学习，GZSL）只是一个可选领域参考包，不是核心工作流。核心对象、模板选择、指标、阈值和 promotion 规则都不得依赖 GZSL；其他计算机视觉方向可以完全不加载本文件。

`assets/reference-packs/gzsl.json` 收录四篇起点资料：

1. Chao 等，*An Empirical Study and Analysis of Generalized Zero-Shot Learning for Object Recognition in the Wild*（2016）：用于理解任务设定与评估问题。
2. Xian 等，*Zero-Shot Learning — The Good, the Bad and the Ugly*（2017）：用于核对数据划分、协议与常见比较陷阱。
3. Xian 等，*Feature Generating Networks for Zero-Shot Learning*（2018）：用于定位生成式机制的论文来源。
4. Schonfeld 等，*Generalized Zero- and Few-Shot Learning via Aligned Variational Autoencoders*（2019）：用于定位对齐表示机制的论文来源。

参考包只提供论文元数据起点，不代表全文已读、claim 已核验，也不提供官方代码出处。使用步骤：

1. 仅在用户明确处理 GZSL Idea 时读取 JSON。
2. 选择与 Idea 机制直接相关的论文，打开原文核对题名、作者、年份、URL 和具体页码/章节/公式。
3. 在 provenance 中把直接机制论文标为 `primary_mechanism`，其余标为 `supporting`；只有实际核对后才写 `verification_status: verified`。
4. 论文不能证明代码来源。若复制、改编或参考官方实现，另行核对仓库、40 位 commit、文件、symbols 与 license。
5. 依据机制的接入位置选择通用结构模板；不要因为论文属于 GZSL 就硬选某个 family。

项目自己的数据字段、评估指标、确认次数和晋级阈值应写在目标项目适配层，不得写回 Skill 模板或此参考包。
