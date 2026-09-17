"""V4 评测：打分（查准/查全/证据有效性）+ 跑批 + 并发 + 报告聚合。"""

from eval.scoring import score_session
from pathlib import Path

__all__ = ["score_session", "GROUNDTRUTH_DIR", "RESULTS_DIR"]

GROUNDTRUTH_DIR = Path(__file__).parent / "groundtruth"
RESULTS_DIR = Path(__file__).parent / "results"
