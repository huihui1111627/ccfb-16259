from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .synthetic_repo import DOMAINS

AUTHORS = [
    "alice.chen", "bob.martinez", "chi.patel", "diana.novak", "ethan.kim",
    "fatima.ali", "gabriel.silva", "hana.sato", "ivan.petrov", "julia.owusu",
    "kai.andersen", "li.wei", "mona.rahimi", "noah.williams",
]

COMMIT_VERBS = [
    "add", "fix", "refactor", "improve", "remove", "update", "polish",
    "optimize", "harden", "simplify",
]
COMMIT_TOPICS = [
    "retry bookkeeping", "invoice rounding", "reservation expiry",
    "permission caching", "event serialization", "batch export",
    "address validation", "webhook retries", "search indexing",
    "ledger reconciliation", "coupon stacking", "shipping split logic",
    "fraud feature payload", "notification templates", "audit pagination",
    "connection pool settings", "idempotency cleanup", "clock injection",
    "tenant query filters", "dead letter replay", "money allocation",
]

ARCHETYPES = [
    ("order_dependence", 0.28),
    ("concurrency", 0.24),
    ("time_or_timeout", 0.21),
    ("shared_state", 0.15),
    ("external_dependency", 0.12),
]

ERROR_BY_CAUSE = {
    "order_dependence": (
        "AssertionError",
        "duplicate key value violates unique constraint; state leaked from previous test",
    ),
    "concurrency": (
        "StaleDataError",
        "concurrent update detected: version token changed; possible race condition",
    ),
    "time_or_timeout": (
        "TimeoutError",
        "deadline exceeded after waiting for asynchronous condition",
    ),
    "shared_state": (
        "AssertionError",
        "expected isolated fixture but found rows/cache/queue state from another test",
    ),
    "external_dependency": (
        "ConnectionError",
        "upstream sandbox connection reset / flaky third-party response",
    ),
    "broken_logic": (
        "AssertionError",
        "expected computed result to match specification after commit",
    ),
    "broken_schema": (
        "ValidationError",
        "response payload is missing required field or has incompatible schema",
    ),
}


def _sha(seq: int, rng: random.Random) -> str:
    return hashlib.sha256(f"commerce-commit-{seq}-{rng.random()}".encode()).hexdigest()[:12]


def generate_commit_plan(cfg, rng: random.Random, conn) -> List[dict]:
    start = datetime.fromisoformat(cfg.start_date)
    commits: List[dict] = []
    seq = 0
    module_rows = conn.execute("SELECT id, domain, layer, path FROM modules").fetchall()
    by_domain: Dict[str, List[dict]] = defaultdict(list)
    platform_modules: List[dict] = []
    for m in module_rows:
        d = dict(m)
        if m["domain"] == "platform":
            platform_modules.append(d)
        else:
            by_domain[m["domain"]].append(d)

    for day in range(cfg.days):
        date = start + timedelta(days=day)
        weekday = date.weekday()
        if weekday < 5:
            n_today = rng.choices([1, 2, 3, 4, 5, 6], [8, 18, 27, 24, 15, 8])[0]
        else:
            n_today = rng.choices([0, 1, 2, 3], [42, 34, 18, 6])[0]

        for _ in range(n_today):
            seq += 1
            hour = rng.choices(range(8, 22), [2, 4, 8, 10, 12, 11, 9, 8, 7, 6, 4, 2, 1, 1])[0]
            minute = rng.randint(0, 59)
            ts = date.replace(hour=hour, minute=minute)

            roll = rng.random()
            if roll < 0.66:
                n_files = rng.randint(1, 4)
                size_bucket = "small"
            elif roll < 0.88:
                n_files = rng.randint(5, 14)
                size_bucket = "medium"
            elif roll < 0.97:
                n_files = rng.randint(15, 34)
                size_bucket = "large"
            else:
                n_files = rng.randint(25, 55)
                size_bucket = "extra_large"

            kind = rng.choices(
                ["feature", "bugfix", "refactor", "test", "config", "docs"],
                [0.37, 0.27, 0.15, 0.08, 0.07, 0.06],
            )[0]

            scenario = rng.random()
            chosen_modules: List[dict] = []
            if kind == "docs":
                chosen_modules = []
            elif scenario < 0.10:
                chosen_modules = rng.sample(platform_modules, min(n_files, len(platform_modules)))
            elif scenario < 0.30:
                domain = rng.choice(DOMAINS)
                candidates = by_domain[domain]
                n = min(n_files, len(candidates))
                chosen_modules = rng.sample(candidates, n)
            else:
                domain = rng.choice(DOMAINS)
                # Mostly one domain, sometimes related layers from that domain.
                candidates = by_domain[domain]
                n = min(n_files, len(candidates))
                chosen_modules = rng.sample(candidates, n)
                if n_files > len(chosen_modules) and rng.random() < 0.6:
                    extra_domains = [d for d in DOMAINS if d != domain]
                    extra = rng.choice(extra_domains)
                    need = n_files - len(chosen_modules)
                    chosen_modules.extend(rng.sample(by_domain[extra], min(need, len(by_domain[extra]))))

            verb = "fix" if kind == "bugfix" else rng.choice(COMMIT_VERBS)
            scope = rng.choice(chosen_modules)["domain"] if chosen_modules else rng.choice(DOMAINS)
            message = f"{kind if kind not in ('feature',) else 'feat'}({scope}): {verb} {rng.choice(COMMIT_TOPICS)}"
            if kind == "feature":
                message = f"feat({scope}): add {rng.choice(COMMIT_TOPICS)}"
            elif kind == "bugfix":
                message = f"fix({scope}): correct {rng.choice(COMMIT_TOPICS)}"

            commits.append({
                "seq": seq,
                "sha": _sha(seq, rng),
                "created_at": ts.isoformat(timespec="seconds"),
                "author": rng.choice(AUTHORS),
                "message": message,
                "change_kind": kind,
                "size_bucket": size_bucket,
                "modules": chosen_modules,
            })

    return commits


def insert_commits(conn, commits: List[dict]) -> None:
    commit_rows = []
    file_rows = []
    for c in commits:
        domains = sorted({m["domain"] for m in c["modules"]})
        layers = sorted({m["layer"] for m in c["modules"]})
        commit_rows.append((
            c["sha"], c["seq"], c["created_at"], c["author"], c["message"],
            c["change_kind"], c["size_bucket"], json.dumps(domains),
            json.dumps(layers), commits[c["seq"] - 2]["sha"] if c["seq"] > 1 else None,
        ))
        seen_paths = set()
        for m in c["modules"]:
            if m["path"] in seen_paths:
                continue
            seen_paths.add(m["path"])
            file_rows.append((c["sha"], m["path"], m["id"], "M"))
        if not c["modules"]:
            file_rows.append((c["sha"], "docs/README.md", None, "M"))

    conn.executemany(
        """
        INSERT INTO commits(sha, seq, created_at, author, message, change_kind,
                            size_bucket, domains_json, layers_json, parent_sha)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        commit_rows,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO commit_files(commit_sha, path, module_id, change_type) VALUES (?,?,?,?)",
        file_rows,
    )
    conn.commit()


def ensure_commit_touches_module(conn, sha: str, module_id: int) -> None:
    row = conn.execute("SELECT path FROM modules WHERE id=?", (module_id,)).fetchone()
    if not row:
        return
    conn.execute(
        "INSERT OR IGNORE INTO commit_files(commit_sha, path, module_id, change_type) VALUES (?,?,?,?)",
        (sha, row["path"], module_id, "M"),
    )
    domains = [r["domain"] for r in conn.execute(
        """SELECT DISTINCT m.domain FROM commit_files cf
           JOIN modules m ON m.id=cf.module_id WHERE cf.commit_sha=?""",
        (sha,),
    )]
    layers = [r["layer"] for r in conn.execute(
        """SELECT DISTINCT m.layer FROM commit_files cf
           JOIN modules m ON m.id=cf.module_id WHERE cf.commit_sha=?""",
        (sha,),
    )]
    conn.execute(
        "UPDATE commits SET domains_json=?, layers_json=? WHERE sha=?",
        (json.dumps(sorted(domains)), json.dumps(sorted(layers)), sha),
    )
    conn.commit()


def assign_flaky_tests(conn, n_tests: int, n_commits: int, rng: random.Random) -> Dict[int, dict]:
    n_flaky = min(280, max(28, round(n_tests * 0.0137)))
    n_transition = min(40, max(4, round(n_flaky * 0.14)))
    n_healed = min(52, max(6, round(n_flaky * 0.19)))
    flaky_ids = sorted(rng.sample(range(1, n_tests + 1), n_flaky))
    transition_ids = set(rng.sample(flaky_ids, n_transition))
    remaining = [i for i in flaky_ids if i not in transition_ids]
    healed_ids = set(rng.sample(remaining, min(n_healed, len(remaining))))

    archetype_names = [a for a, _ in ARCHETYPES]
    archetype_weights = [w for _, w in ARCHETYPES]
    flaky: Dict[int, dict] = {}

    for tid in flaky_ids:
        archetype = rng.choices(archetype_names, archetype_weights, k=1)[0]
        # A minority are chronic; most fail occasionally enough to be noisy.
        if rng.random() < 0.16:
            rate = rng.uniform(0.18, 0.42)
        else:
            rate = rng.uniform(0.025, 0.15)
        healed_seq = None
        broken_seq = None
        if tid in healed_ids:
            healed_seq = rng.randint(int(n_commits * 0.35), n_commits - 35)
        if tid in transition_ids:
            broken_seq = rng.randint(int(n_commits * 0.45), n_commits - 14)

        polluter_id = None
        if archetype in ("order_dependence", "shared_state"):
            row = conn.execute(
                "SELECT path FROM tests WHERE id=?", (tid,)
            ).fetchone()
            candidates = [r["id"] for r in conn.execute(
                "SELECT id FROM tests WHERE path=? AND id<>? LIMIT 80",
                (row["path"], tid),
            ).fetchall()]
            if candidates:
                polluter_id = rng.choice(candidates)
            else:
                polluter_id = max(1, tid - 1)

        flaky[tid] = {
            "test_id": tid,
            "archetype": archetype,
            "rate": rate,
            "polluter_id": polluter_id,
            "healed_seq": healed_seq,
            "broken_seq": broken_seq,
        }
        conn.execute(
            """
            UPDATE tests SET truth_flaky=1, archetype_gt=?, flake_rate=?,
                             polluter_test_id=?, healed_seq=?, broken_seq=?
            WHERE id=?
            """,
            (archetype, rate, polluter_id, healed_seq, broken_seq, tid),
        )
    conn.commit()
    return flaky


def _candidate_tests_for_module(
    conn, module_id: int, kind: str, rng: random.Random, limit: int
) -> List[int]:
    if kind == "direct_coverage":
        rows = conn.execute(
            "SELECT test_id FROM coverage WHERE module_id=? ORDER BY test_id",
            (module_id,),
        ).fetchall()
    elif kind == "reverse_dependency":
        rows = conn.execute(
            """
            WITH RECURSIVE downstream(id, depth) AS (
                SELECT source_id, 1 FROM module_deps WHERE target_id=?
                UNION
                SELECT md.source_id, d.depth + 1
                FROM module_deps md JOIN downstream d ON md.target_id=d.id
                WHERE d.depth < 4
            )
            SELECT DISTINCT c.test_id
            FROM downstream d JOIN coverage c ON c.module_id=d.id
            ORDER BY c.test_id
            """,
            (module_id,),
        ).fetchall()
    elif kind == "same_domain":
        m = conn.execute("SELECT domain FROM modules WHERE id=?", (module_id,)).fetchone()
        rows = conn.execute(
            "SELECT id AS test_id FROM tests WHERE domain=? ORDER BY id",
            (m["domain"],),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id AS test_id FROM tests ORDER BY id"
        ).fetchall()
    ids = [r["test_id"] for r in rows]
    rng.shuffle(ids)
    return ids


def create_incidents(
    conn, commits: List[dict], flaky: Dict[int, dict], rng: random.Random
) -> Dict[int, List[Tuple[int, Optional[int], int]]]:
    """Create true break incidents.

    Returns test_id -> [(introduced_seq, fixed_seq, incident_id)].
    """
    n_commits = len(commits)
    intervals: Dict[int, List[Tuple[int, Optional[int], int]]] = defaultdict(list)

    # Ordinary bad commits, with 1-15 newly broken tests.
    available_bad_seqs = list(range(min(24, max(10, n_commits // 5)), max(1, n_commits - 3)))
    n_bad = min(54, max(6, len(available_bad_seqs) // 2), len(available_bad_seqs))
    bad_seqs = sorted(rng.sample(available_bad_seqs, n_bad))
    incident_id = 0
    for intro_seq in bad_seqs:
        commit = commits[intro_seq - 1]
        mapped = [m["id"] for m in commit["modules"]]
        if not mapped:
            module_id = rng.randint(1, conn.execute("SELECT COUNT(*) c FROM modules").fetchone()["c"])
            ensure_commit_touches_module(conn, commit["sha"], module_id)
        else:
            module_id = rng.choice(mapped)

        incident_id += 1
        domain = conn.execute("SELECT domain FROM modules WHERE id=?", (module_id,)).fetchone()["domain"]
        severity_n = rng.choices([1, 2, 3, 4, 6, 10], [28, 26, 20, 13, 8, 5])[0]
        fixed_seq = None
        fixed_sha = None
        if rng.random() < 0.84:
            delay = rng.choices([1, 2, 3, 5, 8, 14, 24], [24, 22, 18, 14, 10, 8, 4])[0]
            fixed_seq = min(n_commits, intro_seq + delay)
            fixed_sha = commits[fixed_seq - 1]["sha"]
            ensure_commit_touches_module(conn, fixed_sha, module_id)

        error_kind = rng.choices(["broken_logic", "broken_schema"], [0.78, 0.22])[0]
        cur = conn.execute(
            """
            INSERT INTO incidents(id, kind, introduced_sha, fixed_sha, introduced_seq,
                                   fixed_seq, module_id, domain, severity, title)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (incident_id, "bad_commit", commit["sha"], fixed_sha, intro_seq,
             fixed_seq, module_id, domain, severity_n,
             f"{error_kind.replace('_', ' ')} after {commit['sha']}"),
        )

        chosen: List[int] = []
        seen = set()
        # Coupling distribution intentionally includes a few coverage/dependency
        # blind spots so selection miss rate is nonzero and must be estimated.
        for slot in range(severity_n):
            bucket_roll = rng.random()
            if bucket_roll < 0.60:
                kind = "direct_coverage"
            elif bucket_roll < 0.84:
                kind = "reverse_dependency"
            elif bucket_roll < 0.98:
                kind = "same_domain"
            else:
                kind = "global_hidden"
            candidates = _candidate_tests_for_module(conn, module_id, kind, rng, 20)
            for tid in candidates:
                if tid in seen or tid in flaky:
                    continue
                seen.add(tid)
                chosen.append(tid)
                break

        for tid in chosen:
            conn.execute(
                "INSERT OR REPLACE INTO incident_tests(incident_id, test_id, impact_strength) VALUES (?,?,?)",
                (incident_id, tid, round(rng.uniform(0.75, 1.0), 2)),
            )
            intervals[tid].append((intro_seq, fixed_seq, incident_id))

    # Flaky tests that later become deterministically broken by a commit.
    transition_items = [(tid, f) for tid, f in flaky.items() if f["broken_seq"] is not None]
    for tid, f in transition_items:
        intro_seq = f["broken_seq"]
        commit = commits[intro_seq - 1]
        cov = conn.execute(
            "SELECT module_id FROM coverage WHERE test_id=? ORDER BY RANDOM() LIMIT 1",
            (tid,),
        ).fetchone()
        module_id = cov["module_id"] if cov else rng.randint(1, 250)
        ensure_commit_touches_module(conn, commit["sha"], module_id)

        fixed_seq = None
        fixed_sha = None
        if rng.random() < 0.34:
            fixed_seq = min(n_commits, intro_seq + rng.randint(2, 13))
            fixed_sha = commits[fixed_seq - 1]["sha"]
            ensure_commit_touches_module(conn, fixed_sha, module_id)

        incident_id += 1
        domain = conn.execute("SELECT domain FROM tests WHERE id=?", (tid,)).fetchone()["domain"]
        conn.execute(
            """
            INSERT INTO incidents(id, kind, introduced_sha, fixed_sha, introduced_seq,
                                   fixed_seq, module_id, domain, severity, title)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (incident_id, "flake_then_break", commit["sha"], fixed_sha, intro_seq,
             fixed_seq, module_id, domain, 1, "formerly flaky test became deterministically broken"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO incident_tests(incident_id, test_id, impact_strength) VALUES (?,?,?)",
            (incident_id, tid, 1.0),
        )
        intervals[tid].append((intro_seq, fixed_seq, incident_id))

    conn.commit()
    return dict(intervals)


def _failure_context(
    tid: int,
    seq: int,
    run_ctx: dict,
    flake: Optional[dict],
    durations: List[float],
    rng: random.Random,
) -> Tuple[bool, dict]:
    """Return whether a test fails plus observation context."""
    ctx = {
        "duration_ms": durations[tid - 1] * 1000 * rng.uniform(0.72, 1.45),
        "worker": tid % run_ctx["parallelism"],
        "shard": tid % run_ctx["parallelism"],
        "order_index": (tid * 7919 + run_ctx["order_seed"]) % 12000,
        "preceding_test_id": max(1, tid - rng.randint(1, 35)),
        "error_type": None,
        "message": None,
        "category": None,
    }

    if flake is None:
        return False, ctx

    archetype = flake["archetype"]
    rate = flake["rate"]
    p = rate

    if archetype == "order_dependence":
        hit = rng.random() < 0.46
        if hit and flake.get("polluter_id"):
            ctx["preceding_test_id"] = flake["polluter_id"]
            p = min(0.62, rate * 5.2 + 0.16)
        else:
            p = rate * 0.22
    elif archetype == "concurrency":
        if run_ctx["load"] > 0.72 or run_ctx["parallelism"] >= 64:
            p = min(0.58, rate * 3.4 + 0.08)
        else:
            p = rate * 0.35
    elif archetype == "time_or_timeout":
        boundary = run_ctx["time_boundary"] or run_ctx["month_end"]
        if boundary:
            p = min(0.55, rate * 4.1 + 0.12)
        elif run_ctx["load"] > 0.8:
            p = min(0.34, rate * 2.2 + 0.05)
        else:
            p = rate * 0.3
        # Heavy tests are more likely to time out under load.
        if durations[tid - 1] > 20 and run_ctx["load"] > 0.7:
            p = min(0.6, p + 0.12)
            ctx["duration_ms"] *= rng.uniform(2.0, 4.5)
    elif archetype == "shared_state":
        hit = rng.random() < 0.38
        if hit and flake.get("polluter_id"):
            ctx["preceding_test_id"] = flake["polluter_id"]
            ctx["shard"] = rng.randrange(run_ctx["parallelism"])
            p = min(0.5, rate * 4.0 + 0.1)
        else:
            p = rate * 0.28
    else:  # external_dependency
        p = rate * rng.uniform(0.75, 1.55)

    if rng.random() >= p:
        return False, ctx

    err_type, message = ERROR_BY_CAUSE[archetype]
    ctx.update(error_type=err_type, message=message, category=f"flaky:{archetype}")
    if archetype == "time_or_timeout" and ctx["error_type"] != "TimeoutError":
        ctx["error_type"] = "TimeoutError"
        ctx["message"] = ERROR_BY_CAUSE["time_or_timeout"][1]
    return True, ctx


def simulate_runs(
    conn,
    cfg: DemoConfig,
    commits: List[dict],
    flaky: Dict[int, dict],
    intervals: Dict[int, List[Tuple[int, Optional[int], int]]],
    rng: random.Random,
) -> None:
    n_tests = cfg.n_tests
    duration_rows = conn.execute("SELECT id, base_duration FROM tests ORDER BY id").fetchall()
    durations = [r["base_duration"] for r in duration_rows]
    total_test_duration = sum(durations)

    starts: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    ends: Dict[int, List[int]] = defaultdict(list)
    for tid, ints in intervals.items():
        for intro, fixed, incident_id in ints:
            starts[intro].append((tid, incident_id))
            if fixed is not None:
                ends[fixed].append(tid)

    broken_now: Dict[int, int] = {}

    for commit in commits:
        seq = commit["seq"]
        for tid, incident_id in starts.get(seq, []):
            broken_now[tid] = incident_id
        for tid in ends.get(seq, []):
            broken_now.pop(tid, None)

        dt = datetime.fromisoformat(commit["created_at"])
        parallelism = rng.choices([32, 40, 50, 50, 50, 56, 64], [8, 18, 24, 20, 12, 10, 8])[0]
        load = round(rng.uniform(0.16, 0.97), 3)
        order_seed = rng.randrange(1, 2_000_000_000)
        time_boundary = dt.hour >= 23 or dt.hour <= 1
        month_end = dt.day >= 28

        wall_sec = cfg.setup_wall_sec + total_test_duration / parallelism * rng.uniform(0.96, 1.16)
        queue_sec = rng.uniform(240, 1_050) * (0.75 + load * 0.5)
        cur = conn.execute(
            """
            INSERT INTO ci_runs(commit_sha, seq, created_at, trigger, parallelism, load,
                                order_seed, total_count, executed_count, passed_count,
                                failed_count, retry_recovered_count, duration_sec,
                                queue_sec, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (commit["sha"], seq, commit["created_at"], "push", parallelism, load,
             order_seed, n_tests, n_tests, n_tests, 0, 0, wall_sec, queue_sec, "running"),
        )
        run_id = cur.lastrowid

        observations = []
        initial_failures = 0
        retry_recovered = 0

        for tid in range(1, n_tests + 1):
            incident_id = broken_now.get(tid)
            flake = flaky.get(tid)
            run_ctx = {
                "parallelism": parallelism,
                "load": load,
                "order_seed": order_seed,
                "time_boundary": time_boundary,
                "month_end": month_end,
            }

            failed = False
            if incident_id is not None:
                # True regressions are almost deterministic. A small failure
                # escape models environment/assertion flakiness but the change
                # boundary remains statistically obvious.
                if rng.random() < 0.985:
                    failed = True
                    schema_roll = rng.random()
                    err_type, message = ERROR_BY_CAUSE["broken_schema" if schema_roll < 0.22 else "broken_logic"]
                    ctx = {
                        "duration_ms": durations[tid - 1] * 1000 * rng.uniform(0.8, 1.8),
                        "worker": tid % parallelism,
                        "shard": tid % parallelism,
                        "order_index": (tid * 7919 + order_seed) % 12000,
                        "preceding_test_id": max(1, tid - rng.randint(1, 35)),
                        "error_type": err_type,
                        "message": message,
                        "category": "broken",
                    }
                else:
                    ctx = {"duration_ms": durations[tid - 1] * 900, "worker": tid % parallelism,
                           "shard": tid % parallelism, "order_index": 0,
                           "preceding_test_id": None, "error_type": None, "message": None,
                           "category": None}
            else:
                failed, ctx = _failure_context(tid, seq, run_ctx, flake, durations, rng)

            if not failed:
                continue

            initial_failures += 1
            observations.append((
                run_id, commit["sha"], seq, tid, 0, "fail", ctx["duration_ms"],
                ctx["worker"], ctx["shard"], ctx["order_index"], ctx["preceding_test_id"],
                parallelism, load, ctx["error_type"], ctx["message"], ctx["category"],
                incident_id if ctx["category"] == "broken" else None,
                commit["created_at"],
            ))

            # Automatic retry, then occasional manual rerun. Flakes often pass;
            # broken commits almost never do.
            recovered = False
            retry_count = 1 if rng.random() < 0.91 else 0
            for attempt in range(1, 3):
                if attempt == 1 and retry_count == 0:
                    continue
                if attempt == 2 and rng.random() > 0.52:
                    break

                if ctx["category"] == "broken":
                    pass_probability = 0.018
                else:
                    archetype = flake["archetype"] if flake else "external_dependency"
                    pass_probability = {
                        "order_dependence": 0.82,
                        "concurrency": 0.72,
                        "time_or_timeout": 0.86,
                        "shared_state": 0.70,
                        "external_dependency": 0.80,
                    }.get(archetype, 0.78)
                    pass_probability += rng.uniform(-0.08, 0.08)

                retry_pass = rng.random() < pass_probability
                retry_status = "pass" if retry_pass else "fail"
                if retry_pass:
                    recovered = True
                observations.append((
                    run_id, commit["sha"], seq, tid, attempt, retry_status,
                    ctx["duration_ms"] * rng.uniform(0.82, 1.12),
                    rng.randrange(parallelism), rng.randrange(parallelism),
                    ctx["order_index"], ctx["preceding_test_id"], parallelism,
                    load * rng.uniform(0.85, 1.05),
                    None if retry_pass else ctx["error_type"],
                    None if retry_pass else ctx["message"],
                    ctx["category"],
                    incident_id if ctx["category"] == "broken" else None,
                    commit["created_at"],
                ))
                if retry_pass:
                    break

            if recovered:
                retry_recovered += 1

        conn.executemany(
            """
            INSERT INTO test_observations(run_id, commit_sha, seq, test_id, attempt,
                                          status, duration_ms, worker, shard, order_index,
                                          preceding_test_id, parallelism, load, error_type,
                                          message, category_gt, incident_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            observations,
        )
        status = "red" if initial_failures and retry_recovered < initial_failures else (
            "passed_on_rerun" if initial_failures else "green"
        )
        conn.execute(
            """
            UPDATE ci_runs SET passed_count=?, failed_count=?,
                retry_recovered_count=?, status=? WHERE id=?
            """,
            (n_tests - initial_failures, initial_failures, retry_recovered, status, run_id),
        )
        conn.commit()


def generate_history(conn, cfg: DemoConfig, rng: random.Random) -> List[dict]:
    # Use an independent, fixed stream for history. World/coverage generation
    # contains set-derived traversal; this keeps six-month outcomes reproducible.
    rng = random.Random(cfg.seed + 104729)
    commits = generate_commit_plan(cfg, rng, conn)
    insert_commits(conn, commits)
    flaky = assign_flaky_tests(conn, cfg.n_tests, len(commits), rng)
    intervals = create_incidents(conn, commits, flaky, rng)
    simulate_runs(conn, cfg, commits, flaky, intervals, rng)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('n_commits', ?)", (str(len(commits)),))
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('history_generated', '1')")
    conn.commit()
    return commits
