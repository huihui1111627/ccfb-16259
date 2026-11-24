from __future__ import annotations

import json
from collections import Counter
from typing import Dict, List

from .quarantine import QuarantineManager
from .selector import SelectionEngine
from .stats import detect_flakes, evaluate_detection, _beta_upper


def _is_last_commit_of_day(conn, seq: int, sha: str) -> bool:
    row = conn.execute("SELECT created_at FROM commits WHERE sha=?", (sha,)).fetchone()
    day = row["created_at"][:10]
    nxt = conn.execute(
        "SELECT created_at FROM commits WHERE seq=? ", (seq + 1,)
    ).fetchone()
    return nxt is None or nxt["created_at"][:10] != day


def replay_history(conn, cfg) -> dict:
    conn.execute("DELETE FROM selection_snapshots")
    conn.execute("DELETE FROM selection_items")
    conn.execute("DELETE FROM quarantine_events")
    conn.execute("DELETE FROM flake_findings")
    conn.commit()

    commits = conn.execute("SELECT * FROM commits ORDER BY seq").fetchall()
    n_commits = len(commits)
    engine = SelectionEngine(conn, cfg)
    quarantine = QuarantineManager(conn)

    baseline_wall = (
        conn.execute("SELECT SUM(base_duration) s FROM tests").fetchone()["s"]
        / max(1, cfg.parallelism) + cfg.setup_wall_sec
    )

    safe_totals = Counter()
    fast_totals = Counter()
    predicted_missed_sum = 0.0
    rolling_new = 0
    rolling_missed_new = 0
    daily_nightlies = set()

    for commit in commits:
        seq = commit["seq"]
        mandatory = quarantine.monitored_ids

        prior_point_estimate = (rolling_missed_new + 0.5) / (rolling_new + 1.0)
        prior_upper95 = _beta_upper(rolling_missed_new, rolling_new)

        safe = engine.select(
            commit, seq, mandatory_ids=mandatory, tier="safe", persist_items=True
        )
        fast = engine.select(
            commit, seq, mandatory_ids=mandatory, tier="fast", persist_items=False
        )

        outcome_rows = conn.execute(
            """
            SELECT test_id, category_gt, incident_id
            FROM test_observations
            WHERE seq=? AND attempt=0 AND status='fail'
            """,
            (seq,),
        ).fetchall()
        failed_ids = [r["test_id"] for r in outcome_rows]
        true_rows = [r for r in outcome_rows if r["category_gt"] == "broken"]
        true_ids = [r["test_id"] for r in true_rows]
        flaky_failures = sum(1 for r in outcome_rows if (r["category_gt"] or "").startswith("flaky:"))

        new_true_ids = []
        for r in true_rows:
            intro = conn.execute(
                "SELECT introduced_seq FROM incidents WHERE id=?", (r["incident_id"],)
            ).fetchone()
            if intro and intro["introduced_seq"] == seq:
                new_true_ids.append(r["test_id"])

        safe_set = set(safe["selected_ids"])
        fast_set = set(fast["selected_ids"])
        missed_true = [tid for tid in true_ids if tid not in safe_set]
        missed_new = [tid for tid in new_true_ids if tid not in safe_set]
        fast_missed_true = [tid for tid in true_ids if tid not in fast_set]
        fast_missed_new = [tid for tid in new_true_ids if tid not in fast_set]

        nightly = 1 if _is_last_commit_of_day(conn, seq, commit["sha"]) else 0
        if nightly:
            daily_nightlies.add(commit["created_at"][:10])

        conn.execute(
            """
            INSERT INTO selection_snapshots(
                sha, seq, tier, selected_count, total_count, skipped_count,
                quarantined_count, selected_duration_sec, baseline_duration_sec,
                estimated_missed_failures, estimated_miss_rate, miss_rate_upper95,
                true_failures, selected_true_failures, missed_true_failures,
                new_true_failures, missed_new_true_failures, flaky_failures,
                missed_test_ids_json, nightly_full)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                commit["sha"], seq, "safe", safe["selected_count"], cfg.n_tests,
                safe["skipped_count"], len(mandatory), safe["selected_duration_sec"],
                baseline_wall, safe["predicted_missed"], prior_point_estimate,
                prior_upper95, len(true_ids), len(true_ids) - len(missed_true),
                len(missed_true), len(new_true_ids), len(missed_new), flaky_failures,
                json.dumps(missed_true), nightly,
            ),
        )

        safe_totals["selected_count"] += safe["selected_count"]
        safe_totals["duration_sec"] += safe["selected_duration_sec"]
        safe_totals["true_failures"] += len(true_ids)
        safe_totals["missed_true"] += len(missed_true)
        safe_totals["new_true"] += len(new_true_ids)
        safe_totals["missed_new"] += len(missed_new)
        safe_totals["flaky_failures"] += flaky_failures
        safe_totals["quarantined"] += len(mandatory)

        fast_totals["selected_count"] += fast["selected_count"]
        fast_totals["duration_sec"] += fast["selected_duration_sec"]
        fast_totals["true_failures"] += len(true_ids)
        fast_totals["missed_true"] += len(fast_missed_true)
        fast_totals["new_true"] += len(new_true_ids)
        fast_totals["missed_new"] += len(fast_missed_new)

        predicted_missed_sum += safe["predicted_missed"]

        # Quarantine consumes the counterfactual full-suite result. In the
        # deployed architecture this feedback arrives from the nightly run.
        quarantine.commit_outcomes(seq, commit["created_at"], commit["sha"])
        engine.observe(
            seq,
            safe["buckets"],
            failed_ids,
            predicted_missed=safe["predicted_missed"],
            actual_missed_true_new=len(missed_new),
        )

        rolling_new += len(new_true_ids)
        rolling_missed_new += len(missed_new)

        if seq % 50 == 0:
            conn.commit()
            print(f"  replayed {seq}/{n_commits} commits")

    conn.commit()
    detection = detect_flakes(conn, status_override=quarantine.status)
    accuracy = evaluate_detection(conn)

    nights = cfg.days  # scheduled every calendar night, including weekends.
    avg_safe_selected = safe_totals["selected_count"] / n_commits
    avg_safe_duration = safe_totals["duration_sec"] / n_commits
    avg_fast_selected = fast_totals["selected_count"] / n_commits
    avg_fast_duration = fast_totals["duration_sec"] / n_commits

    amortized_tests = (safe_totals["selected_count"] + nights * cfg.n_tests) / n_commits
    amortized_duration = (safe_totals["duration_sec"] + nights * baseline_wall) / n_commits

    q_events = conn.execute("SELECT event_type, COUNT(*) c FROM quarantine_events GROUP BY event_type").fetchall()
    q_counts = {r["event_type"]: r["c"] for r in q_events}
    current_status = conn.execute(
        "SELECT status, COUNT(*) c FROM flake_findings GROUP BY status"
    ).fetchall()
    status_counts = {r["status"]: r["c"] for r in current_status}

    truth_causes = conn.execute(
        "SELECT archetype_gt, COUNT(*) c FROM tests WHERE truth_flaky=1 GROUP BY archetype_gt"
    ).fetchall()
    cause_distribution = {r["archetype_gt"]: r["c"] for r in truth_causes}

    metrics = {
        "scale": {
            "tests": cfg.n_tests,
            "days": cfg.days,
            "commits": n_commits,
            "modules": conn.execute("SELECT COUNT(*) c FROM modules").fetchone()["c"],
            "coverage_edges": conn.execute("SELECT COUNT(*) c FROM coverage").fetchone()["c"],
            "observed_coverage_edges": conn.execute(
                "SELECT COUNT(*) c FROM coverage WHERE observed=1"
            ).fetchone()["c"],
            "baseline_wall_sec": baseline_wall,
            "nightly_full_runs": nights,
        },
        "safe": {
            "avg_selected": avg_safe_selected,
            "avg_skipped": cfg.n_tests - avg_safe_selected,
            "skip_ratio": 1 - avg_safe_selected / cfg.n_tests,
            "avg_duration_sec": avg_safe_duration,
            "duration_reduction": 1 - avg_safe_duration / baseline_wall,
            "all_true_failures": safe_totals["true_failures"],
            "missed_true_failures": safe_totals["missed_true"],
            "true_failure_miss_rate": safe_totals["missed_true"] / max(1, safe_totals["true_failures"]),
            "new_true_failures": safe_totals["new_true"],
            "missed_new_true_failures": safe_totals["missed_new"],
            "new_failure_miss_rate": safe_totals["missed_new"] / max(1, safe_totals["new_true"]),
            "avg_predicted_missed_per_commit": predicted_missed_sum / n_commits,
            "flaky_failures_seen": safe_totals["flaky_failures"],
        },
        "fast": {
            "avg_selected": avg_fast_selected,
            "skip_ratio": 1 - avg_fast_selected / cfg.n_tests,
            "avg_duration_sec": avg_fast_duration,
            "duration_reduction": 1 - avg_fast_duration / baseline_wall,
            "true_failure_miss_rate": fast_totals["missed_true"] / max(1, fast_totals["true_failures"]),
            "new_failure_miss_rate": fast_totals["missed_new"] / max(1, fast_totals["new_true"]),
            "missed_new": fast_totals["missed_new"],
            "new_true": fast_totals["new_true"],
        },
        "nightly_amortized": {
            "avg_tests_per_commit": amortized_tests,
            "test_reduction": 1 - amortized_tests / cfg.n_tests,
            "avg_duration_sec_per_commit": amortized_duration,
            "duration_reduction": 1 - amortized_duration / baseline_wall,
            "note": "将每天一次全量兜底成本摊到当天提交后的每次平均",
        },
        "detection": detection,
        "accuracy": accuracy,
        "quarantine_events": q_counts,
        "current_status": status_counts,
        "cause_distribution_truth": cause_distribution,
    }

    cfg.ensure_dirs()
    with cfg.db_path.parent.joinpath("metrics.json").open("w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('replay_completed', '1')")
    conn.commit()
    return metrics
