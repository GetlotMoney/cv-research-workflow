"use strict";

const API_ENDPOINT = "/api/state";
const WRITE_API_ENDPOINT = "/api/actions";
const SVG_NAMESPACE = ["http:", "", "www.w3.org", "2000", "svg"].join("/");

const VIEW_LABELS = {
  start: "开始与设置",
  relationships: "关系图",
  confirm: "执行确认",
};

const ACTION_EXAMPLES = {
  create_project: {
    directory_name: "my-cv-research",
    display_name: "我的 CV 科研项目",
  },
  open_project: {
    directory_name: "my-cv-research",
  },
  save_research_brief: {
    title: "请替换为本项目标题",
    research_area: "图像分类",
    background: "说明已有研究、真实应用背景和这项研究从哪里开始。",
    problem: "说明当前方法在什么条件下仍有具体问题。",
    objective: "说明本项目要验证什么，不提前填写尚未得到的实验结论。",
    research_questions: ["新方法是否在冻结的评估协议下优于基线？"],
    hypotheses: [
      {
        hypothesis_id: "HYP-0001",
        statement: "新方法应在首要指标上稳定优于基线。",
        falsification_criteria: "若重复实验未超过基线或优势不稳定，则假设不成立。",
      },
    ],
    scope: {
      included: ["本项目实际运行并封存的实验"],
      excluded: ["没有原始结果支撑的成绩句"],
    },
    terminology: [
      {
        term_en: "primary metric",
        term_zh: "首要指标",
        definition_zh: "在实验开始前确定、用于回答主要研究问题的指标。",
      },
    ],
    planned_contributions: [
      {
        contribution_id: "CON-0001",
        statement_zh: "请填写计划验证的项目贡献。",
        provenance: "original",
        source_refs: [],
      },
    ],
    user_confirmation: {
      confirmed: false,
      confirmed_at: "2026-07-27T12:00:00+08:00",
      confirmed_by: "请填写确认人，并在核对后把 confirmed 改为 true",
    },
  },
  create_domain_repo: { direction: "cls", name: "my-cls-project" },
  register_source: {
    source_path: "D:\\请替换\\reference.pdf",
    kind: "paper",
    identity: "请填写 DOI、论文标题或稳定来源编号",
    locator: "D:\\请替换\\reference.pdf",
    revision: "v1",
    commit: null,
    digest: `sha256:${"0".repeat(64)}`,
    license: "请填写来源许可",
  },
  save_idea: {
    status: "ready",
    problem: "请填写要解决的具体问题。",
    mechanism: "请填写准备验证的作用机制。",
    falsifiable_hypothesis: "请填写什么结果会证明这个想法不成立。",
    source_refs: ["SRC-0001"],
    evidence_refs: ["SRC-0001"],
    source_links: [
      {
        source_ref: "SRC-0001",
        locator: "page:请填写",
        supports_field: "problem",
        claim: "请说明这个来源怎样支持当前问题判断。",
      },
    ],
  },
  revise_idea: {
    idea_id: "IDEA-0001",
    revision_reason: "请填写为什么修改。",
    idea: {
      status: "ready",
      problem: "修订后的问题。",
      mechanism: "修订后的机制。",
      falsifiable_hypothesis: "修订后的可证伪假设。",
      source_refs: ["SRC-0001"],
      evidence_refs: ["SRC-0001"],
      source_links: [
        {
          source_ref: "SRC-0001",
          locator: "page:请填写",
          supports_field: "problem",
          claim: "请说明这个来源怎样支持修订后的问题判断。",
        },
      ],
    },
  },
  select_codebase: { codebase_id: "CB-0001" },
  create_task: {
    owner_request: "先验证方向模板和当前配置能否按预期运行",
    route: "tune",
    target_refs: ["PACK-CLS"],
    route_inputs: {
      config: { mode: "synthetic_smoke" },
      seed: 7,
      debug_required: true,
      primary_metric: "score",
      changes_code_behavior: false,
      code_binding: {
        codebase_id: "CB-0001",
        branch: "main",
        commit: "请替换为当前40位Git提交",
        tag: null,
      },
    },
    budget: { max_runs: 2 },
    stop_condition: { type: "max_runs", value: 2 },
  },
  run_task_debug: { task_id: "TASK-0001" },
  run_task_evidence: { task_id: "TASK-0001" },
  seal_run_outputs: { run_id: "RUN-0002" },
  confirm_run_evidence: {
    run_id: "RUN-0002",
    reason: "我已核对封存输出、指标含义和限制。",
    confirmed_by: "请填写确认人",
    acknowledge: false,
  },
  freeze_research_package: {
    brief_id: "BRIEF-0001",
    mode: "hybrid",
    selection: {
      schema: "cv-experiment-workflow.paper-package-selection.v1",
      asset_mode: "hybrid",
      paper_scope: {
        title_hint: "请填写论文标题提示",
        research_area: "图像分类",
        goal: "请填写这篇论文要回答的问题",
        included_claim_ids: ["CLM-0001"],
        excluded_topics: ["没有证据支持的结论"],
      },
      claims: [
        {
          claim_id: "CLM-0001",
          kind: "result",
          origin: "project",
          statement_zh: "请根据已确认 Run 的真实指标填写。",
          statement_en: null,
          maturity: "confirmed",
          run_refs: ["RUN-0002"],
          metric_refs: [{ run_id: "RUN-0002", metric_name: "score" }],
          source_refs: [],
          idea_refs: [],
          module_refs: [],
          innovation_boundary: null,
          allowed_sections: ["experiments"],
        },
      ],
      experiments: [
        {
          experiment_id: "EXP-0001",
          title: "主结果实验",
          objective: "回答首要研究问题",
          task_id: "TASK-0001",
          run_refs: ["RUN-0002"],
          claim_refs: ["CLM-0001"],
        },
      ],
      source_uses: [],
      assets: [
        {
          asset_id: "AST-0001",
          source_object_ref: "RUN-0002",
          path: ".cv-workflow-output/run.log",
          role: "raw_log",
          required_for_writing: true,
          copy_allowed: true,
          license: "project-owned",
          privacy_classification: "internal",
          availability: "available",
          omission_reason: "",
        },
        {
          asset_id: "AST-0002",
          source_object_ref: "RUN-0002",
          path: ".cv-workflow-output/metrics.json",
          role: "result_data",
          required_for_writing: true,
          copy_allowed: true,
          license: "project-owned",
          privacy_classification: "internal",
          availability: "available",
          omission_reason: "",
        },
      ],
      visuals: [],
      supersedes_package_id: null,
    },
  },
};

const INTENT_LABELS = {
  status: "状态查看",
  continue: "继续未完成任务",
  tune: "参数调整",
  ablation: "消融实验",
  reproduction: "复现实验",
  innovation: "创新验证",
};

const ROUTE_HELP = {
  status: "只读取项目、版本、代码适配和实验账本，不需要补充信息。",
  continue: "从账本里选择一个尚未完成的任务；只有一个时会自动选中。",
  tune: "在现有模板或已接受版本上改参数，不默认改变代码行为。",
  ablation: "关闭一个模块并与同一基线比较，用来回答这个模块是否真的有效。",
  reproduction: "按明确的代码、数据和误差标准重做一个已有结果。",
  innovation: "组合想法、模板和模块，验证一个会改变代码行为的新方案。",
};

const FIELD_LABELS = {
  "domain_task.domain": "研究领域",
  "domain_task.task": "具体任务",
  "dataset.source": "数据来源",
  "dataset.split": "数据划分",
  research_goal: "研究目标",
  "base_candidate.kind": "起点类型",
  "base_candidate.reference": "模板或版本编号",
  primary_metric: "首要指标",
  "compute_budget.max_runs": "最多运行次数",
  "compute_budget.max_hours": "最多小时数",
  "compute_budget.max_gpus": "最多 GPU 数",
  stop_condition: "停止条件",
  route_details: "路线细节",
  "route_details.config": "参数配置",
  "route_details.allowed_changes": "允许改动",
  "route_details.baseline": "基线结果",
  "route_details.baseline_run_ref": "基线 Run",
  "route_details.module_ref": "模块编号",
  "route_details.disabled_behavior": "关闭模块后的行为",
  "route_details.source_ref": "来源编号",
  "route_details.source_run_ref": "来源 Run",
  "route_details.tolerance": "允许误差",
  "route_details.code_standard": "代码复现标准",
  "route_details.data_standard": "数据复现标准",
  "route_details.idea_refs": "想法编号",
  "route_details.template_refs": "模板编号",
  "route_details.module_refs": "模块编号",
  unfinished_task: "一个未完成任务",
  task_ref: "要继续的任务",
};

const KIND_LABELS = {
  adapter_repo: "代码仓库",
  project_skill: "项目工作流入口",
  workflow: "通用工作流",
  ledger: "实验账本",
  source: "资料来源",
  idea: "研究想法",
  template: "代码模板",
  module: "实验模块",
  task: "实验任务",
  run: "实验运行",
  evidence: "实验依据",
};

const EDGE_LABELS = {
  adapter_repo_supplies_project: "代码仓库提供项目代码",
  project_uses_workflow: "项目使用工作流",
  workflow_manages_ledger: "工作流管理实验账本",
  source_supports_idea: "资料支持研究想法",
  source_supports_template: "资料支持代码模板",
  source_supports_module: "资料支持实验模块",
  idea_defines_module: "研究想法定义实验模块",
  template_hosts_module: "代码模板承载实验模块",
  catalog_ref_feeds_task: "登记对象作为任务输入",
  task_prerequisite: "前置任务",
  prior_run_feeds_task: "已有运行作为任务输入",
  task_produces_run: "任务产生实验运行",
  run_has_evidence: "运行产生实验依据",
  run_supports_evidence: "运行支持实验依据",
};

const CONDITION_LABELS = {
  project_snapshot: "项目账本可读取",
  workflow_lock: "工作流版本可信",
  intake_complete: "启动信息完整",
  adapter_config: "代码适配配置有效",
  adapter_runtime: "代码仓库现场可用",
  controlled_actions: "固定动作入口可用",
};

const WORKFLOW_TRUST_LABELS = {
  current: "当前版本可信",
  trusted_previous: "已识别旧版本，可安全查看",
};

const ADAPTER_STATUS_LABELS = {
  bound: "已绑定代码仓库",
  unbound: "尚未绑定代码仓库",
};

const RUNTIME_STATUS_LABELS = {
  pass: "代码现场已核对",
  block: "代码现场未通过",
  not_checked: "代码现场尚未检查",
};

const CONDITION_STATUS_LABELS = {
  pass: "通过",
  block: "未通过",
  not_checked: "未检查",
};

const NODE_STATUS_LABELS = {
  bound: "已绑定",
  validated: "已验证",
  registered: "已登记",
  active: "进行中",
  queued: "待运行",
  running: "运行中",
  closed: "已结束",
  done: "已完成",
  succeeded: "成功",
  failed: "失败",
  accepted: "已接受",
  candidate: "候选中",
  planned: "已计划",
  paper: "论文来源",
};

const REASON_LABELS = {
  validated_v2_snapshot: "项目账本结构有效",
  trusted_previous: "已识别的旧版锁，可安全查看",
  current: "当前版本锁可信",
  complete: "必需信息已齐",
  missing_required_inputs: "还有必需信息没有填写",
  validated_adapter_json: "代码适配配置有效",
  verified_runtime_binding: "代码路径与提交已核对",
  adapter_unbound: "还没有绑定代码仓库",
  registered_codebase_available: "已登记 Codebase，运行时再核对 Git",
  capability_missing: "缺少这条路线需要的能力",
  code_source_count_invalid: "绑定的代码来源数量不符合规则",
  worktree_dirty: "代码仓库有尚未提交的改动",
  ignored_executable_code: "发现被 Git 忽略的可执行代码",
  runtime_verification_failed: "代码现场检查没有通过",
  controlled_actions_available: "只能执行页面列出的本机固定动作",
  invalid_project_snapshot: "项目账本损坏或不完整",
  project_snapshot_unavailable: "项目账本不可用，暂不检查",
};

const NEXT_STEP_LABELS = {
  show_status: "查看当前状态",
  choose_experiment_route: "选择一种实验路线",
  choose_task: "选择要继续的任务",
  provide_missing_inputs: "补齐上面标红的信息",
  review_controlled_actions: "核对条件后回到固定动作区",
  repair_project_snapshot: "先修复项目账本",
  repair_adapter_runtime: "先整理代码仓库现场",
  use_controlled_actions: "使用开始页的固定动作继续",
};

const SHARED_FIELDS = [
  {
    group: "实验要回答什么",
    path: "domain_task.domain",
    label: "研究领域",
    placeholder: "例如：图像分类",
    required: true,
  },
  {
    path: "domain_task.task",
    label: "具体任务",
    placeholder: "例如：细粒度分类",
    required: true,
  },
  {
    path: "dataset.source",
    label: "数据来源",
    placeholder: "数据集名称、路径或登记编号",
    required: true,
  },
  {
    path: "dataset.split",
    label: "数据划分",
    placeholder: "例如：官方 train/val/test",
    required: true,
  },
  {
    path: "research_goal",
    label: "研究目标",
    placeholder: "一句话说明要验证的问题",
    type: "textarea",
    wide: true,
    required: true,
  },
  {
    group: "从哪里开始",
    path: "base_candidate.kind",
    label: "起点类型",
    type: "select",
    choices: [
      ["template", "内置或登记模板"],
      ["accepted_version", "已接受版本"],
    ],
    defaultValue: "template",
    required: true,
  },
  {
    path: "base_candidate.reference",
    label: "模板或版本编号",
    placeholder: "例如：TPL-0001 或 accepted-v2",
    required: true,
  },
  {
    path: "primary_metric",
    label: "首要指标",
    placeholder: "例如：Top-1 accuracy",
    required: true,
  },
  {
    group: "预算和停止线",
    path: "compute_budget.max_runs",
    label: "最多运行次数",
    type: "integer",
    min: "1",
    defaultValue: "3",
    required: true,
  },
  {
    path: "compute_budget.max_hours",
    label: "最多小时数",
    type: "number",
    min: "0.1",
    step: "0.1",
    defaultValue: "2",
    required: true,
  },
  {
    path: "compute_budget.max_gpus",
    label: "最多 GPU 数",
    type: "integer",
    min: "0",
    defaultValue: "1",
    required: true,
  },
  {
    path: "stop_condition",
    label: "停止条件（JSON）",
    type: "json",
    defaultValue: '{"type":"max_runs","value":3}',
    help: "必须是非空 JSON 对象。",
    wide: true,
    required: true,
  },
];

const ROUTE_FIELDS = {
  tune: [
    {
      group: "调参范围（可选）",
      path: "route_details.config",
      label: "参数配置（JSON）",
      type: "json",
      defaultValue: "{}",
      wide: true,
    },
    {
      path: "route_details.allowed_changes",
      label: "允许改动（JSON）",
      type: "json",
      placeholder: '例如：["learning_rate","batch_size"]',
      wide: true,
    },
    {
      path: "route_details.baseline",
      label: "已有基线结果",
      type: "number",
      step: "any",
    },
    {
      path: "route_details.baseline_run_ref",
      label: "基线 Run",
      placeholder: "例如：RUN-0001",
    },
  ],
  ablation: [
    {
      group: "消融对照",
      path: "route_details.baseline",
      label: "基线结果",
      type: "number",
      step: "any",
      required: true,
    },
    {
      path: "route_details.module_ref",
      label: "要关闭的模块",
      placeholder: "例如：MOD-0001",
      required: true,
    },
    {
      path: "route_details.baseline_run_ref",
      label: "基线 Run",
      placeholder: "例如：RUN-0001",
      required: true,
    },
    {
      path: "route_details.disabled_behavior",
      label: "关闭后的行为",
      placeholder: "说明替代路径或恒等行为",
      wide: true,
      required: true,
    },
  ],
  reproduction: [
    {
      group: "复现依据",
      path: "route_details.baseline",
      label: "论文或原结果",
      type: "number",
      step: "any",
      required: true,
    },
    {
      path: "route_details.source_ref",
      label: "来源编号",
      placeholder: "例如：SRC-0001",
      required: true,
    },
    {
      path: "route_details.source_run_ref",
      label: "来源 Run",
      placeholder: "例如：RUN-0001",
      required: true,
    },
    {
      path: "route_details.tolerance",
      label: "允许误差",
      type: "number",
      min: "0",
      step: "any",
      defaultValue: "0.01",
      required: true,
    },
    {
      path: "route_details.code_standard",
      label: "代码复现标准（JSON）",
      type: "json",
      defaultValue: '{"commit":"exact"}',
      wide: true,
      required: true,
    },
    {
      path: "route_details.data_standard",
      label: "数据复现标准（JSON）",
      type: "json",
      defaultValue: '{"split":"exact"}',
      wide: true,
      required: true,
    },
  ],
  innovation: [
    {
      group: "创新组合",
      path: "route_details.baseline",
      label: "基线结果",
      type: "number",
      step: "any",
      required: true,
    },
    {
      path: "route_details.idea_refs",
      label: "想法编号",
      type: "csv",
      placeholder: "例如：IDEA-0001, IDEA-0002",
      required: true,
    },
    {
      path: "route_details.template_refs",
      label: "模板编号",
      type: "csv",
      placeholder: "例如：TPL-0001",
      required: true,
    },
    {
      path: "route_details.module_refs",
      label: "模块编号",
      type: "csv",
      placeholder: "例如：MOD-0001",
      required: true,
    },
  ],
};

const GRAPH_LANES = [
  {
    key: "system",
    label: "系统与代码",
    kinds: ["adapter_repo", "project_skill", "workflow", "ledger"],
  },
  {
    key: "research",
    label: "研究素材与设计",
    kinds: ["source", "idea", "template", "module"],
  },
  { key: "task", label: "实验任务", kinds: ["task"] },
  { key: "run", label: "实验运行", kinds: ["run"] },
  { key: "evidence", label: "实验依据", kinds: ["evidence"] },
];

let currentState = null;
let currentIntent = "status";
let writeToken = "";
const routeDrafts = new Map();
const actionContext = {};

const element = (identifier) => document.getElementById(identifier);

function clearChildren(target) {
  while (target.firstChild) {
    target.removeChild(target.firstChild);
  }
}

function setText(identifier, value, fallback = "—") {
  const target = element(identifier);
  target.textContent =
    value === null || value === undefined || value === "" ? fallback : String(value);
}

function makeElement(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function makeSvgElement(tagName, attributes = {}) {
  const node = document.createElementNS(SVG_NAMESPACE, tagName);
  Object.entries(attributes).forEach(([name, value]) => {
    node.setAttribute(name, String(value));
  });
  return node;
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function humanWorkflowTrust(value) {
  return WORKFLOW_TRUST_LABELS[value] || "未识别版本状态";
}

function humanAdapterState(adapter) {
  if (!adapter || typeof adapter !== "object") {
    return "代码现场状态未知";
  }
  const reason = adapter.runtime?.reason;
  if (reason) {
    return REASON_LABELS[reason] || "代码现场状态未知";
  }
  const runtimeStatus = adapter.runtime?.status;
  if (RUNTIME_STATUS_LABELS[runtimeStatus]) {
    return RUNTIME_STATUS_LABELS[runtimeStatus];
  }
  return ADAPTER_STATUS_LABELS[adapter.status] || "代码现场状态未知";
}

function humanConditionStatus(value) {
  return CONDITION_STATUS_LABELS[value] || "未识别检查状态";
}

function humanConditionReason(value) {
  return REASON_LABELS[value] || "未识别检查原因";
}

function humanGraphStatus(node) {
  const value = node?.status;
  if (node?.kind === "workflow") {
    return humanWorkflowTrust(value);
  }
  if (node?.kind === "adapter_repo") {
    return (
      REASON_LABELS[value] ||
      RUNTIME_STATUS_LABELS[value] ||
      "代码现场状态未知"
    );
  }
  return (
    NODE_STATUS_LABELS[value] ||
    REASON_LABELS[value] ||
    CONDITION_STATUS_LABELS[value] ||
    "状态已记录"
  );
}

function humanNodeKind(value) {
  return KIND_LABELS[value] || "未识别对象";
}

function humanEdgeKind(value) {
  return EDGE_LABELS[value] || "未识别关系";
}

function setBusy(isBusy, message) {
  element("app-root").setAttribute("aria-busy", String(isBusy));
  element("preview-action").disabled = isBusy;
  if (message) {
    element("page-status").textContent = message;
  }
}

function showError(message) {
  const target = element("page-error");
  target.textContent = message;
  target.hidden = false;
}

function clearError() {
  const target = element("page-error");
  target.textContent = "";
  target.hidden = true;
}

async function requestState(intent, provided) {
  setBusy(true, intent ? "正在检查这次启动信息……" : "正在读取本地项目状态……");
  clearError();
  try {
    const response = intent
      ? await fetch(API_ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ intent, provided }),
        })
      : await fetch(API_ENDPOINT);
    const returnedToken = response.headers.get("X-Console-Write-Token");
    if (returnedToken) {
      writeToken = returnedToken;
    }
    let payload;
    try {
      payload = await response.json();
    } catch (error) {
      throw new Error("本地服务返回了无法读取的数据。");
    }
    if (!response.ok) {
      const detail =
        payload?.error && typeof payload.error.message === "string"
          ? payload.error.message
          : `本地服务返回 ${response.status}。`;
      throw new Error(detail);
    }
    if (!payload || typeof payload !== "object") {
      throw new Error("项目状态格式不完整。");
    }
    const actionsEnabled =
      payload?.snapshot?.status === "valid" &&
      payload?.execution_enabled === true &&
      Boolean(writeToken);
    element("write-action-submit").disabled = !actionsEnabled;
    if (!actionsEnabled) {
      element("write-action-result").textContent =
        "当前项目账本无效，固定动作保持关闭；请先修复项目。";
    }
    currentState = payload;
    renderState(payload);
    element("page-status").textContent = intent
      ? "只读检查已更新；没有写入任何项目文件。"
      : "本地项目状态已读取。";
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "暂时无法读取本地项目状态。";
    showError(`${message} 请确认本地服务仍在运行后重试。`);
    element("page-status").textContent = "状态读取失败，页面保留上一次结果。";
    if (!currentState) {
      renderEmptyState();
    }
  } finally {
    setBusy(false);
  }
}

async function handleWriteAction(event) {
  event.preventDefault();
  clearError();
  const result = element("write-action-result");
  hidePaperFlowLink();
  let data;
  try {
    data = JSON.parse(element("write-action-data").value);
  } catch (error) {
    result.textContent = "结构化数据不是有效 JSON，对应动作没有执行。";
    return;
  }
  if (!data || typeof data !== "object" || Array.isArray(data) || !writeToken) {
    result.textContent = "缺少对象形式的数据或本次启动令牌；对应动作没有执行。";
    return;
  }
  const action = element("write-action").value;
  element("write-action-submit").disabled = true;
  try {
    const response = await fetch(WRITE_API_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Console-Write-Token": writeToken },
      body: JSON.stringify({ action, data }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload?.error?.message || `动作被服务端拒绝（${response.status}）。`);
    }
    rememberActionResult(action, payload?.result);
    const handoffPath = payload?.result?.handoff_path;
    result.textContent = payload.status === "disabled"
      ? `动作未执行：${payload.reason || "缺少生产能力"}`
      : payload.summary ||
        `动作已完成：${payload.status}${handoffPath ? `；交付包位于项目内 ${handoffPath}` : ""}`;
    showPaperFlowLink(payload?.paperflow);
    if (payload.state) {
      currentState = payload.state;
      renderState(payload.state);
    }
  } catch (error) {
    result.textContent = error instanceof Error ? error.message : "动作未执行。";
  } finally {
    element("write-action-submit").disabled = false;
  }
}

function loadActionExample() {
  const action = element("write-action").value;
  const example = actionExample(action);
  element("write-action-data").value = JSON.stringify(example, null, 2);
  hidePaperFlowLink();
  element("write-action-result").textContent =
    action === "confirm_run_evidence"
      ? "请先核对 Run，再填写确认人并把 acknowledge 改为 true。"
      : action === "save_research_brief"
        ? "这是可编辑样例；核对后填写确认人，并把 confirmed 改为 true。"
        : action === "register_source"
          ? "请让 locator 与 source_path 完全一致，并填入这个来源文件的真实 SHA-256。"
          : action === "create_task"
            ? "默认样例只跑 CPU 合成 debug，不能写论文成绩；真实正式 evidence 必须改成真实数据并使用 CUDA/GPU。"
        : "请把样例中的编号和内容替换成当前项目的真实信息。";
}

function actionExample(action) {
  const example = JSON.parse(JSON.stringify(ACTION_EXAMPLES[action] || {}));
  if (action === "select_codebase" && actionContext.codebaseId) {
    example.codebase_id = actionContext.codebaseId;
  }
  if (action === "create_task" && actionContext.codebaseBinding) {
    example.route_inputs.code_binding = { ...actionContext.codebaseBinding };
  }
  if (action === "create_task" && actionContext.templateId) {
    example.target_refs = [actionContext.templateId];
    const metricByDirection = {
      cls: "top1_accuracy",
      det: "bbox_iou",
      seg: "mIoU",
      instseg: "mask_iou",
      sr: "PSNR",
      gzsl: "H",
    };
    example.route_inputs.primary_metric =
      metricByDirection[actionContext.direction] || "score";
  }
  if (action === "create_task" && actionContext.ideaId) {
    example.target_refs = [actionContext.ideaId];
  }
  if (
    ["save_idea", "revise_idea"].includes(action) &&
    actionContext.sourceId
  ) {
    const idea = action === "revise_idea" ? example.idea : example;
    idea.source_refs = [actionContext.sourceId];
    idea.evidence_refs = [actionContext.sourceId];
    idea.source_links[0].source_ref = actionContext.sourceId;
  }
  if (
    ["run_task_debug", "run_task_evidence"].includes(action) &&
    actionContext.taskId
  ) {
    example.task_id = actionContext.taskId;
  }
  if (
    ["seal_run_outputs", "confirm_run_evidence"].includes(action) &&
    actionContext.runId
  ) {
    example.run_id = actionContext.runId;
  }
  if (action === "freeze_research_package") {
    if (actionContext.briefId) {
      example.brief_id = actionContext.briefId;
    }
    if (actionContext.taskId) {
      example.selection.experiments[0].task_id = actionContext.taskId;
    }
    if (actionContext.runId) {
      example.selection.claims[0].run_refs = [actionContext.runId];
      example.selection.claims[0].metric_refs[0].run_id = actionContext.runId;
      example.selection.experiments[0].run_refs = [actionContext.runId];
      example.selection.assets.forEach((asset) => {
        asset.source_object_ref = actionContext.runId;
      });
    }
    if (actionContext.ideaId) {
      example.selection.claims[0].idea_refs = [actionContext.ideaId];
    }
  }
  return example;
}

function rememberActionResult(action, result) {
  if (!result || typeof result !== "object") {
    return;
  }
  if (["create_project", "open_project"].includes(action)) {
    Object.keys(actionContext).forEach((key) => {
      delete actionContext[key];
    });
  }
  if (action === "save_research_brief" && typeof result.brief_id === "string") {
    actionContext.briefId = result.brief_id;
  }
  if (action === "create_domain_repo" && typeof result.codebase?.id === "string") {
    actionContext.codebaseId = result.codebase.id;
    actionContext.templateId = result.repository?.template_id;
    actionContext.direction = result.repository?.primary_direction;
  }
  if (action === "register_source" && typeof result.id === "string") {
    actionContext.sourceId = result.id;
  }
  if (
    ["save_idea", "revise_idea"].includes(action) &&
    typeof result.id === "string"
  ) {
    actionContext.ideaId = result.id;
  }
  if (action === "select_codebase" && typeof result.codebase_id === "string") {
    actionContext.codebaseId = result.codebase_id;
    actionContext.codebaseBinding = {
      codebase_id: result.codebase_id,
      branch: result.branch,
      commit: result.commit,
      tag: result.tag,
    };
  }
  if (action === "create_task" && typeof result.id === "string") {
    actionContext.taskId = result.id;
  }
  if (
    ["run_task_debug", "run_task_evidence"].includes(action) &&
    typeof result.run?.id === "string"
  ) {
    actionContext.runId = result.run.id;
  }
  if (
    ["seal_run_outputs", "confirm_run_evidence"].includes(action) &&
    typeof result.run_id === "string"
  ) {
    actionContext.runId = result.run_id;
  }
}

function validatedPaperFlowUrl(value) {
  if (typeof value !== "string") {
    return null;
  }
  const match = /^http:\/\/127\.0\.0\.1:([0-9]{1,5})\/#token=([A-Za-z0-9_-]{16,256})$/.exec(
    value,
  );
  if (!match || Number(match[1]) < 1 || Number(match[1]) > 65535) {
    return null;
  }
  return value;
}

function hidePaperFlowLink() {
  const link = element("paperflow-link");
  link.hidden = true;
  link.removeAttribute("href");
}

function showPaperFlowLink(paperflow) {
  const link = element("paperflow-link");
  const href =
    paperflow?.status === "ready"
      ? validatedPaperFlowUrl(paperflow.entrypoint)
      : null;
  if (!href) {
    hidePaperFlowLink();
    return;
  }
  link.href = href;
  link.hidden = false;
}

function normalizeHash() {
  const requested = window.location.hash.slice(1);
  return Object.hasOwn(VIEW_LABELS, requested) ? requested : "start";
}

function activateView() {
  const active = normalizeHash();
  document.querySelectorAll("[data-view='view']").forEach((section) => {
    section.hidden = section.id !== active;
  });
  document.querySelectorAll("[data-nav='view']").forEach((link) => {
    const selected = link.getAttribute("href") === `#${active}`;
    if (selected) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });
  document.title = `科研控制台 · ${VIEW_LABELS[active]}`;
  if (!window.location.hash || !Object.hasOwn(VIEW_LABELS, window.location.hash.slice(1))) {
    window.history.replaceState(null, "", "#start");
  }
}

function captureDraft(intent) {
  const values = {};
  element("provided-fields")
    .querySelectorAll("[data-path]")
    .forEach((control) => {
      values[control.dataset.path] = control.value;
    });
  routeDrafts.set(intent, values);
}

function createGroupTitle(text) {
  return makeElement("div", "field-group-title", text);
}

function createField(definition, draft) {
  const wrapper = makeElement(
    "div",
    `field${definition.wide ? " field-wide" : ""}`,
  );
  const identifier = `field-${definition.path.replaceAll(".", "-")}`;
  const label = document.createElement("label");
  label.htmlFor = identifier;
  label.append(document.createTextNode(definition.label));
  if (definition.required) {
    label.append(makeElement("span", "", "必填"));
  }

  let control;
  if (definition.type === "textarea" || definition.type === "json") {
    control = document.createElement("textarea");
    control.rows = definition.type === "json" ? 3 : 4;
  } else if (definition.type === "select") {
    control = document.createElement("select");
    safeArray(definition.choices).forEach(([value, text]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      control.append(option);
    });
  } else {
    control = document.createElement("input");
    control.type =
      definition.type === "number" || definition.type === "integer"
        ? "number"
        : "text";
  }

  control.id = identifier;
  control.name = definition.path;
  control.dataset.path = definition.path;
  control.dataset.valueType = definition.type || "text";
  if (definition.required) {
    control.setAttribute("aria-required", "true");
  }
  if (definition.placeholder) {
    control.placeholder = definition.placeholder;
  }
  if (definition.min !== undefined) {
    control.min = definition.min;
  }
  if (definition.step !== undefined) {
    control.step = definition.step;
  }
  const savedValue = draft[definition.path];
  control.value =
    savedValue !== undefined ? savedValue : definition.defaultValue || "";
  control.addEventListener("input", () => {
    control.removeAttribute("aria-invalid");
  });

  wrapper.append(label, control);
  if (definition.help) {
    const help = makeElement("p", "field-help", definition.help);
    help.id = `${identifier}-help`;
    control.setAttribute("aria-describedby", help.id);
    wrapper.append(help);
  }
  return wrapper;
}

function unfinishedTasks() {
  const intake = currentState?.start?.intake;
  return safeArray(intake?.discovered?.unfinished_tasks);
}

function renderContinueField(container, draft) {
  container.append(createGroupTitle("选择未完成任务"));
  const wrapper = makeElement("div", "field field-wide");
  const label = document.createElement("label");
  label.htmlFor = "field-task-ref";
  label.append(document.createTextNode("要继续的任务"));
  const select = document.createElement("select");
  select.id = "field-task-ref";
  select.name = "task_ref";
  select.dataset.path = "task_ref";
  select.dataset.valueType = "text";

  const tasks = unfinishedTasks();
  if (tasks.length > 1) {
    select.setAttribute("aria-required", "true");
  }
  if (tasks.length !== 1) {
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = tasks.length ? "请选择一个任务" : "没有未完成任务";
    select.append(placeholder);
  }
  tasks.forEach((task) => {
    const option = document.createElement("option");
    option.value = String(task.id);
    const remaining =
      task.remaining_runs === null || task.remaining_runs === undefined
        ? "预算未知"
        : `还可运行 ${task.remaining_runs} 次`;
    option.textContent = `${task.id} · ${task.route || "未知路线"} · ${remaining}`;
    select.append(option);
  });
  select.disabled = tasks.length === 0;
  select.value =
    draft.task_ref !== undefined
      ? draft.task_ref
      : tasks.length === 1
        ? String(tasks[0].id)
        : "";
  wrapper.append(label, select);
  container.append(wrapper);
}

function renderProvidedFields(intent) {
  const container = element("provided-fields");
  clearChildren(container);
  element("route-help").textContent = ROUTE_HELP[intent] || ROUTE_HELP.status;
  const draft = routeDrafts.get(intent) || {};

  if (intent === "status") {
    const note = makeElement(
      "p",
      "micro-note field-wide",
      "这条路线没有表单：点击检查，只会重新读取当前状态。",
    );
    container.append(note);
    return;
  }
  if (intent === "continue") {
    renderContinueField(container, draft);
    return;
  }

  [...SHARED_FIELDS, ...safeArray(ROUTE_FIELDS[intent])].forEach((definition) => {
    if (definition.group) {
      container.append(createGroupTitle(definition.group));
    }
    container.append(createField(definition, draft));
  });
}

function parseFieldValue(control) {
  const raw = control.value.trim();
  if (!raw) {
    return undefined;
  }
  switch (control.dataset.valueType) {
    case "integer": {
      const value = Number(raw);
      if (!Number.isInteger(value)) {
        throw new Error(`${FIELD_LABELS[control.dataset.path] || control.name} 必须是整数。`);
      }
      return value;
    }
    case "number": {
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        throw new Error(`${FIELD_LABELS[control.dataset.path] || control.name} 必须是有效数字。`);
      }
      return value;
    }
    case "json":
      try {
        return JSON.parse(raw);
      } catch (error) {
        throw new Error(`${FIELD_LABELS[control.dataset.path] || control.name} 不是有效 JSON。`);
      }
    case "csv":
      return raw
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean);
    default:
      return raw;
  }
}

function setNestedValue(target, path, value) {
  const parts = path.split(".");
  let cursor = target;
  parts.forEach((part, index) => {
    if (index === parts.length - 1) {
      cursor[part] = value;
      return;
    }
    if (!cursor[part] || typeof cursor[part] !== "object") {
      cursor[part] = {};
    }
    cursor = cursor[part];
  });
}

function collectProvided(intent) {
  if (intent === "status") {
    return {};
  }
  const provided = {};
  if (!["continue"].includes(intent)) {
    provided.route_details = {};
  }
  const controls = element("provided-fields").querySelectorAll("[data-path]");
  for (const control of controls) {
    control.removeAttribute("aria-invalid");
    let value;
    try {
      value = parseFieldValue(control);
    } catch (error) {
      control.setAttribute("aria-invalid", "true");
      control.focus();
      throw error;
    }
    if (value !== undefined) {
      setNestedValue(provided, control.dataset.path, value);
    }
  }
  return provided;
}

function renderState(state) {
  renderStart(state);
  renderGraph(state);
  renderExecution(state);
}

function renderStart(state) {
  const project = state?.start?.project;
  const workflow = state?.start?.workflow;
  const adapter = state?.start?.adapter;
  const counts = state?.snapshot?.counts || {};
  setText("project-name", project?.name, "项目不可用");
  setText(
    "workflow-version",
    workflow
      ? `${workflow.release_version || "未知"} / ${workflow.system_version || "未知"}`
      : null,
    "版本不可用",
  );
  setText(
    "workflow-trust",
    workflow ? humanWorkflowTrust(workflow?.trust) : null,
    "未检查",
  );
  setText("adapter-status", humanAdapterState(adapter), "代码现场状态未知");
  setText("count-sources", counts.sources, "0");
  setText("count-tasks", counts.tasks, "0");
  setText("count-runs", counts.runs, "0");
  setText("count-evidence", counts.evidence, "0");
  setText(
    "project-instances",
    project?.name
      ? `当前：${project.name}。打开其他项目时，请填写同一实例根中的文件夹名。`
      : null,
    "项目不可用",
  );
  renderIntake(state?.start?.intake);
}

function renderIntake(intake) {
  const requiredContainer = element("required-fields");
  clearChildren(requiredContainer);
  const summary = element("intake-summary");
  summary.classList.remove("is-ready");

  if (!intake) {
    requiredContainer.append(
      makeElement("p", "micro-note", "项目账本不可用，暂时无法判断必需信息。"),
    );
    summary.textContent = "请先修复项目账本，再回来检查启动信息。";
    return;
  }

  const required = safeArray(intake.required);
  const missing = new Set(safeArray(intake.missing));
  if (required.length === 0) {
    requiredContainer.append(
      makeElement("p", "micro-note", "这条路线不需要额外提供信息。"),
    );
  } else {
    const list = makeElement("ul", "chip-list");
    required.forEach((path) => {
      const item = makeElement(
        "li",
        missing.has(path) ? "is-missing" : "",
        FIELD_LABELS[path] || path,
      );
      list.append(item);
    });
    requiredContainer.append(list);
  }

  element("provided-fields")
    .querySelectorAll("[data-path]")
    .forEach((control) => {
      control.setAttribute(
        "aria-invalid",
        String(missing.has(control.dataset.path)),
      );
    });

  const heading = makeElement(
    "strong",
    "",
    intake.can_continue ? "信息已经够用" : `还差 ${missing.size} 项`,
  );
  const next = makeElement(
    "span",
    "",
    NEXT_STEP_LABELS[intake.next_step] || intake.next_step || "等待下一步",
  );
  clearChildren(summary);
  summary.append(heading, next);
  if (intake.can_continue) {
    summary.classList.add("is-ready");
  }
}

function graphData(state) {
  const relationships = state?.relationships;
  const graph = relationships?.graph || relationships || {};
  return {
    nodes: safeArray(graph.nodes),
    edges: safeArray(graph.edges),
  };
}

function laneForNode(node) {
  return (
    GRAPH_LANES.find((lane) => lane.kinds.includes(node.kind)) ||
    GRAPH_LANES[0]
  );
}

function shortLabel(value, maximum = 23) {
  const text = String(value || "未命名");
  return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
}

function renderGraph(state) {
  const { nodes, edges } = graphData(state);
  setText("graph-node-count", nodes.length, "0");
  setText("graph-edge-count", edges.length, "0");
  const canvas = element("graph-canvas");
  const empty = element("graph-empty");
  const list = element("graph-list");
  const edgeList = element("graph-edge-list");
  clearChildren(canvas);
  clearChildren(list);
  clearChildren(edgeList);

  edges.forEach((edge) => {
    const item = document.createElement("li");
    item.append(
      makeElement("strong", "", `${edge.from} → ${edge.to}`),
      makeElement("span", "", `关系类型：${humanEdgeKind(edge.kind)}`),
    );
    edgeList.append(item);
  });

  if (nodes.length === 0) {
    canvas.hidden = true;
    empty.hidden = false;
    return;
  }
  canvas.hidden = false;
  empty.hidden = true;

  const lanes = GRAPH_LANES.map((lane) => ({
    ...lane,
    nodes: nodes.filter((node) => laneForNode(node).key === lane.key),
  }));
  const laneWidth = 202;
  const laneGap = 14;
  const pagePadding = 24;
  const nodeWidth = 166;
  const nodeHeight = 56;
  const nodeGap = 22;
  const maximumRows = Math.max(1, ...lanes.map((lane) => lane.nodes.length));
  const width =
    pagePadding * 2 + lanes.length * laneWidth + (lanes.length - 1) * laneGap;
  const height = Math.max(500, 82 + maximumRows * (nodeHeight + nodeGap));
  canvas.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const definitions = makeSvgElement("defs");
  const marker = makeSvgElement("marker", {
    id: "graph-arrow",
    markerWidth: 7,
    markerHeight: 7,
    refX: 6,
    refY: 3.5,
    orient: "auto",
  });
  marker.append(
    makeSvgElement("path", {
      d: "M 0 0 L 7 3.5 L 0 7 z",
      fill: "#7c8791",
    }),
  );
  definitions.append(marker);
  canvas.append(definitions);

  const positions = new Map();
  lanes.forEach((lane, laneIndex) => {
    const x = pagePadding + laneIndex * (laneWidth + laneGap);
    const laneGroup = makeSvgElement("g");
    laneGroup.append(
      makeSvgElement("rect", {
        class: "graph-lane",
        x,
        y: 18,
        width: laneWidth,
        height: height - 36,
      }),
    );
    const laneLabel = makeSvgElement("text", {
      class: "graph-lane-label",
      x: x + 13,
      y: 42,
    });
    laneLabel.textContent = lane.label;
    laneGroup.append(laneLabel);
    canvas.append(laneGroup);

    lane.nodes.forEach((node, rowIndex) => {
      positions.set(String(node.id), {
        x: x + (laneWidth - nodeWidth) / 2,
        y: 61 + rowIndex * (nodeHeight + nodeGap),
      });
    });
  });

  const edgeLayer = makeSvgElement("g", { "aria-hidden": "true" });
  edges.forEach((edge) => {
    const source = positions.get(String(edge.from));
    const target = positions.get(String(edge.to));
    if (!source || !target) {
      return;
    }
    const startX = source.x + nodeWidth;
    const startY = source.y + nodeHeight / 2;
    const endX = target.x;
    const endY = target.y + nodeHeight / 2;
    const bend = Math.max(28, Math.abs(endX - startX) * 0.46);
    const direction = endX >= startX ? 1 : -1;
    const path = makeSvgElement("path", {
      class: "graph-edge",
      d: [
        `M ${startX} ${startY}`,
        `C ${startX + bend * direction} ${startY}`,
        `${endX - bend * direction} ${endY}`,
        `${endX} ${endY}`,
      ].join(" "),
      "marker-end": "url(#graph-arrow)",
    });
    const title = makeSvgElement("title");
    title.textContent = `${edge.from} → ${edge.to}：${humanEdgeKind(edge.kind)}`;
    path.append(title);
    edgeLayer.append(path);
  });
  canvas.append(edgeLayer);

  const nodeLayer = makeSvgElement("g");
  nodes.forEach((node) => {
    const position = positions.get(String(node.id));
    if (!position) {
      return;
    }
    const humanStatus = humanGraphStatus(node);
    const safeKind = String(node.kind || "unknown").replace(/[^a-z0-9_-]/gi, "");
    const group = makeSvgElement("g", {
      class: `graph-node kind-${safeKind}`,
      transform: `translate(${position.x} ${position.y})`,
      tabindex: "0",
      role: "group",
      "aria-label": `${humanNodeKind(node.kind)} ${node.label || node.id}，状态 ${humanStatus}`,
    });
    const title = makeSvgElement("title");
    title.textContent = `${node.id} · ${node.label || ""} · ${humanStatus}`;
    const rectangle = makeSvgElement("rect", {
      x: 0,
      y: 0,
      width: nodeWidth,
      height: nodeHeight,
    });
    const kind = makeSvgElement("text", {
      class: "node-kind",
      x: 10,
      y: 13,
    });
    kind.textContent = humanNodeKind(node.kind);
    const label = makeSvgElement("text", {
      class: "node-label",
      x: 10,
      y: 31,
    });
    label.textContent = shortLabel(node.label || node.id);
    const status = makeSvgElement("text", {
      class: "node-status",
      x: 10,
      y: 47,
    });
    status.textContent = shortLabel(humanStatus, 28);
    group.append(title, rectangle, kind, label, status);
    nodeLayer.append(group);
  });
  canvas.append(nodeLayer);

  const incoming = new Map();
  const outgoing = new Map();
  edges.forEach((edge) => {
    incoming.set(String(edge.to), (incoming.get(String(edge.to)) || 0) + 1);
    outgoing.set(String(edge.from), (outgoing.get(String(edge.from)) || 0) + 1);
  });
  nodes.forEach((node) => {
    const humanStatus = humanGraphStatus(node);
    const item = document.createElement("li");
    item.append(
      makeElement("strong", "", `${node.id} · ${node.label || "未命名"}`),
      makeElement(
        "span",
        "",
        `${humanNodeKind(node.kind)} / ${humanStatus} / 入 ${incoming.get(String(node.id)) || 0} · 出 ${outgoing.get(String(node.id)) || 0}`,
      ),
    );
    list.append(item);
  });
}

function renderExecution(state) {
  const execution = state?.execution || {};
  const conditions = safeArray(execution.conditions);
  const list = element("condition-list");
  clearChildren(list);

  if (conditions.length === 0) {
    const item = document.createElement("li");
    item.append(makeElement("span", "condition-name", "项目状态不可用"));
    list.append(item);
  } else {
    conditions.forEach((condition) => {
      const item = document.createElement("li");
      const normalizedStatus = String(condition.status || "not_checked").replaceAll(
        "_",
        "-",
      );
      item.append(
        makeElement(
          "span",
          "condition-name",
          CONDITION_LABELS[condition.id] || condition.id,
        ),
        makeElement(
          "span",
          `condition-status status-${normalizedStatus}`,
          humanConditionStatus(condition.status),
        ),
        makeElement(
          "span",
          "condition-reason",
          humanConditionReason(condition.reason),
        ),
      );
      list.append(item);
    });
  }

  const blocked = safeArray(execution.blocked_by);
  const blockedSummary = element("blocked-summary");
  if (blocked.length) {
    blockedSummary.textContent = `仍有 ${blocked.length} 项挡住下一步：${blocked
      .map((identifier) => CONDITION_LABELS[identifier] || identifier)
      .join("、")}。`;
  } else {
    blockedSummary.textContent =
      "检查项没有业务阻塞；可以回到开始页选择一个固定动作继续。";
  }
  setText(
    "preview-intent",
    INTENT_LABELS[execution.intent] || execution.intent,
    "状态查看",
  );
  setText(
    "preview-next-step",
    NEXT_STEP_LABELS[execution.next_step] || execution.next_step,
    "等待下一步",
  );
}

function renderEmptyState() {
  renderStart({});
  renderGraph({});
  renderExecution({});
}

function handleIntentChange(event) {
  captureDraft(currentIntent);
  currentIntent = event.target.value;
  renderProvidedFields(currentIntent);
  if (currentState?.start?.intake?.intent !== currentIntent) {
    element("required-fields").replaceChildren(
      makeElement("p", "micro-note", "点击“检查必需信息”后显示准确结果。"),
    );
    element("intake-summary").textContent = "这里仅检查启动信息；真正写入请使用上方固定动作。";
    element("intake-summary").classList.remove("is-ready");
  }
}

async function handleIntakeSubmit(event) {
  event.preventDefault();
  clearError();
  captureDraft(currentIntent);
  try {
    const intent = currentIntent;
    const provided = collectProvided(intent);
    await requestState(intent, provided);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "表单内容无法读取。";
    showError(message);
    element("page-status").textContent = "请修正标出的表单内容。";
  }
}

function installInteractions() {
  window.addEventListener("hashchange", activateView);
  element("intent").addEventListener("change", handleIntentChange);
  element("write-action").addEventListener("change", loadActionExample);
  element("intake-form").addEventListener("submit", handleIntakeSubmit);
  element("write-action-form").addEventListener("submit", handleWriteAction);
  element("execution-action").addEventListener("click", () => {
    window.location.hash = "start";
    window.setTimeout(() => element("write-action").focus(), 0);
  });
}

async function startApplication() {
  activateView();
  installInteractions();
  currentIntent = element("intent").value;
  loadActionExample();
  renderProvidedFields(currentIntent);
  await requestState();
}

document.addEventListener("DOMContentLoaded", startApplication);
