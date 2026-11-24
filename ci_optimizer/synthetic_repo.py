from __future__ import annotations

import random
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from .config import DemoConfig

DOMAINS = [
    "identity", "users", "authz", "catalog", "pricing", "cart", "checkout",
    "coupons", "tax", "payments", "billing", "ledger", "orders",
    "fulfillment", "shipping", "inventory", "warehouse", "returns",
    "notifications", "messaging", "search", "recommendations", "reviews",
    "risk", "fraud", "support", "webhooks", "analytics", "reporting",
    "experiments",
]

LAYERS = [
    ("api", 0.10),
    ("schema", 0.08),
    ("service", 0.24),
    ("policy", 0.08),
    ("repository", 0.16),
    ("models", 0.10),
    ("events", 0.08),
    ("client", 0.08),
    ("tasks", 0.08),
]

# Intentional business dependency graph. Arrows point from a domain to the
# upstream capabilities it consumes.
DOMAIN_UPSTREAM = {
    "identity": [],
    "users": ["identity"],
    "authz": ["identity", "users"],
    "catalog": [],
    "pricing": ["catalog", "experiments"],
    "cart": ["catalog", "pricing", "inventory"],
    "coupons": ["pricing", "risk"],
    "tax": ["billing"],
    "payments": ["billing", "ledger", "risk"],
    "billing": ["ledger", "payments"],
    "checkout": ["cart", "pricing", "coupons", "tax", "payments", "shipping", "risk"],
    "orders": ["checkout", "cart", "payments", "inventory", "fulfillment", "notifications"],
    "fulfillment": ["orders", "warehouse", "shipping", "inventory"],
    "shipping": ["inventory", "warehouse"],
    "inventory": ["warehouse", "catalog"],
    "warehouse": [],
    "returns": ["orders", "billing", "inventory", "notifications"],
    "notifications": ["messaging", "users"],
    "messaging": [],
    "search": ["catalog"],
    "recommendations": ["catalog", "experiments", "analytics"],
    "reviews": ["catalog", "users", "messaging"],
    "risk": ["identity", "analytics"],
    "fraud": ["risk", "payments"],
    "support": ["orders", "users", "messaging"],
    "webhooks": ["messaging"],
    "analytics": ["messaging"],
    "reporting": ["analytics", "ledger", "warehouse"],
    "experiments": [],
}

PLATFORM_MODULES = [
    "runtime/config", "runtime/clock", "runtime/feature_flags",
    "observability/logging", "observability/metrics", "observability/tracing",
    "observability/audit",
    "security/auth", "security/encryption", "security/secrets",
    "security/data_masking", "security/tenancy",
    "net/http_client", "net/retry", "net/circuit_breaker", "net/rate_limiter",
    "data/postgres", "data/redis", "data/pagination", "data/serialization",
    "data/idempotency", "data/locks",
    "messaging/kafka", "messaging/queue",
    "storage/s3", "storage/blob", "storage/router",
    "clients/email", "clients/sms", "clients/push", "clients/search",
    "clients/ml", "clients/warehouse",
    "commerce/money", "commerce/address", "commerce/scheduling",
]

FEATURE_TOPICS = [
    "creation", "update", "deletion", "validation", "authorization",
    "pagination", "filtering", "sorting", "serialization", "retry",
    "idempotency", "concurrency", "cache", "events", "webhook",
    "refund", "discount", "tax_calculation", "inventory_reservation",
    "shipment_split", "address_normalization", "currency_conversion",
    "permission_check", "audit_trail", "rate_limit", "timeout",
    "dead_letter", "batch", "export", "search_indexing",
]

TEST_VERBS = [
    "creates", "updates", "deletes", "rejects", "accepts", "calculates",
    "validates", "retries", "publishes", "consumes", "locks", "releases",
    "maps", "filters", "paginates", "dedupes", "expires", "refunds",
    "reserves", "releases", "normalizes", "authorizes", "records",
]
TEST_CONDITIONS = [
    "valid_request", "missing_required_field", "duplicate_request",
    "stale_version", "concurrent_update", "downstream_timeout",
    "downstream_500", "empty_result", "boundary_value", "negative_amount",
    "currency_mismatch", "permission_denied", "cross_tenant_access",
    "cache_disabled", "event_replayed", "worker_restart", "network_partition",
    "midnight_utc", "month_end", "leap_day", "large_batch", "slow_query",
    "partial_failure", "rate_limited", "feature_flag_off", "old_schema",
]
TEST_EXPECTATIONS = [
    "persists_record", "returns_200", "returns_4xx", "raises_validation_error",
    "rolls_back_transaction", "emits_event", "enqueues_task",
    "uses_cached_value", "retries_with_backoff", "preserves_idempotency_key",
    "does_not_oversell", "calculates_correct_total", "masks_sensitive_fields",
    "writes_audit_log", "releases_lock", "surfaces_retryable_error",
    "falls_back_to_primary", "marks_dead_letter", "keeps_consistent_state",
]

SUITES = ["unit", "integration", "service", "e2e"]
SUITE_WEIGHTS = [0.68, 0.20, 0.09, 0.03]
SUITE_DURATION = {
    # seconds, triangular low/high/mode
    "unit": (0.01, 1.4, 0.18),
    "integration": (0.8, 22.0, 6.0),
    "service": (6.0, 55.0, 20.0),
    "e2e": (20.0, 150.0, 55.0),
}
COVERAGE_RANGE = {
    "unit": (2, 6),
    "integration": (7, 18),
    "service": (14, 34),
    "e2e": (24, 58),
}


def _weighted_choice(rng: random.Random, items: Sequence[str], weights: Sequence[float]) -> str:
    return rng.choices(list(items), weights=list(weights), k=1)[0]


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def generate_modules(conn, rng: random.Random) -> Dict[Tuple[str, str], int]:
    rows = []
    key_to_id: Dict[Tuple[str, str], int] = {}
    next_id = 1

    for domain in DOMAINS:
        for layer, _weight in LAYERS:
            path = f"src/commerce/domains/{domain}/{layer}.py"
            rows.append((next_id, path, layer, domain, layer, rng.randint(90, 1250)))
            key_to_id[(domain, layer)] = next_id
            next_id += 1

    for slug in PLATFORM_MODULES:
        group, name = slug.split("/", 1)
        path = f"src/commerce/platform/{group}/{name}.py"
        rows.append((next_id, path, name, "platform", group, rng.randint(60, 900)))
        key_to_id[("platform", slug)] = next_id
        next_id += 1

    conn.executemany(
        "INSERT INTO modules(id, path, name, domain, layer, loc) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return key_to_id


def generate_dependencies(
    conn, rng: random.Random, key_to_id: Dict[Tuple[str, str], int]
) -> Dict[int, List[int]]:
    deps = set()

    def add(source: int, target: int, kind: str = "import") -> None:
        if source != target:
            deps.add((source, target, kind))

    platform = {slug: key_to_id[("platform", slug)] for slug in PLATFORM_MODULES}

    for domain in DOMAINS:
        kid = lambda layer: key_to_id[(domain, layer)]  # noqa: E731
        add(kid("api"), kid("schema"), "uses")
        add(kid("api"), kid("service"), "calls")
        add(kid("api"), platform["security/auth"], "middleware")
        add(kid("service"), kid("schema"), "uses")
        add(kid("service"), kid("repository"), "calls")
        add(kid("service"), kid("models"), "uses")
        add(kid("service"), kid("policy"), "calls")
        add(kid("service"), kid("events"), "publishes")
        add(kid("service"), kid("client"), "calls")
        add(kid("service"), platform["observability/metrics"], "observes")
        add(kid("service"), platform["runtime/clock"], "time")
        add(kid("repository"), kid("models"), "uses")
        add(kid("repository"), platform["data/postgres"], "db")
        add(kid("repository"), platform["data/locks"], "locking")
        add(kid("policy"), kid("models"), "uses")
        add(kid("events"), kid("models"), "uses")
        add(kid("events"), platform["messaging/kafka"], "transport")
        add(kid("client"), platform["net/http_client"], "http")
        add(kid("client"), platform["net/retry"], "retry")
        add(kid("client"), platform["net/circuit_breaker"], "resilience")
        add(kid("tasks"), kid("service"), "calls")
        add(kid("tasks"), platform["messaging/queue"], "queue")

        for upstream in DOMAIN_UPSTREAM.get(domain, []):
            # Domain calls upstream through its client or service contract.
            if (upstream, "client") in key_to_id:
                add(kid("client"), key_to_id[(upstream, "client")], "domain-client")
            if rng.random() < 0.55 and (upstream, "service") in key_to_id:
                add(kid("service"), key_to_id[(upstream, "service")], "domain-api")

    common_deps = [
        "runtime/config", "observability/logging", "observability/tracing",
        "data/serialization", "runtime/feature_flags",
    ]
    module_rows = conn.execute("SELECT id, domain, layer FROM modules").fetchall()
    for row in module_rows:
        if row["domain"] == "platform":
            continue
        for slug in common_deps:
            if rng.random() < 0.72:
                add(row["id"], platform[slug], "platform")

    # Platform internal dependencies.
    add(platform["net/http_client"], platform["observability/tracing"], "uses")
    add(platform["net/http_client"], platform["net/retry"], "uses")
    add(platform["data/postgres"], platform["observability/metrics"], "uses")
    add(platform["messaging/kafka"], platform["data/serialization"], "uses")
    add(platform["security/auth"], platform["security/encryption"], "uses")
    add(platform["clients/search"], platform["net/http_client"], "uses")
    add(platform["clients/ml"], platform["net/http_client"], "uses")

    conn.executemany(
        "INSERT OR IGNORE INTO module_deps(source_id, target_id, dep_kind) VALUES (?,?,?)",
        sorted(deps),
    )
    conn.commit()

    adjacency: Dict[int, List[int]] = defaultdict(list)
    # Sort the set-derived edges so tuple/string hash randomization cannot make
    # dependency order differ between Python processes.
    for source, target, _kind in sorted(deps):
        adjacency[source].append(target)
    for source in adjacency:
        adjacency[source] = sorted(set(adjacency[source]))
    return dict(adjacency)


def _related_modules(
    primary: int,
    adjacency: Dict[int, List[int]],
    rng: random.Random,
    low: int,
    high: int,
) -> List[int]:
    """Pick a realistic dependency-centered coverage set."""
    chosen = {primary}
    frontier = [primary]
    target_n = rng.randint(low, high)

    # Direct dependencies first.
    direct = list(adjacency.get(primary, []))
    rng.shuffle(direct)
    for mid in direct:
        chosen.add(mid)
    frontier.extend(direct[:8])

    # A few transitive dependencies.
    for node in frontier[:]:
        for mid in adjacency.get(node, []):
            if len(chosen) >= target_n:
                break
            if rng.random() < 0.55:
                chosen.add(mid)
        if len(chosen) >= target_n:
            break

    # Coverage tools occasionally record broader runtime touches.
    all_adjacent = []
    for node in list(chosen):
        all_adjacent.extend(adjacency.get(node, []))
    rng.shuffle(all_adjacent)
    for mid in all_adjacent:
        if len(chosen) >= target_n:
            break
        chosen.add(mid)

    return list(chosen)[: max(target_n, len(chosen))]


def generate_tests_and_coverage(
    conn,
    cfg: DemoConfig,
    rng: random.Random,
    adjacency: Dict[int, List[int]],
) -> list:
    modules = conn.execute("SELECT * FROM modules").fetchall()
    domain_modules: Dict[str, List[int]] = defaultdict(list)
    for m in modules:
        domain_modules[m["domain"]].append(m["id"])

    authors = [
        "alice.chen", "bob.martinez", "chi.patel", "diana.novak",
        "ethan.kim", "fatima.ali", "gabriel.silva", "hana.sato",
        "ivan.petrov", "Julia.owusu", "kai.andersen", "li.wei",
        "mona.rahimi", "noah.williams",
    ]

    test_rows = []
    coverage_rows = []
    files: Dict[str, List[str]] = defaultdict(list)
    used_nodeids = set()
    group_counter: Dict[Tuple[int, str], int] = defaultdict(int)
    name_counter: Dict[str, int] = defaultdict(int)

    for test_id in range(1, cfg.n_tests + 1):
        # 94% business-domain tests and 6% platform tests.
        if rng.random() < 0.94:
            domain = _weighted_choice(
                rng, DOMAINS, [rng.uniform(0.7, 1.4) for _ in DOMAINS]
            )
            primary = rng.choice(domain_modules[domain])
        else:
            domain = "platform"
            primary = rng.choice(domain_modules["platform"])

        module = next(m for m in modules if m["id"] == primary)
        suite = _weighted_choice(rng, SUITES, SUITE_WEIGHTS)
        low_d, high_d, mode_d = SUITE_DURATION[suite]
        duration = max(0.008, rng.triangular(low_d, high_d, mode_d))

        group_index = group_counter[(primary, suite)] // 16
        group_counter[(primary, suite)] += 1
        topic = FEATURE_TOPICS[(group_index + primary) % len(FEATURE_TOPICS)]
        topic_variant = group_index // len(FEATURE_TOPICS)
        if topic_variant:
            topic = f"{topic}_{topic_variant + 1}"

        if domain == "platform":
            rel_dir = f"tests/platform/{module['layer']}/{suite}"
        else:
            rel_dir = f"tests/domains/{domain}/{module['layer']}/{suite}"
        file_path = f"{rel_dir}/test_{module['name']}_{topic}.py"
        class_name = f"Test{_camel(module['name'])}{_camel(topic)}"

        verb = rng.choice(TEST_VERBS)
        condition = rng.choice(TEST_CONDITIONS)
        expectation = rng.choice(TEST_EXPECTATIONS)
        test_name = f"test_{verb}_{condition}_{expectation}"
        base_nodeid = f"{file_path}::{class_name}::{test_name}"
        nodeid = base_nodeid
        dedupe = 2
        while nodeid in used_nodeids:
            nodeid = f"{base_nodeid}_{dedupe:02d}"
            dedupe += 1
        used_nodeids.add(nodeid)

        owner = f"@{domain}-team" if domain != "platform" else "@platform-team"
        author = rng.choice(authors)
        test_rows.append((
            test_id, nodeid, file_path, class_name,
            nodeid.split("::")[-1], domain, suite, owner, duration, author,
        ))

        files[file_path].append((class_name, nodeid.split("::")[-1]))

        low_cov, high_cov = COVERAGE_RANGE[suite]
        related = _related_modules(primary, adjacency, rng, low_cov, high_cov)
        if primary not in related:
            related.insert(0, primary)

        for module_id in related:
            is_primary = module_id == primary
            if is_primary:
                observed_prob = 0.995
            elif module_id in adjacency.get(primary, []):
                observed_prob = 0.93
            else:
                observed_prob = 0.78
            observed = 1 if rng.random() < observed_prob else 0
            hit_count = rng.randint(3, 260) if observed else rng.randint(1, 20)
            confidence = round(rng.uniform(0.72, 0.99), 3) if observed else round(rng.uniform(0.25, 0.68), 3)
            first_seq = rng.randint(0, 45)
            last_seq = max(first_seq + 1, rng.randint(220, 380))
            coverage_rows.append((
                test_id, module_id, hit_count, confidence, observed,
                first_seq, last_seq,
            ))

    # The author column is useful for realism but schema currently has owner.
    # Encode author in owner? Keep owner team and no author column. Remove it.
    test_rows_without_author = [row[:9] for row in test_rows]
    conn.executemany(
        """
        INSERT INTO tests(id, nodeid, path, class_name, name, domain, suite,
                          owner, base_duration)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        test_rows_without_author,
    )
    conn.executemany(
        """
        INSERT INTO coverage(test_id, module_id, hit_count, confidence, observed,
                             first_seq, last_seq)
        VALUES (?,?,?,?,?,?,?)
        """,
        coverage_rows,
    )
    conn.commit()

    if cfg.materialize_repo:
        _materialize_repo(cfg, conn, files)

    return modules


def _materialize_repo(cfg: DemoConfig, conn, files: Dict[str, List[Tuple[str, str]]]) -> None:
    root = Path(cfg.repo_dir)
    root.mkdir(parents=True, exist_ok=True)

    # Root metadata makes the generated tree look like a real workspace.
    (root / "pyproject.toml").write_text(textwrap.dedent("""
        [project]
        name = "commerce-monorepo-simulation"
        version = "0.0.0"
        description = "Synthetic large monorepo used by the CI intelligence demo"

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        addopts = "-ra --strict-markers"
    """).strip() + "\n", encoding="utf-8")

    modules = conn.execute("SELECT path, domain, layer FROM modules").fetchall()
    for m in modules:
        p = root / m["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(
                f'"""Synthetic {m["domain"]} {m["layer"]} module."""\n\n'
                f"# This file is generated as dependency-graph context. CI behavior is\n"
                f"# simulated by ci_optimizer rather than executing these stubs.\n",
                encoding="utf-8",
            )

    for file_path, methods in files.items():
        p = root / file_path
        p.parent.mkdir(parents=True, exist_ok=True)
        classes: Dict[str, List[str]] = defaultdict(list)
        for class_name, method in methods:
            classes[class_name].append(method)

        lines = [
            '"""Synthetic test module generated for selection and history simulation."""',
            "",
            "import pytest",
            "",
            "",
        ]
        for class_name, method_names in classes.items():
            lines.append(f"class {class_name}:")
            lines.append(f'    """Test cases for {class_name[4:] if class_name.startswith("Test") else class_name}."""')
            lines.append("")
            for method in method_names:
                lines.append(f"    def {method}(self):")
                lines.append("        # Simulated assertion; outcomes are generated in SQLite.")
                lines.append("        assert True")
                lines.append("")
            lines.append("")
        p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_reverse_adjacency(adjacency: Dict[int, List[int]]) -> Dict[int, List[int]]:
    reverse: Dict[int, List[int]] = defaultdict(list)
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].append(source)
    return dict(reverse)


def load_graph(conn) -> Tuple[Dict[int, List[int]], Dict[int, List[int]]]:
    adjacency: Dict[int, List[int]] = defaultdict(list)
    for row in conn.execute("SELECT source_id, target_id FROM module_deps"):
        adjacency[row["source_id"]].append(row["target_id"])
    reverse = build_reverse_adjacency(dict(adjacency))
    for mid in adjacency:
        adjacency[mid] = sorted(adjacency[mid])
    for mid in reverse:
        reverse[mid] = sorted(reverse[mid])
    return dict(adjacency), reverse
