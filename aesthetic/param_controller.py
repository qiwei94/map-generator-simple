"""调参控制器：两阶段模式感知搜索（B 内核，无 LLM 依赖）.

阶段 1 · 模式扫描：以当前连续参数为锚，逐个试 BO_MODES 中未试过的模式，
         赢者成为新基点（模式切换后连续步长重置——地形已变）。
阶段 2 · 连续爬山：在最优模式下做 pattern search（步长衰减/反向减半/保留最优）。

模式感知：带 live_modes 标记的参数仅在其模式下活跃（如 density/count 只在
fill 类模式是活杠杆，oriented_bbox 下自动跳过，不烧无效探测）。
"""

from .config import PARAM_SPACE, BO_MODES


class PatternSearchController:
    def __init__(self, seed: dict, param_space: dict = None,
                 modes: list = None):
        self.space = param_space or PARAM_SPACE
        self.modes = modes or BO_MODES

        self.current = {k: self._clamp(k, seed[k]) for k in self.space
                        if k in seed}
        self.current_mode = seed.get("bo_mode", self.modes[0])
        self._modes_todo = [m for m in self.modes if m != self.current_mode]
        self._phase = "mode_scan" if self._modes_todo else "pattern_search"

        self._reset_steps()
        self.directions = {k: 1 for k in self.space}
        self.best_params = None
        self.best_score = float("-inf")
        self._rr_index = 0
        self._probe = None          # (kind, name, prev_value)
        self._baseline_set = False

    # ─── 内部 ────────────────────────────────────────────────────────

    def _reset_steps(self):
        self.steps = {k: self.space[k][2] for k in self.space}

    def _clamp(self, name: str, v: float):
        lo, hi = self.space[name][0], self.space[name][1]
        is_int = self.space[name][4]
        v = max(lo, min(hi, float(v)))
        return int(round(v)) if is_int else v

    def _live_params(self):
        """当前模式下活跃的参数（步长仍在 min 以上且本模式可消费）。"""
        out = []
        for k, spec in self.space.items():
            live_modes = spec[5] if len(spec) > 5 else None
            if live_modes is not None and self.current_mode not in live_modes:
                continue
            if self.steps[k] >= spec[3]:
                out.append(k)
        return out

    # ─── 基线 ────────────────────────────────────────────────────────

    def set_baseline(self, score: float) -> None:
        self.best_score = score
        self.best_params = self._snapshot()
        self._baseline_set = True

    def _snapshot(self) -> dict:
        return {**self.current, "bo_mode": self.current_mode}

    # ─── 探测 ────────────────────────────────────────────────────────

    def next_probe(self):
        """返回一份待评估的完整 params dict（含 bo_mode）；收敛返回 None。"""
        if not self._baseline_set:
            raise RuntimeError("call set_baseline() before probing")

        # 阶段 1：模式扫描
        if self._phase == "mode_scan":
            if self._modes_todo:
                mode = self._modes_todo.pop(0)
                self._probe = ("mode", mode, self.current_mode)
                return {**self.current, "bo_mode": mode}
            self._phase = "pattern_search"

        # 阶段 2：连续爬山（模式感知活杠杆）
        active = self._live_params()
        if not active:
            return None
        for _ in range(len(active)):
            name = active[self._rr_index % len(active)]
            self._rr_index += 1
            cand = self.current[name] + self.directions[name] * self.steps[name]
            cand = self._clamp(name, cand)
            if cand == self.current[name]:
                self.steps[name] *= 0.5
                self.directions[name] *= -1
                continue
            self._probe = ("param", name, self.current[name])
            probe = dict(self.current)
            probe[name] = cand
            probe["bo_mode"] = self.current_mode
            return probe
        return None

    def report(self, score: float) -> str:
        """汇报探测得分，返回 accepted / rejected。"""
        assert self._probe is not None, "no pending probe"
        kind, name, prev = self._probe

        if score > self.best_score:
            self.best_score = score
            if kind == "mode":
                self.current_mode = name
                self._reset_steps()       # 地形已变，连续步长重置
            else:
                self.current[name] = self._clamp(
                    name, self.current[name]
                    + self.directions[name] * self.steps[name])
                self.steps[name] = min(
                    self.steps[name] * 1.2,
                    self.space[name][1] - self.space[name][0])
            self.best_params = self._snapshot()
            verdict = "accepted"
        else:
            if kind == "param":
                self.steps[name] *= 0.5
                self.directions[name] *= -1
            verdict = "rejected"
        self._probe = None
        return verdict

    @property
    def converged(self) -> bool:
        if not self._baseline_set:
            return False
        return self._phase == "pattern_search" and not self._live_params()
