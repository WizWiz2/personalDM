from __future__ import annotations

import ast
import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "src" / "backend"
APP = BACKEND / "app"
TESTS = BACKEND / "tests"
MIGRATIONS = BACKEND / "alembic"
OUTPUT = ROOT / "audit-output"

BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.Match,
    ast.BoolOp,
    ast.IfExp,
    ast.comprehension,
)


@dataclass
class FunctionMetric:
    qualified_name: str
    start: int
    end: int
    lines: int
    branches: int
    args: int
    is_async: bool


@dataclass
class FileMetric:
    path: str
    module: str
    category: str
    lines: int
    nonblank: int
    codeish: int
    functions: int = 0
    classes: int = 0
    imports: list[str] = field(default_factory=list)
    internal_imports: list[str] = field(default_factory=list)
    function_metrics: list[FunctionMetric] = field(default_factory=list)
    broad_excepts: int = 0
    commit_calls: int = 0
    rollback_calls: int = 0
    flush_calls: int = 0
    direct_session_calls: int = 0
    private_attr_accesses: int = 0
    print_calls: int = 0
    traceback_uses: int = 0


class FileVisitor(ast.NodeVisitor):
    def __init__(self, metric: FileMetric):
        self.metric = metric
        self.scope: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.metric.imports.append(alias.name)
            if alias.name == "app" or alias.name.startswith("app."):
                self.metric.internal_imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        name = node.module or ""
        self.metric.imports.append(name)
        if name == "app" or name.startswith("app."):
            self.metric.internal_imports.append(name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.metric.classes += 1
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.metric.functions += 1
        end = getattr(node, "end_lineno", node.lineno)
        qualified = ".".join([*self.scope, node.name])
        branches = sum(isinstance(item, BRANCH_NODES) for item in ast.walk(node))
        args = (
            len(node.args.posonlyargs)
            + len(node.args.args)
            + len(node.args.kwonlyargs)
            + bool(node.args.vararg)
            + bool(node.args.kwarg)
        )
        self.metric.function_metrics.append(
            FunctionMetric(
                qualified_name=qualified,
                start=node.lineno,
                end=end,
                lines=end - node.lineno + 1,
                branches=branches,
                args=args,
                is_async=isinstance(node, ast.AsyncFunctionDef),
            )
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.metric.broad_excepts += 1
        elif isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
            self.metric.broad_excepts += 1
        elif isinstance(node.type, ast.Tuple):
            names = {item.id for item in node.type.elts if isinstance(item, ast.Name)}
            if names & {"Exception", "BaseException"}:
                self.metric.broad_excepts += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = call_name(node.func)
        if name.endswith(".commit") or name == "commit":
            self.metric.commit_calls += 1
        if name.endswith(".rollback") or name == "rollback":
            self.metric.rollback_calls += 1
        if name.endswith(".flush") or name == "flush":
            self.metric.flush_calls += 1
        if "_session." in name or name.startswith("self._session."):
            self.metric.direct_session_calls += 1
        if name in {"print", "builtins.print"}:
            self.metric.print_calls += 1
        if name.startswith("traceback."):
            self.metric.traceback_uses += 1
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") and not node.attr.startswith("__"):
            base = attr_name(node.value)
            if base and not base.startswith("self") and not base.startswith("cls"):
                self.metric.private_attr_accesses += 1
        self.generic_visit(node)


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def attr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = attr_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def category(path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    if rel == "src/backend/cli.py":
        return "cli"
    if rel.startswith("src/backend/app/api/"):
        return "api"
    if rel.startswith("src/backend/app/services/"):
        return "service"
    if rel.startswith("src/backend/app/db/repositories/"):
        return "repository"
    if rel.startswith("src/backend/app/db/"):
        return "db"
    if rel.startswith("src/backend/app/models/"):
        return "model"
    if rel.startswith("src/backend/app/providers/"):
        return "provider"
    if rel.startswith("src/backend/app/"):
        return "app_other"
    if rel.startswith("src/backend/tests/"):
        return "test"
    if rel.startswith("src/backend/alembic/"):
        return "migration"
    return "other"


def module_name(path: Path) -> str:
    rel = path.relative_to(BACKEND).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def iter_python_files() -> Iterable[Path]:
    for root in (APP, TESTS, MIGRATIONS):
        if root.exists():
            yield from sorted(root.rglob("*.py"))
    cli = BACKEND / "cli.py"
    if cli.exists():
        yield cli


def read_metric(path: Path) -> FileMetric:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    metric = FileMetric(
        path=path.relative_to(ROOT).as_posix(),
        module=module_name(path),
        category=category(path),
        lines=len(lines),
        nonblank=sum(bool(line.strip()) for line in lines),
        codeish=sum(
            bool(line.strip()) and not line.lstrip().startswith("#") for line in lines
        ),
    )
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return metric
    FileVisitor(metric).visit(tree)
    metric.imports = sorted(set(filter(None, metric.imports)))
    metric.internal_imports = sorted(set(filter(None, metric.internal_imports)))
    return metric


def internal_target(module: str, modules: set[str]) -> str | None:
    current = module
    while current:
        if current in modules:
            return current
        if "." not in current:
            break
        current = current.rsplit(".", 1)[0]
    return None


def strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    result: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in graph.get(node, set()):
            if nxt not in indices:
                visit(nxt)
                lowlinks[node] = min(lowlinks[node], lowlinks[nxt])
            elif nxt in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[nxt])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    for node in graph:
        if node not in indices:
            visit(node)
    return sorted(result, key=lambda item: (-len(item), item))


def git_churn(limit: int = 300) -> tuple[list[dict], dict]:
    cmd = [
        "git",
        "-C",
        str(ROOT),
        "log",
        f"-n{limit}",
        "--date=iso-strict",
        "--pretty=format:@@@%H|%cI|%s",
        "--name-only",
    ]
    output = subprocess.check_output(cmd, text=True, errors="replace")
    touches: Counter[str] = Counter()
    commits: list[dict] = []
    current: dict | None = None
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("@@@"):
            sha, created, subject = line[3:].split("|", 2)
            current = {"sha": sha, "created_at": created, "subject": subject, "files": []}
            commits.append(current)
        elif line and current is not None:
            current["files"].append(line)
            touches[line] += 1
    by_day: Counter[str] = Counter()
    by_hour: Counter[str] = Counter()
    for commit in commits:
        value = commit["created_at"]
        by_day[value[:10]] += 1
        by_hour[value[:13]] += 1
    summary = {
        "commits_scanned": len(commits),
        "top_touched_files": touches.most_common(30),
        "commits_by_day": by_day.most_common(),
        "busiest_hours": by_hour.most_common(15),
    }
    return commits, summary


def layer_violations(metrics: list[FileMetric]) -> list[dict]:
    violations: list[dict] = []
    for metric in metrics:
        for imported in metric.internal_imports:
            target = imported
            reason = None
            if metric.category == "model" and ".services" in target:
                reason = "model imports service"
            elif metric.category == "repository" and ".services" in target:
                reason = "repository imports service"
            elif metric.category == "service" and ".api" in target:
                reason = "service imports API"
            elif metric.category == "provider" and ".services" in target:
                reason = "provider imports service"
            elif metric.category == "cli" and ".db.repositories" in target:
                reason = "CLI bypasses service layer and imports repository"
            if reason:
                violations.append(
                    {"file": metric.path, "import": imported, "reason": reason}
                )
    return violations


def concept_clusters(metrics: list[FileMetric]) -> dict[str, list[str]]:
    concepts = {
        "session_zero": ("session_zero", "campaign_setup"),
        "turn_pipeline": ("turn_runner", "turn_planner", "post_turn", "generation"),
        "scene": ("scene_", "scene/", "transition", "bridge", "location"),
        "memory": ("memory", "fact", "canon", "thesis", "continuity"),
        "npc_entity": ("entity", "character", "registrar", "participant", "presence"),
        "validation": ("validator", "validation", "guard", "invariant"),
        "provider": ("provider", "llm", "role_model"),
    }
    result: dict[str, list[str]] = {}
    for name, needles in concepts.items():
        result[name] = sorted(
            metric.path
            for metric in metrics
            if metric.category != "test"
            and any(needle in metric.path.casefold() for needle in needles)
        )
    return result


def markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append("# Personal DM repository architecture audit")
    lines.append("")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append(f"Commit: `{report['commit']}`")
    lines.append("")
    lines.append("## Executive metrics")
    lines.append("")
    for key, value in report["summary"].items():
        lines.append(f"- **{key}:** {value}")
    lines.append("")
    lines.append("## Largest production files")
    lines.append("")
    for item in report["largest_files"][:20]:
        lines.append(
            f"- `{item['path']}` — {item['lines']} lines, "
            f"{item['functions']} functions, {item['classes']} classes"
        )
    lines.append("")
    lines.append("## Longest / most branching functions")
    lines.append("")
    for item in report["function_hotspots"][:30]:
        lines.append(
            f"- `{item['file']}::{item['qualified_name']}` — "
            f"{item['lines']} lines, {item['branches']} branch nodes, {item['args']} args"
        )
    lines.append("")
    lines.append("## Highest import fan-out")
    lines.append("")
    for item in report["fanout"][:20]:
        lines.append(f"- `{item[0]}` — {item[1]} internal dependencies")
    lines.append("")
    lines.append("## Highest import fan-in")
    lines.append("")
    for item in report["fanin"][:20]:
        lines.append(f"- `{item[0]}` — imported by {item[1]} modules")
    lines.append("")
    lines.append("## Import cycles")
    lines.append("")
    if report["cycles"]:
        for cycle in report["cycles"]:
            lines.append("- " + " → ".join(f"`{item}`" for item in cycle))
    else:
        lines.append("- No multi-module cycles detected by static import analysis.")
    lines.append("")
    lines.append("## Transaction and error-boundary hotspots")
    lines.append("")
    for item in report["transaction_hotspots"][:30]:
        lines.append(
            f"- `{item['path']}` — commit={item['commit_calls']}, "
            f"rollback={item['rollback_calls']}, flush={item['flush_calls']}, "
            f"broad_except={item['broad_excepts']}, direct_session={item['direct_session_calls']}"
        )
    lines.append("")
    lines.append("## Layer violations")
    lines.append("")
    if report["layer_violations"]:
        for item in report["layer_violations"]:
            lines.append(
                f"- `{item['file']}` → `{item['import']}`: {item['reason']}"
            )
    else:
        lines.append("- No configured layer violations detected.")
    lines.append("")
    lines.append("## Most frequently changed files")
    lines.append("")
    for path, count in report["git_churn"]["top_touched_files"][:25]:
        lines.append(f"- `{path}` — touched by {count} of scanned commits")
    lines.append("")
    lines.append("## Concept surface area")
    lines.append("")
    for name, paths in report["concept_clusters"].items():
        lines.append(f"### {name} ({len(paths)} production files)")
        for path in paths:
            lines.append(f"- `{path}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    OUTPUT.mkdir(exist_ok=True)
    metrics = [read_metric(path) for path in iter_python_files()]
    production = [
        item
        for item in metrics
        if item.category not in {"test", "migration", "other"}
    ]
    modules = {item.module for item in metrics if item.module}
    graph: dict[str, set[str]] = {item.module: set() for item in metrics if item.module}
    for item in metrics:
        for imported in item.internal_imports:
            target = internal_target(imported, modules)
            if target and target != item.module:
                graph[item.module].add(target)
    fanout = sorted(
        ((module, len(targets)) for module, targets in graph.items()),
        key=lambda item: (-item[1], item[0]),
    )
    reverse: Counter[str] = Counter()
    for targets in graph.values():
        reverse.update(targets)
    fanin = sorted(reverse.items(), key=lambda item: (-item[1], item[0]))
    cycles = strongly_connected_components(graph)
    functions = []
    for item in production:
        for fn in item.function_metrics:
            functions.append({"file": item.path, **asdict(fn)})
    functions.sort(key=lambda item: (-item["lines"], -item["branches"], item["file"]))
    transaction_hotspots = sorted(
        (
            {
                "path": item.path,
                "commit_calls": item.commit_calls,
                "rollback_calls": item.rollback_calls,
                "flush_calls": item.flush_calls,
                "broad_excepts": item.broad_excepts,
                "direct_session_calls": item.direct_session_calls,
                "private_attr_accesses": item.private_attr_accesses,
                "print_calls": item.print_calls,
                "traceback_uses": item.traceback_uses,
            }
            for item in production
            if any(
                (
                    item.commit_calls,
                    item.rollback_calls,
                    item.flush_calls,
                    item.broad_excepts,
                    item.direct_session_calls,
                    item.private_attr_accesses,
                    item.print_calls,
                    item.traceback_uses,
                )
            )
        ),
        key=lambda item: (
            -(
                item["commit_calls"]
                + item["rollback_calls"]
                + item["broad_excepts"]
                + item["direct_session_calls"]
            ),
            item["path"],
        ),
    )
    commits, churn = git_churn()
    commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    category_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"files": 0, "lines": 0, "nonblank": 0, "codeish": 0}
    )
    for item in metrics:
        bucket = category_counts[item.category]
        bucket["files"] += 1
        bucket["lines"] += item.lines
        bucket["nonblank"] += item.nonblank
        bucket["codeish"] += item.codeish
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "summary": {
            "python_files": len(metrics),
            "production_python_files": len(production),
            "production_lines": sum(item.lines for item in production),
            "test_files": sum(item.category == "test" for item in metrics),
            "test_lines": sum(item.lines for item in metrics if item.category == "test"),
            "service_files": sum(item.category == "service" for item in metrics),
            "repository_files": sum(item.category == "repository" for item in metrics),
            "api_files": sum(item.category == "api" for item in metrics),
            "import_cycles": len(cycles),
            "functions_over_80_lines": sum(item["lines"] > 80 for item in functions),
            "functions_over_15_branches": sum(item["branches"] > 15 for item in functions),
            "files_with_broad_except": sum(item.broad_excepts > 0 for item in production),
            "files_calling_commit": sum(item.commit_calls > 0 for item in production),
            "files_calling_rollback": sum(item.rollback_calls > 0 for item in production),
            "commits_scanned": churn["commits_scanned"],
        },
        "category_counts": category_counts,
        "largest_files": [
            {
                "path": item.path,
                "category": item.category,
                "lines": item.lines,
                "nonblank": item.nonblank,
                "functions": item.functions,
                "classes": item.classes,
                "internal_dependencies": len(item.internal_imports),
            }
            for item in sorted(production, key=lambda item: (-item.lines, item.path))
        ],
        "function_hotspots": functions,
        "fanout": fanout,
        "fanin": fanin,
        "cycles": cycles,
        "transaction_hotspots": transaction_hotspots,
        "layer_violations": layer_violations(metrics),
        "git_churn": churn,
        "recent_commits": commits[:40],
        "concept_clusters": concept_clusters(metrics),
        "files": [asdict(item) for item in metrics],
    }
    (OUTPUT / "architecture-audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=dict),
        encoding="utf-8",
    )
    (OUTPUT / "architecture-audit.md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))


if __name__ == "__main__":
    main()
