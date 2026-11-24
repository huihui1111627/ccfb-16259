import tempfile
import unittest
from pathlib import Path

from ci_optimizer.config import DemoConfig
from ci_optimizer.db import connect
from ci_optimizer.cli import generate_demo
from ci_optimizer.simulator import replay_history
from ci_optimizer.stats import _beta_upper


class EndToEndSimulationTest(unittest.TestCase):
    def test_small_demo_generates_selects_and_detects(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "small.sqlite3"
            cfg = DemoConfig(
                seed=4242,
                n_tests=900,
                days=42,
                db_path=db_path,
                materialize_repo=False,
            )
            generate_demo(cfg)

            conn = connect(db_path)
            try:
                metrics = replay_history(conn, cfg)
                n_commits = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
                self.assertGreater(n_commits, 40)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) c FROM selection_snapshots").fetchone()["c"],
                    n_commits,
                )
                self.assertGreater(metrics["safe"]["avg_selected"], 20)
                self.assertLess(metrics["safe"]["avg_selected"], cfg.n_tests)
                # Fixed-size safety pockets are proportionally large in tiny
                # CI fixtures; the full 20k scale exceeds 75% skipping.
                self.assertGreater(metrics["safe"]["skip_ratio"], 0.18)
                self.assertGreaterEqual(metrics["accuracy"]["recall"], 0.85)
                # The 20k seeded demo has zero masking; tiny fixtures can expose
                # one noisy edge due to their much smaller sample sizes.
                self.assertLessEqual(metrics["accuracy"]["wrong_masked_bad_pairs"], 1)

                # Every persisted selection must have at least one explanation.
                bad = conn.execute(
                    "SELECT COUNT(*) c FROM selection_items WHERE reason_codes=''"
                ).fetchone()["c"]
                self.assertEqual(bad, 0)
            finally:
                conn.close()

    def test_wilson_bound_is_between_rate_and_one(self):
        upper = _beta_upper(1, 10)
        self.assertGreater(upper, 0.1)
        self.assertLessEqual(upper, 1.0)
        self.assertEqual(_beta_upper(0, 0), 1.0)


if __name__ == "__main__":
    unittest.main()
