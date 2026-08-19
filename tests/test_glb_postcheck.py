# -*- coding: utf-8 -*-
"""draft GLB 落地后检（check_grounding）测试。

复现历史 bug：所有图层均匀悬浮一层地形厚度（z 基准不一致），
后检必须能抓住整层悬浮与大面积格级悬浮，并放过正常贴地场景。
"""
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from _TEXTURE_STYLE_OF_DEEPSEEK.render_glb import (  # noqa: E402
    _COLORS, _TerrainSampler, _terrain_heightfield, _drape_polys,
    check_grounding)


def flat_terrain(z=0.0, size=100.0, n=15):
    """平面地形网格 mesh（terrain 配色）。"""
    xs = np.linspace(0, size, n)
    xx, yy = np.meshgrid(xs, xs)
    verts = np.column_stack([xx.ravel(), yy.ravel(),
                             np.full(n * n, float(z))])
    idx = np.arange(n * n).reshape(n, n)
    f1 = np.column_stack([idx[:-1, :-1].ravel(), idx[1:, :-1].ravel(),
                          idx[:-1, 1:].ravel()])
    f2 = np.column_stack([idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(),
                          idx[1:, 1:].ravel()])
    m = trimesh.Trimesh(vertices=verts, faces=np.vstack([f1, f2]),
                        process=False)
    m.visual.vertex_colors = _COLORS["terrain"]
    return m


def box(layer, x, y, z, size=8.0, h=2.0):
    """指定图层配色的方块，底面在 z。"""
    m = trimesh.creation.box(extents=(size, size, h))
    m.apply_translation((x, y, z + h / 2.0))
    m.visual.vertex_colors = _COLORS[layer]
    return m


def scene_of(*meshes):
    s = trimesh.Scene()
    for m in meshes:
        s.add_geometry(m)
    return s


class TestCheckGrounding:
    def test_grounded_passes(self):
        s = scene_of(flat_terrain(0.0),
                     box("block_base", 30, 30, 0.0),
                     box("buildings", 60, 60, 0.6))
        r = check_grounding(s, verbose=False)
        assert r["hard"] == []
        assert r["warn"] == []

    def test_whole_layer_float_detected(self):
        # 历史 bug 场景：整层均匀悬浮 4mm（= TERRAIN_THICKNESS_MM）
        s = scene_of(flat_terrain(0.0), box("block_base", 30, 30, 4.0))
        r = check_grounding(s, verbose=False)
        assert len(r["hard"]) == 1
        assert "block_base" in r["hard"][0]
        assert "整层悬浮" in r["hard"][0]

    def test_marker_expected_height_ok(self):
        # marker 底部在地形 +1.5mm 是设计值，不应报悬浮
        m = box("block_base", 20, 20, 0.0)
        pin = trimesh.creation.cylinder(radius=1, height=6, sections=8)
        pin.apply_translation((50, 50, 1.5 + 3.0))
        pin.visual.vertex_colors = (226, 61, 61, 255)
        s = scene_of(flat_terrain(0.0), m, pin)
        r = check_grounding(s, verbose=False)
        assert r["hard"] == []

    def test_embedded_is_fine(self):
        # 宁埋不浮：嵌入地形不算违规
        s = scene_of(flat_terrain(2.0), box("water", 40, 40, 0.0))
        r = check_grounding(s, verbose=False)
        assert r["hard"] == []

    def test_no_terrain_no_crash(self):
        s = scene_of(box("roads", 10, 10, 5.0))
        r = check_grounding(s, verbose=False)
        assert r == {"hard": [], "warn": []}


# ─── 地形底座封闭性（历史 bug：只三角化顶面，无裙边无底）───

class TestTerrainBase:
    GRIDS = {
        "flat": np.zeros((64, 64)),
        "slope": np.tile(np.linspace(0, 500, 64), (64, 1)),
        "rough": np.random.default_rng(0).random((96, 96)) * 800,
        "none": None,
    }

    @pytest.mark.parametrize("kind", list(GRIDS))
    def test_watertight_solid_with_base(self, kind):
        m = _terrain_heightfield(
            self.GRIDS[kind], (-1000, -1000, 1000, 1000), scale=0.098,
            z_gamma=0.45, relief_mm_max=8.0, thickness_mm=4.0)
        assert m.is_watertight, f"{kind}: 地形未封闭（无裙边/底面？）"
        assert m.is_volume, f"{kind}: 地形非实体"
        assert m.volume > 0
        assert m.vertices[:, 2].min() == pytest.approx(-4.0), \
            f"{kind}: 底面应在 -thickness_mm"

    def test_top_surface_baseline_unchanged(self):
        """顶面仍以 z=0 为基准（与 _TerrainSampler 同基准，保图层贴地）。"""
        m = _terrain_heightfield(
            np.zeros((32, 32)), (-500, -500, 500, 500), scale=0.2,
            z_gamma=0.45, relief_mm_max=8.0, thickness_mm=4.0)
        zs = m.vertices[:, 2]
        assert zs.max() == pytest.approx(0.0), "平地顶面应在 z=0"

    def test_shared_print_contract_uses_formal_base_and_relief(self):
        """GLB 顶面/底面必须复用 3MF 的解析参数，不再按区域宽度猜。"""
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import Z_WATER_BASE_MM

        grid = np.tile(np.linspace(0, 100, 32), (32, 1))
        base_mm = 0.8
        relief_mm = 4.6
        terrain_base = Z_WATER_BASE_MM + base_mm
        m = _terrain_heightfield(
            grid, (0, 0, 1000, 1000), scale=0.1,
            z_gamma=1.0, relief_mm_max=relief_mm,
            surface_base_mm=terrain_base, bottom_z_mm=Z_WATER_BASE_MM)

        n_top = grid.size
        top_z = m.vertices[:n_top, 2]
        assert m.vertices[:, 2].min() == pytest.approx(Z_WATER_BASE_MM)
        assert top_z.min() == pytest.approx(terrain_base)
        assert top_z.max() == pytest.approx(terrain_base + relief_mm)

        sampler = _TerrainSampler(
            grid, (0, 0, 1000, 1000), scale=0.1,
            z_gamma=1.0, relief_mm_max=relief_mm,
            z_base_mm=terrain_base)
        assert sampler.z_mm(0, 500) == pytest.approx(top_z.min())
        assert sampler.z_mm(1000, 500) == pytest.approx(top_z.max())


# ─── 网格朝向（历史 bug：row 0 = 南的网格被按 row 0 = 北处理，
#     地形起伏南北翻转，水体坐到镜像高地 → 水面突出地表）───

class TestGridOrientation:
    def test_sampler_row0_is_south(self):
        # 约定：fetch_elevation_grid 返回 row 0 = south。
        # 构造只有北侧（最后一行）高的网格，采样必须在 y_max 处取到高值
        grid = np.zeros((32, 32))
        grid[-1, :] = 100.0
        smp = _TerrainSampler(grid, (0, 0, 1000, 1000), scale=0.1,
                              z_gamma=1.0, relief_mm_max=10.0)
        assert smp.z_mm(500, 1000) == pytest.approx(10.0), "北缘应为最高点"
        assert smp.z_mm(500, 0) == pytest.approx(0.0), "南缘应为最低点"

    def test_heightfield_row0_is_south(self):
        grid = np.zeros((32, 32))
        grid[-1, :] = 100.0
        m = _terrain_heightfield(
            grid, (0, 0, 1000, 1000), scale=0.1,
            z_gamma=1.0, relief_mm_max=10.0, thickness_mm=4.0)
        v = m.vertices
        z_north = v[v[:, 1] > 95.0, 2].max()   # 北缘顶点最高 z
        z_south = v[v[:, 1] < 5.0, 2].max()    # 南缘顶点最高 z
        assert z_north == pytest.approx(10.0), "北缘应为最高点"
        assert z_south == pytest.approx(0.0), "南缘应为最低点（未南翻）"


# ─── 贴地形 drape（道路/河流不得平板悬浮）───────────────────────

class TestDrape:
    def _sampler(self):
        # 东高西低的斜坡网格（值只随列变化）
        grid = np.tile(np.linspace(0, 100, 32), (32, 1))
        return _TerrainSampler(grid, (0, 0, 1000, 1000), scale=0.1,
                               z_gamma=1.0, relief_mm_max=10.0)

    def test_drape_follows_terrain(self):
        from shapely.geometry import box
        smp = self._sampler()
        m = _drape_polys([box(0, 0, 1000, 1000)], smp, scale=0.1,
                         color=(74, 74, 74, 255), offset_mm=0.6,
                         cell_m=50.0)
        assert m is not None
        v = m.vertices
        # 逐顶点 z = 地形 + offset（贴合，非平板）
        expect = smp.z_mm_vec(v[:, 0] / 0.1, v[:, 1] / 0.1) + 0.6
        assert np.abs(v[:, 2] - expect).max() < 1e-6, "drape 必须逐顶点贴地形"
        # 东侧（高）顶点明显高于西侧（低）
        assert v[v[:, 0] > 90, 2].min() > v[v[:, 0] < 10, 2].max()

    def test_drape_hole_respected(self):
        from shapely.geometry import box
        smp = self._sampler()
        poly = box(0, 0, 1000, 1000).difference(box(400, 400, 600, 600))
        m = _drape_polys([poly], smp, scale=0.1, color=(74, 74, 74, 255),
                         offset_mm=0.6, cell_m=50.0)
        assert m is not None
        v = m.vertices
        in_hole = ((v[:, 0] / 0.1 > 420) & (v[:, 0] / 0.1 < 580) &
                   (v[:, 1] / 0.1 > 420) & (v[:, 1] / 0.1 < 580))
        # 洞内不应有内部顶点（边界加密点只在环上）
        assert not in_hole.any(), "洞内不得有 drape 顶点"


# ─── 真实产物回归（存在才跑）───────────────────────────────────────

@pytest.mark.parametrize("glb", [
    "output/custom_11cccc/custom_11cccc_draft.glb",
    "output/westlake/westlake_draft.glb",
])
def test_real_drafts_grounded(glb):
    p = _ROOT / glb
    if not p.exists():
        pytest.skip(f"{glb} 不存在")
    r = check_grounding(trimesh.load(str(p)), verbose=False)
    assert r["hard"] == [], f"{glb} 悬浮: {r['hard']}"
