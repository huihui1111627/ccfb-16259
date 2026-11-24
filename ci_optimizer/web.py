from __future__ import annotations

import json
import mimetypes
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .config import DB_PATH, METRICS_PATH, REPORT_PATH

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _json_default(obj):
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


class DashboardAPI:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)

    def conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def overview(self) -> Dict[str, Any]:
        metrics = {}
        if METRICS_PATH.exists():
            metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        with self.conn() as conn:
            metrics["db"] = {
                "exists": self.db_path.exists(),
                "commits": conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"],
                "selection_rows": conn.execute("SELECT COUNT(*) c FROM selection_snapshots").fetchone()["c"],
                "selection_items": conn.execute("SELECT COUNT(*) c FROM selection_items").fetchone()["c"],
                "flaky_findings": conn.execute("SELECT COUNT(*) c FROM flake_findings").fetchone()["c"],
            }
        return metrics

    def commits(self, limit: int = 500) -> Dict[str, Any]:
        limit = min(max(1, limit), 2000)
        with self.conn() as conn:
            rows = conn.execute(
                """
                SELECT s.seq, s.sha, c.created_at, c.author, c.message, c.change_kind,
                       c.domains_json, c.size_bucket,
                       s.selected_count, s.skipped_count, s.selected_duration_sec,
                       s.estimated_missed_failures, s.estimated_miss_rate, s.miss_rate_upper95,
                       s.true_failures, s.selected_true_failures, s.missed_true_failures,
                       s.new_true_failures, s.missed_new_true_failures, s.flaky_failures,
                       s.quarantined_count, s.nightly_full
                FROM selection_snapshots s JOIN commits c ON c.sha=s.sha
                ORDER BY s.seq DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["domains"] = json.loads(d.pop("domains_json") or "[]")
            result.append(d)
        return {"commits": result}

    def selection(self, sha: str, q: str = "", limit: int = 200, offset: int = 0) -> Dict[str, Any]:
        limit = min(max(1, limit), 1000)
        with self.conn() as conn:
            commit = conn.execute("SELECT * FROM commits WHERE sha=?", (sha,)).fetchone()
            if not commit:
                short = sha[:12]
                commit = conn.execute(
                    "SELECT * FROM commits WHERE sha LIKE ?", (short + "%",)
                ).fetchone()
            if not commit:
                return {"error": "commit not found"}
            sha = commit["sha"]
            snap = conn.execute("SELECT * FROM selection_snapshots WHERE sha=?", (sha,)).fetchone()
            files = conn.execute(
                """
                SELECT cf.path, cf.change_type, m.domain, m.layer
                FROM commit_files cf LEFT JOIN modules m ON m.id=cf.module_id
                WHERE cf.commit_sha=? ORDER BY cf.path
                """,
                (sha,),
            ).fetchall()

            where = "si.sha=?"
            params: list = [sha]
            if q:
                where += " AND (t.nodeid LIKE ? OR t.owner LIKE ? OR si.reason_codes LIKE ?)"
                like = f"%{q}%"
                params.extend([like, like, like])

            total = conn.execute(
                f"SELECT COUNT(*) c FROM selection_items si JOIN tests t ON t.id=si.test_id WHERE {where}",
                params,
            ).fetchone()["c"]
            params.extend([limit, offset])
            items = conn.execute(
                f"""
                SELECT si.score, si.reason_codes,
                       t.id, t.nodeid, t.domain, t.suite, t.owner, t.base_duration,
                       t.truth_flaky, t.archetype_gt
                FROM selection_items si JOIN tests t ON t.id=si.test_id
                WHERE {where}
                ORDER BY si.score DESC, si.test_id ASC LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()

        item_list = []
        for index, r in enumerate(items, start=offset + 1):
            d = dict(r)
            codes = d.pop("reason_codes", "") or ""
            d["rank"] = index
            d["duration_ms"] = float(d.pop("base_duration") or 0) * 1000.0
            d["reason_codes"] = codes.split(",") if codes else []
            d["reason_detail"] = {}
            item_list.append(d)

        missed_tests = []
        if snap and snap["missed_test_ids_json"]:
            missed_ids = json.loads(snap["missed_test_ids_json"])
            if missed_ids:
                placeholders = ",".join("?" for _ in missed_ids)
                missed_rows = conn.execute(
                    f"SELECT id, nodeid, domain, owner FROM tests WHERE id IN ({placeholders})",
                    missed_ids,
                ).fetchall()
                missed_tests = [dict(r) for r in missed_rows]

        return {
            "commit": dict(commit),
            "snapshot": dict(snap) if snap else None,
            "files": [dict(r) for r in files],
            "items": item_list,
            "missed_tests": missed_tests,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def run_report(self, sha: str) -> Dict[str, Any]:
        with self.conn() as conn:
            commit = conn.execute("SELECT * FROM commits WHERE sha=?", (sha,)).fetchone()
            if not commit:
                commit = conn.execute("SELECT * FROM commits WHERE sha LIKE ?", (sha[:12] + "%",)).fetchone()
            if not commit:
                return {"error": "commit not found"}
            sha = commit["sha"]
            snap = conn.execute("SELECT * FROM selection_snapshots WHERE sha=?", (sha,)).fetchone()
            rows = conn.execute(
                """
                SELECT t.id, t.nodeid, t.owner, o.category_gt, o.error_type, o.message,
                       o.incident_id, i.introduced_seq,
                       f.detected, f.classification, f.status, f.causes_json,
                       EXISTS(
                           SELECT 1 FROM selection_items si
                           WHERE si.sha=? AND si.test_id=t.id
                       ) AS selected,
                       COALESCE((
                           SELECT SUM(CASE WHEN ro.status='pass' THEN 1 ELSE 0 END)
                           FROM test_observations ro
                           WHERE ro.seq=o.seq AND ro.test_id=o.test_id AND ro.attempt>0
                       ), 0) AS retry_pass_count
                FROM test_observations o
                JOIN tests t ON t.id=o.test_id
                LEFT JOIN incidents i ON i.id=o.incident_id
                LEFT JOIN flake_findings f ON f.test_id=t.id
                WHERE o.seq=? AND o.attempt=0 AND o.status='fail'
                ORDER BY o.category_gt, t.nodeid
                """,
                (sha, commit["seq"]),
            ).fetchall()

        real, flaky, other = [], [], []
        for r in rows:
            d = dict(r)
            d["causes"] = json.loads(d.pop("causes_json") or "[]")
            d["selected"] = bool(d["selected"])
            cat = d.get("category_gt") or ""
            if cat == "broken":
                real.append(d)
            elif cat.startswith("flaky:"):
                flaky.append(d)
            else:
                other.append(d)

        skipped = snap["skipped_count"] if snap else 0
        selected = snap["selected_count"] if snap else 20400
        total = snap["total_count"] if snap else 20400
        lines = [
            f"## 本次运行报告 `{sha[:8]}`",
            "",
            f"提交：{commit['message']}  ",
            f"作者：{commit['author']} · {commit['created_at']}",
            "",
            f"- 选词执行：**{selected:,}/{total:,}**；跳过 **{skipped:,}** "
            f"({skipped / max(1, total) * 100:.1f}%)。",
        ]
        if snap:
            lines.append(
                f"- 预计 PR 反馈耗时：**{snap['selected_duration_sec'] / 60:.1f} 分钟**，"
                f"基线 {snap['baseline_duration_sec'] / 60:.1f} 分钟。"
            )
        lines += [
            f"- 首跑失败：{len(rows)}；真实提交破坏：**{len(real)}**；"
            f"已知/真值不稳定：**{len(flaky)}**；其他：{len(other)}。",
            "",
        ]

        if real:
            lines.append("### 真实失败（阻塞合并/需要修复）")
            for d in real:
                caught = "已选中" if d["selected"] else "PR 漏检，夜间全量兜底"
                freshness = "本次提交新引入" if d.get("introduced_seq") == commit["seq"] else "既有未修复破坏"
                lines.append(
                    f"- **[{caught} · {freshness}]** `{d['nodeid']}` — {d['error_type'] or 'Failure'}"
                )
            lines.append("")

        if flaky:
            lines.append("### 已知不稳定失败（隔离观察，不阻塞合并）")
            for d in flaky:
                cause = d["causes"][0]["label"] if d["causes"] else (d["category_gt"] or "").replace("flaky:", "")
                retry = "；重跑已恢复" if d["retry_pass_count"] else ""
                status = d.get("status") or "unclassified"
                lines.append(f"- `{d['nodeid']}` — {cause}；状态 `{status}`{retry}")
            lines.append("")

        if other:
            lines.append("### 待分类失败")
            for d in other:
                lines.append(f"- `{d['nodeid']}` — {d['error_type'] or 'Failure'}")
            lines.append("")

        if snap and snap["missed_true_failures"]:
            lines.append(
                f"> 注意：本次有 {snap['missed_true_failures']} 个真实失败未被 PR 选中，"
                "已由夜间全量发现并用于校准。"
            )
        elif real:
            lines.append("> 所有真实失败均已被安全档选中。")

        return {
            "sha": sha,
            "markdown": "\n".join(lines),
            "real_failures": real,
            "flaky_failures": flaky,
            "other_failures": other,
            "skipped_count": skipped,
            "selected_count": selected,
        }

    def flaky_list(self, filter_name: str = "detected", q: str = "", limit: int = 300) -> Dict[str, Any]:
        limit = min(max(1, limit), 1000)
        where = []
        params: list = []
        if filter_name == "detected":
            where.append("f.detected=1")
        elif filter_name in ("quarantined", "broken_suspect", "healthy"):
            where.append("f.status=?")
            params.append(filter_name)
        elif filter_name == "truth":
            where.append("t.truth_flaky=1")
        elif filter_name == "all":
            where.append("1=1")
        else:
            where.append("f.detected=1")

        if q:
            where.append("(t.nodeid LIKE ? OR t.owner LIKE ? OR t.domain LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])

        sql_where = " AND ".join(where)
        params.append(limit)
        with self.conn() as conn:
            rows = conn.execute(
                f"""
                SELECT t.id, t.nodeid, t.domain, t.suite, t.owner, t.truth_flaky,
                       t.archetype_gt, t.broken_seq, t.healed_seq,
                       f.flake_score, f.pass_rate, f.fail_count, f.retry_pass_count,
                       f.mixed_sha_count, f.flip_count, f.classification, f.status,
                       f.causes_json, f.boundary_seq
                FROM tests t LEFT JOIN flake_findings f ON f.test_id=t.id
                WHERE {sql_where}
                ORDER BY COALESCE(f.flake_score, 0) DESC, t.id
                LIMIT ?
                """,
                params,
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["causes"] = json.loads(d.pop("causes_json") or "[]")
            result.append(d)
        return {"tests": result}

    def flaky_detail(self, test_id: int) -> Dict[str, Any]:
        with self.conn() as conn:
            test = conn.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
            if not test:
                return {"error": "test not found"}
            finding = conn.execute("SELECT * FROM flake_findings WHERE test_id=?", (test_id,)).fetchone()
            daily = conn.execute(
                """
                WITH days AS (
                    SELECT DISTINCT substr(created_at, 1, 10) AS day FROM ci_runs
                ),
                fd AS (
                    SELECT substr(created_at, 1, 10) AS day,
                           SUM(CASE WHEN attempt=0 AND status='fail' THEN 1 ELSE 0 END) AS fails,
                           SUM(CASE WHEN attempt>0 AND status='pass' THEN 1 ELSE 0 END) AS retry_passes
                    FROM test_observations WHERE test_id=?
                    GROUP BY day
                )
                SELECT d.day,
                       (SELECT COUNT(*) FROM ci_runs r
                        WHERE substr(r.created_at,1,10)=d.day) AS runs,
                       COALESCE(fd.fails,0) AS fails,
                       COALESCE(fd.retry_passes,0) AS retry_passes
                FROM days d LEFT JOIN fd ON fd.day=d.day
                ORDER BY d.day
                """,
                (test_id,),
            ).fetchall()
            observations = conn.execute(
                """
                SELECT o.seq, o.commit_sha, o.attempt, o.status, o.duration_ms,
                       o.worker, o.shard, o.load, o.error_type, o.message,
                       o.category_gt, o.created_at, p.nodeid AS preceding_nodeid
                FROM test_observations o
                LEFT JOIN tests p ON p.id=o.preceding_test_id
                WHERE o.test_id=?
                ORDER BY o.seq DESC, o.attempt ASC LIMIT 80
                """,
                (test_id,),
            ).fetchall()
            events = conn.execute(
                "SELECT * FROM quarantine_events WHERE test_id=? ORDER BY seq DESC",
                (test_id,),
            ).fetchall()
            incidents = conn.execute(
                """
                SELECT i.* FROM incidents i JOIN incident_tests it ON it.incident_id=i.id
                WHERE it.test_id=? ORDER BY i.introduced_seq
                """,
                (test_id,),
            ).fetchall()

        daily_series = []
        for r in daily:
            d = dict(r)
            d["pass_rate"] = max(0.0, min(1.0, 1 - d["fails"] / max(1, d["runs"])))
            daily_series.append(d)

        finding_dict = dict(finding) if finding else None
        if finding_dict:
            finding_dict["causes"] = json.loads(finding_dict.pop("causes_json") or "[]")
            finding_dict["evidence"] = json.loads(finding_dict.pop("evidence_json") or "{}")

        return {
            "test": dict(test),
            "finding": finding_dict,
            "daily": daily_series,
            "observations": [dict(r) for r in observations],
            "events": [dict(r) for r in events],
            "incidents": [dict(r) for r in incidents],
        }

    def quarantine_events(self, limit: int = 120) -> Dict[str, Any]:
        limit = min(max(1, limit), 500)
        with self.conn() as conn:
            rows = conn.execute(
                """
                SELECT qe.*, t.nodeid, t.domain, t.owner
                FROM quarantine_events qe JOIN tests t ON t.id=qe.test_id
                ORDER BY qe.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
            result.append(d)
        return {"events": result}

    def report(self) -> Dict[str, str]:
        if REPORT_PATH.exists():
            return {"markdown": REPORT_PATH.read_text(encoding="utf-8")}
        return {"markdown": "尚未生成报告，请先运行 `python -m ci_optimizer demo`。"}


def create_handler(api: DashboardAPI):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Keep terminal output compact.
            return

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if path.suffix in (".js", ".css"):
                ctype = "text/" + path.suffix[1:] + "; charset=utf-8"
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            try:
                if path.startswith("/api/"):
                    if path == "/api/overview":
                        return self._send_json(api.overview())
                    if path == "/api/commits":
                        return self._send_json(api.commits(int(qs.get("limit", ["500"])[0])))
                    if path == "/api/selection":
                        sha = qs.get("sha", [""])[0]
                        if not sha:
                            return self._send_json({"error": "sha required"}, 400)
                        return self._send_json(api.selection(
                            sha,
                            q=qs.get("q", [""])[0],
                            limit=int(qs.get("limit", ["200"])[0]),
                            offset=int(qs.get("offset", ["0"])[0]),
                        ))
                    if path == "/api/run_report":
                        sha = qs.get("sha", [""])[0]
                        if not sha:
                            return self._send_json({"error": "sha required"}, 400)
                        return self._send_json(api.run_report(sha))
                    if path == "/api/flaky":
                        return self._send_json(api.flaky_list(
                            qs.get("filter", ["detected"])[0],
                            qs.get("q", [""])[0],
                            int(qs.get("limit", ["300"])[0]),
                        ))
                    if path == "/api/flaky_detail":
                        return self._send_json(api.flaky_detail(int(qs.get("id", ["0"])[0])))
                    if path == "/api/quarantine_events":
                        return self._send_json(api.quarantine_events(int(qs.get("limit", ["120"])[0])))
                    if path == "/api/report":
                        return self._send_json(api.report())
                    return self._send_json({"error": "unknown api"}, 404)

                if path in ("/", "/index.html"):
                    return self._send_file(STATIC_DIR / "index.html")
                if path == "/app.js":
                    return self._send_file(STATIC_DIR / "app.js")
                if path == "/styles.css":
                    return self._send_file(STATIC_DIR / "styles.css")
                if path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                return self.send_error(404)
            except Exception as exc:  # noqa: BLE001 - dashboard should surface API errors
                return self._send_json({"error": type(exc).__name__, "detail": str(exc)}, 500)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, db_path: Path = DB_PATH) -> None:
    db_path = Path(db_path)
    if not db_path.exists():
        # Create an empty schema so API calls return structured zero data rather
        # than "no such table"; the UI then prompts to run the demo generator.
        from .db import connect, init_db
        empty_conn = connect(db_path)
        try:
            init_db(empty_conn)
        finally:
            empty_conn.close()
        print(f"No simulation database found at {db_path}.")
        print("Run `python -m ci_optimizer demo` in another terminal to generate it.")

    api = DashboardAPI(db_path)
    httpd = ThreadingHTTPServer((host, port), create_handler(api))
    url = f"http://{host}:{port}/"
    print(f"CI intelligence dashboard: {url}")
    print("Press Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping dashboard")
    finally:
        httpd.server_close()
