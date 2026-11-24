from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from .config import DemoConfig, DB_PATH, METRICS_PATH, REPORT_PATH
from .db import connect, init_db, reset_database
from .history import generate_history
from .report import write_report
from .simulator import replay_history
from .synthetic_repo import generate_dependencies, generate_modules, generate_tests_and_coverage


def generate_demo(cfg: DemoConfig) -> None:
    cfg.ensure_dirs()
    print(f"Generating synthetic monorepo and six months of history in {cfg.db_path} ...")
    reset_database(cfg.db_path)
    conn = connect(cfg.db_path)
    init_db(conn)
    rng = random.Random(cfg.seed)

    key_to_id = generate_modules(conn, rng)
    adjacency = generate_dependencies(conn, rng, key_to_id)
    generate_tests_and_coverage(conn, cfg, rng, adjacency)
    generate_history(conn, cfg, rng)

    counts = {
        "modules": conn.execute("SELECT COUNT(*) c FROM modules").fetchone()["c"],
        "tests": conn.execute("SELECT COUNT(*) c FROM tests").fetchone()["c"],
        "commits": conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"],
        "observations": conn.execute("SELECT COUNT(*) c FROM test_observations").fetchone()["c"],
        "incidents": conn.execute("SELECT COUNT(*) c FROM incidents").fetchone()["c"],
    }
    print("Generated:", json.dumps(counts, ensure_ascii=False))
    conn.close()


def replay_demo(cfg: DemoConfig) -> dict:
    conn = connect(cfg.db_path)
    try:
        metrics = replay_history(conn, cfg)
    finally:
        conn.close()
    write_report(metrics, METRICS_PATH, REPORT_PATH)
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report_summary(metrics)
    print(f"\nMarkdown report: {REPORT_PATH}")
    print(f"Metrics JSON:    {METRICS_PATH}")
    return metrics


def print_report_summary(metrics: dict) -> None:
    safe = metrics["safe"]
    acc = metrics["accuracy"]
    print("\n=== Replay summary ===")
    print(f"safe avg selected : {safe['avg_selected']:,.0f} / {metrics['scale']['tests']:,} "
          f"({safe['skip_ratio'] * 100:.1f}% skipped)")
    print(f"safe feedback time: {safe['avg_duration_sec'] / 60:.1f} min "
          f"({safe['duration_reduction'] * 100:.1f}% faster)")
    print(f"new-break miss    : {safe['missed_new_true_failures']}/"
          f"{safe['new_true_failures']} = {safe['new_failure_miss_rate'] * 100:.2f}%")
    print(f"flake P/R/F1      : {acc['precision'] * 100:.1f}% / "
          f"{acc['recall'] * 100:.1f}% / {acc['f1']:.3f}")
    print(f"broken masked     : {acc['wrong_masked_bad_pairs']} bad-commit test pairs")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ci_optimizer", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--tests", type=int, default=20_400)
        sp.add_argument("--days", type=int, default=184)
        sp.add_argument("--seed", type=int, default=20261125)
        sp.add_argument("--db", type=Path, default=DB_PATH)
        sp.add_argument("--no-materialize", action="store_true",
                        help="do not write generated_repo source/test stubs")

    sp_gen = sub.add_parser("generate", help="generate synthetic repo and CI history")
    add_common(sp_gen)

    sp_replay = sub.add_parser("replay", help="replay selection and quarantine over history")
    add_common(sp_replay)

    sp_all = sub.add_parser("demo", help="generate, replay, and write report")
    add_common(sp_all)
    sp_all.add_argument("--serve", action="store_true", help="start dashboard after demo")
    sp_all.add_argument("--port", type=int, default=8765)

    sub.add_parser("report", help="render data/report.md from data/metrics.json")

    sp_serve = sub.add_parser("serve", help="serve the dashboard and JSON API")
    sp_serve.add_argument("--host", default="127.0.0.1")
    sp_serve.add_argument("--port", type=int, default=8765)
    sp_serve.add_argument("--db", type=Path, default=DB_PATH)
    return p


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .web import serve
        serve(host=args.host, port=args.port, db_path=args.db)
        return

    if args.command == "report":
        write_report()
        print(REPORT_PATH)
        return

    cfg = DemoConfig(
        seed=getattr(args, "seed", 20261125),
        n_tests=getattr(args, "tests", 20_400),
        days=getattr(args, "days", 184),
        db_path=getattr(args, "db", DB_PATH),
        materialize_repo=not getattr(args, "no_materialize", False),
    )

    if args.command in ("generate", "demo"):
        generate_demo(cfg)
    if args.command in ("replay", "demo"):
        replay_demo(cfg)
    if args.command == "demo" and args.serve:
        from .web import serve
        serve(host="127.0.0.1", port=args.port, db_path=cfg.db_path)


if __name__ == "__main__":
    main()
