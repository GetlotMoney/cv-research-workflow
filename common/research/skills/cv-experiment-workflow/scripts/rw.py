from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, Sequence

from workflow_core.attempts import (
    new_attempt,
    promote,
    promotion_check,
    record_result,
    validate_project_workflow,
)
from workflow_core.coordinator import interpret_request
from workflow_core.codebases import register_codebase, verify_codebase_gate
from workflow_core.domain_packs import (
    DOMAIN_PACK_REGISTRY_SCHEMA,
    create_domain_repository,
    direction_availability,
    discover_domain_packs,
    resolve_domain_pack,
)
from workflow_core.engine import execute_task
from workflow_core.framework_workspace import (
    confirm_framework_run,
    create_framework_experiment,
    create_framework_idea,
    create_gzsl_repository,
    export_framework_paper_package,
    import_standardized_gzsl_framework,
    prepare_experiment_worktree,
    promote_innovation_framework,
    register_gzsl_dataset,
    run_framework_experiment,
    validate_framework_workspace,
)
from workflow_core.intake import check_intake
from workflow_core.output_seal import seal_run_outputs
from workflow_core.project import init_project
from workflow_core.project_skills import (
    initialize_project_skill,
    install_project_skill,
    rebind_project_skill,
)
from workflow_core.project_templates import register_template, render_template
from workflow_core.policy import set_confirmation_policy
from workflow_core.planning import plan_next, status, task_list
from workflow_core.paper_handoff import (
    export_paperflow,
    verification_failure_report,
    verify_paperflow_handoff,
)
from workflow_core.paper_package import (
    export_paper_package,
    save_research_brief,
    seal_paper_package,
    verify_paper_package,
)
from workflow_core.records import (
    activate_idea,
    activate_version,
    new_idea,
    register_version,
    revise_idea,
    set_implementation_mapping,
)
from workflow_core.releases import check_workflow_drift, upgrade_workflow_lock
from workflow_core.roles import ROLE_NAMES, update_role_memory
from workflow_core.templates import new_trial, sync_code_asset
from workflow_core.tasking import create_task
from workflow_core.ui_service import console_state
from workflow_core.v2_catalog import (
    register_module,
    register_source,
    revise_catalog_idea,
    save_idea,
)


class StrictArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = StrictArgumentParser(description="管理 CV 实验工作流")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=StrictArgumentParser,
    )

    init = commands.add_parser("init", help="初始化实验工作流项目")
    init.add_argument("--path", required=True, type=Path, help="项目根目录")
    init.add_argument("--name", required=True, help="项目名称")
    init.add_argument("--layout", choices=("v1", "v2"), default="v1")
    init.set_defaults(handler=_handle_init)

    init_skill = commands.add_parser(
        "init-project-skill",
        help="为已有项目初始化专属总协调 Skill",
    )
    init_skill.add_argument("--project", required=True, type=Path)
    init_skill.set_defaults(handler=_handle_init_project_skill)

    rebind_skill = commands.add_parser(
        "rebind-project-skill",
        help="项目移动后安全刷新专属 Skill 的路径绑定",
    )
    rebind_skill.add_argument("--project", required=True, type=Path)
    rebind_skill.set_defaults(handler=_handle_rebind_project_skill)

    install_skill = commands.add_parser(
        "install-project-skill",
        help="把项目专属 Skill 显式安装到 Codex Skills 目录",
    )
    install_skill.add_argument("--project", required=True, type=Path)
    install_skill.add_argument("--skills-root", required=True, type=Path)
    install_skill.set_defaults(handler=_handle_install_project_skill)

    idea = commands.add_parser("new-idea", help="创建草稿 Idea")
    idea.add_argument("--project", required=True, type=Path)
    idea.add_argument("--title", required=True)
    idea.add_argument("--note", required=True)
    idea.add_argument("--source-label")
    idea.add_argument("--source-locator")
    idea.add_argument("--source-note")
    idea.add_argument("--parent-idea")
    idea.add_argument("--related-idea", action="append", default=[])
    idea.set_defaults(handler=_handle_new_idea)

    activate = commands.add_parser("activate-idea", help="激活 Idea")
    activate.add_argument("--project", required=True, type=Path)
    activate.add_argument("--idea", required=True)
    activate.add_argument("--problem", required=True)
    activate.add_argument("--mechanism", required=True)
    activate.add_argument("--hypothesis", required=True)
    activate.set_defaults(handler=_handle_activate_idea)

    revise = commands.add_parser("revise-idea", help="修订 active Idea 科学字段")
    revise.add_argument("--project", required=True, type=Path)
    revise.add_argument("--idea", required=True)
    revise.add_argument("--reason", required=True)
    revise.add_argument("--problem")
    revise.add_argument("--mechanism")
    revise.add_argument("--hypothesis")
    revise.add_argument("--evidence", required=True, type=_json_array)
    revise.set_defaults(handler=_handle_revise_idea)

    mapping = commands.add_parser(
        "set-implementation-mapping", help="设置 Idea 到代码 attachment 的映射",
    )
    mapping.add_argument("--project", required=True, type=Path)
    mapping.add_argument("--idea", required=True)
    mapping.add_argument("--mapping", required=True, type=_json_object)
    mapping.set_defaults(handler=_handle_set_implementation_mapping)

    version = commands.add_parser("register-version", help="登记代码版本")
    version.add_argument("--project", required=True, type=Path)
    version.add_argument("--name", required=True)
    version.add_argument("--repo-url", required=True)
    version.add_argument("--commit", required=True)
    version.add_argument("--code-path", default=".")
    version.set_defaults(handler=_handle_register_version)

    register_template_command = commands.add_parser(
        "register-template", help="登记轻量项目模板元数据",
    )
    register_template_command.add_argument("--project", required=True, type=Path)
    register_template_command.add_argument("--manifest", required=True, type=Path)
    register_template_command.add_argument("--code-root", required=True, type=Path)
    register_template_command.set_defaults(handler=_handle_register_template)

    register_source_command = commands.add_parser(
        "register-source", help="为 v2 项目登记只读来源身份",
    )
    register_source_command.add_argument("--project", required=True, type=Path)
    register_source_command.add_argument("--manifest", required=True, type=Path)
    register_source_command.add_argument(
        "--source-path", required=True, type=Path,
    )
    register_source_command.set_defaults(handler=_handle_register_source)

    register_codebase_command = commands.add_parser(
        "register-codebase", help="为 v2 项目登记不可变 Codebase Git 身份",
    )
    register_codebase_command.add_argument("--project", required=True, type=Path)
    register_codebase_command.add_argument("--manifest", required=True, type=Path)
    register_codebase_command.add_argument(
        "--repo-root", required=True, type=Path,
    )
    register_codebase_command.set_defaults(handler=_handle_register_codebase)

    verify_codebase_command = commands.add_parser(
        "verify-codebase", help="只读复核 Codebase 当前 Git 身份",
    )
    verify_codebase_command.add_argument("--project", required=True, type=Path)
    verify_codebase_command.add_argument("--codebase", required=True)
    verify_codebase_command.add_argument(
        "--expected-git", type=_strict_json_object,
    )
    verify_codebase_command.set_defaults(handler=_handle_verify_codebase)

    list_domain_packs_command = commands.add_parser(
        "list-domain-packs",
        help="只读列出随 Skill 发布且已核验的方向仓库模板",
    )
    list_domain_packs_command.set_defaults(handler=_handle_list_domain_packs)

    create_domain_repo_command = commands.add_parser(
        "create-domain-repo",
        help="从内置方向包新建 Git 仓库并登记 Codebase",
    )
    create_domain_repo_command.add_argument(
        "--project",
        required=True,
        type=Path,
    )
    create_domain_repo_command.add_argument("--pack", required=True)
    create_domain_repo_command.add_argument(
        "--destination",
        required=True,
        type=Path,
    )
    create_domain_repo_command.add_argument("--name", required=True)
    create_domain_repo_command.set_defaults(handler=_handle_create_domain_repo)

    create_gzsl_repository_command = commands.add_parser(
        "create-gzsl-repository",
        help="创建 GPU-only GZSL 仓库和基础 Framework",
    )
    create_gzsl_repository_command.add_argument(
        "--destination", required=True, type=Path,
    )
    create_gzsl_repository_command.add_argument("--name", required=True)
    create_gzsl_repository_command.add_argument(
        "--environment", default="dvsr_gpu",
    )
    create_gzsl_repository_command.set_defaults(
        handler=_handle_create_gzsl_repository,
    )

    import_gzsl_framework_command = commands.add_parser(
        "import-standardized-gzsl-framework",
        help="把已标准化且等价验证通过的外来 GZSL 代码登记为顶层 Framework",
    )
    import_gzsl_framework_command.add_argument(
        "--repository", required=True, type=Path,
    )
    import_gzsl_framework_command.add_argument(
        "--source-repository", required=True, type=Path,
    )
    import_gzsl_framework_command.add_argument("--framework", required=True)
    import_gzsl_framework_command.add_argument("--title", required=True)
    import_gzsl_framework_command.add_argument("--version", required=True)
    import_gzsl_framework_command.add_argument(
        "--component-map", required=True, type=_strict_json_object,
    )
    import_gzsl_framework_command.add_argument(
        "--equivalence", required=True, type=_strict_json_object,
    )
    import_gzsl_framework_command.add_argument(
        "--i-trust-this-code",
        action="store_true",
        help="确认已人工审读，并允许外来代码在本机 GPU 环境执行",
    )
    import_gzsl_framework_command.set_defaults(
        handler=_handle_import_standardized_gzsl_framework,
    )

    create_framework_idea_command = commands.add_parser(
        "create-framework-idea",
        help="在仓库 Idea 树中创建可证伪 Idea",
    )
    create_framework_idea_command.add_argument(
        "--repository", required=True, type=Path,
    )
    create_framework_idea_command.add_argument("--title", required=True)
    create_framework_idea_command.add_argument("--problem", required=True)
    create_framework_idea_command.add_argument("--mechanism", required=True)
    create_framework_idea_command.add_argument("--hypothesis", required=True)
    create_framework_idea_command.add_argument(
        "--source-notes", required=True, type=_json_array,
    )
    create_framework_idea_command.add_argument("--parent-idea")
    create_framework_idea_command.set_defaults(
        handler=_handle_create_framework_idea,
    )

    create_framework_experiment_command = commands.add_parser(
        "create-framework-experiment",
        help="在 Framework 下创建复现、调参、消融或创新实验",
    )
    create_framework_experiment_command.add_argument(
        "--repository", required=True, type=Path,
    )
    create_framework_experiment_command.add_argument(
        "--framework", required=True,
    )
    create_framework_experiment_command.add_argument(
        "--route", required=True, choices=(
            "reproduction", "tuning", "ablation", "innovation",
        ),
    )
    create_framework_experiment_command.add_argument("--slug", required=True)
    create_framework_experiment_command.add_argument("--title", required=True)
    create_framework_experiment_command.add_argument("--summary", required=True)
    create_framework_experiment_command.add_argument("--idea")
    create_framework_experiment_command.add_argument(
        "--route-contract",
        required=True,
        type=_strict_json_object,
    )
    create_framework_experiment_command.set_defaults(
        handler=_handle_create_framework_experiment,
    )

    prepare_framework_worktree_command = commands.add_parser(
        "prepare-framework-worktree",
        help="为一个实验分支创建独立 Git Worktree",
    )
    prepare_framework_worktree_command.add_argument(
        "--repository", required=True, type=Path,
    )
    prepare_framework_worktree_command.add_argument(
        "--experiment", required=True,
    )
    prepare_framework_worktree_command.set_defaults(
        handler=_handle_prepare_framework_worktree,
    )

    register_gzsl_dataset_command = commands.add_parser(
        "register-gzsl-dataset",
        help="把小型 GZSL NPZ 固定到实验分支并生成 GPU 配置",
    )
    register_gzsl_dataset_command.add_argument(
        "--repository", required=True, type=Path,
    )
    register_gzsl_dataset_command.add_argument(
        "--experiment", required=True,
    )
    register_gzsl_dataset_command.add_argument(
        "--worktree", required=True, type=Path,
    )
    register_gzsl_dataset_command.add_argument(
        "--source", required=True, type=Path,
    )
    register_gzsl_dataset_command.add_argument("--slug", required=True)
    register_gzsl_dataset_command.add_argument("--dataset-id", required=True)
    register_gzsl_dataset_command.add_argument("--version", required=True)
    register_gzsl_dataset_command.add_argument("--source-uri", required=True)
    register_gzsl_dataset_command.add_argument("--license", required=True)
    register_gzsl_dataset_command.set_defaults(
        handler=_handle_register_gzsl_dataset,
    )

    run_framework_experiment_command = commands.add_parser(
        "run-framework-experiment",
        help="用 GPU 运行一个 Framework 实验并登记 Run",
    )
    run_framework_experiment_command.add_argument(
        "--repository", required=True, type=Path,
    )
    run_framework_experiment_command.add_argument(
        "--experiment", required=True,
    )
    run_framework_experiment_command.add_argument(
        "--worktree", required=True, type=Path,
    )
    run_framework_experiment_command.add_argument(
        "--config-json", required=True, type=_strict_json_object,
    )
    run_framework_experiment_command.add_argument(
        "--seed", required=True, type=int,
    )
    run_framework_experiment_command.add_argument(
        "--purpose", required=True, choices=("debug", "evidence"),
    )
    run_framework_experiment_command.set_defaults(
        handler=_handle_run_framework_experiment,
    )

    confirm_framework_run_command = commands.add_parser(
        "confirm-framework-run",
        help="人工复核后把正式 Run 确认为可用证据",
    )
    confirm_framework_run_command.add_argument(
        "--repository", required=True, type=Path,
    )
    confirm_framework_run_command.add_argument(
        "--experiment", required=True,
    )
    confirm_framework_run_command.add_argument("--run", required=True)
    confirm_framework_run_command.add_argument("--reason", required=True)
    confirm_framework_run_command.add_argument("--proposed-by", required=True)
    confirm_framework_run_command.add_argument("--checked-by", required=True)
    confirm_framework_run_command.add_argument(
        "--innovation-outcome",
        choices=("accepted", "rejected"),
    )
    confirm_framework_run_command.add_argument("--innovation-conclusion")
    confirm_framework_run_command.set_defaults(
        handler=_handle_confirm_framework_run,
    )

    export_framework_package_command = commands.add_parser(
        "export-framework-paper-package",
        help="用户主动把已确认 Run 导出为 PaperFlow 科研交付包",
    )
    export_framework_package_command.add_argument(
        "--repository", required=True, type=Path,
    )
    export_framework_package_command.add_argument(
        "--experiment", required=True,
    )
    export_framework_package_command.add_argument("--run", required=True)
    export_framework_package_command.add_argument(
        "--research-brief-json", required=True, type=_strict_json_object,
    )
    export_framework_package_command.add_argument("--claim", required=True)
    export_framework_package_command.add_argument(
        "--confirmed-by", required=True,
    )
    export_framework_package_command.add_argument(
        "--destination-root", type=Path,
    )
    export_framework_package_command.set_defaults(
        handler=_handle_export_framework_paper_package,
    )

    promote_framework_command = commands.add_parser(
        "promote-framework",
        help="把已提交的创新实验晋级为稳定子 Framework",
    )
    promote_framework_command.add_argument(
        "--repository", required=True, type=Path,
    )
    promote_framework_command.add_argument("--experiment", required=True)
    promote_framework_command.add_argument(
        "--worktree", required=True, type=Path,
    )
    promote_framework_command.add_argument("--child-slug", required=True)
    promote_framework_command.add_argument("--title", required=True)
    promote_framework_command.add_argument("--version", required=True)
    promote_framework_command.add_argument(
        "--framework-view", required=True, type=_strict_json_object,
    )
    promote_framework_command.set_defaults(
        handler=_handle_promote_framework,
    )

    validate_framework_command = commands.add_parser(
        "validate-framework-workspace",
        help="检查 GZSL Framework、四类实验、Idea、Run 与 Git 引用",
    )
    validate_framework_command.add_argument(
        "--repository", required=True, type=Path,
    )
    validate_framework_command.set_defaults(
        handler=_handle_validate_framework_workspace,
    )

    save_idea_command = commands.add_parser(
        "save-idea", help="为 v2 项目保存可证伪 Idea",
    )
    save_idea_command.add_argument("--project", required=True, type=Path)
    save_idea_command.add_argument("--manifest", required=True, type=Path)
    save_idea_command.set_defaults(handler=_handle_save_idea)

    save_research_brief_command = commands.add_parser(
        "save-research-brief",
        help="为 v2 项目保存不可覆盖的 Research Brief",
    )
    save_research_brief_command.add_argument(
        "--project", required=True, type=Path,
    )
    save_research_brief_command.add_argument(
        "--manifest", required=True, type=Path,
    )
    save_research_brief_command.set_defaults(
        handler=_handle_save_research_brief,
    )

    seal_paper_package_command = commands.add_parser(
        "seal-paper-package",
        help="为 v2 项目封存不可覆盖的内部论文包",
    )
    seal_paper_package_command.add_argument(
        "--project", required=True, type=Path,
    )
    seal_paper_package_command.add_argument("--brief", required=True)
    seal_paper_package_command.add_argument(
        "--selection", required=True, type=Path,
    )
    seal_paper_package_command.set_defaults(
        handler=_handle_seal_paper_package,
    )

    export_paper_package_command = commands.add_parser(
        "export-paper-package",
        help="把已封存的内部科研包导出成可独立核验的论文交付目录",
    )
    export_paper_package_command.add_argument(
        "--project", required=True, type=Path,
    )
    export_paper_package_command.add_argument("--package", required=True)
    export_paper_package_command.add_argument(
        "--mode", required=True, choices=("hybrid", "full"),
    )
    export_paper_package_command.add_argument(
        "--out", required=True, type=Path,
    )
    export_paper_package_command.set_defaults(
        handler=_handle_export_paper_package,
    )

    verify_paper_package_command = commands.add_parser(
        "verify-paper-package",
        help="只读取交付目录本身，核验科研论文交付包",
    )
    verify_paper_package_command.add_argument(
        "--package-dir", required=True, type=Path,
    )
    verify_paper_package_command.set_defaults(
        handler=_handle_verify_paper_package,
    )

    revise_catalog_idea_command = commands.add_parser(
        "revise-catalog-idea",
        help="为 v2 Idea 创建不可变的新修订记录",
    )
    revise_catalog_idea_command.add_argument("--project", required=True, type=Path)
    revise_catalog_idea_command.add_argument("--idea", required=True)
    revise_catalog_idea_command.add_argument(
        "--manifest", required=True, type=Path,
    )
    revise_catalog_idea_command.add_argument("--reason", required=True)
    revise_catalog_idea_command.set_defaults(handler=_handle_revise_catalog_idea)

    register_module_command = commands.add_parser(
        "register-module", help="为 v2 项目登记可关闭 Module",
    )
    register_module_command.add_argument("--project", required=True, type=Path)
    register_module_command.add_argument("--manifest", required=True, type=Path)
    register_module_command.add_argument(
        "--code-root", required=True, type=Path,
    )
    register_module_command.set_defaults(handler=_handle_register_module)

    render_template_command = commands.add_parser(
        "render-template", help="派生离线 Template HTML 框架视图",
    )
    render_template_command.add_argument("--project", required=True, type=Path)
    render_template_command.add_argument("--template", required=True)
    render_template_command.add_argument("--output", required=True, type=Path)
    render_template_command.set_defaults(handler=_handle_render_template)

    trial = commands.add_parser("new-trial", help="从结构模板创建 Trial")
    trial.add_argument("--project", required=True, type=Path)
    trial.add_argument("--idea", required=True)
    trial.add_argument("--base-version", required=True)
    trial_source = trial.add_mutually_exclusive_group(required=True)
    trial_source.add_argument("--template-family")
    trial_source.add_argument("--template")
    trial.add_argument("--attachment-point", required=True)
    trial.add_argument(
        "--claim-scope",
        choices=("none", "paper-derived", "original-hypothesis"),
        default="none",
    )
    trial.add_argument("--provenance-file", type=Path)
    trial.set_defaults(handler=_handle_new_trial)

    sync = commands.add_parser(
        "sync-code-asset", help="同步已编辑 CodeAsset 的摘要与派生框架图",
    )
    sync.add_argument("--project", required=True, type=Path)
    sync.add_argument("--asset", required=True)
    sync.add_argument("--code-root", type=Path)
    sync.add_argument("--relative-path")
    sync.set_defaults(handler=_handle_sync_code_asset)

    attempt = commands.add_parser("new-attempt", help="创建冻结身份的实验 Attempt")
    attempt.add_argument("--project", required=True, type=Path)
    attempt.add_argument("--target", required=True)
    attempt.add_argument(
        "--type",
        required=True,
        choices=("innovation", "tune", "ablation", "reproduction"),
    )
    attempt.add_argument("--seed", required=True, type=int)
    attempt.add_argument("--command", required=True)
    attempt.add_argument("--config", type=_json_object, default={})
    attempt.set_defaults(handler=_handle_new_attempt)

    result = commands.add_parser("record-result", help="为 Attempt 原子记录 Result")
    result.add_argument("--project", required=True, type=Path)
    result.add_argument("--attempt", required=True)
    result.add_argument("--metrics", required=True, type=_json_object)
    result.add_argument(
        "--decision", choices=("accept", "reject", "inconclusive"),
    )
    result.add_argument(
        "--implementation-status", choices=("valid", "invalid", "uncertain"),
    )
    result.add_argument(
        "--hypothesis-status",
        choices=("supported", "not_supported", "inconclusive", "not_evaluated"),
    )
    result.add_argument("--conclusion", required=True)
    result.add_argument("--artifacts", type=_json_array, default=[])
    result.add_argument("--limitations", type=_json_array, default=[])
    result.set_defaults(handler=_handle_record_result)

    check = commands.add_parser("promotion-check", help="检查 Attempt 是否可 promotion")
    check.add_argument("--project", required=True, type=Path)
    check.add_argument("--attempt", required=True)
    check.set_defaults(handler=_handle_promotion_check)

    promotion = commands.add_parser("promote", help="以 accepted Attempt 创建 draft Version")
    promotion.add_argument("--project", required=True, type=Path)
    promotion.add_argument("--attempt", required=True, action="append")
    promotion.set_defaults(handler=_handle_promote)

    policy = commands.add_parser(
        "set-confirmation-policy", help="设置最小重复 Attempt 与不同 seed 门槛",
    )
    policy.add_argument("--project", required=True, type=Path)
    policy.add_argument("--minimum-accepted-attempts", required=True, type=int)
    policy.add_argument("--minimum-distinct-seeds", required=True, type=int)
    policy.set_defaults(handler=_handle_set_confirmation_policy)

    start_task = commands.add_parser(
        "start-task", help="创建一张结构化四路线实验 Task",
    )
    start_task.add_argument("--project", required=True, type=Path)
    start_task.add_argument("--request", required=True)
    start_task.add_argument("--target-ref", required=True, action="append")
    start_task.add_argument(
        "--route",
        choices=("tune", "ablation", "reproduction", "innovation"),
        default="tune",
    )
    start_task.add_argument("--config", type=_json_object, default={})
    start_task.add_argument(
        "--route-options",
        type=_json_object,
        default={},
        help="消融或复现所需的路线专属 JSON；保留字段不能重复",
    )
    start_task.add_argument("--primary-metric", type=_metric_name, default="score")
    start_task.add_argument("--baseline", type=_finite_float)
    start_task.add_argument("--budget", required=True, type=_json_object)
    start_task.add_argument("--stop-condition", required=True, type=_json_object)
    start_task.add_argument("--seed", type=int, default=7)
    start_task.add_argument(
        "--debug-required", choices=("true", "false"), default="true",
    )
    start_task.add_argument("--changes-code-behavior", action="store_true")
    start_task.add_argument(
        "--code-verified", choices=("true", "false"), default="true",
    )
    start_task.set_defaults(handler=_handle_start_task)

    run_task = commands.add_parser("run-task", help="执行一张已创建的 Task")
    run_task.add_argument("--project", required=True, type=Path)
    run_task.add_argument("--task", required=True)
    run_task.add_argument(
        "--purpose", choices=("debug", "evidence"), default="evidence",
    )
    run_task.add_argument(
        "--backend", choices=("local", "project"), default="project",
    )
    run_task.set_defaults(handler=_handle_run_task)

    seal_outputs = commands.add_parser(
        "seal-run-outputs",
        help="封存一条正式 Run 的固定输出目录",
    )
    seal_outputs.add_argument("--project", required=True, type=Path)
    seal_outputs.add_argument("--run", required=True)
    seal_outputs.set_defaults(handler=_handle_seal_run_outputs)

    version_activation = commands.add_parser(
        "activate-version", help="激活 promotion 产生的 draft Version",
    )
    version_activation.add_argument("--project", required=True, type=Path)
    version_activation.add_argument("--version", required=True)
    version_activation.add_argument("--repo-url", required=True)
    version_activation.add_argument("--commit", required=True)
    version_activation.add_argument("--code-path", required=True)
    version_activation.set_defaults(handler=_handle_activate_version)

    memory = commands.add_parser(
        "update-role-memory", help="原子更新经审核的长期角色记忆",
    )
    memory.add_argument("--project", required=True, type=Path)
    memory.add_argument("--role", required=True, choices=ROLE_NAMES)
    memory.add_argument("--content-file", required=True, type=Path)
    memory.set_defaults(handler=_handle_update_role_memory)

    drift = commands.add_parser(
        "check-workflow-drift",
        help="只读比较源码 Skill、当前运行 Skill 与实例版本锁",
    )
    drift.add_argument("--project", required=True, type=Path)
    drift.add_argument("--source-skill", required=True, type=Path)
    drift.set_defaults(handler=_handle_check_workflow_drift)

    upgrade_lock = commands.add_parser(
        "upgrade-workflow-lock",
        help="显式把实例旧锁升级为当前运行 Skill 的代码指纹锁",
    )
    upgrade_lock.add_argument("--project", required=True, type=Path)
    upgrade_lock.add_argument("--source-skill", required=True, type=Path)
    upgrade_lock.set_defaults(handler=_handle_upgrade_workflow_lock)

    validate = commands.add_parser("validate", help="验证项目工作流对象")
    validate.add_argument("--project", required=True, type=Path)
    validate.set_defaults(handler=_handle_validate)

    status_command = commands.add_parser("status", help="只读查看项目工作流状态")
    status_command.add_argument("--project", required=True, type=Path)
    status_command.set_defaults(handler=_handle_status)

    workflow_status = commands.add_parser(
        "workflow-status", help="只读查看 v1/v2 项目工作流状态",
    )
    workflow_status.add_argument("--project", required=True, type=Path)
    workflow_status.set_defaults(handler=_handle_status)

    task_list_command = commands.add_parser(
        "task-list", help="只读查看 v2 Task 派生清单",
    )
    task_list_command.add_argument("--project", required=True, type=Path)
    task_list_command.add_argument("--task")
    task_list_command.set_defaults(handler=_handle_task_list)

    intake = commands.add_parser(
        "intake-check",
        help="只读检查启动必需输入，不创建 Task 或执行实验",
    )
    intake.add_argument("--project", required=True, type=Path)
    intake.add_argument(
        "--intent",
        required=True,
        choices=(
            "status",
            "continue",
            "tune",
            "ablation",
            "reproduction",
            "innovation",
        ),
    )
    intake.add_argument("--provided", type=_strict_json_object, default=None)
    intake.set_defaults(handler=_handle_intake_check)

    console = commands.add_parser(
        "console-state",
        help="只读返回本地控制台的统一状态，不创建 Task 或执行实验",
    )
    console.add_argument("--project", required=True, type=Path)
    console.add_argument(
        "--intent",
        required=True,
        choices=(
            "status",
            "continue",
            "tune",
            "ablation",
            "reproduction",
            "innovation",
        ),
    )
    console.add_argument("--provided", type=_strict_json_object, default=None)
    console.set_defaults(handler=_handle_console_state)

    plan = commands.add_parser("plan-next", help="只读派生下一步实验建议")
    plan.add_argument("--project", required=True, type=Path)
    plan.set_defaults(handler=_handle_plan_next)

    interpret = commands.add_parser(
        "interpret-request", help="用离线关键词降级解释一句人话",
    )
    interpret.add_argument("--project", required=True, type=Path)
    interpret.add_argument("--request", required=True)
    interpret.set_defaults(handler=_handle_interpret_request)

    export = commands.add_parser(
        "export-paperflow",
        help="把合格科研结果导出为不可覆盖的 PaperFlow 交接快照",
    )
    export.add_argument("--project", required=True, type=Path)
    export.add_argument("--run", required=True)
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--dry-run", action="store_true")
    export.set_defaults(handler=_handle_export_paperflow)

    verify_handoff = commands.add_parser(
        "verify-paperflow-handoff",
        help="只读现场核验 PaperFlow 交接包是否仍与科研事实一致",
    )
    verify_handoff.add_argument("--project", required=True, type=Path)
    verify_handoff.add_argument("--handoff", required=True, type=Path)
    verify_handoff.add_argument(
        "--expected-run",
        required=True,
        type=_handoff_run_id,
    )
    verify_handoff.add_argument(
        "--expected-payload-sha256",
        required=True,
        type=_sha256_identity,
    )
    verify_handoff.set_defaults(handler=_handle_verify_paperflow_handoff)

    return parser


def _handle_init(args: argparse.Namespace) -> dict[str, Any]:
    return init_project(args.path, args.name, layout=args.layout)


def _handle_init_project_skill(args: argparse.Namespace) -> dict[str, Any]:
    return initialize_project_skill(args.project)


def _handle_rebind_project_skill(args: argparse.Namespace) -> dict[str, Any]:
    return rebind_project_skill(args.project)


def _handle_install_project_skill(args: argparse.Namespace) -> dict[str, Any]:
    return install_project_skill(args.project, args.skills_root)


def _handle_check_workflow_drift(args: argparse.Namespace) -> dict[str, Any]:
    return check_workflow_drift(args.project, args.source_skill)


def _handle_upgrade_workflow_lock(args: argparse.Namespace) -> dict[str, Any]:
    return upgrade_workflow_lock(args.project, args.source_skill)


def _handle_new_idea(args: argparse.Namespace) -> dict[str, Any]:
    return new_idea(
        args.project,
        args.title,
        args.note,
        source_label=args.source_label,
        source_locator=args.source_locator,
        source_note=args.source_note,
        parent_idea_id=args.parent_idea,
        related_idea_ids=args.related_idea,
    )


def _handle_activate_idea(args: argparse.Namespace) -> dict[str, Any]:
    return activate_idea(
        args.project,
        args.idea,
        args.problem,
        args.mechanism,
        args.hypothesis,
    )


def _handle_revise_idea(args: argparse.Namespace) -> dict[str, Any]:
    return revise_idea(
        args.project, args.idea, args.reason,
        problem=args.problem, mechanism=args.mechanism,
        hypothesis=args.hypothesis, evidence=args.evidence,
    )


def _handle_set_implementation_mapping(args: argparse.Namespace) -> dict[str, Any]:
    return set_implementation_mapping(args.project, args.idea, args.mapping)


def _handle_register_version(args: argparse.Namespace) -> dict[str, Any]:
    return register_version(
        args.project,
        args.name,
        args.repo_url,
        args.commit,
        args.code_path,
    )


def _handle_register_template(args: argparse.Namespace) -> dict[str, Any]:
    return register_template(args.project, args.manifest, args.code_root)


def _handle_register_source(args: argparse.Namespace) -> dict[str, Any]:
    return register_source(args.project, args.manifest, args.source_path)


def _handle_register_codebase(args: argparse.Namespace) -> dict[str, Any]:
    return register_codebase(args.project, args.manifest, args.repo_root)


def _handle_verify_codebase(args: argparse.Namespace) -> dict[str, Any]:
    return verify_codebase_gate(
        args.project,
        args.codebase,
        expected_git=args.expected_git,
    )


def _handle_list_domain_packs(args: argparse.Namespace) -> dict[str, Any]:
    availability = direction_availability()
    return {
        "schema": DOMAIN_PACK_REGISTRY_SCHEMA,
        "packs": [
            {
                **pack,
                "availability": availability[pack["id"]],
            }
            for pack in discover_domain_packs()
        ],
    }


def _handle_create_domain_repo(args: argparse.Namespace) -> dict[str, Any]:
    pack_id = str(args.pack).split("@", 1)[0]
    availability = direction_availability()
    if availability.get(pack_id) != "ready":
        raise ValueError(f"研究方向尚未开放：{pack_id}")
    return create_domain_repository(
        args.project,
        resolve_domain_pack(args.pack),
        args.destination,
        args.name,
    )


def _handle_create_gzsl_repository(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return create_gzsl_repository(
        args.destination,
        name=args.name,
        environment_name=args.environment,
    )


def _handle_import_standardized_gzsl_framework(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return import_standardized_gzsl_framework(
        args.repository,
        source_repository=args.source_repository,
        framework_slug=args.framework,
        title=args.title,
        version=args.version,
        component_map=args.component_map,
        equivalence=args.equivalence,
        trusted_execution_acknowledged=args.i_trust_this_code,
    )


def _handle_create_framework_idea(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return create_framework_idea(
        args.repository,
        title=args.title,
        problem=args.problem,
        mechanism=args.mechanism,
        falsifiable_hypothesis=args.hypothesis,
        source_notes=args.source_notes,
        parent_idea_ref=args.parent_idea,
    )


def _handle_create_framework_experiment(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return create_framework_experiment(
        args.repository,
        framework_slug=args.framework,
        route=args.route,
        slug=args.slug,
        title=args.title,
        summary=args.summary,
        idea_ref=args.idea,
        route_contract=args.route_contract,
    )


def _handle_prepare_framework_worktree(
    args: argparse.Namespace,
) -> dict[str, Any]:
    worktree = prepare_experiment_worktree(
        args.repository,
        experiment_id=args.experiment,
    )
    return {"worktree": str(worktree)}


def _handle_register_gzsl_dataset(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return register_gzsl_dataset(
        args.repository,
        experiment_id=args.experiment,
        worktree=args.worktree,
        source_path=args.source,
        dataset_slug=args.slug,
        dataset_id=args.dataset_id,
        version=args.version,
        source_uri=args.source_uri,
        license_name=args.license,
    )


def _handle_run_framework_experiment(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return run_framework_experiment(
        args.repository,
        experiment_id=args.experiment,
        worktree=args.worktree,
        config=args.config_json,
        seed=args.seed,
        purpose=args.purpose,
    )


def _handle_confirm_framework_run(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return confirm_framework_run(
        args.repository,
        experiment_id=args.experiment,
        local_run_id=args.run,
        reason=args.reason,
        proposed_by=args.proposed_by,
        checked_by=args.checked_by,
        innovation_accepted=(
            None
            if args.innovation_outcome is None
            else args.innovation_outcome == "accepted"
        ),
        innovation_conclusion=args.innovation_conclusion,
    )


def _handle_export_framework_paper_package(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return export_framework_paper_package(
        args.repository,
        experiment_id=args.experiment,
        local_run_id=args.run,
        research_brief=args.research_brief_json,
        claim_statement_zh=args.claim,
        confirmed_by=args.confirmed_by,
        destination_root=args.destination_root,
    )


def _handle_promote_framework(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return promote_innovation_framework(
        args.repository,
        experiment_id=args.experiment,
        worktree=args.worktree,
        child_slug=args.child_slug,
        title=args.title,
        version=args.version,
        framework_view=args.framework_view,
    )


def _handle_validate_framework_workspace(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return validate_framework_workspace(args.repository)


def _handle_save_idea(args: argparse.Namespace) -> dict[str, Any]:
    return save_idea(args.project, args.manifest)


def _handle_save_research_brief(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return save_research_brief(args.project, args.manifest)


def _handle_seal_paper_package(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return seal_paper_package(args.project, args.brief, args.selection)


def _handle_export_paper_package(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return export_paper_package(
        args.project,
        args.package,
        args.mode,
        args.out,
    )


def _handle_verify_paper_package(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return verify_paper_package(args.package_dir)


def _handle_revise_catalog_idea(args: argparse.Namespace) -> dict[str, Any]:
    return revise_catalog_idea(
        args.project,
        args.idea,
        args.manifest,
        args.reason,
    )


def _handle_register_module(args: argparse.Namespace) -> dict[str, Any]:
    return register_module(args.project, args.manifest, args.code_root)


def _handle_render_template(args: argparse.Namespace) -> dict[str, Any]:
    return render_template(args.project, args.template, args.output)


def _handle_new_trial(args: argparse.Namespace) -> dict[str, Any]:
    return new_trial(
        args.project,
        args.idea,
        args.base_version,
        args.template_family,
        args.attachment_point,
        template_id=args.template,
        claim_scope=args.claim_scope,
        provenance_file=args.provenance_file,
    )


def _handle_sync_code_asset(args: argparse.Namespace) -> dict[str, Any]:
    return sync_code_asset(
        args.project, args.asset,
        code_root=args.code_root, relative_path=args.relative_path,
    )


def _handle_new_attempt(args: argparse.Namespace) -> dict[str, Any]:
    return new_attempt(
        args.project, args.target, args.type, args.seed, args.command, args.config,
    )


def _handle_record_result(args: argparse.Namespace) -> dict[str, Any]:
    return record_result(
        args.project,
        args.attempt,
        args.metrics,
        args.decision,
        args.conclusion,
        args.artifacts,
        args.limitations,
        implementation_status=args.implementation_status,
        hypothesis_status=args.hypothesis_status,
    )


def _handle_promotion_check(args: argparse.Namespace) -> dict[str, Any]:
    return promotion_check(args.project, args.attempt)


def _handle_promote(args: argparse.Namespace) -> dict[str, Any]:
    return promote(args.project, args.attempt)


def _handle_set_confirmation_policy(args: argparse.Namespace) -> dict[str, Any]:
    return set_confirmation_policy(
        args.project,
        args.minimum_accepted_attempts,
        args.minimum_distinct_seeds,
    )


def _handle_start_task(args: argparse.Namespace) -> dict[str, Any]:
    if args.route == "innovation" and args.baseline is None:
        raise ValueError("innovation 必须提供有限 --baseline")
    route_inputs = {
        "config": args.config,
        "seed": args.seed,
        "debug_required": args.debug_required == "true",
        "changes_code_behavior": (
            True if args.route == "innovation" else args.changes_code_behavior
        ),
        "code_verified": args.code_verified == "true",
        "primary_metric": args.primary_metric,
    }
    reserved = set(route_inputs) | {"baseline"}
    overlap = reserved.intersection(args.route_options)
    if overlap:
        raise ValueError(
            f"--route-options 不能覆盖公共字段：{', '.join(sorted(overlap))}"
        )
    route_inputs.update(args.route_options)
    if args.baseline is not None:
        route_inputs["baseline"] = args.baseline
    return create_task(
        args.project,
        owner_request=args.request,
        route=args.route,
        target_refs=args.target_ref,
        route_inputs=route_inputs,
        budget=args.budget,
        stop_condition=args.stop_condition,
    )


def _handle_run_task(args: argparse.Namespace) -> dict[str, Any]:
    return execute_task(
        args.project,
        args.task,
        purpose=args.purpose,
        backend=args.backend,
    )


def _handle_seal_run_outputs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "status": "sealed",
        "run": seal_run_outputs(args.project, args.run),
    }


def _handle_activate_version(args: argparse.Namespace) -> dict[str, Any]:
    return activate_version(
        args.project, args.version, args.repo_url, args.commit, args.code_path,
    )


def _handle_update_role_memory(args: argparse.Namespace) -> dict[str, Any]:
    return update_role_memory(args.project, args.role, args.content_file)


def _handle_validate(args: argparse.Namespace) -> dict[str, Any]:
    return validate_project_workflow(args.project)


def _handle_status(args: argparse.Namespace) -> dict[str, Any]:
    return status(args.project)


def _handle_task_list(args: argparse.Namespace) -> dict[str, Any]:
    return task_list(args.project, args.task)


def _handle_intake_check(args: argparse.Namespace) -> dict[str, Any]:
    return check_intake(
        args.project,
        intent=args.intent,
        provided=args.provided,
    )


def _handle_console_state(args: argparse.Namespace) -> dict[str, Any]:
    return console_state(
        args.project,
        intent=args.intent,
        provided=args.provided,
    )


def _handle_plan_next(args: argparse.Namespace) -> dict[str, Any]:
    return plan_next(args.project)


def _handle_interpret_request(args: argparse.Namespace) -> dict[str, Any]:
    return interpret_request(args.project, args.request)


def _handle_export_paperflow(args: argparse.Namespace) -> dict[str, Any]:
    return export_paperflow(
        args.project,
        args.run,
        args.out,
        dry_run=args.dry_run,
    )


def _handle_verify_paperflow_handoff(
    args: argparse.Namespace,
) -> dict[str, Any]:
    return verify_paperflow_handoff(
        args.project,
        args.handoff,
        expected_run_id=args.expected_run,
        expected_payload_sha256=args.expected_payload_sha256,
    )


def _json_object(value: str) -> dict[str, Any]:
    payload = _json_value(value)
    if not isinstance(payload, dict):
        raise argparse.ArgumentTypeError("必须是 JSON object")
    return payload


def _strict_json_object(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            value,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, ValueError) as error:
        message = error.msg if isinstance(error, json.JSONDecodeError) else str(error)
        raise argparse.ArgumentTypeError(f"无效 JSON：{message}") from error
    if not isinstance(payload, dict):
        raise argparse.ArgumentTypeError("必须是 JSON object")
    return payload


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"重复 JSON key：{key}")
        payload[key] = value
    return payload


def _finite_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("baseline 必须是有限数字") from error
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("baseline 必须是有限数字")
    return number


def _metric_name(value: str) -> str:
    if value != value.strip() or not 1 <= len(value) <= 256:
        raise argparse.ArgumentTypeError(
            "primary-metric 必须是 1..256 字符的规范非空名称"
        )
    return value


def _handoff_run_id(value: str) -> str:
    if re.fullmatch(r"RUN-[0-9]{4}", value) is None:
        raise argparse.ArgumentTypeError("expected-run 必须是 RUN-0001 格式")
    return value


def _sha256_identity(value: str) -> str:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "expected-payload-sha256 必须是 sha256:64hex"
        )
    return value


def _json_array(value: str) -> list[Any]:
    payload = _json_value(value)
    if not isinstance(payload, list):
        raise argparse.ArgumentTypeError("必须是 JSON array")
    return payload


def _json_value(value: str) -> Any:
    try:
        return json.loads(
            value,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        message = error.msg if isinstance(error, json.JSONDecodeError) else str(error)
        raise argparse.ArgumentTypeError(f"无效 JSON：{message}") from error


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"非有限 JSON 数字：{constant}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["verify-paperflow-handoff"]:
        try:
            with redirect_stderr(io.StringIO()):
                args = build_parser().parse_args(arguments)
        except SystemExit as error:
            if error.code == 0:
                return 0
            print(json.dumps(
                verification_failure_report("invalid_arguments"),
                ensure_ascii=True,
            ))
            return 2
    else:
        args = build_parser().parse_args(arguments)
    try:
        payload = args.handler(args)
        output = json.dumps(
            payload,
            ensure_ascii=args.command == "verify-paperflow-handoff",
        )
        print(output)
        if (
            args.command == "verify-paperflow-handoff"
            and payload.get("status") != "pass"
        ):
            return 2
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
