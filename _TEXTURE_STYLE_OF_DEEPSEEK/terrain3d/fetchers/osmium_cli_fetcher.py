"""使用 osmium CLI 获取 OSM 数据

高性能方案：使用 osmium 命令行工具，速度提升 10-20倍。

标准流程（所有要素类型，包括水体）：
1. osmium extract - 区域裁剪
2. osmium tags-filter - 标签过滤（水体用 natural=water water waterway landuse=reservoir）
3. osmium export - 导出 GeoJSON
4. Python 读取 GeoJSON → GeoDataFrame
"""

import logging
import os
import shlex
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional

import geopandas as gpd

logger = logging.getLogger(__name__)

# 默认 PBF 文件目录
DEFAULT_PBF_DIR = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'pbf_cache')
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
PORTABLE_OSMIUM_SCRIPT = os.path.join(PROJECT_ROOT, 'tools', 'osmium_pyosmium.py')


class OsmiumCLIFetcher:
    """使用 osmium CLI 获取 OSM 数据"""

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
        # 水体全量过滤：江河湖泊一网打尽
        # 去掉了 r/ 前缀和 =* 后缀，让 Way 和 Relation 都能匹配
        'water': 'natural=water water waterway landuse=reservoir',
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

        # Prefer the native binary, but keep the pipeline functional on machines
        # where only the Python dependency can be installed.
        self.native_osmium_available = self._check_tool('osmium')
        self.osmium_command = self._resolve_osmium_command()
        self.osmium_available = self.osmium_command is not None
        self.osmium_backend = (
            'native' if self.native_osmium_available
            else 'pyosmium' if self.osmium_available
            else 'unavailable'
        )

        # 检查 ogr2ogr 是否可用（用于 relation 数据的 bbox 裁剪）
        self.ogr2ogr_available = self._check_tool('ogr2ogr')

        if self.osmium_backend == 'pyosmium':
            logger.info("原生 osmium 不可用，使用 portable pyosmium 回退")
        elif not self.osmium_available:
            logger.warning(
                "osmium 不可用。安装 osmium-tool，或 pip install osmium 以启用 portable 回退"
            )
        if not self.ogr2ogr_available:
            logger.warning("ogr2ogr 未安装。安装: conda install -c conda-forge gdal")
            logger.warning("ogr2ogr 用于河流 relation 数据的 bbox 裁剪，缺失可能导致河流数据不完整")

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
    
    def _get_tool_path(self, tool_name: str) -> str:
        """获取工具的完整路径"""
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

    def _resolve_osmium_command(self) -> Optional[List[str]]:
        """Return an argv prefix for native osmium or the bundled fallback."""
        if self.native_osmium_available:
            return [self._get_tool_path('osmium')]

        if not os.path.isfile(PORTABLE_OSMIUM_SCRIPT):
            return None

        command = [sys.executable, PORTABLE_OSMIUM_SCRIPT]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            result = subprocess.run(
                command + ['--version'],
                capture_output=True,
                timeout=10,
                creationflags=flags,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return command if result.returncode == 0 else None

    def _get_tool_command(self, tool_name: str) -> List[str]:
        """Return a subprocess-safe argv prefix for a supported tool."""
        if tool_name == 'osmium':
            return list(self.osmium_command or [])
        return [self._get_tool_path(tool_name)]

    @staticmethod
    def _format_command(command: List[str]) -> str:
        """Format argv for logs only; execution always receives the list."""
        return shlex.join(command)
    
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
        output_path = os.path.join(project_tmp, f"osmium_{tag_type}_{south:.4f}_{west:.4f}_{north:.4f}_{east:.4f}.geojson")

        logger.info(f"使用 CLI 方式获取 {tag_type} 数据...")
        logger.info(f"边界框: ({south:.4f}, {west:.4f}, {north:.4f}, {east:.4f})")
        print(f"\n  [CLI Pipeline] Starting {tag_type} data extraction...")
        print(f"  Bounding box: ({south:.4f}, {west:.4f}, {north:.4f}, {east:.4f})")

        try:
            # GeoJSON cache: if file already exists and is non-empty, reuse it
            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                print(f"  [CLI Pipeline] Using cached GeoJSON: {output_path}")
                gdf = gpd.read_file(output_path)
                if len(gdf) > 0:
                    print(f"  [CLI Pipeline] Loaded {len(gdf)} features from cache\n")
                    # Buildings: ensure est_height column
                    if tag_type in ('building', 'building_landmarks') and len(gdf) > 0:
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
                        gdf["est_height"] = _estimate_building_heights(gdf, ndsm_heights)
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
                if tag_type in ('building', 'building_landmarks') and len(gdf) > 0:
                    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
                        _estimate_building_heights,
                        get_ndsm_grid,
                    )
                    ndsm_heights = None
                    ndsm_cache = get_ndsm_grid()
                    if ndsm_cache is not None:
                        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.ndsm import (
                            sample_building_heights_from_ndsm,
                        )
                        grid, s, w, n, e = ndsm_cache
                        ndsm_heights = sample_building_heights_from_ndsm(
                            gdf, grid, s, w, n, e
                        )
                    gdf["est_height"] = _estimate_building_heights(gdf, ndsm_heights)

                return gdf
        except Exception as e:
            logger.error(f"CLI 执行失败: {e}")
            return gpd.GeoDataFrame()

        return gpd.GeoDataFrame()
    
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

        osmium_command = self._get_tool_command('osmium')
        if not osmium_command:
            logger.error("osmium command unavailable")
            return False

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

        cmd1 = osmium_command + [
            'extract',
            '-b', bbox_str,
        ]
        if is_water:
            cmd1.extend(['-s', 'smart'])
        cmd1.extend([
            pbf_file,
            '-o', area_pbf,
            '--overwrite'
        ])

        smart_label = ' -s smart' if is_water else ''
        logger.info("Step 1: %s", self._format_command(cmd1))
        print(f"  [Step 1/{n_steps}] osmium extract (clipping area){' (smart mode)' if is_water else ''}...")
        print(f"           Command: osmium extract -b {bbox_str}{smart_label} {pbf_file} -o {area_pbf} --overwrite")
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

        cmd2 = osmium_command + [
            'tags-filter',
            area_pbf,
        ] + filter_expr.split() + [
            '-o', filtered_pbf,
            '--overwrite'
        ]

        logger.info("Step 2: %s", self._format_command(cmd2))
        print(f"  [Step 2/{n_steps}] osmium tags-filter (filtering {tag_type} features)...")
        print(f"           Command: osmium tags-filter {area_pbf} {filter_expr} -o {filtered_pbf} --overwrite")
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

        cmd3 = osmium_command + [
            'export',
            filtered_pbf,
            '-o', raw_geojson,
            '-f', 'geojson',
            '--overwrite'
        ]

        logger.info("Step 3: %s", self._format_command(cmd3))
        print(f"  [Step 3/{n_steps}] osmium export (converting to GeoJSON)...")
        print(f"           Command: osmium export {filtered_pbf} -o {raw_geojson} -f geojson --overwrite")
        t3 = time.time()
        result3 = subprocess.run(cmd3, capture_output=True, timeout=120, creationflags=flags)
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

        # Step 4 (水体专用): ogr2ogr 做最终边界硬裁剪
        # smart 模式会带出边界外的毛刺，需要 ogr2ogr 硬切
        elapsed4 = 0
        if is_water:
            if not self.ogr2ogr_available:
                logger.warning("ogr2ogr 不可用，跳过 bbox 硬裁剪，返回 smart 模式原始数据")
                print(f"  [Step 4/{n_steps}] ogr2ogr not available, skipping bbox clip")
                import shutil
                shutil.copy2(raw_geojson, output_path)
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
        cmd_extract = osmium_command + [
            'extract',
            '-b', bbox_str,
            '-s', 'smart',
            pbf_file,
            '-o', area_pbf,
            '--overwrite'
        ]

        logger.info("Step 1a: %s", self._format_command(cmd_extract))
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
        cmd1 = osmium_command + [
            'tags-filter',
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

        cmd2 = osmium_command + [
            'export',
            filtered_pbf,
            '-o', full_geojson,
            '-f', 'geojson',
            '--overwrite'
        ]

        logger.info("Step 2: %s", self._format_command(cmd2))
        print(f"  [Step 2/3] osmium export (converting to GeoJSON)...")
        print(f"           Command: osmium export {filtered_pbf} -o {full_geojson} -f geojson --overwrite")
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
    osmium_command = fetcher._get_tool_command('osmium')
    if not osmium_command:
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
        subprocess.run(osmium_command + [
            'extract',
            '-b', f'{west},{south},{east},{north}', '-s', 'smart',
            pbf_file, '-o', area_pbf, '--overwrite'
        ], capture_output=True, timeout=120, check=True, creationflags=flags)
        # Step 2: osmium tags-filter (building only)
        subprocess.run(osmium_command + [
            'tags-filter',
            area_pbf, 'nwr/building',
            '-o', bldg_pbf, '--overwrite'
        ], capture_output=True, timeout=60, check=True, creationflags=flags)
        # Step 3: osmium fileinfo -e (text format) → parse "Number of ways"
        info = subprocess.run(osmium_command + [
            'fileinfo', '-e', bldg_pbf
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
