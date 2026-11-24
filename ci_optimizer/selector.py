from __future__ import annotations

import json
import heapq
import math
from collections import defaultdict, deque
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .synthetic_repo import load_graph


class SelectionEngine:
    """Point-in-time predictive test selection.

    The engine is intentionally stateful: co-failure counts only contain runs
    that occurred before the commit currently being scored.
    """

    def __init__(self, conn, cfg):
        self.conn = conn
        self.cfg = cfg
        self.n_tests = cfg.n_tests

        self.test_rows = {
            r["id"]: dict(r)
            for r in conn.execute("SELECT id, nodeid, domain, suite, base_duration, owner FROM tests")
        }
        self.duration_by_id = [
            r["base_duration"] for r in conn.execute("SELECT base_duration FROM tests ORDER BY id")
        ]
        self.module_rows = {
            r["id"]: dict(r) for r in conn.execute("SELECT id, path, domain, layer FROM modules")
        }
        self.adjacency, self.reverse = load_graph(conn)

        self.module_to_observed_tests: Dict[int, Set[int]] = defaultdict(set)
        self.test_to_observed_modules: Dict[int, Set[int]] = defaultdict(set)
        for r in conn.execute("SELECT test_id, module_id FROM coverage WHERE observed=1"):
            self.module_to_observed_tests[r["module_id"]].add(r["test_id"])
            self.test_to_observed_modules[r["test_id"]].add(r["module_id"])

        self.domain_to_tests: Dict[str, Set[int]] = defaultdict(set)
        for tid, row in self.test_rows.items():
            self.domain_to_tests[row["domain"]].add(tid)

        # Deterministic cross-domain smoke set: low-duration tests spread over
        # every domain and suite.
        self.smoke_ids: Set[int] = set()
        for domain, ids in self.domain_to_tests.items():
            ordered = sorted(ids, key=lambda tid: (self.duration_by_id[tid - 1], tid))
            self.smoke_ids.update(ordered[:5])
        self.smoke_ids = set(sorted(self.smoke_ids)[:180])

        # Online history state.
        self.bucket_counts: Dict[str, int] = defaultdict(int)
        self.bucket_failures: Dict[str, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.test_failures: Dict[int, int] = defaultdict(int)
        self.recent_failure_seq: Dict[int, int] = {}
        self.global_failure_observations = 0
        self.calibration_factor = 1.0
        self.recent_pred_actual = deque(maxlen=60)

    # ------------------------------------------------------------------
    # History updates
    # ------------------------------------------------------------------
    def observe(
        self,
        seq: int,
        buckets: Iterable[str],
        failed_test_ids: Iterable[int],
        predicted_missed: float = 0.0,
        actual_missed_true_new: Optional[int] = None,
    ) -> None:
        bucket_list = list(buckets)
        failed = list(failed_test_ids)
        for bucket in bucket_list:
            self.bucket_counts[bucket] += 1
        for tid in failed:
            self.test_failures[tid] += 1
            self.global_failure_observations += 1
            self.recent_failure_seq[tid] = seq
            for bucket in bucket_list:
                self.bucket_failures[bucket][tid] += 1

        if actual_missed_true_new is not None:
            self.recent_pred_actual.append((max(0.0, predicted_missed), actual_missed_true_new))
            pred = sum(p for p, _ in self.recent_pred_actual)
            actual = sum(a for _, a in self.recent_pred_actual)
            if pred > 0.01:
                # Shrink strongly toward 1 so one noisy commit cannot swing it.
                empirical = (actual + 0.25) / (pred + 0.25)
                self.calibration_factor = min(8.0, max(0.05, 0.85 * self.calibration_factor + 0.15 * empirical))

    def commit_buckets(self, commit) -> Tuple[List[str], Set[int], Set[str]]:
        sha = commit["sha"]
        rows = self.conn.execute(
            """
            SELECT cf.module_id, m.domain, m.layer
            FROM commit_files cf LEFT JOIN modules m ON m.id=cf.module_id
            WHERE cf.commit_sha=?
            """,
            (sha,),
        ).fetchall()
        module_ids = {r["module_id"] for r in rows if r["module_id"] is not None}
        domains = {r["domain"] for r in rows if r["domain"]}
        layers = {r["layer"] for r in rows if r["layer"]}
        buckets = [f"module:{mid}" for mid in module_ids]
        buckets += [f"domain:{d}" for d in domains]
        buckets += [f"layer:{x}" for x in layers]
        buckets += [f"kind:{commit['change_kind']}", f"size:{commit['size_bucket']}"]
        return buckets, module_ids, domains

    # ------------------------------------------------------------------
    # Risk model
    # ------------------------------------------------------------------
    def _base_prior(self, seq: int) -> float:
        exposures = max(1, seq * self.n_tests)
        return max(1e-7, self.global_failure_observations / exposures)

    def test_risk(
        self,
        tid: int,
        seq: int,
        buckets: List[str],
        changed_domains: Set[str],
        structural_bonus: float = 0.0,
    ) -> float:
        runs = max(1, seq - 1)
        prior = self._base_prior(seq)
        fails = self.test_failures.get(tid, 0)
        # Strong empirical-Bayes shrinkage toward the global failure rate.
        base = (fails + 40.0 * prior) / (runs + 40.0)

        cofail = prior
        for bucket in buckets:
            n = self.bucket_counts.get(bucket, 0)
            if n <= 0:
                continue
            f = self.bucket_failures.get(bucket, {}).get(tid, 0)
            # Bucket-specific prior is slightly stronger because cells are sparse.
            p_bucket = (f + 18.0 * prior) / (n + 18.0)
            # Maximum is robust to correlated module/domain buckets.
            cofail = max(cofail, p_bucket)

        recent = self.recent_failure_seq.get(tid)
        recent_bonus = 0.0
        if recent is not None and seq - recent <= 8:
            recent_bonus = 0.18 * (1.0 - (seq - recent) / 9.0)

        domain_bonus = 0.0012 if self.test_rows[tid]["domain"] in changed_domains else 0.00004
        risk = 0.58 * cofail + 0.22 * base + structural_bonus + recent_bonus + domain_bonus
        return max(1e-7, min(0.97, risk * self.calibration_factor))

    def residual_probability(
        self, tid: int, seq: int, buckets: List[str], changed_domains: Set[str]
    ) -> float:
        """Estimated chance this unselected test is a *new* regression."""
        runs = max(1, seq - 1)
        fails = self.test_failures.get(tid, 0)
        # For residual selection we care about unseen change-induced breakage,
        # not the ordinary failure base rate. Use a tight stable prior.
        if fails >= 2:
            prior = max(2e-5, self.global_failure_observations / max(1, seq * self.n_tests))
            base = (fails + 8.0 * prior) / (runs + 8.0)
        else:
            base = (fails + 0.004) / (runs + 120.0)

        cofail = 0.0
        for bucket in buckets:
            n = self.bucket_counts.get(bucket, 0)
            f = self.bucket_failures.get(bucket, {}).get(tid, 0)
            if n:
                if f > 0:
                    # Sparse Beta posterior once an actual co-failure exists.
                    p = (f + 0.05) / (n + 8.0)
                else:
                    # Zero co-failures must remain a very small tail risk.
                    p = 0.0005 / (n + 80.0)
                cofail = max(cofail, p)

        same_domain = self.test_rows[tid]["domain"] in changed_domains
        structural = 0.00025 if same_domain else 0.000004
        p = structural + 0.35 * cofail + 0.12 * base
        return max(1e-8, min(0.2, p * self.calibration_factor))

    # ------------------------------------------------------------------
    # Graph expansion
    # ------------------------------------------------------------------
    def _reverse_closure(self, module_ids: Set[int], max_depth: Optional[int]) -> Dict[int, int]:
        distances: Dict[int, int] = {mid: 0 for mid in module_ids}
        queue = deque((mid, 0) for mid in module_ids)
        while queue:
            mid, depth = queue.popleft()
            if max_depth is not None and depth >= max_depth:
                continue
            for downstream in self.reverse.get(mid, []):
                nd = depth + 1
                if downstream not in distances or nd < distances[downstream]:
                    distances[downstream] = nd
                    queue.append((downstream, nd))
        return distances

    def select(
        self,
        commit: dict,
        seq: int,
        mandatory_ids: Optional[Set[int]] = None,
        tier: str = "safe",
        persist_items: bool = False,
    ) -> dict:
        mandatory_ids = set(mandatory_ids or set())
        buckets, changed_modules, changed_domains = self.commit_buckets(commit)

        reasons: Dict[int, Dict[str, str]] = defaultdict(dict)
        selected: Set[int] = set()

        def add(tid: int, code: str, detail: str) -> None:
            selected.add(tid)
            # Keep the strongest/most specific detail for each reason code.
            reasons[tid].setdefault(code, detail)

        # 1. Historical file-level coverage of changed modules.
        for mid in changed_modules:
            m = self.module_rows[mid]
            tests = self.module_to_observed_tests.get(mid, set())
            for tid in tests:
                add(tid, "coverage", f"历史覆盖率记录包含 {m['path']}")

        # 2. Dependency graph: downstream modules that import changed code.
        max_depth = 1 if tier == "fast" else None
        affected = self._reverse_closure(changed_modules, max_depth)
        for mid, distance in affected.items():
            if distance == 0:
                continue
            m = self.module_rows[mid]
            code = "dependency_d1" if distance == 1 else "dependency_graph"
            detail = f"反向依赖距离 {distance}: 下游 {m['path']}"
            for tid in self.module_to_observed_tests.get(mid, set()):
                add(tid, code, detail)

        # 3. Safe tier adds the whole changed domain and broad smoke tests.
        if tier == "safe":
            for domain in changed_domains:
                for tid in self.domain_to_tests.get(domain, set()):
                    add(tid, "domain_safety_net", f"同域兜底: {domain}")
            for tid in self.smoke_ids:
                add(tid, "smoke", "跨域冒烟测试")
        else:
            # Fast tier gets a smaller smoke set.
            for tid in list(sorted(self.smoke_ids))[:60]:
                add(tid, "smoke", "精简冒烟测试")

        # 4. Recently failing and quarantined tests always execute.
        for tid, last_seq in self.recent_failure_seq.items():
            if seq - last_seq <= 7:
                add(tid, "recent_failure", f"最近第 {last_seq} 次提交失败，保留回归验证")
        for tid in mandatory_ids:
            status = "隔离测试继续记录，失败不阻塞合并"
            add(tid, "quarantined_monitor", status)

        # 5. Historical co-failure candidates, even without graph/coverage link.
        cofail_candidates: Set[int] = set()
        for bucket in buckets:
            for tid, count in self.bucket_failures.get(bucket, {}).items():
                n = self.bucket_counts.get(bucket, 1)
                if count >= 2 or (count >= 1 and n <= 12):
                    cofail_candidates.add(tid)
        for tid in cofail_candidates:
            best_rate = 0.0
            best_bucket = ""
            for bucket in buckets:
                n = self.bucket_counts.get(bucket, 0)
                f = self.bucket_failures.get(bucket, {}).get(tid, 0)
                if n:
                    rate = (f + 18.0 * self._base_prior(seq)) / (n + 18.0)
                    if rate > best_rate:
                        best_rate, best_bucket = rate, bucket
            if best_rate > 0:
                add(tid, "cofailure", f"历史上在 {best_bucket} 类改动中共同失败，后验概率 {best_rate:.1%}")

        # Rank current union and add a global high-risk pocket. Ranking is not
        # merely cosmetic: the first jobs give the fastest failure signal.
        scored: List[Tuple[int, float]] = []
        for tid in selected:
            structural = 0.01 if reasons[tid].get("coverage") else 0.0
            risk = self.test_risk(tid, seq, buckets, changed_domains, structural)
            scored.append((tid, risk))

        if tier == "safe":
            risk_pocket = 350
        else:
            risk_pocket = 220
        pocket: List[Tuple[float, int]] = []
        selected_copy = set(selected)
        for tid in range(1, self.n_tests + 1):
            if tid in selected_copy:
                continue
            r = self.test_risk(tid, seq, buckets, changed_domains)
            if len(pocket) < risk_pocket:
                heapq.heappush(pocket, (r, tid))
            elif r > pocket[0][0]:
                heapq.heapreplace(pocket, (r, tid))
        for r, tid in pocket:
            add(tid, "failure_prediction", f"跨模块失败风险模型 Top 候选，风险分 {r * 100:.1f}")
            selected.add(tid)
            scored.append((tid, r))

        # 6. Risk-budget expansion for the safe tier. The estimate is updated
        # online from nightly full-suite feedback.
        residual_scores: List[Tuple[float, int]] = []
        predicted_missed = 0.0
        for tid in range(1, self.n_tests + 1):
            if tid in selected:
                p = 0.0
            else:
                p = self.residual_probability(tid, seq, buckets, changed_domains)
                predicted_missed += p
                residual_scores.append((p, tid))

        target_miss_rate = 0.018 if tier == "safe" else 0.06
        expected_total = max(0.01, predicted_missed + sum(r for _, r in scored))
        estimated_miss_rate = predicted_missed / expected_total

        expanded = 0
        if tier == "safe" and estimated_miss_rate > target_miss_rate and len(selected) < 9000:
            residual_scores.sort(reverse=True)
            for p, tid in residual_scores:
                if estimated_miss_rate <= target_miss_rate or expanded >= 900:
                    break
                add(tid, "risk_budget", f"残余漏检风险 {p:.5f}，安全档位追加")
                selected.add(tid)
                scored.append((tid, self.test_risk(tid, seq, buckets, changed_domains)))
                predicted_missed = max(0.0, predicted_missed - p)
                expected_total = max(0.01, predicted_missed + sum(r for _, r in scored))
                estimated_miss_rate = predicted_missed / expected_total
                expanded += 1

        # Final ranking.
        final_scored = []
        score_map = dict(scored)
        for tid in selected:
            score = score_map.get(tid)
            if score is None:
                score = self.test_risk(tid, seq, buckets, changed_domains)
            # Mandatory monitoring tests should be visible but not necessarily
            # jump ahead of predicted blockers; keep their model score.
            final_scored.append((tid, score))
        final_scored.sort(key=lambda x: x[1], reverse=True)

        selected_duration = sum(self.duration_by_id[tid - 1] for tid, _ in final_scored)
        baseline_duration = sum(self.duration_by_id) / max(1, self.cfg.parallelism) + self.cfg.setup_wall_sec
        selected_wall = self.cfg.selected_setup_wall_sec + selected_duration / max(1, self.cfg.parallelism)

        items = []
        if persist_items:
            for tid, score in final_scored:
                code_list = sorted(reasons[tid].keys())
                items.append((
                    commit["sha"], tid, round(score * 100.0, 3),
                    ",".join(code_list),
                ))
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO selection_items
                    (sha, test_id, score, reason_codes)
                VALUES (?,?,?,?)
                """,
                items,
            )
            self.conn.commit()

        return {
            "sha": commit["sha"],
            "seq": seq,
            "tier": tier,
            "selected_ids": [tid for tid, _ in final_scored],
            "scores": dict(final_scored),
            "reasons": {tid: reasons[tid] for tid in selected},
            "selected_count": len(selected),
            "skipped_count": self.n_tests - len(selected),
            "selected_duration_sec": selected_wall,
            "selected_test_duration_sec": selected_duration,
            "baseline_duration_sec": baseline_duration,
            "predicted_missed": predicted_missed,
            "estimated_miss_rate": estimated_miss_rate,
            "buckets": buckets,
        }
