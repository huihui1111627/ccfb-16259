from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
GENERATED_REPO_DIR = ROOT / "generated_repo"
DB_PATH = DATA_DIR / "ci_optimizer.sqlite3"
METRICS_PATH = DATA_DIR / "metrics.json"
REPORT_PATH = DATA_DIR / "report.md"


@dataclass
class DemoConfig:
    seed: int = 20261125
    n_tests: int = 20_400
    days: int = 184
    start_date: str = "2026-05-25T09:00:00"
    parallelism: int = 50
    setup_wall_sec: float = 120.0
    selected_setup_wall_sec: float = 55.0
    materialize_repo: bool = True
    db_path: Path = DB_PATH
    repo_dir: Path = GENERATED_REPO_DIR

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.materialize_repo:
            self.repo_dir.mkdir(parents=True, exist_ok=True)
