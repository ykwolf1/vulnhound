"""CLI：python -m eval --target http://127.0.0.1:8080 --runs 3 --concurrency 1 --gt dvwa"""

import argparse
import asyncio

from eval.report import save_result
from eval.runner import run_batch


def main() -> None:
    ap = argparse.ArgumentParser(prog="eval", description="vulnhound 量化评测（需 DVWA + LLM_API_KEY）")
    ap.add_argument("--target", default="http://127.0.0.1:8080")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--gt", default="dvwa", help="ground truth 评测集名（eval/groundtruth/<name>.json）")
    ap.add_argument("--loop-version", default="v2", choices=["v1", "v2"])
    args = ap.parse_args()

    result = asyncio.run(
        run_batch(args.target, args.runs, args.concurrency, gt_name=args.gt, loop_version=args.loop_version)
    )
    eid = save_result(result)
    print(f"eval_id={eid}")
    from eval.report import render_markdown
    print(render_markdown(result))


if __name__ == "__main__":
    main()
