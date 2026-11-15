"""AMD FPGA Flow 的命令行接口。"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from .config import ProfileError
from .engine import FlowEngine, FlowError
from .fingerprint import FingerprintError
from .manifest import ManifestError
from .providers.amd import builtin_providers


class ChineseArgumentParser(argparse.ArgumentParser):
    """使用中文帮助提示的参数解析器。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)
        self.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")


def build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(prog="fpga-flow", description="与项目解耦的 AMD FPGA 工作流工具")
    parser._positionals.title = "命令"
    parser._optionals.title = "选项"
    parser.add_argument("--config", default="fpga-flow.toml", help="项目 profile 文件")
    parser.add_argument("--json", dest="json_output", action="store_true", help="输出机器可读 JSON")
    subparsers = parser.add_subparsers(dest="action", required=True, parser_class=ChineseArgumentParser)

    def common(sub: argparse.ArgumentParser) -> None:
        sub._positionals.title = "位置参数"
        sub._optionals.title = "选项"
        # 支持在子命令前后指定全局选项。
        sub.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sub.add_argument("--json", dest="json_output", action="store_true", default=argparse.SUPPRESS,
                         help="输出机器可读 JSON")

    for name in ("doctor", "plan", "status", "resume"):
        sub = subparsers.add_parser(name, help=f"查看 {name} 状态")
        common(sub)
        if name in {"doctor", "plan", "status"}:
            sub.add_argument("--target", help="目标 profile 标识")

    build = subparsers.add_parser("build", help="运行一个构建阶段")
    common(build)
    build.add_argument("--stage", required=True, help="kind=build 的阶段名称")
    build.add_argument("--with-deps", action="store_true", help="按依赖顺序运行前置阶段")
    build.add_argument("--force", action="store_true", help="忽略有效的 build/test 阶段缓存")
    build.add_argument("--target", help="目标 profile 标识")

    validate = subparsers.add_parser("validate", help="运行配置的验证套件")
    common(validate)
    validate.add_argument("--suite", required=True, help="fpga-flow.toml 中的套件名称")
    validate.add_argument("--force", action="store_true", help="强制运行确定性测试阶段")
    validate.add_argument("--target", help="目标 profile 标识")

    deploy = subparsers.add_parser("deploy", help="显式部署到一个目标")
    common(deploy)
    deploy.add_argument("--target", required=True, help="目标 profile 的精确标识")
    deploy.add_argument("--stage", help="同一目标有多种传输方式时，选择一个部署阶段")
    deploy.add_argument("--allow-destructive", action="store_true",
                        help="确认执行配置中标记为破坏性的部署阶段")

    release = subparsers.add_parser("release", help="运行所有已配置的构建和测试；部署需显式选择")
    common(release)
    release.add_argument("--target", help="目标 profile 标识")
    release.add_argument("--deploy", action="store_true", help="同时运行与 --target 匹配的一个部署阶段")
    release.add_argument("--deploy-stage", help="存在多种传输方式时选择部署阶段")
    release.add_argument("--allow-destructive", action="store_true",
                         help="确认执行破坏性部署阶段")
    release.add_argument("--force", action="store_true", help="强制运行确定性 build/test 阶段")
    return parser


_HUMAN_LABELS = {
    "run": "执行", "deploy": "部署", "validate": "验证",
    "explicit-deploy-only": "仅显式部署", "skip-target": "跳过未选目标", "blocked": "被阻止",
    "pending": "待执行", "succeeded": "通过", "failed": "失败", "fail": "失败",
    "running": "运行中", "cache-hit": "缓存命中", "explicit-only": "仅显式执行",
    "not-selected": "未选择", "ready": "就绪", "ok": "通过", "needs-attention": "需要处理",
    "verified": "已核验", "missing": "缺失", "unrecorded": "未记录",
    "tampered-or-missing": "证据已损坏或缺失", "unknown": "未知",
}


def _human_label(value: Any) -> str:
    text = str(value)
    return _HUMAN_LABELS.get(text, text)


def _human(result: dict[str, Any], action: str) -> None:
    if action == "plan":
        print(f"Flow 执行计划：项目={result.get('project')} 目标={result.get('target') or '-'}（未启动命令）")
        for stage in result.get("stages", []):
            print(f"  阶段 {stage.get('stage')}：动作={_human_label(stage.get('action'))}，状态={_human_label(stage.get('status'))} — {stage.get('detail', '')}")
    elif action == "status":
        print(f"Flow 状态：项目={result.get('project')} 目标={result.get('target') or '-'}")
        for stage in result.get("stages", []):
            print(f"  阶段 {stage.get('stage')}：当前={_human_label(stage.get('status'))}，上次={_human_label(stage.get('last_status') or '-') }，"
                  f"运行={stage.get('last_run_id') or '-'}，证据={_human_label(stage.get('last_evidence', 'unknown'))}")
    elif action == "resume":
        print(f"恢复状态：{_human_label(result.get('status'))}（项目={result.get('project')}）")
        for stage in result.get("unfinished_or_failed", []):
            print(f"  阶段 {stage.get('stage')}：状态={_human_label(stage.get('status'))}，运行={stage.get('run_id')}，日志目录={stage.get('log_dir')}")
            if stage.get("suggested_command"):
                print(f"    下一步：{stage['suggested_command']}")
        if not result.get("unfinished_or_failed"):
            print(f"  {result.get('next')}")
    elif action == "doctor":
        print(f"环境检查：{_human_label(result.get('status'))}（项目={result.get('project')}）运行={result.get('run_id')}")
        for name, item in result.get("toolchain_details", {}).items():
            print(f"  {name}：{_human_label(item.get('status'))} — {item.get('detail')}")
    else:
        print(f"{action}：{_human_label(result.get('status'))} 目标={result.get('target') or '-'}")
        for stage in result.get("stages", []):
            label = "缓存命中" if stage.get("cache_hit") else _human_label(stage.get("status", "unknown"))
            print(f"  阶段 {stage.get('stage')}：{label}，运行={stage.get('run_id', '-')}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json_output", False))
    try:
        engine = FlowEngine.from_file(args.config, providers=builtin_providers())
        if args.action == "doctor":
            result = engine.doctor(target=args.target)
        elif args.action == "plan":
            result = engine.plan(target=args.target)
        elif args.action == "status":
            result = engine.status(target=args.target)
        elif args.action == "resume":
            result = engine.resume()
        elif args.action == "build":
            result = engine.build(args.stage, with_deps=args.with_deps, force=args.force, target=args.target)
        elif args.action == "validate":
            result = engine.validate(args.suite, force=args.force, target=args.target)
        elif args.action == "deploy":
            result = engine.deploy(args.target, stage_name=args.stage,
                                   allow_destructive=args.allow_destructive)
        elif args.action == "release":
            result = engine.release(target=args.target, deploy=args.deploy,
                                    deploy_stage=args.deploy_stage,
                                    allow_destructive=args.allow_destructive, force=args.force)
        else:
            parser.error(f"不支持的子命令 {args.action!r}")
            return 2
        if as_json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _human(result, args.action)
        return 1 if result.get("status") == "failed" else 0
    except (FlowError, ProfileError, ManifestError, FingerprintError, OSError, ValueError) as exc:
        failure = {"status": "failed", "error_code": getattr(exc, "code", type(exc).__name__), "error": str(exc)}
        if as_json:
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        else:
            print(f"[{failure['error_code']}] {failure['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
