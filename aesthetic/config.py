"""闭环专属常量：调参边界、步长、收敛阈值、原型权重。

独立于此包，不改动主 config.py —— 所有注入走运行时猴补丁/显式 override。
"""

import os

# ─── 参考作品目录（city_demo）─────────────────────────────────────────
CITY_DEMO_DIR = os.environ.get(
    "CITY_DEMO_DIR", r"C:\Users\kiwi\OneDrive\Desktop\city_demo"
)

# ─── 闭环默认参数 ─────────────────────────────────────────────────────
MAX_ROUNDS = 10
TARGET_SCORE = 8.0          # 0-10 制，达到即收敛
PLATEAU_ROUNDS = 4          # 连续 N 轮无提升 → 收敛
EPSILON = 0.05              # 分数提升 < 此值视为无提升

# ─── BO 生成模式（闭环可调的离散维度）──────────────────────────────
# 全部可用模式（_aggregate_in_blocks 实现）；种子=当前 config 的 BUILDING_V2_MODE
BO_MODES = [
    "oriented_bbox",   # 现状：矩形∩街区，碎边
    "block_fill",      # 密度达标整块填充（reference 风格，饱满块）
    "density_fill",    # 密整块/疏缓冲，折中
    "convex_hull",     # 凸包∩街区
    "concave_hull",    # 凹包∩街区
    "union",           # 原始并集∩街区
    "buffered_union",  # 缓冲并集∩街区
]

# ─── 可调参数边界 / 步长（B 内核只迭代“便宜”的图层/渲染参数）─────────
# name: (lo, hi, init_step, min_step, is_int, live_modes|None)
# live_modes=None → 所有模式下都是活杠杆；否则仅在列出的模式下活跃。
PARAM_SPACE = {
    "building_print_limit_m2": (1000.0, 8000.0, 800.0, 200.0, False, None),
    # 注：print_limit 走 BL 个体分类（size_lm），在 block_fill 下仍活；
    # 但 BO 出口面积过滤在 block_fill 被跳过（建筑块不再受限）。
    "building_v2_road_tier": (2, 5, 1, 1, True, None),
    "road_width_multiplier": (2.0, 8.0, 0.8, 0.2, False, None),
    "building_height_mm_max": (2.8, 12.0, 1.2, 0.3, False, None),
    "building_simplify_tol_m": (5.0, 40.0, 5.0, 1.0, False, None),
    "aggregate_simplify_m": (10.0, 80.0, 10.0, 2.0, False, None),
    # 以下两个只在 fill 类模式下被消费（oriented_bbox 下是死杠杆，已实证）
    "building_density_threshold": (0.001, 0.05, 0.004, 0.0005, False,
                                   ("block_fill", "density_fill")),
    "building_count_threshold": (1, 5, 1, 1, True, ("block_fill",)),
}

# ─── 指标目标带（按城市原型）─────────────────────────────────────────
# coverage: 建筑面积占比目标带 (lo, hi, falloff)
# height_spread_mm: DSM 高度 p90-median 的目标落差（天际线城市要高）
# landmark_count: 地标数量目标带
PROTOTYPE_TARGETS = {
    "skyline": {
        "coverage": (0.25, 0.55, 0.15),
        "height_spread_mm": 2.5,
        "landmark_ratio": (0.05, 0.40, 0.05),
        "road_ratio": (0.005, 0.05, 0.005),
        "weights": {
            "coverage": 0.16, "regularity": 0.16, "height_var": 0.26,
            "water": 0.10, "landmark": 0.12, "road": 0.12, "edge": 0.08,
        },
    },
    "landscape": {
        "coverage": (0.10, 0.35, 0.10),
        "height_spread_mm": 0.8,
        "landmark_ratio": (0.02, 0.25, 0.02),
        "road_ratio": (0.005, 0.05, 0.005),
        "weights": {
            "coverage": 0.13, "regularity": 0.13, "height_var": 0.09,
            "water": 0.27, "landmark": 0.21, "road": 0.12, "edge": 0.05,
        },
    },
    "classic": {
        "coverage": (0.18, 0.45, 0.12),
        "height_spread_mm": 1.5,
        "landmark_ratio": (0.03, 0.35, 0.03),
        "road_ratio": (0.005, 0.05, 0.005),
        "weights": {
            "coverage": 0.16, "regularity": 0.16, "height_var": 0.17,
            "water": 0.17, "landmark": 0.15, "road": 0.12, "edge": 0.07,
        },
    },
}

# ─── 渲染 ─────────────────────────────────────────────────────────────
REVIEW_GRID_SIZE = 2048     # 评审图栅格边长
HEIGHT_DSM_SIZE = 1024      # 高度场栅格边长

# ─── VLM（可插拔）─────────────────────────────────────────────────────
VLM_BLEND_WEIGHT = 0.35     # final = (1-w)*metric + w*vlm
