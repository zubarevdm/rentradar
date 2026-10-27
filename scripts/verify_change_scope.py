"""Read-only comparison of protected collection behavior against a Git revision.

Run: python scripts/verify_change_scope.py [baseline-ref]
Does not load Settings, .env, a database, or contact any external service.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("baseline_ref", nargs="?", default="HEAD")
parser.add_argument(
    "--photo-fixes",
    action="store_true",
    help="Allow only Avito photo extraction changes requested after the main update",
)
options = parser.parse_args()
REF = options.baseline_ref


def original(path: str) -> str:
    return subprocess.check_output(["git", "show", f"{REF}:{path}"], cwd=ROOT).decode("utf-8")


def current(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def functions(source: str) -> dict[str, str]:
    return {
        n.name: dump(n)
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def check(condition: bool, label: str) -> None:
    if not condition:
        raise SystemExit(f"CHANGED: {label}")
    print(f"OK: {label}")


def main() -> None:
    protected = [
        "profiles.yaml",
        "src/rentradar/pipeline.py",
        "src/rentradar/models.py",
        "src/rentradar/personal/filters.py",
    ]
    protected += [
        p.relative_to(ROOT).as_posix()
        for directory in ("collectors", "scoring")
        for p in (ROOT / "src/rentradar" / directory).glob("*.py")
    ]
    avito = "src/rentradar/collectors/avito.py"
    if options.photo_fixes:
        protected.remove(avito)
        before, after = original(avito), current(avito)
        old_functions, new_functions = functions(before), functions(after)
        photo_functions = {"_card_photos", "parse_detail"}
        check(
            all(
                new_functions.get(k) == v
                for k, v in old_functions.items()
                if k not in photo_functions
            ),
            "all collector network/normalization functions unchanged",
        )
        old_parse = next(
            n
            for n in ast.parse(before).body
            if isinstance(n, ast.FunctionDef) and n.name == "parse_detail"
        )
        new_parse = next(
            n
            for n in ast.parse(after).body
            if isinstance(n, ast.FunctionDef) and n.name == "parse_detail"
        )

        def payment_tail(node: ast.FunctionDef) -> list[str]:
            first = next(
                i
                for i, item in enumerate(node.body)
                if isinstance(item, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "deposit" for t in item.targets)
            )
            return [dump(item) for item in node.body[first:]]

        check(
            payment_tail(old_parse) == payment_tail(new_parse),
            "Avito payment/description extraction unchanged",
        )

        class PhotoOnly(ast.NodeTransformer):
            def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
                return None if node.module == "photos" else node

            def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
                if node.name in photo_functions:
                    node.body = [ast.Pass()]
                return self.generic_visit(node)

        check(
            dump(PhotoOnly().visit(ast.parse(before))) == dump(PhotoOnly().visit(ast.parse(after))),
            "Avito differences limited to photo selection and its helper import",
        )
    check(
        all(original(p) == current(p) for p in protected),
        "other collectors, pipeline, scoring, models, filter intervals/matching, profiles unchanged"
        if options.photo_fixes
        else "collectors, pipeline, scoring, models, filter intervals/matching, profiles unchanged",
    )

    def defaults(source: str) -> dict[str, str]:
        cls = next(
            n
            for n in ast.parse(source).body
            if isinstance(n, ast.ClassDef) and n.name == "Settings"
        )
        return {
            n.target.id: dump(n.value)
            for n in cls.body
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value
        }

    old, new = (
        defaults(source)
        for source in (original("src/rentradar/config.py"), current("src/rentradar/config.py"))
    )
    commercial = {
        "sub_price_rub",
        "sub_days",
        "referral_days",
        "referral_activation_days",
        "free_sends_limit",
        "channel_bonus_days",
    }
    check(
        all(new.get(k) == v for k, v in old.items() if k not in commercial),
        "all protected Settings defaults, including dispatch caps/freshness, unchanged",
    )

    def env_values(source: str) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in source.splitlines()
            if line.startswith("RENTRADAR_") and "=" in line
        )

    old_env, new_env = (
        env_values(source) for source in (original(".env.example"), current(".env.example"))
    )
    obsolete = {"RENTRADAR_" + name.upper() for name in commercial}
    obsolete.add("RENTRADAR_YOOKASSA_PROVIDER_TOKEN")
    check(
        all(new_env.get(k) == v for k, v in old_env.items() if k not in obsolete),
        "protected .env.example settings unchanged",
    )

    cli = "src/rentradar/cli.py"
    before, after = original(cli), current(cli)
    start = "    scheduler = AsyncIOScheduler()"
    end = "    try:\n        if bot is not None:"
    check(
        before[before.index(start) : before.index(end, before.index(start))]
        == after[after.index(start) : after.index(end, after.index(start))],
        "scheduler configuration, schedules and startup_jobs order unchanged",
    )
    old_f, new_f = functions(before), functions(after)
    for name in (
        "collect_job",
        "public_job",
        "personal_job",
        "vision_job",
        "lifecycle_job",
        "startup_jobs",
        "_build_collectors",
        "_run_cycle",
        "_build_pipeline",
    ):
        check(old_f[name] == new_f[name], f"{name} body unchanged")
    old_calls = [
        dump(n)
        for n in ast.walk(ast.parse(before))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_task"
    ]
    new_calls = [
        dump(n)
        for n in ast.walk(ast.parse(after))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_task"
    ]
    check(old_calls == new_calls, "background task dispatch unchanged")

    repo = "src/rentradar/storage/repository.py"
    old_f, new_f = functions(original(repo)), functions(current(repo))
    check(
        all(
            new_f.get(k) == v
            for k, v in old_f.items()
            if k not in {"init", "_migrate_add_missing_columns"}
        ),
        "listing storage/enrichment logic unchanged",
    )
    print("Protected behavior matches", REF)


if __name__ == "__main__":
    main()
