"""使用 osmium CLI 获取 OSM 数据

高性能方案：使用 osmium 命令行工具，速度提升 10-20倍。

标准流程（所有要素类型，包括水体）：
1. osmium extract - 区域裁剪
2. osmium tags-filter - 标签过滤（水体含 coastline，后续闭合为海面）
3. osmium export - 导出 GeoJSON
4. Python 读取 GeoJSON → GeoDataFrame
"""

import logging
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, Optional

import geopandas as gpd
import pandas as pd

logger = logging.getLogger(__name__)

# 默认 PBF 文件目录
DEFAULT_PBF_DIR = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'pbf_cache')

# Non-interactive SSH and service managers often omit Homebrew directories
# from PATH.  Probe the conventional locations before falling back to the
# bundled pyosmium compatibility backend.
_STANDARD_EXECUTABLE_DIRS = (
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/usr/bin",
)


def _find_standard_executable(name: str, exclude=()) -> Optional[str]:
    excluded = {os.path.realpath(path) for path in exclude if path}
    candidates = [shutil.which(name)]
    candidates.extend(os.path.join(directory, name)
                      for directory in _STANDARD_EXECUTABLE_DIRS)
    for candidate in candidates:
        if not candidate:
            continue
        resolved = os.path.realpath(candidate)
        if resolved in excluded:
            continue
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _bbox_option(west, south, east, north) -> str:
    """Return an unambiguous bbox token for native and portable osmium.

    A western-hemisphere bbox begins with ``-``.  Keeping the option and value
    in one token avoids backend-specific option parsing differences, and both
    native osmium and the portable pyosmium backend accept this spelling.
    """
    return f'--bbox={west},{south},{east},{north}'


def _export_timeout_seconds(filtered_size_kib: float,
                            osmium_command) -> int:
    """Scale export time without killing the portable backend mid-feature."""
    portable = (len(osmium_command) != 1
                or os.path.basename(str(osmium_command[0])) != "osmium")
    if portable:
        # Cairo roads: a 4.8 MiB filtered PBF legitimately takes roughly
        # 8 minutes in pyosmium on the Intel Mac.  The old 136 second budget
        # cached an empty road layer as a false success.
        return max(300, min(1800, int(filtered_size_kib / 8) + 120))
    return max(120, min(600, int(filtered_size_kib / 64) + 60))


class OsmiumCLIFetcher:
    """使用 osmium CLI 获取 OSM 数据"""

    # osmium export emits sparse properties, but GeoPandas expands their union
    # into a dense table.  Writing that table back to per-tile GeoJSON repeats
    # every null-valued regional tag for every feature.  In dense extracts this
    # turned a ~150 MB Paris frame into 500-800 MB *per tile*.  Keep the fields
    # consumed by filtering, height estimation, landmark classification and
    # layer builders; geometry is appended separately by _prune_cache_columns.
    _CACHE_COLUMNS = (
        'osm_type', 'osm_id',
        'building', 'building:part', 'height', 'building:height',
        'building:levels', 'building:levels:underground', 'min_height',
        'name', 'name:en', 'wikidata', 'wikipedia', 'historic', 'heritage',
        'tourism', 'amenity', 'man_made', 'tower:type', 'religion',
        'government', 'military', 'museum',
        'highway', 'bridge', 'tunnel', 'covered', 'layer', 'width',
        'natural', 'water', 'waterway', 'landuse', 'leisure', 'boundary',
        'railway', 'intermittent',
    )

    # Water v2 adds coastline ways.  Keep it in a separate namespace so old
    # GeoJSON/tile caches (which cannot contain coastlines) are never reused.
    _CACHE_NAMESPACES = {'water': 'water_coastline_v2'}

    @staticmethod
    def _pbf_cache_namespace(pbf_file: str) -> str:
        """Return a stable, filesystem-safe source identity for caches.

        Geographic coordinates alone are not sufficient: asking a Zhejiang
        extract for Beijing creates a valid empty GeoJSON at the same bbox as
        the later correct Beijing request.  Bind every full-frame and tile
        cache to the concrete PBF name, size, and modification timestamp so a
        wrong or updated regional source can never poison another request.
        """

        absolute = os.path.abspath(pbf_file)
        basename = os.path.basename(absolute)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip("-._")
        try:
            stat = os.stat(absolute)
            identity = f"{basename}:{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            identity = f"{basename}:missing"
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
        return f"{stem or 'pbf'}-{digest}"

    # 标准标签过滤表达式（使用 nwr = node/way/relation）
    # 适用于建筑、道路、植被等普通要素
    TAG_FILTERS = {
        'building': 'nwr/building',
        # 地标建筑专用过滤器 — 只提取有 landmark 标签的建筑（MERGE 模式下跳过全量建筑）
        # 覆盖 _landmark.py 的 Tier 1 + Tier 2 信号，大幅减少数据量（1.9M → ~几百）
        'building_landmarks': (
            'nwr/building=stadium,university,college,hospital,train_station,'
            'mall,public,government,museum,cathedral,church,temple,mosque,'
            'synagogue,civic,library,pagoda,shrine,chapel,monastery,convent,abbey '
            'nwr/historic '
            'nwr/tourism=museum,gallery,attraction,theme_park,aquarium '
            'nwr/amenity=university,hospital,mall,theatre,cinema,place_of_worship,'
            'library,townhall,courthouse,college '
            'nwr/man_made=tower,lighthouse,water_tower,obelisk '
            'nwr/wikidata '
            'nwr/heritage'
        ),
        'road': 'nwr/highway',
        'vegetation': (
            'nwr/landuse=forest,grass,meadow,cemetery,recreation_ground,'
            'village_green,allotments,orchard,vineyard,plant_nursery '
            'nwr/natural=wood,grassland,scrub,heath '
            'nwr/leisure=park,garden,golf_course,common,nature_reserve'
        ),
        'park': 'nwr/leisure=park,garden nwr/landuse=recreation_ground',
        'wetland': 'nwr/natural=wetland,marsh,swamp',
        # 水体全量过滤：江河湖泊 + OSM 以定向线表达的海岸。
        'water': ('nwr/natural=water,coastline nwr/water nwr/waterway '
                  'nwr/landuse=reservoir'),
        # 自然/植被地标：保护区 + 国家公园 + 命名湿地 / 景区
        # （西溪 OSM: boundary=national_park + leisure=nature_reserve + wikidata=Q1089272）
        'protected_area': ('nwr/boundary=national_park,protected_area '
                            'nwr/leisure=nature_reserve '
                            'nwr/natural=wetland '
                            'nwr/tourism=theme_park,zoo,aquarium'),
        # 铁路：city L / 地铁 / 通勤 / 轻轨
        'railway': 'nwr/railway=rail,light_rail,subway,tram,monorail,narrow_gauge',
        # 码头 / 防波堤 / 浮桥 — 伸入水里的人造陆地
        'pier': 'nwr/man_made=pier,breakwater,wharf,groyne',
        # 体育场馆 / 游艇港 — civic landmark
        'stadium': 'nwr/leisure=stadium,sports_centre,marina',
        # 全量 landuse（用于 block 语义分类）
        'landuse': 'nwr/landuse',
    }

    # (水体已移至 TAG_FILTERS，使用标准管线)

    # 是否对特定类型使用 relation-first 管线（先过滤后裁剪）
    RELATION_FIRST_TYPES = set()

    def __init__(self, pbf_dir: str = None):
        """
        Args:
            pbf_dir: PBF 文件存放目录
        """
        self.pbf_dir = pbf_dir or DEFAULT_PBF_DIR

        # 查找 conda 环境中的工具路径
        self.conda_env_path = self._find_conda_env_path()

        # 检查 osmium 是否可用
        self.osmium_available = self._check_tool('osmium')

        # 检查 ogr2ogr 是否可用（用于 relation 数据的 bbox 裁剪；
        # 缺失时自动降级为 shapely 裁剪，见 _shapely_clip_geojson）
        self.ogr2ogr_available = self._check_tool('ogr2ogr')

        if not self.osmium_available:
            logger.warning("osmium CLI 未安装。安装: conda install -c conda-forge osmium-tool")
        if not self.ogr2ogr_available:
            logger.info("ogr2ogr 不可用，水体 bbox 裁剪将使用 shapely 实现（功能等价）")

    def _find_conda_env_path(self) -> Optional[str]:
        """查找 conda 环境路径"""
        import sys
        
        python_path = sys.executable
        if 'conda' in python_path or 'miniconda' in python_path or 'anaconda' in python_path:
            return os.path.dirname(python_path)
        
        common_paths = [
            os.path.expanduser("~\\Anaconda3\\Scripts"),
            os.path.expanduser("~\\Anaconda3\\Library\\bin"),
            os.path.expanduser("~\\Miniconda3\\Scripts"),
            os.path.expanduser("~\\Miniconda3\\Library\\bin"),
            os.path.expanduser("~\\AppData\\Local\\Continuum\\anaconda3\\Scripts"),
            os.path.expanduser("~\\AppData\\Local\\Continuum\\anaconda3\\Library\\bin"),
            os.path.expanduser("~\\AppData\\Local\\Continuum\\miniconda3\\Scripts"),
            os.path.expanduser("~\\AppData\\Local\\Continuum\\miniconda3\\Library\\bin"),
        ]
        
        for path in common_paths:
            if os.path.exists(path):
                logger.info(f"找到 conda 环境: {path}")
                return path
        
        return None
    
    def _check_tool(self, tool_name: str) -> bool:
        """检查命令行工具是否可用"""
        try:
            override = (os.environ.get('OSMIUM_BIN')
                        if tool_name == 'osmium' else None)
            if override:
                if not os.path.isfile(override) or not os.access(override, os.X_OK):
                    logger.warning("OSMIUM_BIN is not executable: %s", override)
                    return False
                result = subprocess.run(
                    [override, '--version'], capture_output=True, timeout=5)
                return result.returncode == 0

            if tool_name == 'osmium':
                command = self._get_osmium_command()
                result = subprocess.run(
                    command + ['--version'], capture_output=True, timeout=10)
                return result.returncode == 0

            if self.conda_env_path:
                # Try both Scripts and Library/bin directories
                conda_dirs = [
                    self.conda_env_path,
                    os.path.join(os.path.dirname(self.conda_env_path), 'Library', 'bin'),
                ]
                # Also try if conda_env_path is already Scripts, try its parent's Library/bin
                if self.conda_env_path.endswith('Scripts'):
                    conda_dirs.append(os.path.join(os.path.dirname(self.conda_env_path), 'Library', 'bin'))
                elif self.conda_env_path.endswith('Library'):
                    conda_dirs.append(os.path.join(self.conda_env_path, 'bin'))
                
                for conda_dir in conda_dirs:
                    if not os.path.exists(conda_dir):
                        continue
                    tool_path = os.path.join(conda_dir, f"{tool_name}.exe")
                    if os.path.exists(tool_path):
                        try:
                            result = subprocess.run(
                                [tool_path, '--version'],
                                capture_output=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW
                            )
                            if result.returncode == 0:
                                return True
                        except:
                            pass
                    tool_path_no_ext = os.path.join(conda_dir, tool_name)
                    if os.path.exists(tool_path_no_ext):
                        try:
                            result = subprocess.run(
                                [tool_path_no_ext, '--version'],
                                capture_output=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW
                            )
                            if result.returncode == 0:
                                return True
                        except:
                            pass
            
            if os.name == 'nt':
                result = subprocess.run(
                    ['where', tool_name],
                    capture_output=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                if result.returncode == 0:
                    tool_paths = result.stdout.decode().strip().split('\n')
                    for tp in tool_paths:
                        tp = tp.strip()
                        if tp:
                            try:
                                ver_result = subprocess.run(
                                    [tp, '--version'],
                                    capture_output=True, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW
                                )
                                return ver_result.returncode == 0
                            except:
                                continue
                    return False
                return False
            else:
                result = subprocess.run(
                    [tool_name, '--version'],
                    capture_output=True, timeout=5
                )
                return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    def _get_osmium_command(self) -> list:
        """Resolve native osmium or the bundled pyosmium backend.

        The bundled entry must run under the *current* Python interpreter.
        Relying on ``#!/usr/bin/env python3`` can select a system Python which
        does not have pyosmium even though the active pipeline environment does.
        """
        override = os.environ.get('OSMIUM_BIN')
        if override and os.path.isfile(override) and os.access(override, os.X_OK):
            return [override]

        candidate = self._get_tool_path('osmium')
        if candidate != 'osmium':
            return [candidate]

        bundled_entry = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..', '..', '..', 'tools', 'osmium'))
        bundled_backend = os.path.join(os.path.dirname(bundled_entry),
                                       'osmium_pyosmium.py')
        native = _find_standard_executable('osmium', exclude=(bundled_entry,))
        if native and os.path.realpath(native) != os.path.realpath(bundled_entry):
            return [native]

        try:
            import osmium  # noqa: F401 - availability probe for current Python
        except ImportError:
            if native:
                return [native]
            return ['osmium']
        if os.path.isfile(bundled_backend):
            return [sys.executable, bundled_backend]
        if native:
            return [native]
        return ['osmium']
    
    def _get_tool_path(self, tool_name: str) -> str:
        """获取工具的完整路径"""
        override = (os.environ.get('OSMIUM_BIN')
                    if tool_name == 'osmium' else None)
        if override and os.path.isfile(override) and os.access(override, os.X_OK):
            return override

        if self.conda_env_path:
            # Try both Scripts and Library/bin directories
            conda_dirs = [
                self.conda_env_path,
                os.path.join(os.path.dirname(self.conda_env_path), 'Library', 'bin'),
            ]
            if self.conda_env_path.endswith('Scripts'):
                conda_dirs.append(os.path.join(os.path.dirname(self.conda_env_path), 'Library', 'bin'))
            elif self.conda_env_path.endswith('Library'):
                conda_dirs.append(os.path.join(self.conda_env_path, 'bin'))
            
            for conda_dir in conda_dirs:
                if not os.path.exists(conda_dir):
                    continue
                tool_path = os.path.join(conda_dir, f"{tool_name}.exe")
                if os.path.exists(tool_path):
                    return tool_path
                tool_path_no_ext = os.path.join(conda_dir, tool_name)
                if os.path.exists(tool_path_no_ext):
                    return tool_path_no_ext
        
        return tool_name
    
    def _find_pbf_file(self, region: str) -> Optional[str]:
        """查找区域对应的 PBF 文件"""
        patterns = [
            f"{region}-latest.osm.pbf",
            f"{region}.osm.pbf",
            f"{region}_latest.osm.pbf",
        ]
        
        for pattern in patterns:
            path = os.path.join(self.pbf_dir, pattern)
            if os.path.exists(path):
                return path
        
        for f in os.listdir(self.pbf_dir):
            if f.endswith('.pbf') and region.lower() in f.lower():
                return os.path.join(self.pbf_dir, f)
        
        return None

    def fetch_features(
        self,
        tag_type: str,
        south: float,
        west: float,
        north: float,
        east: float,
        pbf_file: str = None,
        region: str = None,
    ) -> gpd.GeoDataFrame:
        """使用 CLI 工具获取 OSM 数据

        Args:
            tag_type: 数据类型 ('building', 'road', 'water', 'vegetation', 'park', 'wetland')
            south, west, north, east: 边界框 (WGS84)
            pbf_file: 直接指定 PBF 文件路径
            region: 区域名称 (如 'zhejiang', 'china')

        Returns:
            GeoDataFrame
        """
        if not self.osmium_available:
            logger.error("osmium CLI 未安装，无法使用 CLI 方式")
            return gpd.GeoDataFrame()

        if pbf_file is None:
            if region:
                pbf_file = self._find_pbf_file(region)
            else:
                logger.error("必须指定 pbf_file 或 region")
                return gpd.GeoDataFrame()

        if pbf_file is None or not os.path.exists(pbf_file):
            logger.error(f"PBF 文件不存在: {pbf_file}")
            return gpd.GeoDataFrame()

        # 输出到项目 tmp/ 目录下（不缓存）
        project_tmp = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'tmp')
        os.makedirs(project_tmp, exist_ok=True)
        cache_tag = self._CACHE_NAMESPACES.get(tag_type, tag_type)
        pbf_namespace = self._pbf_cache_namespace(pbf_file)
        output_path = os.path.join(
            project_tmp,
            f"osmium_{cache_tag}_{pbf_namespace}_"
            f"{south:.4f}_{west:.4f}_{north:.4f}_{east:.4f}.geojson",
        )

        logger.info(f"使用 CLI 方式获取 {tag_type} 数据...")
        logger.info(f"边界框: ({south:.4f}, {west:.4f}, {north:.4f}, {east:.4f})")
        print(f"\n  [CLI Pipeline] Starting {tag_type} data extraction...")
        print(f"  Bounding box: ({south:.4f}, {west:.4f}, {north:.4f}, {east:.4f})")

        try:
            # GeoJSON cache: if file already exists and is non-empty, reuse it
            # 空结果（合法的空 FeatureCollection）同样命中缓存，避免重跑 osmium extract
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                try:
                    _cached_empty = gpd.read_file(output_path)
                    _cache_readable = True
                except Exception:
                    _cached_empty = None
                    _cache_readable = False
                if _cache_readable and len(_cached_empty) == 0:
                    print(f"  [CLI Pipeline] Using cached GeoJSON (empty result): {output_path}")
                    return _cached_empty

            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                print(f"  [CLI Pipeline] Using cached GeoJSON: {output_path}")
                gdf = gpd.read_file(output_path)
                if len(gdf) > 0:
                    print(f"  [CLI Pipeline] Loaded {len(gdf)} features from cache\n")
                    # Buildings: ensure est_height column
                    gdf = self._enrich_building_heights(
                        gdf, tag_type, south, west, north, east)
                    return gdf

            result = self._run_osmium_pipeline(
                pbf_file, tag_type, south, west, north, east, output_path
            )
            if result:
                gdf = gpd.read_file(output_path)
                logger.info(f"CLI 管线完成: {len(gdf)} 条记录")
                print(f"  [CLI Pipeline] Complete: {len(gdf)} features extracted\n")
                print(f"  Output: {output_path}")

                # Buildings: ensure est_height column matches osm.py fetch_buildings output
                gdf = self._enrich_building_heights(
                    gdf, tag_type, south, west, north, east)

                return gdf
        except Exception as e:
            logger.error(f"CLI 执行失败: {e}")
            return gpd.GeoDataFrame()

        return gpd.GeoDataFrame()
    
    # ==================================================================
    # 建筑高度补全（与 osm.py fetch_buildings 输出对齐）
    # ==================================================================

    def _enrich_building_heights(self, gdf, tag_type, south, west, north, east):
        """为建筑图层补 est_height 列（nDSM 采样 + Overture 增强）。"""
        if tag_type not in ('building', 'building_landmarks') or len(gdf) == 0:
            return gdf
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
            _estimate_building_heights, get_ndsm_grid,
        )
        ndsm_heights = None
        ndsm_cache = get_ndsm_grid()
        if ndsm_cache is not None:
            from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.ndsm import (
                sample_building_heights_from_ndsm,
            )
            grid, s, w, n, e = ndsm_cache
            ndsm_heights = sample_building_heights_from_ndsm(
                gdf, grid, s, w, n, e)
        overture_heights = None
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import OVERTURE_ENABLED, OVERTURE_CACHE_DIR
        if OVERTURE_ENABLED:
            try:
                from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.height_enrichment import (
                    load_overture_heights,
                )
                _ov_cache = OVERTURE_CACHE_DIR
                if not os.path.isabs(_ov_cache):
                    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(
                        os.path.dirname(os.path.abspath(__file__)))))
                    _ov_cache = os.path.join(_project_root, _ov_cache)
                overture_heights, _ = load_overture_heights(
                    gdf, bbox_wgs84=(south, west, north, east),
                    cache_dir=_ov_cache)
            except Exception:
                pass
        gdf["est_height"] = _estimate_building_heights(
            gdf, ndsm_heights, overture_heights)
        return gdf

    # ==================================================================
    # 瓦片级缓存（Phase 2）：跨网格线偏移的部分复用
    # ==================================================================

    # 单瓦片提取外扩 buffer（度，≈200m）：保证跨界要素完整，
    # 合并时靠 osm_type+osm_id 去重。
    _TILE_BUFFER_DEG = 0.002

    @classmethod
    def _prune_cache_columns(cls, gdf, tag_type=None):
        """Drop export-only tag columns before a frame can inflate memory/cache.

        The whitelist is deliberately a shared superset for every layer.  This
        keeps cache files compatible across feature types while retaining every
        field used by downstream filters, height enrichment and landmark rules.
        """
        if gdf is None:
            return gdf
        keep = [column for column in cls._CACHE_COLUMNS
                if column in gdf.columns]
        if 'geometry' in gdf.columns:
            keep.append('geometry')
        return gdf.loc[:, keep].copy()

    @classmethod
    def _try_read_geojson_cache(cls, path, tag_type=None):
        """读 GeoJSON 缓存：合法（含空集合）返回 GeoDataFrame，损坏/不存在返回 None。"""
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return None
        try:
            # Column projection happens inside the GDAL reader, before a dense
            # pandas table is allocated.  This is essential when opening old
            # Paris tiles whose schemas contain thousands of mostly-null tags.
            return cls._prune_cache_columns(
                gpd.read_file(path, columns=list(cls._CACHE_COLUMNS)),
                tag_type)
        except Exception:
            return None

    @staticmethod
    def _dedupe_features(gdf):
        """瓦片合并后去重：优先 osm_type+osm_id，缺失时降级几何指纹。"""
        if gdf is None or len(gdf) == 0:
            return gdf
        if 'osm_type' in gdf.columns and 'osm_id' in gdf.columns:
            return gdf.drop_duplicates(
                subset=['osm_type', 'osm_id']).reset_index(drop=True)
        try:
            fp = gdf.geometry.apply(
                lambda g: (g.geom_type, round(g.centroid.x, 7),
                           round(g.centroid.y, 7), round(g.area, 10)))
            return gdf[~fp.duplicated()].reset_index(drop=True)
        except Exception:
            return gdf

    def _tile_cache_path(self, tag_type, ix, iy, pbf_file):
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        cache_tag = self._CACHE_NAMESPACES.get(tag_type, tag_type)
        pbf_namespace = self._pbf_cache_namespace(pbf_file)
        d = os.path.join(
            project_root, 'cache', 'tiles', pbf_namespace, cache_tag)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{ix}_{iy}.geojson")

    @staticmethod
    def _should_refresh_full_frame(missing_count, total_count,
                                   missing_bbox=None, frame_bbox=None):
        """Return True when reusing sparse tiles would cost more memory than a refresh.

        A merged extraction already spans the bounding rectangle of every missing
        tile.  When that rectangle covers the requested frame, or at least half
        the frame is missing, loading the existing GeoJSON tiles first only keeps
        duplicate feature sets alive during the split/concat step.  Dense cities
        can turn that duplication into multi-gigabyte RSS spikes.
        """
        if missing_count < 2 or total_count <= 0:
            return False
        if missing_count * 2 >= total_count:
            return True
        if missing_bbox is None or frame_bbox is None:
            return False
        ms, mw, mn, me = missing_bbox
        fs, fw, fn, fe = frame_bbox
        eps = 1e-10
        return (ms <= fs + eps and mw <= fw + eps and
                mn >= fn - eps and me >= fe - eps)

    @staticmethod
    def _atomic_write_gdf(gdf, path):
        """原子写：tmp 文件 + os.replace，并发安全。"""
        tmp_path = path + f".tmp{os.getpid()}"
        gdf.to_file(tmp_path, driver='GeoJSON')
        os.replace(tmp_path, path)

    def _split_to_tiles(
        self, gdf, tag_type, ix0, iy0, ix1, iy1, step, pbf_file,
    ):
        """全框取数结果拆入瓦片缓存（含空瓦片），供跨网格线请求复用。"""
        from shapely.geometry import box
        from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import tile_bbox
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                ts, tw, tn, te = tile_bbox(ix, iy, step)
                buf = self._TILE_BUFFER_DEG
                if len(gdf) > 0:
                    mask = gdf.geometry.intersects(
                        box(tw - buf, ts - buf, te + buf, tn + buf))
                    part = gdf[mask]
                else:
                    part = gdf
                self._atomic_write_gdf(
                    part, self._tile_cache_path(
                        tag_type, ix, iy, pbf_file))

    def fetch_tiled_features(self, tag_type, south, west, north, east,
                             pbf_file=None, region=None, step=None):
        """瓦片级缓存取数：跨网格线偏移的请求只重算新增瓦片。

        策略：
        - 全框缓存（tmp/osmium_{tag}_{snap}.geojson）命中 → 直接返回；
        - 瓦片全缺 → 全框提取一次（单次全量 PBF 读，最快）+ 拆瓦片缓存；
        - 部分瓦片缺失 → 只提取缺失瓦片（带 ~200m buffer），合并去重。
        """
        from _TEXTURE_STYLE_OF_DEEPSEEK._tile_grid import (
            DEFAULT_TILE_STEP, snap_bbox, tile_range, tile_bbox)

        if not self.osmium_available:
            logger.error("osmium CLI 未安装，无法使用 CLI 方式")
            return gpd.GeoDataFrame()
        if pbf_file is None:
            if region:
                pbf_file = self._find_pbf_file(region)
            else:
                logger.error("必须指定 pbf_file 或 region")
                return gpd.GeoDataFrame()
        if pbf_file is None or not os.path.exists(pbf_file):
            logger.error(f"PBF 文件不存在: {pbf_file}")
            return gpd.GeoDataFrame()

        step = step or DEFAULT_TILE_STEP
        fs, fw, fn, fe = snap_bbox(south, west, north, east, step)

        # 1) 全框缓存快路径（与 Phase 1 全框缓存兼容）
        project_tmp = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'tmp')
        os.makedirs(project_tmp, exist_ok=True)
        cache_tag = self._CACHE_NAMESPACES.get(tag_type, tag_type)
        pbf_namespace = self._pbf_cache_namespace(pbf_file)
        full_path = os.path.join(
            project_tmp,
            f"osmium_{cache_tag}_{pbf_namespace}_"
            f"{fs:.4f}_{fw:.4f}_{fn:.4f}_{fe:.4f}.geojson")
        gdf = self._try_read_geojson_cache(full_path, tag_type)
        if gdf is not None:
            print(f"  [CLI Pipeline] Using cached GeoJSON (full-frame): {full_path}")
            if len(gdf) == 0:
                print(f"  [CLI Pipeline] Cached empty result (0 features)\n")
                return gdf
            print(f"  [CLI Pipeline] Loaded {len(gdf)} features from cache\n")
            return self._enrich_building_heights(gdf, tag_type, fs, fw, fn, fe)

        # 2) 瓦片路径。先只看文件是否存在，不立刻把所有 GeoJSON 读进
        # 内存。巴黎等高密城市的单瓦片可达数百 MB；若大部分瓦片缺失，
        # 后面本来就要做一次近似整框提取，提前加载旧瓦片会造成数倍峰值。
        ix0, iy0, ix1, iy1 = tile_range(fs, fw, fn, fe, step)
        cached_paths, missing = [], []
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                tp = self._tile_cache_path(tag_type, ix, iy, pbf_file)
                if os.path.exists(tp) and os.path.getsize(tp) > 0:
                    cached_paths.append((ix, iy, tp))
                else:
                    missing.append((ix, iy, tp))

        total_tiles = len(cached_paths) + len(missing)
        if cached_paths and missing:
            buf = self._TILE_BUFFER_DEG
            ms = min(tile_bbox(ix, iy, step)[0] for ix, iy, _ in missing) - buf
            mw = min(tile_bbox(ix, iy, step)[1] for ix, iy, _ in missing) - buf
            mn = max(tile_bbox(ix, iy, step)[2] for ix, iy, _ in missing) + buf
            me = max(tile_bbox(ix, iy, step)[3] for ix, iy, _ in missing) + buf
            if self._should_refresh_full_frame(
                    len(missing), total_tiles, (ms, mw, mn, me),
                    (fs, fw, fn, fe)):
                print(f"  [Tile Cache] {tag_type}: {len(missing)}/{total_tiles} "
                      "tiles missing; full-frame refresh avoids duplicate RSS")
                tmp_out = full_path + f".tmp{os.getpid()}"
                try:
                    ok = self._run_osmium_pipeline(
                        pbf_file, tag_type, fs, fw, fn, fe, tmp_out)
                    if ok:
                        os.replace(tmp_out, full_path)
                    elif os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                    raise
                if not ok:
                    raise RuntimeError(
                        f"osmium {tag_type} full-frame refresh failed; "
                        "refusing to cache a false empty result")
                gdf = self._prune_cache_columns(
                    gpd.read_file(full_path,
                                  columns=list(self._CACHE_COLUMNS)),
                    tag_type)
                self._split_to_tiles(
                    gdf, tag_type, ix0, iy0, ix1, iy1, step, pbf_file)
                print(f"  [Tile Cache] {tag_type}: {len(gdf)} features, "
                      "full-frame refresh split done\n")
                return self._enrich_building_heights(
                    gdf, tag_type, fs, fw, fn, fe)

        # Only now load reusable tiles. Corrupt files are downgraded to misses.
        frames = []
        for ix, iy, tp in cached_paths:
            tgdf = self._try_read_geojson_cache(tp, tag_type)
            if tgdf is not None:
                frames.append(tgdf)
                print(f"  [Tile Cache] HIT {tag_type} tile ({ix},{iy}): "
                      f"{len(tgdf)} features")
            else:
                missing.append((ix, iy, tp))

        if frames and not missing:
            merged = pd.concat(frames, ignore_index=True) \
                if len(frames) > 1 else frames[0]
            merged = self._dedupe_features(merged)
            print(f"  [Tile Cache] {tag_type}: {len(merged)} features "
                  f"from {len(frames)} tiles\n")
            return self._enrich_building_heights(merged, tag_type, fs, fw, fn, fe)

        if not frames:
            # 全冷：全框提取只读一次全量 PBF，比逐瓦片提取快；
            # 完成后拆入瓦片缓存，供跨网格线请求复用。
            print(f"  [Tile Cache] {tag_type}: cold frame, full-frame extract "
                  f"then split into {len(missing)} tiles")
            tmp_out = full_path + f".tmp{os.getpid()}"
            try:
                ok = self._run_osmium_pipeline(
                    pbf_file, tag_type, fs, fw, fn, fe, tmp_out)
                if ok:
                    os.replace(tmp_out, full_path)
                elif os.path.exists(tmp_out):
                    os.remove(tmp_out)
            except Exception:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
                raise
            if not ok:
                raise RuntimeError(
                    f"osmium {tag_type} cold-frame extract failed; "
                    "refusing to cache a false empty result")
            gdf = self._prune_cache_columns(
                gpd.read_file(full_path, columns=list(self._CACHE_COLUMNS)),
                tag_type)
            self._split_to_tiles(
                gdf, tag_type, ix0, iy0, ix1, iy1, step, pbf_file)
            print(f"  [Tile Cache] {tag_type}: {len(gdf)} features, "
                  f"split into tiles done\n")
            return self._enrich_building_heights(gdf, tag_type, fs, fw, fn, fe)

        # 3) 混合：提取缺失瓦片。
        #    缺 ≥2 块时合并成一次提取再拆瓦片（relation-first 水体逐瓦片
        #    提取每次都要全量扫 PBF，合并提取可省掉重复扫描）；
        #    只缺 1 块时直接单瓦片提取（带 buffer 保证跨界要素完整）。
        buf = self._TILE_BUFFER_DEG
        if len(missing) >= 2:
            us = min(tile_bbox(ix, iy, step)[0] for ix, iy, _ in missing) - buf
            uw = min(tile_bbox(ix, iy, step)[1] for ix, iy, _ in missing) - buf
            un = max(tile_bbox(ix, iy, step)[2] for ix, iy, _ in missing) + buf
            ue = max(tile_bbox(ix, iy, step)[3] for ix, iy, _ in missing) + buf
            print(f"  [Tile Cache] {tag_type}: {len(missing)} missing tiles, "
                  f"merged extract ({us:.3f},{uw:.3f},{un:.3f},{ue:.3f})")
            tmp_out = full_path + f".mrg{os.getpid()}"
            try:
                ok = self._run_osmium_pipeline(
                    pbf_file, tag_type, us, uw, un, ue, tmp_out)
                if not ok:
                    raise RuntimeError(
                        f"osmium {tag_type} merged tile extract failed; "
                        "refusing to cache a false empty result")
                mgdf = self._prune_cache_columns(
                    gpd.read_file(tmp_out,
                                  columns=list(self._CACHE_COLUMNS)),
                    tag_type)
                # 按瓦片拆分缓存（仅覆盖缺失瓦片，不碰已有文件）；
                # 跨界要素由合并框完整提取，无需 buffer 重叠。
                from shapely.geometry import box
                for ix, iy, tp in missing:
                    ts, tw, tn, te = tile_bbox(ix, iy, step)
                    if len(mgdf) > 0:
                        mask = mgdf.geometry.intersects(
                            box(tw, ts, te, tn))
                        part = mgdf[mask]
                    else:
                        part = mgdf
                    self._atomic_write_gdf(part, tp)
                    frames.append(part)
                    print(f"  [Tile Cache] MISS->split {tag_type} tile "
                          f"({ix},{iy}): {len(part)} features")
            finally:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
            merged = pd.concat(frames, ignore_index=True) \
                if len(frames) > 1 else frames[0]
            merged = self._dedupe_features(merged)
            print(f"  [Tile Cache] {tag_type}: {len(merged)} features merged\n")
            return self._enrich_building_heights(merged, tag_type, fs, fw, fn, fe)

        for ix, iy, tp in missing:
            ts, tw, tn, te = tile_bbox(ix, iy, step)
            buf = self._TILE_BUFFER_DEG
            tmp_out = tp + f".tmp{os.getpid()}"
            ok = self._run_osmium_pipeline(
                pbf_file, tag_type,
                ts - buf, tw - buf, tn + buf, te + buf, tmp_out)
            if ok:
                os.replace(tmp_out, tp)
                tgdf = gpd.read_file(tp)
                frames.append(tgdf)
                print(f"  [Tile Cache] MISS->fetched {tag_type} tile "
                      f"({ix},{iy}): {len(tgdf)} features")
            else:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
                raise RuntimeError(
                    f"osmium {tag_type} tile ({ix},{iy}) extract failed; "
                    "refusing to cache a false empty result")
        merged = pd.concat(frames, ignore_index=True) \
            if len(frames) > 1 else frames[0]
        merged = self._dedupe_features(merged)
        print(f"  [Tile Cache] {tag_type}: {len(merged)} features merged\n")
        return self._enrich_building_heights(merged, tag_type, fs, fw, fn, fe)

    def _run_osmium_pipeline(
        self,
        pbf_file: str,
        tag_type: str,
        south: float,
        west: float,
        north: float,
        east: float,
        output_path: str,
    ) -> bool:
        """执行 osmium 管线

        对于普通要素（建筑、道路、植被）：
          Step 1: osmium extract - 区域裁剪
          Step 2: osmium tags-filter - 标签过滤
          Step 3: osmium export - 导出 GeoJSON

        对于 relation 要素（水体/河流）：
          Step 1: osmium tags-filter - 从完整 PBF 过滤 relation（使用 r/ 前缀）
          Step 2: osmium export - 导出 GeoJSON
          Step 3: ogr2ogr -clipsrc - 精确裁剪到 bbox

        Returns:
            True if success
        """
        import time

        # 临时文件放在项目 tmp/ 目录下，用 PID 隔离防止多进程冲突
        project_tmp = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'tmp')
        os.makedirs(project_tmp, exist_ok=True)
        _pid = os.getpid()
        temp_dir = os.path.join(project_tmp, f"osmium_cli_{tag_type}_{south:.4f}_{west:.4f}_pid{_pid}")
        os.makedirs(temp_dir, exist_ok=True)

        base_name = os.path.splitext(os.path.basename(pbf_file))[0]

        if os.name == 'nt':
            flags = subprocess.CREATE_NO_WINDOW
        else:
            flags = 0

        osmium_command = self._get_osmium_command()

        # 判断是否使用 relation-first 管线
        use_relation_first = tag_type in self.RELATION_FIRST_TYPES

        if use_relation_first:
            return self._run_relation_first_pipeline(
                osmium_command, pbf_file, tag_type, south, west, north, east,
                output_path, temp_dir, base_name, flags
            )
        else:
            return self._run_standard_pipeline(
                osmium_command, pbf_file, tag_type, south, west, north, east,
                output_path, temp_dir, base_name, flags
            )

    def _shapely_clip_geojson(
        self, raw_geojson: str, output_path: str,
        south: float, west: float, north: float, east: float
    ) -> bool:
        """用 shapely 把 GeoJSON 硬裁剪到 bbox（ogr2ogr -clipsrc 的等价实现）。

        逐个 feature 与 bbox 求交，丢弃空结果后写回 GeoJSON。
        项目约定：坐标/bbox 裁剪统一用 shapely，不依赖 GDAL CLI。
        """
        import json
        from shapely.geometry import box, shape, mapping

        try:
            with open(raw_geojson, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"shapely 裁剪读取 GeoJSON 失败: {e}")
            return False

        clip_box = box(west, south, east, north)
        feats = data.get('features', [])
        clipped = []
        for feat in feats:
            geom = feat.get('geometry')
            if not geom:
                continue
            try:
                g = shape(geom)
                if not g.is_valid:
                    g = g.buffer(0)
                c = g.intersection(clip_box)
            except Exception:
                continue
            if c.is_empty:
                continue
            new_feat = dict(feat)
            new_feat['geometry'] = mapping(c)
            clipped.append(new_feat)

        data['features'] = clipped
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"shapely 裁剪写出 GeoJSON 失败: {e}")
            return False

        logger.info(f"shapely bbox 裁剪完成: {len(feats)} -> {len(clipped)} 个要素")
        return True

    def _run_standard_pipeline(
        self, osmium_command, pbf_file, tag_type, south, west, north, east,
        output_path, temp_dir, base_name, flags
    ) -> bool:
        """标准管线：extract → tags-filter → export → (ogr2ogr clip for water)"""
        import time

        is_water = (tag_type == 'water')
        n_steps = 4 if is_water else 3
        bbox_str = f"{west},{south},{east},{north}"

        # Step 1: bbox 裁剪（水体用 -s smart 保留跨 bbox 的完整 way/relation）
        area_pbf = os.path.join(temp_dir, f"{base_name}_area.pbf")

        osmium_label = ' '.join(osmium_command)
        cmd1 = [
            *osmium_command, 'extract',
            _bbox_option(west, south, east, north),
        ]
        if is_water:
            cmd1.extend(['-s', 'smart'])
        cmd1.extend([
            pbf_file,
            '-o', area_pbf,
            '--overwrite'
        ])

        smart_label = ' -s smart' if is_water else ''
        logger.info(f"Step 1: {osmium_label} extract -b {bbox_str}{smart_label}")
        print(f"  [Step 1/{n_steps}] osmium extract (clipping area){' (smart mode)' if is_water else ''}...")
        print(f"           Command: {osmium_label} extract -b {bbox_str}{smart_label} {pbf_file} -o {area_pbf} --overwrite")
        t1 = time.time()
        result1 = None
        for attempt in range(3):
            result1 = subprocess.run(cmd1, capture_output=True, timeout=600, creationflags=flags)
            if result1.returncode == 0:
                break
            if attempt < 2:
                import time as _time
                _time.sleep(2)
                print(f"           Retry {attempt+2}/3...")
        elapsed1 = time.time() - t1

        if result1.returncode != 0:
            stderr_msg = result1.stderr.decode('utf-8', errors='replace')
            logger.error(f"osmium extract 失败 (3 attempts): {stderr_msg}")
            print(f"           FAILED after 3 attempts: {stderr_msg}")
            return False

        pbf_size = os.path.getsize(area_pbf) / 1024 if os.path.exists(area_pbf) else 0
        print(f"           Done in {elapsed1:.1f}s, output: {pbf_size:.1f} KB")

        # Step 2: 标签过滤
        filtered_pbf = os.path.join(temp_dir, f"{base_name}_{tag_type}.osm.pbf")
        filter_expr = self.TAG_FILTERS.get(tag_type, '')

        if not filter_expr:
            logger.error(f"未知的 tag_type: {tag_type}")
            return False

        cmd2 = [
            *osmium_command, 'tags-filter',
            area_pbf,
        ] + filter_expr.split() + [
            '-o', filtered_pbf,
            '--overwrite'
        ]

        logger.info(f"Step 2: {osmium_label} tags-filter {filter_expr}")
        print(f"  [Step 2/{n_steps}] osmium tags-filter (filtering {tag_type} features)...")
        print(f"           Command: {osmium_label} tags-filter {area_pbf} {filter_expr} -o {filtered_pbf} --overwrite")
        t2 = time.time()
        result2 = subprocess.run(cmd2, capture_output=True, timeout=300, creationflags=flags)
        elapsed2 = time.time() - t2

        if result2.returncode != 0:
            logger.error(f"osmium tags-filter 失败: {result2.stderr.decode()}")
            print(f"           FAILED: {result2.stderr.decode()}")
            return False

        filtered_size = os.path.getsize(filtered_pbf) / 1024 if os.path.exists(filtered_pbf) else 0
        print(f"           Done in {elapsed2:.1f}s, output: {filtered_size:.1f} KB")

        if filtered_size == 0:
            print(f"           WARNING: Filtered PBF is empty - no {tag_type} features found!")
            print(f"           Keeping temp files for debugging: {area_pbf}, {filtered_pbf}")
            return False

        # Step 3: 导出 GeoJSON（水体先导出到临时文件，后面 ogr2ogr 裁剪后才是最终输出）
        if is_water:
            raw_geojson = os.path.join(temp_dir, f"{base_name}_{tag_type}_full.geojson")
        else:
            raw_geojson = output_path

        cmd3 = [
            *osmium_command, 'export',
            filtered_pbf,
            '-o', raw_geojson,
            '-f', 'geojson',
            '--overwrite'
        ]

        logger.info(f"Step 3: {osmium_label} export -f geojson")
        print(f"  [Step 3/{n_steps}] osmium export (converting to GeoJSON)...")
        print(f"           Command: {osmium_label} export {filtered_pbf} -o {raw_geojson} -f geojson --overwrite")
        t3 = time.time()
        # The portable Python-backed osmium fallback is much slower than the
        # native C++ tool on dense extracts. Scale the export budget with the
        # filtered PBF size instead of killing a healthy export at 120 seconds.
        export_timeout = _export_timeout_seconds(
            filtered_size, osmium_command)
        result3 = subprocess.run(
            cmd3, capture_output=True, timeout=export_timeout,
            creationflags=flags)
        elapsed3 = time.time() - t3

        if result3.returncode != 0:
            logger.error(f"osmium export 失败: {result3.stderr.decode()}")
            print(f"           FAILED: {result3.stderr.decode()}")
            return False

        raw_size = os.path.getsize(raw_geojson) / 1024 if os.path.exists(raw_geojson) else 0
        print(f"           Done in {elapsed3:.1f}s, output: {raw_size:.1f} KB")

        if raw_size == 0:
            print(f"           WARNING: GeoJSON output is empty!")
            print(f"           Debug: Check intermediate files:")
            print(f"             - Area PBF: {area_pbf}")
            print(f"             - Filtered PBF: {filtered_pbf}")
            return False

        # Step 4 (水体专用): 边界硬裁剪（优先 ogr2ogr，缺失时降级 shapely）
        # smart 模式会带出边界外的毛刺，需要硬切
        elapsed4 = 0
        if is_water:
            if not self.ogr2ogr_available:
                print(f"  [Step 4/{n_steps}] bbox clip via shapely (ogr2ogr not installed)...")
                t4 = time.time()
                ok = self._shapely_clip_geojson(
                    raw_geojson, output_path, south, west, north, east)
                elapsed4 = time.time() - t4
                if not ok:
                    logger.warning("shapely 裁剪失败，返回 smart 模式原始数据")
                    import shutil
                    shutil.copy2(raw_geojson, output_path)
                else:
                    final_size = os.path.getsize(output_path) / 1024 if os.path.exists(output_path) else 0
                    print(f"           Done in {elapsed4:.1f}s, output: {final_size:.1f} KB")
            else:
                ogr2ogr_path = self._get_tool_path('ogr2ogr')
                cmd4 = [
                    ogr2ogr_path,
                    '-f', 'GeoJSON',
                    output_path,
                    raw_geojson,
                    '-clipsrc', str(west), str(south), str(east), str(north)
                ]

                logger.info(f"Step 4: ogr2ogr -clipsrc {bbox_str}")
                print(f"  [Step 4/{n_steps}] ogr2ogr -clipsrc (hard clipping to bbox)...")
                print(f"           Command: ogr2ogr -f GeoJSON {output_path} {raw_geojson} -clipsrc {bbox_str}")
                t4 = time.time()
                result4 = subprocess.run(cmd4, capture_output=True, timeout=120, creationflags=flags)
                elapsed4 = time.time() - t4

                if result4.returncode != 0:
                    stderr_msg = result4.stderr.decode('utf-8', errors='replace')
                    logger.error(f"ogr2ogr 裁剪失败: {stderr_msg}")
                    print(f"           FAILED: {stderr_msg}")
                    # 降级：shapely 裁剪，再不行才用未裁剪数据
                    if not self._shapely_clip_geojson(
                            raw_geojson, output_path, south, west, north, east):
                        import shutil
                        shutil.copy2(raw_geojson, output_path)
                        print(f"           Fallback: using unclipped data")
                else:
                    final_size = os.path.getsize(output_path) / 1024 if os.path.exists(output_path) else 0
                    print(f"           Done in {elapsed4:.1f}s, output: {final_size:.1f} KB")

            # 清理水体的临时 raw GeoJSON
            try:
                os.remove(raw_geojson)
            except:
                pass

        # 清理临时文件和目录
        try:
            os.remove(area_pbf)
            os.remove(filtered_pbf)
            os.rmdir(temp_dir)
        except:
            pass

        total_time = elapsed1 + elapsed2 + elapsed3 + elapsed4
        logger.info(f"CLI 管道完成: {output_path}")
        print(f"  [Pipeline] Total time: {total_time:.1f}s")
        return True

    def _run_relation_first_pipeline(
        self, osmium_command, pbf_file, tag_type, south, west, north, east,
        output_path, temp_dir, base_name, flags
    ) -> bool:
        """Relation-first 管线：tags-filter → export → ogr2ogr clip

        用于河流等 multipolygon relation 数据，避免 osmium extract 破坏 relation 结构。
        """
        import time

        bbox_str = f"{west},{south},{east},{north}"
        filter_expr = self.WATER_RELATION_FILTERS.get(tag_type, '')

        if not filter_expr:
            logger.error(f"未知的 relation tag_type: {tag_type}")
            return False

        # Step 1a: extract 裁剪区域（避免对完整国家 PBF 做 tags-filter 触发 zlib buffer error）
        area_pbf = os.path.join(temp_dir, f"{base_name}_{tag_type}_area.pbf")
        osmium_label = ' '.join(osmium_command)
        cmd_extract = [
            *osmium_command, 'extract',
            _bbox_option(west, south, east, north),
            '-s', 'smart',
            pbf_file,
            '-o', area_pbf,
            '--overwrite'
        ]

        logger.info(f"Step 1a: {osmium_label} extract -b {bbox_str}")
        print(f"  [Step 1/3] osmium extract → tags-filter (extract area then filter {tag_type})...")
        print(f"           Extract: osmium extract -b {bbox_str} -s smart {pbf_file}")
        t1 = time.time()
        result_ext = None
        for attempt in range(3):
            result_ext = subprocess.run(cmd_extract, capture_output=True, timeout=600, creationflags=flags)
            if result_ext.returncode == 0:
                break
            if attempt < 2:
                import time as _time
                _time.sleep(2)
                print(f"           Retry {attempt+2}/3...")

        if result_ext.returncode != 0:
            stderr_msg = result_ext.stderr.decode('utf-8', errors='replace')
            logger.error(f"osmium extract 失败 (3 attempts): {stderr_msg}")
            print(f"           Extract FAILED after 3 attempts: {stderr_msg}")
            return False

        area_size = os.path.getsize(area_pbf) / 1024 if os.path.exists(area_pbf) else 0
        print(f"           Extract done: {area_size:.1f} KB")

        # Step 1b: tags-filter 在裁切后的小文件上（不会触发 buffer error）
        filtered_pbf = os.path.join(temp_dir, f"{base_name}_{tag_type}_full.pbf")
        cmd1 = [
            *osmium_command, 'tags-filter',
            area_pbf,
        ] + filter_expr.split() + [
            '-o', filtered_pbf,
            '--overwrite'
        ]

        print(f"           Filter: osmium tags-filter {filter_expr}")
        result1 = subprocess.run(cmd1, capture_output=True, timeout=600, creationflags=flags)
        elapsed1 = time.time() - t1

        if result1.returncode != 0:
            stderr_msg = result1.stderr.decode('utf-8', errors='replace')
            logger.error(f"osmium tags-filter 失败: {stderr_msg}")
            print(f"           Filter FAILED: {stderr_msg}")
            return False

        filtered_size = os.path.getsize(filtered_pbf) / 1024 if os.path.exists(filtered_pbf) else 0
        print(f"           Done in {elapsed1:.1f}s, filtered: {filtered_size:.1f} KB")

        if filtered_size == 0:
            print(f"           WARNING: Filtered PBF is empty - no {tag_type} relations found in area")
            return False

        # Step 2: 导出为 GeoJSON（保持完整 relation 结构）
        full_geojson = os.path.join(temp_dir, f"{base_name}_{tag_type}_full.geojson")

        cmd2 = [
            *osmium_command, 'export',
            filtered_pbf,
            '-o', full_geojson,
            '-f', 'geojson',
            '--overwrite'
        ]

        logger.info(f"Step 2: {osmium_label} export -f geojson")
        print(f"  [Step 2/3] osmium export (converting to GeoJSON)...")
        print(f"           Command: {osmium_label} export {filtered_pbf} -o {full_geojson} -f geojson --overwrite")
        t2 = time.time()
        result2 = subprocess.run(cmd2, capture_output=True, timeout=120, creationflags=flags)
        elapsed2 = time.time() - t2

        if result2.returncode != 0:
            logger.error(f"osmium export 失败: {result2.stderr.decode()}")
            print(f"           FAILED: {result2.stderr.decode()}")
            return False

        full_geojson_size = os.path.getsize(full_geojson) / 1024 if os.path.exists(full_geojson) else 0
        print(f"           Done in {elapsed2:.1f}s, output: {full_geojson_size:.1f} KB")

        if full_geojson_size == 0:
            print(f"           WARNING: GeoJSON output is empty!")
            return False

        # Step 3: 使用 ogr2ogr 精确裁剪到 bbox
        if not self.ogr2ogr_available:
            logger.warning("ogr2ogr 不可用，跳过 bbox 裁剪，返回全部数据")
            print(f"  [Step 3/3] ogr2ogr not available, skipping bbox clip")
            # 直接复制未裁剪的数据
            import shutil
            shutil.copy2(full_geojson, output_path)
        else:
            ogr2ogr_path = self._get_tool_path('ogr2ogr')
            cmd3 = [
                ogr2ogr_path,
                '-f', 'GeoJSON',
                output_path,
                full_geojson,
                '-clipsrc', str(west), str(south), str(east), str(north)
            ]

            logger.info(f"Step 3: ogr2ogr -clipsrc {bbox_str}")
            print(f"  [Step 3/3] ogr2ogr -clipsrc (clipping to bbox)...")
            print(f"           Command: ogr2ogr -f GeoJSON {output_path} {full_geojson} -clipsrc {bbox_str}")
            t3 = time.time()
            result3 = subprocess.run(cmd3, capture_output=True, timeout=120, creationflags=flags)
            elapsed3 = time.time() - t3

            if result3.returncode != 0:
                stderr_msg = result3.stderr.decode('utf-8', errors='replace')
                logger.error(f"ogr2ogr 裁剪失败: {stderr_msg}")
                print(f"           FAILED: {stderr_msg}")
                # 如果 ogr2ogr 失败，回退到未裁剪数据
                import shutil
                shutil.copy2(full_geojson, output_path)
                print(f"           Fallback: using unclipped data")
            else:
                geojson_size = os.path.getsize(output_path) / 1024 if os.path.exists(output_path) else 0
                print(f"           Done in {elapsed3:.1f}s, output: {geojson_size:.1f} KB")

        # 清理临时文件和目录
        try:
            os.remove(filtered_pbf)
            os.remove(full_geojson)
            os.rmdir(temp_dir)
        except:
            pass

        total_time = elapsed1 + elapsed2
        if 'elapsed3' in locals():
            total_time += elapsed3
        logger.info(f"CLI Relation-first 管道完成: {output_path}")
        print(f"  [Pipeline] Total time: {total_time:.1f}s")
        return True
    
    def fetch_water(self, south: float, west: float, north: float, east: float,
                    pbf_file: str = None, region: str = None) -> gpd.GeoDataFrame:
        """获取水体数据"""
        return self.fetch_features('water', south, west, north, east, pbf_file, region)
    
    def fetch_roads(self, south: float, west: float, north: float, east: float,
                    pbf_file: str = None, region: str = None) -> gpd.GeoDataFrame:
        """获取道路数据"""
        return self.fetch_features('road', south, west, north, east, pbf_file, region)
    
    def fetch_buildings(self, south: float, west: float, north: float, east: float,
                        pbf_file: str = None, region: str = None) -> gpd.GeoDataFrame:
        """获取建筑数据"""
        return self.fetch_features('building', south, west, north, east, pbf_file, region)
    
    def fetch_vegetation(self, south: float, west: float, north: float, east: float,
                         pbf_file: str = None, region: str = None) -> gpd.GeoDataFrame:
        """获取植被数据"""
        return self.fetch_features('vegetation', south, west, north, east, pbf_file, region)


# 全局实例
_cli_fetcher: Optional[OsmiumCLIFetcher] = None


def get_cli_fetcher(pbf_dir: str = None) -> OsmiumCLIFetcher:
    """获取全局 CLI fetcher 实例"""
    global _cli_fetcher
    if _cli_fetcher is None:
        _cli_fetcher = OsmiumCLIFetcher(pbf_dir)
    return _cli_fetcher


def sample_building_density(
    pbf_file: str,
    south: float, west: float, north: float, east: float,
) -> dict:
    """快速采样建筑密度，返回 {count_est, area_km2, density, should_filter}。

    通过 osmium extract + tags-filter 裁剪 PBF，用 fileinfo -e 获取 way 数量估算建筑数。
    should_filter=True 表示建筑过多，建议用 building_landmarks 过滤器。
    """
    import re, shutil, time as _time
    fetcher = get_cli_fetcher()
    osmium_command = fetcher._get_osmium_command()
    if not fetcher.osmium_available:
        return {'count_est': 0, 'area_km2': 0, 'density': 0, 'should_filter': False}

    t0 = _time.time()
    tmp_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'tmp', '_sample_density')
    os.makedirs(tmp_dir, exist_ok=True)
    area_pbf = os.path.join(tmp_dir, 'extract.osm.pbf')
    bldg_pbf = os.path.join(tmp_dir, 'buildings.osm.pbf')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    count_est = 0
    try:
        # Step 1: osmium extract (bbox clip)
        subprocess.run([
            *osmium_command, 'extract',
            _bbox_option(west, south, east, north), '-s', 'smart',
            pbf_file, '-o', area_pbf, '--overwrite'
        ], capture_output=True, timeout=120, check=True, creationflags=flags)
        # Step 2: osmium tags-filter (building only)
        subprocess.run([
            *osmium_command, 'tags-filter',
            area_pbf, 'nwr/building',
            '-o', bldg_pbf, '--overwrite'
        ], capture_output=True, timeout=60, check=True, creationflags=flags)
        # Step 3: osmium fileinfo -e (text format) → parse "Number of ways"
        info = subprocess.run([
            *osmium_command, 'fileinfo', '-e', bldg_pbf
        ], capture_output=True, text=True, timeout=30, creationflags=flags)
        if info.returncode == 0:
            # Parse "Number of ways: 36118" from text output
            m = re.search(r'Number of ways:\s*(\d+)', info.stdout)
            if m:
                count_est = int(m.group(1))
            else:
                # Fallback: file size estimate (~38 bytes/way in compressed PBF)
                fsize = os.path.getsize(bldg_pbf)
                count_est = max(0, fsize // 38)
        else:
            # Fallback: file size estimate
            fsize = os.path.getsize(bldg_pbf)
            count_est = max(0, fsize // 38)
    except Exception as e:
        print(f"  [sample] 采样失败: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Compute area in km2
    import numpy as np
    mid_lat = (south + north) / 2
    area_km2 = (north - south) * 111.0 * (east - west) * 111.0 * np.cos(np.radians(mid_lat))
    density = count_est / area_km2 if area_km2 > 0 and count_est > 0 else 0
    elapsed = _time.time() - t0

    # Decision: >150K buildings or >500/km² → use building_landmarks filter
    should_filter = count_est > 150000 or density > 500

    print(f"  [sample] 建筑: ~{count_est:,} ways, 面积: {area_km2:.0f}km², "
          f"密度: {density:.0f}/km², 过滤: {should_filter} ({elapsed:.1f}s)")
    return {
        'count_est': count_est,
        'area_km2': area_km2,
        'density': density,
        'should_filter': should_filter,
    }


def fetch_from_cli(
    tag_type: str,
    south: float,
    west: float,
    north: float,
    east: float,
    pbf_file: str = None,
    region: str = None,
) -> gpd.GeoDataFrame:
    """使用 CLI 方式获取数据的便捷函数"""
    fetcher = get_cli_fetcher()
    return fetcher.fetch_features(tag_type, south, west, north, east, pbf_file, region)


def fetch_tiled_from_cli(
    tag_type: str,
    south: float,
    west: float,
    north: float,
    east: float,
    pbf_file: str = None,
    region: str = None,
) -> gpd.GeoDataFrame:
    """瓦片级缓存取数的便捷函数（Phase 2，跨网格线偏移部分复用）"""
    fetcher = get_cli_fetcher()
    return fetcher.fetch_tiled_features(tag_type, south, west, north, east,
                                        pbf_file, region)
