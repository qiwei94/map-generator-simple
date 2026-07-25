"""快速冒烟测试：小区域验证管线完整性（~10秒）.

用法: python -m relief_studio.smoke_test
"""

import os
import sys
import time
import numpy as np

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def test_relief_pipeline():
    """用合成数据验证 relief_map + renderer 完整链路."""
    from relief_studio.relief_map import (
        build_relief_heightmap,
        compute_hillshade,
        compute_ambient_occlusion,
        compute_edge_darkening,
        _dilate_buildings,
    )
    from relief_studio.renderer import render_relief

    print("[smoke_test] 1. 合成测试数据...")
    # 模拟 256x256 高度图
    grid_size = 256
    heightmap = np.zeros((grid_size, grid_size), dtype=np.float32)

    # 模拟建筑群（随机方块）
    rng = np.random.default_rng(42)
    for _ in range(200):
        x, y = rng.integers(20, 200, 2)
        w, h = rng.integers(3, 12, 2)
        height = rng.uniform(5, 100)
        heightmap[y:y+h, x:x+w] = height

    # 模拟水体（右侧 1/3）
    water_mask = np.zeros((grid_size, grid_size), dtype=bool)
    water_mask[:, 180:] = True
    heightmap[water_mask] = 0

    relief_data = {
        "heightmap": heightmap,
        "water_mask": water_mask,
        "transform": None,
        "bbox_utm": (0, 0, 2560, 2560),
        "grid_size": grid_size,
    }

    print("[smoke_test] 2. 测试 hillshade...")
    shade = compute_hillshade(heightmap, pixel_size=10.0)
    assert shade.shape == (grid_size, grid_size)
    assert 0 <= shade.min() and shade.max() <= 1.0

    print("[smoke_test] 3. 测试 AO...")
    ao = compute_ambient_occlusion(heightmap, radius=3)
    assert ao.shape == (grid_size, grid_size)

    print("[smoke_test] 4. 测试边缘暗化...")
    edges = compute_edge_darkening(heightmap)
    assert edges.shape == (grid_size, grid_size)

    print("[smoke_test] 5. 测试膨胀...")
    dilated = _dilate_buildings(heightmap, iterations=2)
    coverage_before = (heightmap > 0).mean()
    coverage_after = (dilated > 0).mean()
    assert coverage_after > coverage_before, "膨胀后覆盖率应增大"
    print(f"  覆盖率: {coverage_before*100:.1f}% → {coverage_after*100:.1f}%")

    print("[smoke_test] 6. 测试渲染 (mono_light)...")
    out_dir = os.path.join(_project_root, "relief_studio", "output")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "smoke_test_light.png")
    render_relief(relief_data, out_path, style="mono_light", output_size_px=512)
    assert os.path.exists(out_path)

    print("[smoke_test] 7. 测试渲染 (mono_dark)...")
    out_path2 = os.path.join(out_dir, "smoke_test_dark.png")
    render_relief(relief_data, out_path2, style="mono_dark", output_size_px=512)
    assert os.path.exists(out_path2)

    print("[smoke_test] 8. 测试渲染 (warm)...")
    out_path3 = os.path.join(out_dir, "smoke_test_warm.png")
    render_relief(relief_data, out_path3, style="warm", output_size_px=512)
    assert os.path.exists(out_path3)

    # 验证图像内容
    from PIL import Image
    img = np.array(Image.open(out_path))
    # 水体区域应该是黑色（取深处避免边缘插值）
    water_region = img[:, 400:] if img.ndim == 2 else img[:, 400:, :]
    assert water_region.max() < 15, f"水体应接近纯黑, max={water_region.max()}"

    print(f"\n{'='*50}")
    print(f"  ALL TESTS PASSED")
    print(f"  output: {out_dir}/smoke_test_*.png")
    print(f"{'='*50}")


def test_agent_imports():
    """验证 agent 模块导入正常."""
    print("\n[smoke_test] 9. 测试 agent 导入...")
    from relief_studio.agent.tools import generate_relief, evaluate_image, get_data_stats
    from relief_studio.agent.relief_agent import create_relief_agent, SYSTEM_PROMPT
    assert "艺术总监" in SYSTEM_PROMPT
    print("  agent module import OK")


if __name__ == "__main__":
    t0 = time.time()
    test_relief_pipeline()
    test_agent_imports()
    print(f"\n  总耗时: {time.time()-t0:.1f}s")
