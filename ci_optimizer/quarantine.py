from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple


class QuarantineManager:
    """Incremental flaky quarantine / self-healing registry."""

    def __init__(self, conn, green_release_streak: int = 40):
        self.conn = conn
        self.green_release_streak = green_release_streak

        self.status: Dict[int, str] = {}  # quarantined | broken_suspect | healthy
        self.quarantined_seq: Dict[int, int] = {}
        self.stats: Dict[int, dict] = defaultdict(lambda: {
            "runs": 0,
            "fail_count": 0,
            "retry_pass_count": 0,
            "mixed_sha_count": 0,
            "failure_seqs": [],
            "mixed_seqs": [],
            "fail_streak": 0,
            "green_streak": 0,
            "recent": deque(maxlen=12),
        })
        self.event_count = 0

    @property
    def monitored_ids(self) -> Set[int]:
        return {tid for tid, status in self.status.items()
                if status in ("quarantined", "broken_suspect")}

    def _emit(self, seq: int, created_at: str, sha: str, tid: int,
              event_type: str, reason: str, score: float, detail: dict) -> None:
        self.event_count += 1
        self.conn.execute(
            """
            INSERT INTO quarantine_events(seq, created_at, sha, test_id, event_type,
                                          reason, flake_score, detail_json)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (seq, created_at, sha, tid, event_type, reason, score,
             json.dumps(detail, ensure_ascii=False)),
        )

    def commit_outcomes(self, seq: int, created_at: str, sha: str) -> Dict[int, str]:
        rows = self.conn.execute(
            """
            SELECT test_id,
                   MAX(CASE WHEN attempt=0 AND status='fail' THEN 1 ELSE 0 END) AS initial_failed,
                   SUM(CASE WHEN attempt>0 AND status='pass' THEN 1 ELSE 0 END) AS retry_passes,
                   SUM(CASE WHEN attempt>0 AND status='fail' THEN 1 ELSE 0 END) AS retry_fails
            FROM test_observations
            WHERE seq=?
            GROUP BY test_id
            """,
            (seq,),
        ).fetchall()
        failed_info = {r["test_id"]: r for r in rows if r["initial_failed"]}
        failed_set = set(failed_info)

        # Update every test we have ever tracked. Tests with no failure are
        # implicit passes and do not need per-test rows for all 20k tests.
        tracked = set(self.stats.keys()) | failed_set
        for tid in tracked:
            st = self.stats[tid]
            st["runs"] += 1
            if tid in failed_set:
                info = failed_info[tid]
                st["fail_count"] += 1
                st["fail_streak"] += 1
                st["green_streak"] = 0
                st["recent"].append(1)
                st["failure_seqs"].append(seq)
                retry_passes = int(info["retry_passes"] or 0)
                if retry_passes:
                    st["retry_pass_count"] += retry_passes
                    st["mixed_sha_count"] += 1
                    st["mixed_seqs"].append(seq)
            else:
                st["fail_streak"] = 0
                st["green_streak"] += 1
                st["recent"].append(0)

        events: Dict[int, str] = {}

        # New flaky quarantine decisions.
        for tid in failed_set:
            st = self.stats[tid]
            if self.status.get(tid) in ("quarantined", "broken_suspect"):
                continue

            recent_fail_rate = sum(st["recent"]) / max(1, len(st["recent"]))
            scattered = self._scattered_failures(st["failure_seqs"])
            mixed_evidence = st["mixed_sha_count"] >= 1
            strong_mixed_evidence = st["mixed_sha_count"] >= 2 or st["retry_pass_count"] >= 3
            recent_mixed = [x for x in st["mixed_seqs"] if seq - x <= 45]
            recent_mixed_short = [x for x in st["mixed_seqs"] if seq - x <= 6]
            enough = (
                # Repeated same-SHA pass/fail evidence is the defining signal.
                st["mixed_sha_count"] >= 2 and len(recent_mixed) >= 1
            )
            first_fail = min(st["failure_seqs"]) if st["failure_seqs"] else seq
            onset_span = max(1, seq - first_fail + 1)
            onset_rate = st["fail_count"] / onset_span
            persistent_break = (
                st["fail_streak"] >= 6
                or (st["fail_streak"] >= 5 and not recent_mixed_short)
                or (st["fail_count"] >= 6 and onset_rate >= 0.86 and st["mixed_sha_count"] <= 2)
                or (len(st["recent"]) >= 10 and sum(st["recent"]) >= 9)
            )

            if persistent_break:
                self.status[tid] = "broken_suspect"
                score = self._score(st)
                self._emit(
                    seq, created_at, sha, tid, "broken_detected",
                    "失败从特定提交后持续出现，缺少同代码稳定翻转，疑似提交破坏",
                    score,
                    {
                        "fail_streak": st["fail_streak"],
                        "recent_fail_rate": recent_fail_rate,
                        "onset_rate": round(onset_rate, 3),
                        "mixed_sha_count": st["mixed_sha_count"],
                    },
                )
                events[tid] = "broken_detected"
            elif enough and st["fail_streak"] <= 3 and recent_fail_rate < 0.68 and onset_rate < 0.82:
                self.status[tid] = "quarantined"
                self.quarantined_seq[tid] = seq
                score = self._score(st)
                self._emit(
                    seq, created_at, sha, tid, "quarantined",
                    "同一提交/重跑中既成功又失败，或失败高度离散，判定为不稳定测试",
                    score,
                    {
                        "fail_count": st["fail_count"],
                        "mixed_sha_count": st["mixed_sha_count"],
                        "retry_pass_count": st["retry_pass_count"],
                        "scattered": scattered,
                    },
                )
                events[tid] = "quarantined"

        # State transitions for already quarantined / suspected tests.
        for tid, status in list(self.status.items()):
            st = self.stats[tid]
            recent_fail_rate = sum(st["recent"]) / max(1, len(st["recent"]))

            if status == "quarantined":
                recent_mixed_short = [x for x in st["mixed_seqs"] if seq - x <= 6]
                # A quarantined flake can reveal that it was actually a broken
                # commit. Persistence + change boundary overrides quarantine.
                if (
                    st["fail_streak"] >= 6
                    or (st["fail_streak"] >= 4 and not recent_mixed_short)
                    or (len(st["recent"]) >= 10 and sum(st["recent"]) >= 9)
                ):
                    self.status[tid] = "broken_suspect"
                    self._emit(
                        seq, created_at, sha, tid, "escalate_broken",
                        "隔离后出现连续确定性失败，改判为提交破坏并通知负责人/值班",
                        self._score(st),
                        {"fail_streak": st["fail_streak"], "recent_fail_rate": recent_fail_rate},
                    )
                    events[tid] = "escalate_broken"
                elif st["green_streak"] >= self.green_release_streak:
                    self.status[tid] = "healthy"
                    self._emit(
                        seq, created_at, sha, tid, "released",
                        f"连续 {st['green_streak']} 次主干/流水线运行通过，自动解除隔离",
                        self._score(st),
                        {"green_streak": st["green_streak"]},
                    )
                    events[tid] = "released"

            elif status == "broken_suspect":
                # A second deterministic regression can happen after an earlier
                # episode. Re-emit at a short, exact streak so metrics/triage see
                # this break window even while the status stays broken_suspect.
                if st["fail_streak"] == 4:
                    self._emit(
                        seq, created_at, sha, tid, "escalate_broken",
                        "疑似破坏状态下再次出现连续确定性失败，重新提醒并关联当前提交边界",
                        self._score(st),
                        {"fail_streak": st["fail_streak"], "recent_fail_rate": recent_fail_rate},
                    )
                    events[tid] = "escalate_broken"
                if st["green_streak"] >= 20:
                    self.status[tid] = "healthy"
                    self._emit(
                        seq, created_at, sha, tid, "broken_resolved",
                        f"疑似破坏已修复并连续 {st['green_streak']} 次通过，恢复正常选择",
                        self._score(st),
                        {"green_streak": st["green_streak"]},
                    )
                    events[tid] = "broken_resolved"

            elif status == "healthy":
                # A released test that flakes again is re-quarantined only on
                # repeated same-code inconsistency, not ordinary red failures.
                recent_mixed = [x for x in st["mixed_seqs"] if seq - x <= 20]
                if tid in failed_set and len(recent_mixed) >= 2 and st["fail_streak"] <= 3:
                    self.status[tid] = "quarantined"
                    self.quarantined_seq[tid] = seq
                    self._emit(
                        seq, created_at, sha, tid, "requarantined",
                        "自动放回后再次出现同代码翻转，重新隔离",
                        self._score(st),
                        {"fail_count": st["fail_count"]},
                    )
                    events[tid] = "requarantined"

        self.conn.commit()
        return events

    @staticmethod
    def _scattered_failures(seqs: List[int]) -> bool:
        if len(seqs) < 4:
            return False
        # Failure days/commits spread over at least 12 sequence positions,
        # rather than one contiguous bad-commit interval.
        return seqs[-1] - seqs[0] >= 12

    @staticmethod
    def _score(st: dict) -> float:
        runs = max(1, st["runs"])
        rate = st["fail_count"] / runs
        mixed = min(1.0, st["mixed_sha_count"] / 3.0)
        retry = min(1.0, st["retry_pass_count"] / 5.0)
        return round(min(0.99, 0.25 + 1.8 * rate + 0.3 * mixed + 0.2 * retry), 3)
