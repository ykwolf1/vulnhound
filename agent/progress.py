"""无进展检测（v1/v2 共用）：连续重复命令或 stdout 高相似度。"""

import difflib

__all__ = ["ProgressTracker", "stdout_similarity"]


def stdout_similarity(a: str, b: str) -> float:
    """两段 stdout 的相似度（0~1），基于 SequenceMatcher。"""
    return difflib.SequenceMatcher(None, a, b).ratio()


class ProgressTracker:
    """跟踪最近命令与 stdout，判断是否陷入无进展。

    与 MVP loop.py 原逻辑逐行为等价：
    - 连续 window 步命令完全相同 → 无进展
    - 相邻两次 stdout 非空且相似度 > threshold → 无进展
    """

    def __init__(self, window: int = 3, similarity_threshold: float = 0.9):
        self.window = window
        self.similarity_threshold = similarity_threshold
        self.recent_cmds: list[str] = []
        self.last_stdout: str | None = None

    def update(self, cmd: str, stdout: str) -> bool:
        """记录一步执行；返回 True 表示检测到无进展。"""
        self.recent_cmds.append(cmd)
        if len(self.recent_cmds) >= self.window and len(set(self.recent_cmds[-self.window :])) == 1:
            return True
        if (
            self.last_stdout is not None
            and stdout.strip()
            and self.last_stdout.strip()
            and stdout_similarity(self.last_stdout, stdout) > self.similarity_threshold
        ):
            return True
        self.last_stdout = stdout
        return False

    def reset(self) -> None:
        """方向切换后重置，新方向从零开始计进展。"""
        self.recent_cmds.clear()
        self.last_stdout = None
