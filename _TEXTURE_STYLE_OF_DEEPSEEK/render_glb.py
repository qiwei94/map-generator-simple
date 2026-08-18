"""Draft GLB 快速预览：跳过 brick/boolean 的轻量 3D 导出。

产品定位：两级渲染的第一级——用户先看秒级/半分钟级的 GLB 草稿
（浏览器 <model-viewer> 可直接预览），确认构图后才触发打印级 3MF。

与正式管线的差异（换速度的三刀）：
    1. 不做 Manifold boolean / brick 纹理 / 圆角——纯棱柱挤出
    2. 道路/水体逐顶点贴地形 drape（贴合浮雕起伏）；建筑/街区等
       体积层仍按质心采样整体抬升
    3. 地形用降采样 heightfield（默认 128²）+ 裙边，不追求 watertight

消费 Stage 4.5 的 layers（与 3MF 同源，所见即所得的构图）。
"""

import time

import numpy as np
import trimesh

# ─── 图层配色（GLB 顶点色，对标 render_png 纸面风格）────────────────
_COLORS = {
    "terrain": (232, 226, 214, 255),
    "block_base": (217, 217, 217, 255),
    "vegetation": (196, 205, 186, 255),
    "water": (38, 40, 48, 255),
    "roads": (74, 74, 74, 255),
    "buildings": (250, 250, 250, 255),
    "landmarks": (255, 240, 200, 255),
}

# 各层挤出参数（mm）：(底部 z 偏移, 厚度)
# ── 落地后检（grounding postcheck）参数 ──
# 各层底面相对地形面的预期 z 偏移（mm）
_EXPECTED_Z0 = {
    "block_base": 0.01, "water": 0.0, "vegetation": -0.10,
    "roads": 0.51, "buildings": -0.04, "landmarks": -0.04,
    "route": 1.05, "marker": 1.5,
}
_ROUTE_COLOR = (224, 164, 88)
_MARKER_COLOR = (226, 61, 61)
_LAYER_TOL_MM = 1.2     # 层级容差：地形降采样/最近邻查询误差
_CELL_TOL_MM = 2.5      # 单元格容差：坡地质心采样的局部误差
_CELL_FAIL_RATIO = 0.05  # 超过 5% 占用格悬浮 → 硬失败


def check_grounding(scene, verbose: bool = True) -> dict:
    """后检：场景内任何图层不得悬浮于地形面之上。

    按顶点颜色识别图层（GLB 往返会丢节点名），对每层计算
    gap = 顶点 z - 同 (x,y) 处地形面 z：
      • 层级：全层最小 gap > z0 + 容差 → 整层悬浮（硬失败）
      • 格级：xy 分格后单格最小 gap 超限；悬浮格占比超阈 → 硬失败，
        零星超限仅警告（陡坡边缘的 draft 取舍）
    返回 {"hard": [...], "warn": [...]}；可直接传 trimesh.load(path)。
    """
    import numpy as np
    from scipy.spatial import cKDTree

    color2layer = {tuple(v[:3]): k for k, v in _COLORS.items()}
    color2layer[_ROUTE_COLOR] = "route"
    color2layer[_MARKER_COLOR] = "marker"

    terrain_pts = None
    layers = []   # (layer_name, vertices)
    for g in scene.geometry.values():
        try:
            c = tuple(int(x) for x in
                      np.asarray(g.visual.vertex_colors)[0][:3])
        except Exception:
            continue
        name = color2layer.get(c)
        if name == "terrain":
            terrain_pts = np.asarray(g.vertices, dtype=np.float64)
        elif name is not None:
            layers.append((name, np.asarray(g.vertices, dtype=np.float64)))

    if terrain_pts is None or not layers:
        return {"hard": [], "warn": []}

    # 地形已是封闭实体（顶面+裙边+平底），同一 xy 会有上下两层顶点；
    # 比对必须用顶面，否则最近邻可能命中底面 → gap 虚高 → 全面误报
    key = np.round(terrain_pts[:, :2], 3)
    order = np.lexsort((key[:, 1], key[:, 0]))
    ks, zs_sorted = key[order], terrain_pts[order, 2]
    new_grp = np.ones(len(ks), dtype=bool)
    new_grp[1:] = (ks[1:] != ks[:-1]).any(axis=1)
    starts = np.flatnonzero(new_grp)
    terrain_pts = np.column_stack([ks[starts],
                                   np.maximum.reduceat(zs_sorted, starts)])

    tree = cKDTree(terrain_pts[:, :2])
    xy_min = terrain_pts[:, :2].min(axis=0)
    xy_span = terrain_pts[:, :2].max(axis=0) - xy_min
    xy_span[xy_span <= 0] = 1.0
    n_cells = 48

    hard, warn = [], []
    for name, verts in layers:
        z0 = _EXPECTED_Z0.get(name, 0.6)
        _, idx = tree.query(verts[:, :2])
        gaps = verts[:, 2] - terrain_pts[idx, 2]
        min_gap = float(gaps.min())
        if min_gap > z0 + _LAYER_TOL_MM:
            hard.append(f"{name}: 整层悬浮 {min_gap - z0:.2f}mm")
            continue
        if name == "marker":
            # 细高装饰物：球头顶点横向溢入邻格会误报格级悬浮，
            # 针底落地（层级检查）已足够
            continue
        # 格级：每个占用格的最小 gap
        cells = ((verts[:, :2] - xy_min) / xy_span * (n_cells - 1)) \
            .astype(int)
        keys = cells[:, 0] * n_cells + cells[:, 1]
        order = np.argsort(keys)
        uk, starts = np.unique(keys[order], return_index=True)
        cell_min = np.minimum.reduceat(gaps[order], starts)
        bad = int((cell_min > z0 + _CELL_TOL_MM).sum())
        ratio = bad / max(len(uk), 1)
        if ratio > _CELL_FAIL_RATIO:
            hard.append(f"{name}: {bad}/{len(uk)} 格悬浮"
                        f"（{ratio:.0%} > {_CELL_FAIL_RATIO:.0%}）")
        elif bad:
            warn.append(f"{name}: {bad}/{len(uk)} 格局部悬浮（陡坡边缘容许）")

    if verbose:
        if hard:
            print(f"  [postcheck] FAIL: {'; '.join(hard)}")
        elif warn:
            print(f"  [postcheck] PASS (warn: {'; '.join(warn)})")
        else:
            print("  [postcheck] PASS: 全部图层落地")
    return {"hard": hard, "warn": warn}


def _iter_polys(polys):
    for p in polys:
        if p is None or p.is_empty:
            continue
        if p.geom_type == "Polygon":
            yield p
        elif hasattr(p, "geoms"):
            for g in p.geoms:
                if not g.is_empty and g.geom_type == "Polygon":
                    yield g


class _TerrainSampler:
    """质心 → 地形高度 (mm)，与正式地形使用同一 Z 基准。"""

    def __init__(self, elevation_grid, bbox_local, scale, z_gamma,
                 relief_mm_max, z_base_mm=0.0):
        self.grid = elevation_grid
        self.xmin, self.ymin, self.xmax, self.ymax = bbox_local
        self.scale = scale
        if elevation_grid is None:
            self.zmin = self.zrange = 0.0
        else:
            self.zmin = float(np.nanmin(elevation_grid))
            self.zrange = max(float(np.nanmax(elevation_grid)) - self.zmin,
                              1e-6)
        self.z_gamma = z_gamma
        self.relief_mm_max = relief_mm_max
        self.z_base_mm = float(z_base_mm)

    def z_mm(self, x, y) -> float:
        return float(self.z_mm_vec(np.array([x]), np.array([y]))[0])

    def z_mm_vec(self, xs, ys) -> np.ndarray:
        """向量化地形采样（mm）。grid 行 0 = 南（y_min），与
        fetch_elevation_grid / build_terrain_mesh 约定一致（历史 bug：
        曾按行 0 = 北处理，地形南北镜像 → 水体突出地表）。"""
        xs = np.asarray(xs, dtype=float)
        ys = np.asarray(ys, dtype=float)
        if self.grid is None or self.zrange <= 1e-6:
            # 正式管线的平地也放在浮雕范围中点，不是 z=0。
            return np.full_like(xs, self.z_base_mm + self.relief_mm_max / 2.0)
        h, w = self.grid.shape
        col = np.clip(((xs - self.xmin) / (self.xmax - self.xmin) * (w - 1))
                      .astype(int), 0, w - 1)
        row = np.clip(((ys - self.ymin) / (self.ymax - self.ymin) * (h - 1))
                      .astype(int), 0, h - 1)
        t = (self.grid[row, col] - self.zmin) / self.zrange
        return (self.z_base_mm
                + (np.maximum(t, 0.0) ** self.z_gamma) * self.relief_mm_max)


def _try_extrude(poly, height_m):
    """健壮挤出：失败则 make_valid/buffer(0) 修复后重试，Multi 展开逐个。

    返回 mesh 列表（可能空）——大水体/带洞多边形曾在这里静默丢弃，
    导致西湖/钱塘江整体消失。"""
    try:
        return [trimesh.creation.extrude_polygon(poly, height=height_m)]
    except Exception:
        pass
    try:
        from shapely.validation import make_valid
        fixed = make_valid(poly)
    except Exception:
        try:
            fixed = poly.buffer(0)
        except Exception:
            return []
    out = []
    for g in _iter_polys([fixed]):
        try:
            out.append(trimesh.creation.extrude_polygon(g, height=height_m))
        except Exception:
            continue
    return out


def _extrude_polys(polys_with_h, sampler: _TerrainSampler, scale: float,
                   color, simplify_m: float = 0.0,
                   z_sample: str = "centroid") -> "trimesh.Trimesh | None":
    """[(poly, z0_mm, thickness_mm)] → 单个合并 mesh（局部米 → 场景 mm）。

    z_sample: "centroid" 质心单点采样（小 footprint，如建筑）；
              "min" 轮廓多点采最低（大平板层，宁埋不浮）。
    """
    parts = []
    dropped = 0
    total_area = 0.0
    dropped_area = 0.0
    for poly, z0, th in polys_with_h:
        if simplify_m > 0:
            simp = poly.simplify(simplify_m, preserve_topology=True)
            if not simp.is_empty and simp.geom_type == "Polygon":
                poly = simp
        total_area += poly.area
        meshes = _try_extrude(poly, th / scale)
        if not meshes:
            dropped += 1
            dropped_area += poly.area
            continue
        c = poly.centroid
        if z_sample == "min":
            # 大多边形跨起伏地形时，贴最低点：坡体会遮住下半截，
            # 观感远好于整块悬浮在山谷上空
            zs = _contour_z(poly, sampler)
            base_z = min(zs) + z0
        else:
            base_z = sampler.z_mm(c.x, c.y) + z0
        for m in meshes:
            m.apply_scale(scale)                 # 米 → mm
            m.apply_translation((0, 0, base_z))
            parts.append(m)
    if dropped:
        pct = dropped_area / max(total_area, 1e-9) * 100
        print(f"  [glb] WARN: {dropped} polys dropped "
              f"({pct:.1f}% area) after repair attempts")
    if not parts:
        return None
    mesh = trimesh.util.concatenate(parts)
    mesh.visual.vertex_colors = color
    return mesh


def _contour_z(poly, sampler: _TerrainSampler):
    """轮廓均匀采样（含质心）的地形 z 列表（mm）。"""
    coords = list(poly.exterior.coords)
    step = max(1, len(coords) // 16)
    zs = [sampler.z_mm(x, y) for x, y in coords[::step]]
    c = poly.centroid
    zs.append(sampler.z_mm(c.x, c.y))
    return zs


def _drape_polys(polys, sampler: _TerrainSampler, scale: float, color,
                 offset_mm: float, cell_m: float,
                 simplify_m: float = 0.0) -> "trimesh.Trimesh | None":
    """贴地形共形投影：三角化多边形，逐顶点 z = 地形 + offset。

    道路/河流必须“贴”在浮雕起伏上（用户可见的悬浮即来自旧平板
    挤出）。做法：边界按 cell_m 加密 + 内部散点，Delaunay 后保留
    质心落在多边形内的三角形（近似凹形与洞）。单面壳，无侧壁。
    """
    import shapely
    from scipy.spatial import Delaunay

    parts = []
    dropped = 0
    for poly in polys:
        if poly is None or poly.is_empty:
            continue
        if simplify_m > 0:
            simp = poly.simplify(simplify_m, preserve_topology=True)
            if not simp.is_empty and simp.geom_type == "Polygon":
                poly = simp
        try:
            dense = shapely.segmentize(poly, cell_m)
        except Exception:
            dense = poly
        if dense.geom_type != "Polygon":
            dropped += 1
            continue
        rings = [dense.exterior] + list(dense.interiors)
        bpts = np.vstack([np.asarray(r.coords)[:-1] for r in rings])
        minx, miny, maxx, maxy = dense.bounds
        gx = np.arange(minx, maxx + cell_m, cell_m)
        gy = np.arange(miny, maxy + cell_m, cell_m)
        ipts = np.empty((0, 2))
        if 1 < len(gx) * len(gy) <= 200000:
            XX, YY = np.meshgrid(gx, gy)
            inside = shapely.contains_xy(dense, XX.ravel(), YY.ravel())
            ipts = np.column_stack([XX.ravel()[inside], YY.ravel()[inside]])
        pts = np.vstack([bpts, ipts])
        if len(pts) < 3:
            dropped += 1
            continue
        try:
            tri = Delaunay(pts)
        except Exception:
            dropped += 1
            continue
        cents = pts[tri.simplices].mean(axis=1)
        keep = shapely.contains_xy(dense, cents[:, 0], cents[:, 1])
        faces = tri.simplices[keep]
        if len(faces) == 0:
            dropped += 1
            continue
        z = sampler.z_mm_vec(pts[:, 0], pts[:, 1]) + offset_mm
        verts = np.column_stack([pts[:, 0] * scale, pts[:, 1] * scale, z])
        parts.append(trimesh.Trimesh(vertices=verts, faces=faces,
                                     process=False))
    if dropped:
        print(f"  [glb] WARN: drape dropped {dropped} polys")
    if not parts:
        return None
    mesh = trimesh.util.concatenate(parts)
    mesh.visual.vertex_colors = color
    return mesh


def _drape_lines(lines_with_width, sampler: _TerrainSampler, scale: float,
                 color, offset_mm: float, cell_m: float,
                 simplify_m: float = 0.0) -> "trimesh.Trimesh | None":
    """Build lightweight terrain-following road ribbons without polygon booleans.

    Print geometry buffers every line into a polygon and Delaunay-triangulates
    it.  A browser preview only needs a visible ribbon, so this path emits two
    vertices per sampled centerline point.  It is intentionally an open shell.
    """
    import shapely

    parts = []
    for geometry, width_m in lines_with_width:
        if geometry is None or geometry.is_empty:
            continue
        geoms = (geometry.geoms if hasattr(geometry, "geoms")
                 else (geometry,))
        for line in geoms:
            if line.is_empty or line.geom_type != "LineString":
                continue
            if simplify_m > 0:
                line = line.simplify(simplify_m, preserve_topology=False)
            try:
                line = shapely.segmentize(line, cell_m)
            except Exception:
                pass
            points = np.asarray(line.coords, dtype=float)
            if len(points) < 2:
                continue
            deltas = np.empty_like(points)
            deltas[0] = points[1] - points[0]
            deltas[-1] = points[-1] - points[-2]
            if len(points) > 2:
                deltas[1:-1] = points[2:] - points[:-2]
            lengths = np.linalg.norm(deltas, axis=1)
            valid = lengths > 1e-9
            if not valid.all():
                points = points[valid]
                deltas = deltas[valid]
                lengths = lengths[valid]
            if len(points) < 2:
                continue
            normals = np.column_stack([-deltas[:, 1], deltas[:, 0]])
            normals /= lengths[:, None]
            offset = normals * max(float(width_m), 1.0) / 2.0
            left = points + offset
            right = points - offset
            xy = np.empty((len(points) * 2, 2), dtype=float)
            xy[0::2] = left
            xy[1::2] = right
            z = sampler.z_mm_vec(xy[:, 0], xy[:, 1]) + offset_mm
            vertices = np.column_stack([xy * scale, z])
            i = np.arange(len(points) - 1) * 2
            faces = np.vstack([
                np.column_stack([i, i + 1, i + 2]),
                np.column_stack([i + 1, i + 3, i + 2]),
            ])
            parts.append(trimesh.Trimesh(
                vertices=vertices, faces=faces, process=False))
    if not parts:
        return None
    mesh = trimesh.util.concatenate(parts)
    mesh.visual.vertex_colors = color
    return mesh


def _terrain_heightfield(elevation_grid, bbox_local, scale, z_gamma,
                         relief_mm_max, thickness_mm=None, grid_n=128,
                         *, surface_base_mm=0.0, bottom_z_mm=None):
    """降采样 heightfield + 四周裙边 + 平底 → 封闭实体底座。

    顶面 z 与 _TerrainSampler.z_mm 同基准，
    图层才能贴地——历史 bug：顶面曾整体下移 thickness_mm，
    导致所有图层均匀悬浮一层地形厚度。

    底面固定在 bottom_z_mm；旧调用仍可传 thickness_mm，
    等价于 bottom_z_mm=-thickness_mm。四周裙边封侧面、底面成底座，
    产物为 watertight 实体（旧版只三角化顶面，是一张透空薄壳）。"""
    xmin, ymin, xmax, ymax = bbox_local
    legacy_zero_baseline = (bottom_z_mm is None and thickness_mm is not None
                            and float(surface_base_mm) == 0.0)
    if elevation_grid is None:
        elevation_grid = np.zeros((2, 2), dtype=np.float32)
    h, w = elevation_grid.shape
    rs = np.linspace(0, h - 1, min(grid_n, h)).astype(int)
    cs = np.linspace(0, w - 1, min(grid_n, w)).astype(int)
    sub = elevation_grid[np.ix_(rs, cs)].astype(np.float64)
    # 归一化基线用全网格 min/max，与 _TerrainSampler 严格一致
    zmin = float(np.nanmin(elevation_grid))
    zr_raw = float(np.nanmax(elevation_grid)) - zmin
    if zr_raw > 0.01:
        zn = (np.clip((sub - zmin) / zr_raw, 0, 1) ** z_gamma
              * relief_mm_max + float(surface_base_mm))
    else:
        flat_z = (float(surface_base_mm) if legacy_zero_baseline else
                  float(surface_base_mm) + relief_mm_max / 2.0)
        zn = np.full_like(sub, flat_z)

    ny, nx = zn.shape
    xs = np.linspace(xmin, xmax, nx) * scale
    ys = np.linspace(ymin, ymax, ny) * scale        # 行 0 = 南
    xx, yy = np.meshgrid(xs, ys)
    if bottom_z_mm is None:
        if thickness_mm is None:
            raise ValueError("bottom_z_mm or thickness_mm is required")
        bottom_z_mm = -float(thickness_mm)
    z_bot = float(bottom_z_mm)

    n_top = ny * nx
    top = np.column_stack([xx.ravel(), yy.ravel(), zn.ravel()])
    idx = np.arange(n_top).reshape(ny, nx)

    # ── 顶面（法线朝上）──
    f_top = np.vstack([
        np.column_stack([idx[:-1, :-1].ravel(), idx[1:, :-1].ravel(),
                         idx[:-1, 1:].ravel()]),
        np.column_stack([idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(),
                         idx[1:, 1:].ravel()]),
    ])

    # ── 底面：四个角就够（平面），避免重复网格 ──
    # ── 边界环（从上方看绕一周，首尾不重复；行 0 = 南）──
    ring = np.concatenate([
        idx[0, :],                   # 南边：西 → 东
        idx[1:ny - 1, nx - 1],       # 东边：南 → 北
        idx[ny - 1, ::-1],           # 北边：东 → 西
        idx[ny - 2:0:-1, 0],         # 西边：北 → 南
    ])
    n_ring = len(ring)

    # ── 裙边：环上每点复制一份底部顶点 → 连续 quad 带（无缝）──
    bot_ring = np.column_stack([top[ring, 0], top[ring, 1],
                               np.full(n_ring, z_bot)])
    b0 = n_top                       # 底部环起始索引
    center_i = n_top + n_ring        # 底面中心点索引
    center = np.array([[(xs[0] + xs[-1]) / 2, (ys[0] + ys[-1]) / 2, z_bot]])

    i = np.arange(n_ring)
    j = (i + 1) % n_ring             # 闭环
    f_skirt = np.vstack([
        np.column_stack([ring[i], ring[j], b0 + j]),
        np.column_stack([ring[i], b0 + j, b0 + i]),
    ])
    # ── 底面：中心点扇形三角化 ──
    f_bot = np.column_stack([np.full(n_ring, center_i), b0 + j, b0 + i])

    verts = np.vstack([top, bot_ring, center])
    faces = np.vstack([f_top, f_skirt, f_bot])
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    mesh.fix_normals()
    mesh.visual.vertex_colors = _COLORS["terrain"]
    return mesh


_HILITE_COLOR = (226, 61, 61, 255)      # 标注红
_HILITE_TOP_MM = 2.0                    # 取范围内最高点往下 2mm 作为“高处”


def mark_peaks(scene, markers_local, scale: float, bbox_width_m: float):
    """在标注点附近把最高处染红（取代大头针几何）。

    不动几何、不加零件，只改顶点颜色：每个标注点取半径内所有
    图层的顶点，找到该范围的最高 z，将贴顶那一层（往下 2mm）染成
    红色——建筑就红屋顶、山就红山顶，比插针含蓄。

    markers_local: [(x_m, y_m), ...]；返回实际生效的标注数。
    """
    if not markers_local:
        return 0
    # 标注半径：模型宽度的 3%，限幅 [3, 9] mm（200mm 板 → 6mm）
    radius_mm = min(max(bbox_width_m * scale * 0.03, 3.0), 9.0)
    geoms = list(scene.geometry.values())
    hit = 0
    for mx, my in markers_local:
        cx, cy = mx * scale, my * scale
        # 一遍：找范围内最高 z
        peak_z = None
        picks = []
        for g in geoms:
            v = g.vertices
            d2 = (v[:, 0] - cx) ** 2 + (v[:, 1] - cy) ** 2
            m = d2 <= radius_mm ** 2
            if not m.any():
                continue
            picks.append((g, m))
            zmax = float(v[m, 2].max())
            peak_z = zmax if peak_z is None else max(peak_z, zmax)
        if peak_z is None:
            print(f"  [glb] mark ({mx:.0f}m, {my:.0f}m) 超出模型范围，跳过")
            continue
        # 二遍：只染贴顶那一层顶点
        cut = peak_z - _HILITE_TOP_MM
        painted = 0
        for g, m in picks:
            sel = m & (g.vertices[:, 2] >= cut)
            if not sel.any():
                continue
            vc = np.asarray(g.visual.vertex_colors).copy()
            vc[sel] = _HILITE_COLOR
            g.visual.vertex_colors = vc
            painted += int(sel.sum())
        if painted:
            hit += 1
            print(f"  [glb] mark ({mx:.0f}m, {my:.0f}m): "
                  f"peak z={peak_z:.1f}mm, {painted} verts 染红")
    return hit



# 3D 几何用的河道宽度（渲染配置里 river=500m 是给 PNG 视觉 prominence 的，
# 直接用于几何 buffer 会让城市小河变成 500m 宽黑条带）
_GLB_RIVER_WIDTH_DEFAULT = 60.0   # 无 OSM width 标签时的 river 默认宽度
_GLB_RIVER_WIDTH_MIN = 20.0       # 低于此宽度的 LineString 不补面（WL 已处理）


def _resolve_glb_river_width(row) -> float:
    """解析 3D 几何用的河道宽度：OSM width 标签 > 合理默认值。

    不使用渲染配置的 500m（那是给 1:125K PNG 视觉 prominence 的）。
    """
    import math
    # 优先用 OSM 显式 width 标签
    osm_w = row.get("width", None)
    if osm_w is not None:
        try:
            if not (isinstance(osm_w, float) and math.isnan(osm_w)):
                parsed = float(osm_w)
                if 0 < parsed < 5000:
                    return min(parsed, 2000.0)  # 超宽河也 cap 到 2km
        except (TypeError, ValueError):
            pass
    # 无标签：按 waterway 类型给合理默认（几何用，不是渲染用）
    wway = row.get("waterway", "river")
    defaults = {"river": _GLB_RIVER_WIDTH_DEFAULT, "riverbank": 200.0,
                "canal": 30.0, "stream": 12.0}
    return defaults.get(wway, _GLB_RIVER_WIDTH_DEFAULT)


def _river_polys_from_gdf(water_gdf, widths: dict, default_w: float):
    """water_gdf 里的 LineString 河道 → 按真实宽度 buffer 成面。

    仅补 OSM 只有中心线（无 Polygon）的河道。宽度优先取 OSM width 标签，
    无标签时用合理默认（river=150m），避免渲染配置的 500m 把城市小河
    变成巨型黑条带。"""
    from shapely.geometry import LineString, MultiLineString
    out = []
    for _, row in water_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if isinstance(geom, LineString):
            lines = [geom]
        elif isinstance(geom, MultiLineString):
            lines = list(geom.geoms)
        else:
            continue
        w = _resolve_glb_river_width(row)
        if w < _GLB_RIVER_WIDTH_MIN:
            continue  # 小沟渠留给 WL 细条
        for line in lines:
            if line.length < 50:
                continue
            try:
                buf = line.buffer(w / 2.0, quad_segs=2)
            except Exception:
                continue
            out.extend(_iter_polys([buf]))
    return out


def _amap_water_polys(ctx: dict):
    """高德无标注瓦片提取的真实水面（钱塘江级大河的唯一可靠形状源）。

    OSM 对大河往往只有中心线，固定宽 buffer 与真实江面（~2km）差距大。
    复用 _water_supplement 的瓦片提取（磁盘缓存命中则秒回；境外 bbox
    自动跳过），投影到本地坐标并裁到 bbox。任何失败返回 []。"""
    prepared = ctx.get("amap_water_polys")
    if prepared is not None:
        return list(prepared)

    bbox_wgs84 = ctx.get("bbox_wgs84")
    utm_crs = ctx.get("utm_crs")
    origin = ctx.get("origin")
    if not (bbox_wgs84 and utm_crs and origin):
        return []
    try:
        from _TEXTURE_STYLE_OF_DEEPSEEK._water_supplement import (
            fetch_amap_water_local)
        return fetch_amap_water_local(
            bbox_wgs84, utm_crs, origin,
            bbox_local=ctx.get("bbox_local"), min_area_m2=50000.0)
    except Exception as e:
        print(f"  [glb] amap water skipped: {e}")
        return []


def render_glb_preview(layers, ctx: dict, output_path: str,
                       elevation_grid=None, markers=None,
                       water_gdf=None,
                       base_thickness_mm=None,
                       terrain_relief_mm=None,
                       preview_quality="balanced") -> str:
    """Draft GLB 导出主入口。

    Args:
        layers: preprocess_layers 产物（BL/BO/WL/WO/VL/VO/block_base/roads_lines）
        ctx: 需含 bbox_local（本地米）与 scale（mm/m）
        output_path: .glb 输出路径
        elevation_grid: 可选 DEM 网格（None → 平面）
        markers: 可选 [(x_m, y_m), ...] 本地米坐标，附近最高处染红
        water_gdf: 可选已投影水体 GDF，LineString 大河按渲染宽度补面
    """
    # 函数级 import：吃 auto-params 运行时猴补丁
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
        Z_GAMMA, TERRAIN_THICKNESS_MM, BUILDING_AGGREGATE_HEIGHT_MM,
        ROAD_WIDTHS, ROAD_DEFAULT_WIDTH_M, ROAD_WIDTH_MULTIPLIER,
        WATERWAY_WIDTHS, WATER_BASE_THICKNESS_MM, Z_WATER_BASE_MM,
        BLOCK_BASE_THICKNESS_MM, ROAD_THICKNESS_MM,
        Z_ROAD_ABOVE_TERRAIN_MM, Z_BUILDING_EMBED_MM,
        VEGETATION_THICKNESS_MM, VL_Z_OFFSET_MM, VO_Z_OFFSET_MM,
    )

    if preview_quality not in ("balanced", "fast"):
        raise ValueError("preview_quality must be 'balanced' or 'fast'")
    fast = preview_quality == "fast"
    t0 = time.time()
    bbox_local = ctx["bbox_local"]
    scale = float(ctx["scale"])
    xmin, ymin, xmax, ymax = bbox_local
    relief_mm_max = (TERRAIN_THICKNESS_MM if terrain_relief_mm is None
                     else float(terrain_relief_mm))
    slab_mm = (WATER_BASE_THICKNESS_MM if base_thickness_mm is None
               else float(base_thickness_mm))
    if not 0.4 <= slab_mm <= 3.0:
        raise ValueError("base thickness must be between 0.4 and 3.0mm")
    terrain_base_z = Z_WATER_BASE_MM + slab_mm

    sampler = _TerrainSampler(elevation_grid, bbox_local, scale,
                              Z_GAMMA, relief_mm_max, terrain_base_z)
    scene = trimesh.Scene()

    # ── 地形 ──
    scene.add_geometry(
        _terrain_heightfield(elevation_grid, bbox_local, scale, Z_GAMMA,
                             relief_mm_max, surface_base_mm=terrain_base_z,
                             bottom_z_mm=Z_WATER_BASE_MM,
                             grid_n=64 if fast else 128),
        node_name="terrain")

    # ── 平板层（block_base / water / vegetation）──
    # 草稿几何简化容差：按区域宽度自适应（大区域粗一些，控制 GLB 体积）
    draft_tol_m = max((xmax - xmin) / (900.0 if fast else 2000.0),
                      10.0 if fast else 5.0)
    # drape 三角化边长：地形贴合密度（道路/水体用）
    drape_cell_m = max((xmax - xmin) / (220.0 if fast else 512.0),
                       35.0 if fast else 20.0)

    flat_specs = [
        ("block_base", list(_iter_polys(layers.block_base)),
         0.01, BLOCK_BASE_THICKNESS_MM),
        ("vegetation", list(_iter_polys(layers.VL)),
         VL_Z_OFFSET_MM - VEGETATION_THICKNESS_MM,
         VEGETATION_THICKNESS_MM),
        ("vegetation", list(_iter_polys(layers.VO)),
         VO_Z_OFFSET_MM - VEGETATION_THICKNESS_MM,
         VEGETATION_THICKNESS_MM),
    ]
    for name, polys, z0, th in flat_specs:
        if not polys:
            continue
        mesh = _extrude_polys([(p, z0, th) for p in polys], sampler, scale,
                              _COLORS[name], simplify_m=draft_tol_m,
                              z_sample="min")
        if mesh is not None:
            scene.add_geometry(mesh, node_name=name)
            print(f"  [glb] {name}: {len(mesh.faces):,} faces")

    # ── 水体：数据源优先级链 ──
    #   1. 卫星水面多边形（高德，真实形状）
    #   2. OSM Polygon 水体（WL/WO，真实形状）
    #   3. OSM LineString + width 标签 → 用标签值 buffer
    #   4. 无标签 LineString → 自适应 buffer（最后手段）
    # 关键：当 #1 或 #2 可用时，跳过 LineString buffer，
    #   避免用猜的宽度覆盖真实形状
    water_polys = list(_iter_polys(list(layers.WL) + list(layers.WO)))
    has_polygon_water = bool(water_polys)  # OSM 有真实多边形水面
    amap_polys = _amap_water_polys(ctx)
    has_satellite_water = bool(amap_polys)
    if has_satellite_water:
        print(f"  [glb] water: +{len(amap_polys)} satellite polys (true shape)")
        water_polys += amap_polys

    # 仅当无任何多边形水面数据时，才用 LineString buffer 补面
    if water_gdf is not None and len(water_gdf) > 0 and not has_satellite_water:
        # 统计 OSM LineString 中有无 width 标签
        lines_with_width = 0
        lines_total = 0
        from shapely.geometry import LineString, MultiLineString
        for _, row in water_gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            if not isinstance(geom, (LineString, MultiLineString)):
                continue
            lines_total += 1
            if row.get("width") is not None:
                lines_with_width += 1
        width_tag_ratio = lines_with_width / max(lines_total, 1)
        print(f"  [glb] water: {lines_total} LineStrings, "
              f"{lines_with_width} have width tag ({width_tag_ratio:.0%})")
        river_polys = _river_polys_from_gdf(
            water_gdf, WATERWAY_WIDTHS, ROAD_DEFAULT_WIDTH_M)
        if river_polys:
            tag = "width-tag" if width_tag_ratio > 0.3 else "adaptive-fallback"
            print(f"  [glb] water: +{len(river_polys)} river polys "
                  f"({tag})")
            water_polys += river_polys
    elif has_satellite_water and water_gdf is not None:
        print(f"  [glb] water: satellite polys available, "
              f"LineString buffer skipped (true shape used)")
    if water_polys:
        try:
            from shapely.ops import unary_union
            water_polys = list(_iter_polys([unary_union(water_polys)]))
        except Exception:
            pass
        mesh = _drape_polys(water_polys, sampler, scale, _COLORS["water"],
                            0.0, drape_cell_m, simplify_m=draft_tol_m)
        if mesh is not None:
            scene.add_geometry(mesh, node_name="water")
            print(f"  [glb] water: {len(mesh.faces):,} faces")

    # ── 道路（贴地形 drape：随浮雕起伏，不悬浮）──
    if layers.roads_lines:
        road_specs = []
        for item in layers.roads_lines:
            line, highway = item[0], item[1]
            if line is None or line.is_empty:
                continue
            w_m = ROAD_WIDTHS.get(highway,
                                  ROAD_DEFAULT_WIDTH_M) * ROAD_WIDTH_MULTIPLIER
            road_specs.append((line, w_m))
        if fast:
            mesh = _drape_lines(
                road_specs, sampler, scale, _COLORS["roads"],
                Z_ROAD_ABOVE_TERRAIN_MM, drape_cell_m,
                simplify_m=draft_tol_m)
        else:
            road_polys = []
            for line, w_m in road_specs:
                try:
                    road_polys.append(line.buffer(w_m / 2.0, quad_segs=1))
                except Exception:
                    continue
            mesh = _drape_polys(list(_iter_polys(road_polys)), sampler, scale,
                                _COLORS["roads"], Z_ROAD_ABOVE_TERRAIN_MM,
                                drape_cell_m)
        if mesh is not None:
            scene.add_geometry(mesh, node_name="roads")
            print(f"  [glb] roads: {len(mesh.faces):,} faces")

    # ── 建筑（BO 聚合高度 / BL 各自高度，压在 block_base 上）──
    bo_h = float(BUILDING_AGGREGATE_HEIGHT_MM)
    bo_items = [(p, -Z_BUILDING_EMBED_MM, bo_h)
                for p in _iter_polys(layers.BO)]
    bl_items = [(p, -Z_BUILDING_EMBED_MM, max(float(h), 0.5))
                for p, h in layers.BL if p is not None and not p.is_empty]
    for name, items in (("buildings", bo_items), ("landmarks", bl_items)):
        simplify_m = (draft_tol_m if fast and name == "buildings"
                      else draft_tol_m / 2.0 if fast else 0.0)
        mesh = _extrude_polys(items, sampler, scale, _COLORS[name],
                              simplify_m=simplify_m)
        if mesh is not None:
            scene.add_geometry(mesh, node_name=name)
            print(f"  [glb] {name}: {len(mesh.faces):,} faces")

    # ── 落地后检：任何图层悬浮直接失败（z 基准回归防线）──
    # 先体检，再染色：染色只改顶点颜色，不影响几何判定
    report = check_grounding(scene)
    if report["hard"]:
        raise RuntimeError(
            "[glb postcheck] 图层悬浮，拒绝导出: " + "; ".join(report["hard"]))

    # ── 标注点：附近最高处染红（不插针、不加几何）──
    if markers:
        in_box = [(mx, my) for mx, my in markers
                  if xmin <= mx <= xmax and ymin <= my <= ymax]
        skipped = len(markers) - len(in_box)
        if skipped:
            print(f"  [glb] {skipped} 个标注点超出取景范围，已忽略")
        n = mark_peaks(scene, in_box, scale, xmax - xmin)
        print(f"  [glb] marks: {n}/{len(markers)} 生效")

    scene.export(output_path)
    print(f"  [glb] exported: {output_path} ({time.time() - t0:.1f}s)")
    return output_path
