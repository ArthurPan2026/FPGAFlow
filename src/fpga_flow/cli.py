"""FPGAFlow 的跨平台命令行接口。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Sequence

from . import __version__

lazy from .config import ProfileError
lazy from .engine import FlowEngine, FlowError
lazy from .fingerprint import FingerprintError
lazy from .manifest import ManifestError
lazy from .providers.amd import builtin_providers


class ChineseArgumentParser(argparse.ArgumentParser):
    """使用中文帮助提示的参数解析器。"""

    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)
        self.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")


def build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(prog="fpga-flow", description="AMD FPGA 构建、验证与板级流程工具")
    parser._positionals.title = "命令"
    parser._optionals.title = "选项"
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default=None, help="兼容模式的完整 Flow profile 文件")
    parser.add_argument("--project", default=None, help="声明式 FPGA 项目清单")
    parser.add_argument("--workspace", default=None, help="跨项目 fpga-workspace.toml 清单")
    parser.add_argument("--json", dest="json_output", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--details", action="store_true",
                        help="完整展开输入、输出和证据哈希；默认只返回数量")
    subparsers = parser.add_subparsers(dest="action", required=True, parser_class=ChineseArgumentParser)

    def common(sub: argparse.ArgumentParser) -> None:
        sub._positionals.title = "位置参数"
        sub._optionals.title = "选项"
        sub.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sub.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sub.add_argument("--workspace", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sub.add_argument("--json", dest="json_output", action="store_true", default=argparse.SUPPRESS,
                         help="输出机器可读 JSON")
        sub.add_argument("--details", action="store_true", default=argparse.SUPPRESS,
                         help="完整展开输入、输出和证据哈希；默认只返回数量")

    def mutating(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--wait-lock", action="store_true",
                         help="项目输出目录被占用时等待；默认立即报告锁持有者")

    inventory = subparsers.add_parser("inventory", help="解析项目配置与 Vivado XPR 并生成源码清单")
    common(inventory)
    mutating(inventory)

    migration = subparsers.add_parser("migrate-hardware", help="核验旧硬件运行证据并迁移")
    common(migration)
    migration.add_argument("--legacy-run", required=True)
    migration.add_argument("--proof", required=True)

    for name in ("doctor", "plan", "status", "resume"):
        sub = subparsers.add_parser(name, help=f"查看 {name} 状态")
        common(sub)
        sub.add_argument("--target", help="目标 profile 标识")
        sub.add_argument("--stage", help="限定到一个阶段")
        sub.add_argument("--suite", help="限定到一个验证套件")
        if name == "doctor":
            mutating(sub)

    build = subparsers.add_parser("build", help="运行构建阶段")
    common(build)
    mutating(build)
    build.add_argument("--stage", required=True, help="kind=build 的阶段名称")
    build.add_argument("--with-deps", action="store_true", help="按依赖顺序运行前置阶段")
    build.add_argument("--force", action="store_true", help="忽略有效的 build/test 阶段缓存")
    build.add_argument("--target", help="目标 profile 标识")
    build.add_argument("--allow-side-effects", action="store_true",
                       help="确认运行 profile 声明的外部副作用阶段")

    validate = subparsers.add_parser("validate", help="运行配置的验证套件")
    common(validate)
    mutating(validate)
    validate.add_argument("--suite", required=True, help="fpga-flow.toml 中的套件名称")
    validate.add_argument("--force", action="store_true", help="强制运行确定性测试阶段")
    validate.add_argument("--target", help="目标 profile 标识")
    validate.add_argument("--allow-side-effects", action="store_true",
                          help="确认运行 profile 声明的外部副作用阶段")

    deploy = subparsers.add_parser("deploy", help="显式部署到一个目标")
    common(deploy)
    mutating(deploy)
    deploy.add_argument("--target", required=True, help="目标 profile 的精确标识")
    deploy.add_argument("--stage", help="同一目标有多种传输方式时，选择一个部署阶段")
    deploy.add_argument("--allow-destructive", action="store_true",
                        help="确认执行配置中标记为破坏性的部署阶段")
    deploy.add_argument("--allow-side-effects", action="store_true",
                        help="确认执行 profile 声明的外部副作用")

    release = subparsers.add_parser("release", help="运行配置的构建和测试；部署需显式选择")
    common(release)
    mutating(release)
    release.add_argument("--target", help="目标 profile 标识")
    release.add_argument("--deploy", action="store_true", help="同时运行与 --target 匹配的部署阶段")
    release.add_argument("--deploy-stage", help="存在多种传输方式时选择部署阶段")
    release.add_argument("--allow-destructive", action="store_true",
                         help="确认执行破坏性部署阶段")
    release.add_argument("--allow-side-effects", action="store_true",
                         help="确认运行 profile 声明的外部副作用阶段")
    release.add_argument("--force", action="store_true", help="强制运行确定性 build/test 阶段")

    watch = subparsers.add_parser("watch", help="查看运行状态和实时日志尾部")
    common(watch)
    watch.add_argument("--run-id", required=True, help="运行标识")
    watch.add_argument("--tail-lines", type=int, default=40, help="每个日志文件显示的尾部行数")
    watch.add_argument("--follow", action="store_true", help="持续轮询；JSON 模式按行输出快照")
    watch.add_argument("--poll-seconds", type=float, default=1.0, help="follow 轮询间隔")

    history = subparsers.add_parser("history", help="查询不可变运行记录")
    common(history)
    history.add_argument("--limit", type=int, default=20, help="返回记录数，范围为 1 到 500")
    history.add_argument("--before", help="从此 run_id 之前继续查询")
    history.add_argument("--stage", help="筛选阶段名称")

    workspace_run = subparsers.add_parser("run", help="执行跨项目 workspace DAG")
    common(workspace_run)
    mutating(workspace_run)
    workspace_run.add_argument("--force", action="store_true", help="强制运行 workspace 所选阶段")
    workspace_run.add_argument("--include-optional", action="store_true",
                               help="运行标记为 optional 的 host/RTL 回归节点")
    workspace_run.add_argument("--allow-side-effects", action="store_true",
                               help="确认运行 workspace 中声明的板卡或运行时副作用")
    workspace_run.add_argument("--allow-destructive", action="store_true",
                               help="确认运行 workspace 中声明的破坏性操作")

    return parser


_HUMAN_LABELS = {
    "run": "执行", "deploy": "部署", "validate": "验证",
    "explicit-deploy-only": "仅显式部署", "skip-target": "跳过未选目标", "blocked": "被阻止",
    "pending": "待执行", "succeeded": "通过", "failed": "失败", "fail": "失败",
    "running": "运行中", "cache-hit": "缓存命中", "explicit-only": "仅显式执行",
    "not-selected": "未选择", "ready": "就绪", "ok": "通过", "needs-attention": "需要处理",
    "warning": "警告", "verified": "已核验", "missing": "缺失",
    "unrecorded": "未记录", "tampered-or-missing": "证据已损坏或缺失", "unknown": "未知",
}


def _human_label(value: Any) -> str:
    text = str(value)
    return _HUMAN_LABELS.get(text, text)


def _human(result: dict[str, Any], action: str) -> None:
    if action == "plan":
        print(f"Flow 计划：项目={result.get('project')} 目标={result.get('target') or '-'}（未启动命令）")
        for stage in result.get("stages", []):
            print(f"  阶段 {stage.get('stage')}：动作={_human_label(stage.get('action'))}，"
                  f"状态={_human_label(stage.get('status'))}，{stage.get('detail', '')}")
            if stage.get("command_argv"):
                print(f"    argv={stage['command_argv']}")
            if stage.get("side_effects"):
                print(f"    副作用={','.join(stage['side_effects'])}，需 --allow-side-effects")
    elif action == "status":
        print(f"Flow 状态：{_human_label(result.get('status'))} "
              f"（项目={result.get('project')}，目标={result.get('target') or '-'}）")
        print(f"最近运行={result.get('latest_run_id') or '-'}，累计记录={result.get('run_count', 0)}")
        for stage in result.get("stages", []):
            print(f"  {stage.get('stage')}：当前={_human_label(stage.get('action'))}，"
                  f"上次={_human_label(stage.get('last_status') or '-') }，"
                  f"run_id={stage.get('last_run_id') or '-'}，证据={_human_label(stage.get('last_evidence'))}")
    elif action == "resume":
        print(f"恢复建议：{_human_label(result.get('status'))} "
              f"（项目={result.get('project')}，目标={result.get('target') or '-'}）")
        next_action = result.get("next_action")
        if next_action:
            print(f"  阶段={next_action.get('stage') or '-'}：{next_action.get('reason') or ''}")
            if next_action.get("command"):
                print(f"  下一步：{next_action['command']}")
            for option in next_action.get("options", []):
                print(f"    可选部署阶段：{option}")
        else:
            print(f"  {result.get('detail', '没有待执行操作')}")
    elif action == "doctor":
        print(f"环境预检：{_human_label(result.get('status'))}（项目={result.get('project')}）"
              f" run_id={result.get('run_id')}")
        for name, item in result.get("toolchain_details", {}).items():
            print(f"  工具链 {name}：{_human_label(item.get('status'))} — {item.get('detail')}")
        for name, item in result.get("preflight_details", {}).items():
            print(f"  目标阶段 {name}：{_human_label(item.get('status'))} — "
                  f"{item.get('error') or item.get('detail') or item.get('result')}")
    elif action == "watch":
        print(f"运行 {result.get('run_id')}：阶段={result.get('stage') or '-'}，状态={_human_label(result.get('status'))}")
        execution = result.get("execution", {})
        if execution:
            print(f"  执行状态={execution.get('state') or '-'}，PID={execution.get('pid') or '-'}，"
                  f"stdout={execution.get('stdout_bytes', 0)} B，stderr={execution.get('stderr_bytes', 0)} B")
        for log in result.get("logs", []):
            print(f"--- {log.get('path')} ---")
            for line in log.get("tail", []):
                print(line)
    elif action == "history":
        print(f"运行记录：{result.get('count', 0)} 条")
        for run in result.get("runs", []):
            print(f"  {run.get('run_id')} {run.get('stage')} {_human_label(run.get('status'))} "
                  f"{run.get('elapsed_seconds') or '-'} s {run.get('error_code') or ''}")
    elif action == "inventory":
        print(f"项目 inventory 已生成：{result.get('path')}")
        print(f"  源文件={result.get('source_count', 0)}，测试台={result.get('test_count', 0)}")
    else:
        print(f"{action}：{_human_label(result.get('status'))} 目标={result.get('target') or '-'}")
        for stage in result.get("stages", []):
            label = "缓存命中" if stage.get("cache_hit") else _human_label(stage.get("status", "unknown"))
            print(f"  阶段 {stage.get('stage')}：{label}，运行={stage.get('run_id', '-')}")


def _progress(event: dict[str, Any]) -> None:
    kind = event.get("event")
    if kind == "stage_started":
        print(f"[FPGA-FLOW] 阶段 {event.get('stage')} 开始 run_id={event.get('run_id')}", file=sys.stderr, flush=True)
    elif kind == "stage_progress":
        print(f"[FPGA-FLOW] 阶段 {event.get('stage')} 运行 {event.get('elapsed_seconds')} s，"
              f"stdout={event.get('stdout_bytes', 0)} B，stderr={event.get('stderr_bytes', 0)} B",
              file=sys.stderr, flush=True)
    elif kind == "stage_finished":
        print(f"[FPGA-FLOW] 阶段 {event.get('stage')} {event.get('status')} "
              f"run_id={event.get('run_id')}", file=sys.stderr, flush=True)


_DETAIL_FIELDS = {"inputs": "input_count", "outputs": "output_count", "evidence": "evidence_count"}

def _compact_json_result(value: Any) -> Any:
    """压缩默认 JSON 阶段报告，完整哈希映射仍保留在 run.json。"""
    def compact_record(record: Any) -> Any:
        if not isinstance(record, dict):
            return record
        result = dict(record)
        for field, count_field in _DETAIL_FIELDS.items():
            details = result.pop(field, None)
            if isinstance(details, (dict, list, tuple)):
                result.setdefault(count_field, len(details))
            elif details is not None:
                result.setdefault(count_field, 1)
        run_id = result.get("run_id")
        if isinstance(run_id, str) and run_id:
            result.setdefault("record_path", f"runs/{run_id}/run.json")
        return result

    compact = compact_record(value)
    if not isinstance(compact, dict):
        return compact
    for field in ("stages", "runs"):
        records = compact.get(field)
        if isinstance(records, list):
            compact[field] = [compact_record(record) for record in records]
    return compact


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace_path = getattr(args, "workspace", None)
    as_json = bool(getattr(args, "json_output", False))
    if workspace_path:
        if args.config or args.project:
            parser.error("--workspace 不能与 --config 或 --project 同时使用")
        if args.action not in {"plan", "status", "history", "run"}:
            parser.error("--workspace 只支持 plan/status/history/run")
        try:
            from .workspace_dag import WorkspaceDAG
            workspace = WorkspaceDAG.from_file(workspace_path)
            if args.action == "plan":
                result = workspace.plan()
            elif args.action == "status":
                result = workspace.status()
            elif args.action == "history":
                result = workspace.history(limit=args.limit)
            else:
                result = workspace.run(force=args.force, include_optional=args.include_optional,
                                      allow_side_effects=args.allow_side_effects,
                                      allow_destructive=args.allow_destructive, wait_lock=args.wait_lock)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
            return 1 if result.get("status") == "failed" else 0
        except (OSError, ValueError) as exc:
            failure = {"status": "failed", "error_code": getattr(exc, "code", type(exc).__name__),
                       "error": str(exc)}
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
            return 1
    if args.action == "run":
        parser.error("run 需要 --workspace <fpga-workspace.toml>")
    if args.config and args.project:
        parser.error("--config 和 --project 不能同时指定")
    config_path = args.project or args.config
    if config_path is None:
        config_path = "fpga-project.toml" if Path("fpga-project.toml").is_file() else "fpga-flow.toml"
    # Progress is written to stderr, so JSON stdout remains parseable while long runs stay visible.
    progress = _progress
    try:
        engine = FlowEngine.from_file(config_path, providers=builtin_providers(), progress_callback=progress)
        if args.action == "inventory":
            result = engine.write_project_inventory(wait_lock=args.wait_lock)
        elif args.action == "migrate-hardware":
            from .migration import migrate_hardware
            result = dict(migrate_hardware(engine, Path(args.legacy_run), Path(args.proof)))
        elif args.action == "doctor":
            result = engine.doctor(target=args.target, stage=args.stage, suite=args.suite,
                                   wait_lock=args.wait_lock)
        elif args.action == "plan":
            result = engine.plan(target=args.target, stage=args.stage, suite=args.suite)
        elif args.action == "status":
            result = engine.status(target=args.target, stage=args.stage, suite=args.suite)
        elif args.action == "resume":
            result = engine.resume(target=args.target, stage=args.stage, suite=args.suite)
        elif args.action == "build":
            result = engine.build(args.stage, with_deps=args.with_deps, force=args.force,
                                  target=args.target, allow_side_effects=args.allow_side_effects,
                                  wait_lock=args.wait_lock)
        elif args.action == "validate":
            result = engine.validate(args.suite, force=args.force, target=args.target,
                                     allow_side_effects=args.allow_side_effects,
                                     wait_lock=args.wait_lock)
        elif args.action == "deploy":
            result = engine.deploy(args.target, stage_name=args.stage,
                                   allow_destructive=args.allow_destructive,
                                   allow_side_effects=args.allow_side_effects,
                                   wait_lock=args.wait_lock)
        elif args.action == "release":
            result = engine.release(target=args.target, deploy=args.deploy,
                                    deploy_stage=args.deploy_stage,
                                    allow_destructive=args.allow_destructive,
                                    allow_side_effects=args.allow_side_effects,
                                    force=args.force, wait_lock=args.wait_lock)
        elif args.action == "history":
            result = engine.history(limit=args.limit, before_run_id=args.before, stage=args.stage)
        elif args.action == "watch":
            if not args.follow:
                result = engine.watch(args.run_id, tail_lines=args.tail_lines)
            else:
                if args.poll_seconds <= 0:
                    parser.error("--poll-seconds 必须大于 0")
                previous = None
                previous_logs: dict[str, list[str]] = {}
                previous_progress = None
                while True:
                    result = engine.watch(args.run_id, tail_lines=args.tail_lines)
                    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
                    if serialized != previous:
                        if as_json:
                            payload = result if getattr(args, "details", False) else _compact_json_result(result)
                            print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
                        else:
                            execution = result.get("execution", {})
                            progress = (result.get("status"), execution.get("stdout_bytes"),
                                        execution.get("stderr_bytes"), execution.get("last_activity_at_utc"))
                            if progress != previous_progress:
                                print(f"运行 {args.run_id}：阶段={result.get('stage') or '-'}，"
                                      f"状态={result.get('status')}，stdout={execution.get('stdout_bytes', 0)} B，"
                                      f"stderr={execution.get('stderr_bytes', 0)} B", flush=True)
                                previous_progress = progress
                            for log in result.get("logs", []):
                                path = log.get("path", "")
                                current = list(log.get("tail", []))
                                old = previous_logs.get(path, [])
                                overlap = 0
                                for size in range(min(len(old), len(current)), 0, -1):
                                    if old[-size:] == current[:size]:
                                        overlap = size
                                        break
                                for line in current[overlap:]:
                                    print(line, flush=True)
                                previous_logs[path] = current
                        previous = serialized
                    if result.get("status") not in {"running", "draining"}:
                        if not as_json:
                            _human(result, "watch")
                        break
                    time.sleep(args.poll_seconds)
        else:
            parser.error(f"不支持的子命令 {args.action!r}")
            return 2
        if as_json:
            if not (args.action == "watch" and args.follow):
                payload = result if getattr(args, "details", False) else _compact_json_result(result)
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        elif args.action != "watch" or not args.follow:
            _human(result, args.action)
        return 1 if result.get("status") == "failed" else 0
    except (FlowError, ProfileError, ManifestError, FingerprintError, OSError, ValueError) as exc:
        failure = {"status": "failed", "error_code": getattr(exc, "code", type(exc).__name__),
                   "error": str(exc)}
        if as_json:
            # JSON 模式只在 stdout 写一个可解析的结果对象；退出码仍表示失败。
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        else:
            print(f"[{failure['error_code']}] {failure['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
