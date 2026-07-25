"""美学闭环主循环：生成 → 评审 → 调参 → 再生成。

收敛停止：分数达标 / 连续 plateau 轮无提升 / 控制器步长全收敛 / 达 max_rounds。
始终保留历史最优（抗评分噪声）。单轮 preprocess 异常按 0 分拒绝处理，不中断闭环。
"""

import os
import time
from dataclasses import dataclass, field

from .config import (
    MAX_ROUNDS, TARGET_SCORE, PLATEAU_ROUNDS, VLM_BLEND_WEIGHT,
)
from .metrics import compute_metrics
from .param_controller import PatternSearchController
from .review_agent import vlm_score
from .review_render import render_review_bundle
from .rerun_harness import CityHarness
from .trajectory import Trajectory


@dataclass
class LoopResult:
    city: str
    prototype: str
    best_params: dict
    best_score: float
    baseline_score: float
    rounds: int
    stop_reason: str
    trajectory_path: str
    summary_path: str
    best_renders: dict = field(default_factory=dict)


class AestheticLoop:
    def __init__(self, harness: CityHarness, out_dir: str,
                 use_vlm: bool = False, max_rounds: int = MAX_ROUNDS,
                 target: float = TARGET_SCORE,
                 seed_override: dict = None, modes: list = None):
        self.harness = harness
        self.out_dir = out_dir
        self.use_vlm = use_vlm
        self.max_rounds = max_rounds
        self.target = target
        self.seed_override = seed_override   # 上次学成的配置（续跑）
        self.modes = modes                   # None = 全模式扫描

    # ─── 单轮评估 ────────────────────────────────────────────────────

    def _evaluate(self, params: dict, tag: str):
        layers = self.harness.run_round(params)
        bundle = render_review_bundle(
            layers, self.harness.ctx,
            road_width_multiplier=float(params["road_width_multiplier"]),
            out_dir=self.out_dir, tag=tag)
        result = compute_metrics(layers, bundle, self.harness.preset.prototype)
        score = result["overall"]

        vlm = None
        if self.use_vlm:
            vlm = vlm_score(bundle, self.harness.preset.reference_images,
                            self.harness.preset.name)
            if vlm is not None:
                score = round((1 - VLM_BLEND_WEIGHT) * score
                              + VLM_BLEND_WEIGHT * vlm["overall"], 3)

        return score, bundle, result, vlm

    # ─── 主循环 ──────────────────────────────────────────────────────

    def run(self) -> LoopResult:
        self.harness.prepare()
        preset = self.harness.preset
        traj = Trajectory(self.out_dir)
        seed = self.seed_override or self.harness.seed_params()
        controller = PatternSearchController(seed, modes=self.modes)

        print(f"\n[loop] start: city={preset.name} proto={preset.prototype} "
              f"target={self.target} max_rounds={self.max_rounds} "
              f"vlm={'on' if self.use_vlm else 'off'}")

        # Round 0: 基线
        t0 = time.time()
        score0, bundle0, m0, vlm0 = self._evaluate(controller.current,
                                                   "r00_baseline")
        controller.set_baseline(score0)
        traj.log({"round": 0, "kind": "baseline", "params": controller.current,
                  "score": score0, "metrics": m0["metrics"],
                  "details": m0["details"],
                  "renders": {"topdown": bundle0["topdown"],
                              "height": bundle0["height"]},
                  "vlm": vlm0, "wall_s": round(time.time() - t0, 1)})
        print(f"  [r00] baseline score={score0:.2f} "
              f"metrics={_fmt_metrics(m0['metrics'])}")

        best_bundle = {"topdown": bundle0["topdown"], "height": bundle0["height"]}
        plateau = 0
        # pattern search 的拒绝是正常探索成本（反向减半重试），
        # plateau 阈值必须覆盖「全参数 × 双向」一轮，否则会错杀未试方向的杠杆
        plateau_limit = max(PLATEAU_ROUNDS, 2 * len(controller.space))
        stop_reason = "max_rounds"
        rounds_done = 0

        for r in range(1, self.max_rounds + 1):
            probe = controller.next_probe()
            if probe is None:
                stop_reason = "converged"
                break
            rounds_done = r
            _snap = {**controller.current, "bo_mode": controller.current_mode}
            changed = [k for k in probe if probe[k] != _snap.get(k)]

            tr = time.time()
            try:
                score, bundle, m, vlm = self._evaluate(probe, f"r{r:02d}")
            except Exception as e:
                print(f"  [r{r:02d}] round failed ({e}); treat as reject")
                controller.report(0.0)
                traj.log({"round": r, "kind": "error", "params": probe,
                          "error": str(e)})
                plateau += 1
                if plateau >= plateau_limit:
                    stop_reason = "plateau"
                    break
                continue

            verdict = controller.report(score)
            plateau = 0 if verdict == "accepted" else plateau + 1
            if verdict == "accepted":
                best_bundle = {"topdown": bundle["topdown"],
                               "height": bundle["height"]}

            traj.log({"round": r, "kind": "probe", "changed": changed,
                      "params": probe, "score": score, "verdict": verdict,
                      "metrics": m["metrics"], "details": m["details"],
                      "renders": {"topdown": bundle["topdown"],
                                  "height": bundle["height"]},
                      "vlm": vlm, "wall_s": round(time.time() - tr, 1)})
            print(f"  [r{r:02d}] {changed[0] if changed else '?'} → "
                  f"score={score:.2f} ({verdict}) best={controller.best_score:.2f}")

            if score >= self.target:
                stop_reason = "target_reached"
                break
            if plateau >= plateau_limit:
                stop_reason = "plateau"
                break

        summary_path = traj.write_summary({
            "city": preset.name, "prototype": preset.prototype,
            "baseline_score": score0,
            "best_score": controller.best_score,
            "improvement": round(controller.best_score - score0, 3),
            "best_params": controller.best_params,
            "rounds": rounds_done, "stop_reason": stop_reason,
            "best_renders": best_bundle,
            "resolved_by": "aesthetic-loop (B-core"
                           + ("+vlm" if self.use_vlm else "") + ")",
        })

        # 每城配置档案：学成的 best_params 持久化，供下次续跑/回喂主管线
        import json as _json
        from datetime import datetime as _dt
        best_cfg_path = os.path.join(self.out_dir, "best_config.json")
        with open(best_cfg_path, "w", encoding="utf-8") as f:
            _json.dump({
                "city": preset.name, "prototype": preset.prototype,
                "learned_at": _dt.now().isoformat(timespec="seconds"),
                "best_score": controller.best_score,
                **controller.best_params,
            }, f, indent=2, ensure_ascii=False)
        print(f"  best config saved: {best_cfg_path}")
        print(f"\n[loop] done: {stop_reason}, best={controller.best_score:.2f} "
              f"(baseline {score0:.2f}, d{controller.best_score - score0:+.2f})")
        print(f"  best params: {controller.best_params}")

        return LoopResult(
            city=preset.name, prototype=preset.prototype,
            best_params=controller.best_params,
            best_score=controller.best_score,
            baseline_score=score0,
            rounds=rounds_done, stop_reason=stop_reason,
            trajectory_path=traj.path, summary_path=summary_path,
            best_renders=best_bundle,
        )


def _fmt_metrics(m: dict) -> str:
    return " ".join(f"{k}={v:.2f}" for k, v in m.items())
