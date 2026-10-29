"""AST mutation runner for the backend (see README.md in this folder).

Usage (from the repository root):
    PDM_TEXT_PROVIDER=local python scripts/mutation/mutate.py app/services/fact_repo.py [more modules...] \
        [--workers 8] [--out .mutation] [--tests tests/test_x.py ...] [--reuse-coverage]

1. Runs the selected tests (default: the whole suite) once under coverage with per-test contexts.
2. Generates mutants for each module: comparison flip, and/or swap, `not` drop, True/False swap,
   number +1, `return X` -> `return None`, if/while forced True/False, call statement and
   break/continue deleted.
3. Runs each mutant in an isolated copy of src/backend against only the tests covering its line.
Results: <out>/results.jsonl (one row per mutant) and a per-module score table on stdout.
Score = (killed + timeout) / total; mutants on lines no test executes count as no_coverage.
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE,
       ast.LtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}


def mutation_sites(tree: ast.AST) -> list[tuple]:
    out = []
    for n in ast.walk(tree):
        ln = getattr(n, "lineno", None)
        if isinstance(n, ast.Compare):
            for i, op in enumerate(n.ops):
                if type(op) in CMP:
                    out.append((ln, "cmp", n, ("ops", i, CMP[type(op)]())))
        elif isinstance(n, ast.BoolOp):
            out.append((ln, "boolop", n, ("op", None, ast.Or() if isinstance(n.op, ast.And) else ast.And())))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            out.append((ln, "not_drop", n, ("unwrap",)))
        elif isinstance(n, ast.Constant) and isinstance(n.value, bool):
            out.append((ln, "bool", n, ("value", None, not n.value)))
        elif isinstance(n, ast.Constant) and type(n.value) in (int, float):
            out.append((ln, "num", n, ("value", None, n.value + 1)))
        elif isinstance(n, ast.Return) and n.value is not None and not (
            isinstance(n.value, ast.Constant) and n.value.value is None
        ):
            out.append((ln, "ret_none", n, ("value", None, ast.Constant(None))))
        elif isinstance(n, (ast.If, ast.While)):
            out.append((ln, "cond_true", n, ("test", None, ast.Constant(True))))
            out.append((ln, "cond_false", n, ("test", None, ast.Constant(False))))
        elif isinstance(n, ast.Expr) and isinstance(n.value, (ast.Call, ast.Await)):
            out.append((ln, "del_call", n, ("replace_pass",)))
        elif isinstance(n, (ast.Continue, ast.Break)):
            out.append((ln, "del_flow", n, ("replace_pass",)))
    return out


def apply_mutant(src: str, idx: int) -> str:
    tree = ast.parse(src)
    _, _, node, act = mutation_sites(tree)[idx]
    if act[0] == "ops":
        node.ops[act[1]] = act[2]
    elif act[0] == "op":
        node.op = act[2]
    elif act[0] == "value":
        node.value = act[2]
    elif act[0] == "test":
        node.test = act[2]
    else:
        replacement = ast.Pass() if act[0] == "replace_pass" else node.operand
        for parent in ast.walk(tree):
            for field, value in ast.iter_fields(parent):
                if isinstance(value, list):
                    for i, item in enumerate(value):
                        if item is node:
                            value[i] = replacement
                elif value is node:
                    setattr(parent, field, replacement)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def collect_coverage(out: Path, tests: list[str], modules: list[str]) -> Path:
    data = out / "ctx.coverage"

    env = dict(os.environ, COVERAGE_FILE=str(data))
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                    "--cov=app", "--cov-context=test", f"--cov-config={out / 'rc'}", *tests],
                   cwd=BACKEND, env=env, check=False)

    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("modules", nargs="+", help="paths relative to src/backend, e.g. app/services/fact_repo.py")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default=".mutation")
    ap.add_argument("--tests", nargs="*", default=["tests"])
    ap.add_argument("--reuse-coverage", action="store_true")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "rc").write_text("[run]\nrelative_files = False\n", encoding="utf-8")
    data = out / "ctx.coverage"
    if not (args.reuse_coverage and data.exists()):
        data = collect_coverage(out, args.tests, args.modules)
    from coverage import CoverageData

    cov = CoverageData(str(data))
    cov.read()
    mutants = []
    for mod in args.modules:
        src = (BACKEND / mod).read_text(encoding="utf-8")
        measured = [f for f in cov.measured_files() if f.endswith(mod)]
        ctx = cov.contexts_by_lineno(measured[0]) if measured else {}
        for i, (ln, kind, node, _) in enumerate(mutation_sites(ast.parse(src))):
            tests = sorted({c.split("|")[0] for c in ctx.get(ln, []) if c})
            seg = (ast.get_source_segment(src, node) or "").split("\n")[0][:160]
            mutants.append(dict(mod=mod, idx=i, line=ln, kind=kind, orig=seg, tests=tests))

    workers: queue.Queue[Path] = queue.Queue()
    for i in range(args.workers):
        copy = out / f"w{i}"
        if copy.exists():
            shutil.rmtree(copy)
        shutil.copytree(BACKEND, copy, ignore=shutil.ignore_patterns("__pycache__", "data", ".ruff_cache"))
        workers.put(copy)
    results = (out / "results.jsonl").open("w", encoding="utf-8")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    def run_one(m: dict) -> None:
        row = {k: m[k] for k in ("mod", "idx", "line", "kind", "orig")}
        if not m["tests"]:
            row["status"] = "no_coverage"
        else:
            copy = workers.get()
            target = copy / m["mod"]
            original = target.read_text(encoding="utf-8")
            try:
                target.write_text(apply_mutant(original, m["idx"]), encoding="utf-8")
                proc = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider",
                                       "--no-header", "-o", "addopts=", *m["tests"]], cwd=copy, env=env,
                                      capture_output=True, text=True, timeout=max(60, len(m["tests"]) * 3))
                row["status"] = {0: "survived", 1: "killed"}.get(proc.returncode, f"error{proc.returncode}")
                if proc.returncode == 1:
                    row["killer"] = re.findall(r"^FAILED (\S+)", proc.stdout, re.M)[:1]
            except subprocess.TimeoutExpired:
                row["status"] = "timeout"
            finally:
                target.write_text(original, encoding="utf-8")
                workers.put(copy)
        results.write(json.dumps(row, ensure_ascii=False) + "\n")
        results.flush()

    started = time.time()
    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(run_one, mutants))
    results.close()
    rows = [json.loads(line) for line in (out / "results.jsonl").open(encoding="utf-8")]
    by_mod: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for row in rows:
        by_mod[row["mod"]][row["status"]] += 1
    for mod, c in by_mod.items():
        total = sum(c.values())
        score = (c["killed"] + c["timeout"]) / total if total else 0
        print(f"{mod}: {total} mutants, killed {c['killed']}, survived {c['survived']}, "
              f"no_cov {c['no_coverage']}, timeout {c['timeout']}, score {score:.0%}")
    print(f"done in {time.time() - started:.0f}s; rows in {out / 'results.jsonl'}")


if __name__ == "__main__":
    main()
