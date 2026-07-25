"""美学闭环 CLI 入口。

用法：
    python -m aesthetic.run_aesthetic --city chicago
    python -m aesthetic.run_aesthetic --city westlake --max-rounds 12 --use-vlm
    python -m aesthetic.run_aesthetic --city chicago --prep-only   # 只准备数据
"""

import argparse
import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from aesthetic.config import MAX_ROUNDS, TARGET_SCORE
from aesthetic.loop import AestheticLoop
from aesthetic.presets import get_preset, list_presets
from aesthetic.rerun_harness import CityHarness


def main():
    # Windows GBK 控制台防 UnicodeEncodeError（其余字符集环境无影响）
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    ap = argparse.ArgumentParser(description="城市地图美学闭环自动调参")
    ap.add_argument("--city", required=True, choices=list_presets())
    ap.add_argument("--max-rounds", type=int, default=MAX_ROUNDS)
    ap.add_argument("--target", type=float, default=TARGET_SCORE)
    ap.add_argument("--use-vlm", action="store_true",
                    help="叠加视觉大模型评分（需 ANTHROPIC_API_KEY）")
    ap.add_argument("--no-cache", action="store_true",
                    help="禁用 PipelineCache（全量重算）")
    ap.add_argument("--prep-only", action="store_true",
                    help="只做一次性数据准备，不进闭环")
    ap.add_argument("--fresh", action="store_true",
                    help="忽略已学成的 best_config.json，从规则引擎种子重跑")
    ap.add_argument("--rescan-modes", action="store_true",
                    help="即使有学成模式也重新全模式扫描")
    ap.add_argument("--out-dir", default=None,
                    help="输出目录（默认 output/aesthetic/<city>）")
    args = ap.parse_args()

    preset = get_preset(args.city)
    out_dir = args.out_dir or os.path.join(
        _PROJECT_ROOT, "output", "aesthetic", preset.name)

    harness = CityHarness(preset, use_cache=not args.no_cache)
    harness.prepare()

    if args.prep_only:
        print(f"\n[done] prep-only. profile={harness.profile.to_dict()}")
        return

    # 学成续跑：默认从 best_config.json 续（不重扫模式）；--fresh 重零开始
    seed_override, modes = None, None
    best_cfg_path = os.path.join(out_dir, "best_config.json")
    if not args.fresh and os.path.exists(best_cfg_path):
        with open(best_cfg_path, encoding="utf-8") as f:
            saved = json.load(f)
        seed_override = {k: v for k, v in saved.items()
                         if k not in ("city", "prototype", "learned_at",
                                      "best_score")}
        if not args.rescan_modes and "bo_mode" in seed_override:
            modes = [seed_override["bo_mode"]]   # 学成模式直接续调，不重扫
        print(f"[seed] continuing from learned config: {best_cfg_path} "
              f"(score={saved.get('best_score')}, mode={seed_override.get('bo_mode')})")

    loop = AestheticLoop(harness, out_dir=out_dir,
                         use_vlm=args.use_vlm,
                         max_rounds=args.max_rounds, target=args.target,
                         seed_override=seed_override, modes=modes)
    result = loop.run()

    print(f"\n{'=' * 60}")
    print(f"  {result.city} ({result.prototype}) — {result.stop_reason}")
    print(f"  baseline={result.baseline_score:.2f} → best={result.best_score:.2f}")
    print(f"  best params:")
    print(json.dumps(result.best_params, indent=4, ensure_ascii=False))
    print(f"  renders: {result.best_renders.get('topdown')}")
    print(f"  summary: {result.summary_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
