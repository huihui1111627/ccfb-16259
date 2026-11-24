from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple


CAUSE_LABELS = {
    "order_dependence": "执行顺序依赖",
    "concurrency": "并发/竞态",
    "time_or_timeout": "时间/超时/跨日跨月",
    "shared_state": "共享状态/资源泄漏",
    "external_dependency": "网络/外部依赖",
}


def _beta_upper(k: int, n: int, z: float = 1.96) -> float:
    """Wilson upper bound for a Binomial proportion."""
    if n == 0:
        return 1.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return min(1.0, center + margin)


def find_boundary(fail_seqs: List[int], n_commits: int) -> Optional[dict]:
    """Find a persistent failure boundary using only point-in-time outcomes."""
    if len(fail_seqs) < 6:
        return None
    fail_set = set(fail_seqs)
    best = None
    sorted_fails = sorted(fail_seqs)
    for b in sorted_fails:
        pre_n = max(0, b - 1)
        pre_f = sum(1 for x in sorted_fails if x < b)
        post_n = n_commits - b + 1
        post_f = len(sorted_fails) - pre_f
        if post_n < 8 or post_f < 7:
            continue
        post_rate = post_f / post_n
        pre_rate = pre_f / pre_n if pre_n else 0.0
        rate_jump = post_rate - pre_rate
        if post_rate < 0.78 or rate_jump < 0.32 or pre_rate > 0.5:
            continue
        # Prefer high post recall and a large post failure count.
        score = post_rate * 2 + min(post_f, 30) / 30 + rate_jump - pre_rate * 0.2
        candidate = {
            "boundary_seq": b,
            "post_rate": round(post_rate, 3),
            "pre_rate": round(pre_rate, 3),
            "rate_jump": round(rate_jump, 3),
            "pre_failures": pre_f,
            "score": score,
        }
        if best is None or score > best["score"]:
            best = candidate
    return best


def find_deterministic_episodes(fail_seqs: List[int]) -> List[dict]:
    """Find dense failure clusters caused by a bad commit, even if later fixed."""
    if not fail_seqs:
        return []
    ordered = sorted(set(fail_seqs))
    clusters = []
    current = [ordered[0]]
    for seq in ordered[1:]:
        if seq - current[-1] <= 4:
            current.append(seq)
        else:
            clusters.append(current)
            current = [seq]
    clusters.append(current)

    episodes = []
    for cluster in clusters:
        span = cluster[-1] - cluster[0] + 1
        density = len(cluster) / span
        if len(cluster) >= 6 and density >= 0.72:
            episodes.append({
                "start_seq": cluster[0],
                "end_seq": cluster[-1],
                "failures": len(cluster),
                "span": span,
                "density": round(density, 3),
            })
    episodes.sort(key=lambda e: (e["failures"], e["density"]), reverse=True)
    return episodes


def classify_causes(conn, test_id: int, fail_count: int) -> List[dict]:
    causes = []
    if fail_count == 0:
        return causes

    top_predecessor = conn.execute(
        """
        SELECT preceding_test_id, COUNT(*) AS c
        FROM test_observations
        WHERE test_id=? AND attempt=0 AND status='fail'
          AND preceding_test_id IS NOT NULL
        GROUP BY preceding_test_id
        ORDER BY c DESC LIMIT 1
        """,
        (test_id,),
    ).fetchone()
    top_share = (top_predecessor["c"] / fail_count) if top_predecessor else 0.0

    state_rows = conn.execute(
        """
        SELECT COUNT(*) c FROM test_observations
        WHERE test_id=? AND attempt=0 AND status='fail'
          AND (message LIKE '%state leaked%' OR message LIKE '%fixture% state%'
               OR message LIKE '%cache%' OR message LIKE '%unique constraint%')
        """,
        (test_id,),
    ).fetchone()["c"]
    state_share = state_rows / fail_count

    if top_predecessor and top_predecessor["c"] >= 3 and top_share >= 0.45:
        order_conf = min(0.96, 0.45 + top_share * 0.45 + top_predecessor["c"] / max(20, fail_count))
        causes.append({
            "code": "order_dependence",
            "label": CAUSE_LABELS["order_dependence"],
            "confidence": round(order_conf, 2),
            "evidence": f"{top_predecessor['c']}/{fail_count} 次失败紧跟同一前置测试 #{top_predecessor['preceding_test_id']}",
        })

    if state_share >= 0.25:
        causes.append({
            "code": "shared_state",
            "label": CAUSE_LABELS["shared_state"],
            "confidence": round(min(0.93, 0.4 + state_share * 0.55), 2),
            "evidence": f"{state_rows}/{fail_count} 次错误包含唯一约束/缓存/夹具状态泄漏特征",
        })

    load_stats = conn.execute(
        """
        SELECT AVG(o.load) fail_load,
               (SELECT AVG(load) FROM ci_runs) all_load,
               SUM(CASE WHEN o.load > 0.72 OR o.parallelism >= 56 THEN 1 ELSE 0 END) high_load_fails
        FROM test_observations o
        WHERE o.test_id=? AND o.attempt=0 AND o.status='fail'
        """,
        (test_id,),
    ).fetchone()
    high_load_share = (load_stats["high_load_fails"] or 0) / fail_count
    load_delta = (load_stats["fail_load"] or 0) - (load_stats["all_load"] or 0.5)
    if high_load_share >= 0.55 and load_delta > 0.04:
        causes.append({
            "code": "concurrency",
            "label": CAUSE_LABELS["concurrency"],
            "confidence": round(min(0.94, 0.42 + high_load_share * 0.42 + load_delta), 2),
            "evidence": f"{high_load_share:.0%} 的失败发生在高负载/高并发分片，平均负载比全量运行高 {load_delta:.2f}",
        })

    time_stats = conn.execute(
        """
        SELECT
          SUM(CASE WHEN CAST(strftime('%H', o.created_at) AS INTEGER) >= 23
                    OR CAST(strftime('%H', o.created_at) AS INTEGER) <= 1
                    OR CAST(strftime('%d', o.created_at) AS INTEGER) >= 28
                   THEN 1 ELSE 0 END) boundary_fails,
          SUM(CASE WHEN o.error_type='TimeoutError' THEN 1 ELSE 0 END) timeout_fails
        FROM test_observations o
        WHERE o.test_id=? AND o.attempt=0 AND o.status='fail'
        """,
        (test_id,),
    ).fetchone()
    boundary_fails = time_stats["boundary_fails"] or 0
    timeout_fails = time_stats["timeout_fails"] or 0
    boundary_share = boundary_fails / fail_count
    # Rough calendar boundary share: 3 late hours + last ~3 days.
    boundary_base = 3 / 24 + 3 / 30 * 0.35
    time_score = 0.0
    evidence_parts = []
    if boundary_share > boundary_base * 1.5 and boundary_fails >= 3:
        time_score += 0.45
        evidence_parts.append(f"{boundary_fails}/{fail_count} 次发生在深夜/跨日/月末窗口")
    if timeout_fails / fail_count >= 0.35:
        time_score += 0.45
        evidence_parts.append(f"{timeout_fails}/{fail_count} 次为 TimeoutError")
    if time_score:
        causes.append({
            "code": "time_or_timeout",
            "label": CAUSE_LABELS["time_or_timeout"],
            "confidence": round(min(0.95, 0.35 + time_score), 2),
            "evidence": "；".join(evidence_parts),
        })

    external = conn.execute(
        """
        SELECT COUNT(*) c FROM test_observations
        WHERE test_id=? AND attempt=0 AND status='fail'
          AND error_type IN ('ConnectionError','ReadTimeout','ConnectTimeout')
        """,
        (test_id,),
    ).fetchone()["c"]
    external_share = external / fail_count
    if external_share >= 0.45:
        causes.append({
            "code": "external_dependency",
            "label": CAUSE_LABELS["external_dependency"],
            "confidence": round(min(0.9, 0.38 + external_share * 0.5), 2),
            "evidence": f"{external}/{fail_count} 次为网络/上游依赖错误，重试通常恢复",
        })

    causes.sort(key=lambda x: x["confidence"], reverse=True)
    return causes[:3]


def detect_flakes(conn, status_override: Optional[Dict[int, str]] = None) -> dict:
    n_commits = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]

    agg_rows = conn.execute(
        """
        SELECT test_id,
               SUM(CASE WHEN attempt=0 AND status='fail' THEN 1 ELSE 0 END) fail_count,
               SUM(CASE WHEN attempt>0 AND status='pass' THEN 1 ELSE 0 END) retry_pass_count,
               COUNT(DISTINCT CASE WHEN attempt=0 AND status='fail' THEN commit_sha END) failed_sha_count
        FROM test_observations
        GROUP BY test_id
        """
    ).fetchall()
    agg = {r["test_id"]: dict(r) for r in agg_rows}

    mixed_rows = conn.execute(
        """
        SELECT test_id, COUNT(*) AS c FROM (
            SELECT test_id, commit_sha
            FROM test_observations
            GROUP BY test_id, commit_sha
            HAVING SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END) > 0
               AND SUM(CASE WHEN status='pass' THEN 1 ELSE 0 END) > 0
        )
        GROUP BY test_id
        """
    ).fetchall()
    mixed = {r["test_id"]: r["c"] for r in mixed_rows}

    conn.execute("DELETE FROM flake_findings")
    detected_ids = set()
    classifications = Counter()

    for tid, a in agg.items():
        fail_count = int(a["fail_count"] or 0)
        if fail_count == 0:
            continue
        retry_pass = int(a["retry_pass_count"] or 0)
        mixed_count = int(mixed.get(tid, 0))

        fail_seq_rows = conn.execute(
            """
            SELECT DISTINCT seq FROM test_observations
            WHERE test_id=? AND attempt=0 AND status='fail' ORDER BY seq
            """,
            (tid,),
        ).fetchall()
        fail_seqs = [r["seq"] for r in fail_seq_rows]
        fail_set = set(fail_seqs)
        flip_count = 0
        previous = 1 in fail_set
        for seq in range(2, n_commits + 1):
            current = seq in fail_set
            if current != previous:
                flip_count += 1
            previous = current

        boundary = find_boundary(fail_seqs, n_commits)
        episodes = find_deterministic_episodes(fail_seqs)
        total_runs = n_commits
        pass_rate = 1 - fail_count / max(1, total_runs)

        scattered_span = (fail_seqs[-1] - fail_seqs[0]) >= 18 if len(fail_seqs) >= 4 else False
        strong_mixed = mixed_count >= 2 or retry_pass >= 3
        detected_flake = (
            strong_mixed
            or (fail_count >= 8 and mixed_count >= 1 and retry_pass >= 2)
            or (fail_count >= 9 and scattered_span and retry_pass >= 2)
        )

        pre_flaky = False
        if boundary:
            pre_flaky = boundary["pre_failures"] >= 3 and (
                boundary["pre_rate"] >= 0.018 or mixed_count >= 3 or retry_pass >= 3
            )

        dense_episode = episodes[0] if episodes else None
        episode_pre_flaky = False
        if dense_episode:
            before = [s for s in fail_seqs if s < dense_episode["start_seq"]]
            episode_pre_flaky = len(before) >= 3 and (mixed_count >= 3 or retry_pass >= 3)

        if boundary and boundary["post_rate"] >= 0.78:
            classification = "flaky_then_broken" if pre_flaky else "broken"
        elif dense_episode and episode_pre_flaky:
            classification = "flaky_then_broken"
            detected_flake = True
        elif dense_episode and not detected_flake:
            classification = "broken_episode"
        elif detected_flake:
            classification = "flaky"
        elif boundary or dense_episode:
            classification = "broken_episode"
        elif fail_count >= 10 and scattered_span and retry_pass >= 1:
            classification = "unstable_suspect"
            detected_flake = True
        else:
            classification = "stable"

        if classification in ("flaky", "flaky_then_broken", "unstable_suspect"):
            detected_ids.add(tid)

        causes = classify_causes(conn, tid, fail_count) if detected_flake or classification == "flaky_then_broken" else []
        primary_cause = causes[0]["code"] if causes else None

        score = round(min(0.99, (
            0.28 * min(1.0, mixed_count / 3.0)
            + 0.22 * min(1.0, flip_count / 12.0)
            + 0.22 * min(1.0, fail_count / max(8, n_commits * 0.12))
            + 0.16 * min(1.0, retry_pass / 5.0)
            + (0.12 if scattered_span else 0.0)
        )), 3)

        if status_override and tid in status_override:
            status = status_override[tid]
        elif classification == "flaky":
            status = "quarantined"
        elif classification == "flaky_then_broken":
            status = "broken_suspect"
        elif classification in ("broken", "broken_episode"):
            status = "broken_suspect"
        else:
            status = "healthy"

        effective_boundary_seq = boundary["boundary_seq"] if boundary else (
            dense_episode["start_seq"] if dense_episode else None
        )
        effective_post_rate = boundary["post_rate"] if boundary else (
            dense_episode["density"] if dense_episode else None
        )
        evidence = {
            "failed_sha_count": a["failed_sha_count"],
            "flip_count": flip_count,
            "scattered_span": scattered_span,
            "primary_cause": primary_cause,
            "deterministic_episodes": episodes[:3],
        }
        conn.execute(
            """
            INSERT INTO flake_findings(test_id, detected, flake_score, first_seq, last_seq,
                                       total_runs, fail_count, retry_pass_count,
                                       mixed_sha_count, flip_count, pass_rate,
                                       boundary_seq, post_boundary_fail_rate, classification,
                                       status, causes_json, evidence_json, updated_seq)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (tid, 1 if tid in detected_ids else 0, score,
             fail_seqs[0], fail_seqs[-1], total_runs, fail_count, retry_pass,
             mixed_count, flip_count, round(pass_rate, 4),
             effective_boundary_seq, effective_post_rate,
             classification, status,
             json.dumps(causes, ensure_ascii=False),
             json.dumps(evidence, ensure_ascii=False), n_commits),
        )
        classifications[classification] += 1

    conn.commit()
    return {
        "detected_count": len(detected_ids),
        "classifications": dict(classifications),
        "mixed_sha_tests": len(mixed),
    }


def evaluate_detection(conn) -> dict:
    truth_rows = conn.execute(
        "SELECT id, truth_flaky, archetype_gt, broken_seq, healed_seq FROM tests"
    ).fetchall()
    findings = {r["test_id"]: r for r in conn.execute("SELECT * FROM flake_findings")}

    tp = fp = fn = tn = 0
    cause_correct = cause_total = 0
    transition_total = transition_correct = 0
    fp_ids = []
    fn_ids = []

    for t in truth_rows:
        tid = t["id"]
        truth = bool(t["truth_flaky"])
        f = findings.get(tid)
        predicted = bool(f and f["detected"])
        if truth and predicted:
            tp += 1
        elif not truth and predicted:
            fp += 1
            if len(fp_ids) < 20:
                fp_ids.append(tid)
        elif truth and not predicted:
            fn += 1
            if len(fn_ids) < 20:
                fn_ids.append(tid)
        else:
            tn += 1

        if truth and t["archetype_gt"] and predicted and f:
            causes = json.loads(f["causes_json"])
            if causes:
                cause_total += 1
                if causes[0]["code"] == t["archetype_gt"]:
                    cause_correct += 1

        if t["broken_seq"] is not None:
            transition_total += 1
            if f and f["classification"] == "flaky_then_broken":
                transition_correct += 1

    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    f1 = 2 * precision * recall / max(1e-9, precision + recall)
    accuracy = (tp + tn) / max(1, len(truth_rows))

    # Episode-level masking metric. A true bad commit is masked only if the
    # manager quarantined an affected test before flagging deterministic break.
    wrong_masked = conn.execute(
        """
        SELECT COUNT(DISTINCT it.incident_id || ':' || it.test_id) AS c
        FROM incident_tests it
        JOIN incidents i ON i.id=it.incident_id AND i.kind='bad_commit'
        JOIN quarantine_events qe ON qe.test_id=it.test_id
        WHERE qe.event_type='quarantined'
          AND qe.seq >= i.introduced_seq
          AND (i.fixed_seq IS NULL OR qe.seq < i.fixed_seq)
          AND NOT EXISTS (
              SELECT 1 FROM quarantine_events qe2
              WHERE qe2.test_id=it.test_id
                AND qe2.event_type IN ('broken_detected','escalate_broken')
                AND qe2.seq <= qe.seq
                AND qe2.seq >= i.introduced_seq
          )
        """
    ).fetchone()["c"]

    bad_episode_test_pairs = conn.execute(
        """
        SELECT COUNT(*) c FROM incident_tests it
        JOIN incidents i ON i.id=it.incident_id
        WHERE i.kind='bad_commit'
        """
    ).fetchone()["c"]

    transition_masked = conn.execute(
        """
        SELECT COUNT(*) AS c FROM tests t
        WHERE t.broken_seq IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM quarantine_events qe
            WHERE qe.test_id=t.id
              AND qe.event_type IN ('escalate_broken', 'broken_detected')
              AND qe.seq BETWEEN t.broken_seq AND t.broken_seq + 12
          )
        """
    ).fetchone()["c"]

    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": accuracy,
        "false_positive_ids": fp_ids,
        "false_negative_ids": fn_ids,
        "cause_accuracy": cause_correct / max(1, cause_total),
        "cause_correct": cause_correct,
        "cause_total": cause_total,
        "transition_total": transition_total,
        "transition_correct": transition_correct,
        "wrong_masked_bad_pairs": wrong_masked,
        "bad_episode_test_pairs": bad_episode_test_pairs,
        "transition_not_escalated_12_runs": transition_masked,
    }
