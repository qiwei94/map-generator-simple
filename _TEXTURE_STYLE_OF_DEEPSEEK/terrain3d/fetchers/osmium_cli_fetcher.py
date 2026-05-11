"""使用 osmium CLI 获取 OSM 数据

高性能方案：使用 osmium 命令行工具，速度提升 10-20倍。

标准流程（建筑、道路、植被等）：
1. osmium extract - 区域裁剪
2. osmium tags-filter - 标签过滤
3. osmium export - 导出 GeoJSON
4. Python 读取 GeoJSON → GeoDataFrame

河流 relation 专用流程（保留完整 multipolygon 结构）：
1. osmium tags-filter - 从完整 PBF 过滤河流 relation（使用 r/ 前缀）
2. osmium export - 导出 GeoJSON
3. ogr2ogr -clipsrc - 精确裁剪到 bbox
"""

import logging
import os
import subprocess
import tempfile
from typing import Dict, Optional

import geopandas as gpd

logger = logging.getLogger(__name__)

# 默认 PBF 文件目录
DEFAULT_PBF_DIR = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'pbf_cache')


class OsmiumCLIFetcher:
    """使用 osmium CLI 获取 OSM 数据"""

    # 标准标签过滤表达式（使用 nwr = node/way/relation）
    # 适用于建筑、道路、植被等普通要素
    TAG_FILTERS = {
        'building': 'nwr/building',
        'road': 'nwr/highway',
        'vegetation': 'nwr/landuse=forest,grass,meadow nwr/natural=wood,grassland,scrub',
        'park': 'nwr/leisure=park,garden nwr/landuse=recreation_ground',
        'wetland': 'nwr/natural=wetland,marsh,swamp',
    }

    # 水体专用过滤器（使用 r/ 前缀提取 relation，保留完整 multipolygon 结构）
    # 河流在 OSM 中以 multipolygon relation 形式存储，必须用 r/ 前缀
    WATER_RELATION_FILTERS = {
        'water': 'r/natural=water r/water=* r/waterway=* r/landuse=reservoir',
    }

    # 是否对特定类型使用 relation-first 管线（先过滤后裁剪）
    RELATION_FIRST_TYPES = {'water'}

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

        # 检查 ogr2ogr 是否可用（用于 relation 数据的 bbox 裁剪）
        self.ogr2ogr_available = self._check_tool('ogr2ogr')

        if not self.osmium_available:
            logger.warning("osmium CLI 未安装。安装: conda install -c conda-forge osmium-tool")
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
            result = self._run_osmium_pipeline(
                pbf_file, tag_type, south, west, north, east, output_path
            )
            if result:
                gdf = gpd.read_file(output_path)
                logger.info(f"CLI 管线完成: {len(gdf)} 条记录")
                print(f"  [CLI Pipeline] Complete: {len(gdf)} features extracted\n")
                print(f"  Output: {output_path}")
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

        # 临时文件放在项目 tmp/ 目录下，便于调试
        project_tmp = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'tmp')
        os.makedirs(project_tmp, exist_ok=True)
        temp_dir = os.path.join(project_tmp, f"osmium_cli_{tag_type}_{south:.4f}_{west:.4f}")
        os.makedirs(temp_dir, exist_ok=True)

        base_name = os.path.splitext(os.path.basename(pbf_file))[0]

        if os.name == 'nt':
            flags = subprocess.CREATE_NO_WINDOW
        else:
            flags = 0

        osmium_path = self._get_tool_path('osmium')

        # 判断是否使用 relation-first 管线
        use_relation_first = tag_type in self.RELATION_FIRST_TYPES

        if use_relation_first:
            return self._run_relation_first_pipeline(
                osmium_path, pbf_file, tag_type, south, west, north, east,
                output_path, temp_dir, base_name, flags
            )
        else:
            return self._run_standard_pipeline(
                osmium_path, pbf_file, tag_type, south, west, north, east,
                output_path, temp_dir, base_name, flags
            )

    def _run_standard_pipeline(
        self, osmium_path, pbf_file, tag_type, south, west, north, east,
        output_path, temp_dir, base_name, flags
    ) -> bool:
        """标准管线：extract → tags-filter → export"""
        import time

        # Step 1: 区域裁剪
        area_pbf = os.path.join(temp_dir, f"{base_name}_area.pbf")
        bbox_str = f"{west},{south},{east},{north}"

        cmd1 = [
            osmium_path, 'extract',
            '-b', bbox_str,
            pbf_file,
            '-o', area_pbf,
            '--overwrite'
        ]

        logger.info(f"Step 1: {osmium_path} extract -b {bbox_str}")
        print(f"  [Step 1/3] osmium extract (clipping area)...")
        print(f"           Command: osmium extract -b {bbox_str} {pbf_file} -o {area_pbf} --overwrite")
        t1 = time.time()
        result1 = subprocess.run(cmd1, capture_output=True, timeout=60, creationflags=flags)
        elapsed1 = time.time() - t1

        if result1.returncode != 0:
            stderr_msg = result1.stderr.decode('utf-8', errors='replace')
            logger.error(f"osmium extract 失败: {stderr_msg}")
            print(f"           FAILED: {stderr_msg}")
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
            osmium_path, 'tags-filter',
            area_pbf,
        ] + filter_expr.split() + [
            '-o', filtered_pbf,
            '--overwrite'
        ]

        logger.info(f"Step 2: {osmium_path} tags-filter {filter_expr}")
        print(f"  [Step 2/3] osmium tags-filter (filtering {tag_type} features)...")
        print(f"           Command: osmium tags-filter {area_pbf} {filter_expr} -o {filtered_pbf} --overwrite")
        t2 = time.time()
        result2 = subprocess.run(cmd2, capture_output=True, timeout=60, creationflags=flags)
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

        # Step 3: 导出 GeoJSON
        cmd3 = [
            osmium_path, 'export',
            filtered_pbf,
            '-o', output_path,
            '-f', 'geojson',
            '--overwrite'
        ]

        logger.info(f"Step 3: {osmium_path} export -f geojson")
        print(f"  [Step 3/3] osmium export (converting to GeoJSON)...")
        print(f"           Command: osmium export {filtered_pbf} -o {output_path} -f geojson --overwrite")
        t3 = time.time()
        result3 = subprocess.run(cmd3, capture_output=True, timeout=120, creationflags=flags)
        elapsed3 = time.time() - t3

        if result3.returncode != 0:
            logger.error(f"osmium export 失败: {result3.stderr.decode()}")
            print(f"           FAILED: {result3.stderr.decode()}")
            return False

        geojson_size = os.path.getsize(output_path) / 1024 if os.path.exists(output_path) else 0
        print(f"           Done in {elapsed3:.1f}s, output: {geojson_size:.1f} KB")

        if geojson_size == 0:
            print(f"           WARNING: GeoJSON output is empty!")
            print(f"           Debug: Check intermediate files:")
            print(f"             - Area PBF: {area_pbf}")
            print(f"             - Filtered PBF: {filtered_pbf}")
            return False

        # 清理临时文件和目录
        try:
            os.remove(area_pbf)
            os.remove(filtered_pbf)
            os.rmdir(temp_dir)
        except:
            pass

        total_time = elapsed1 + elapsed2 + elapsed3
        logger.info(f"CLI 管道完成: {output_path}")
        print(f"  [Pipeline] Total time: {total_time:.1f}s")
        return True

    def _run_relation_first_pipeline(
        self, osmium_path, pbf_file, tag_type, south, west, north, east,
        output_path, temp_dir, base_name, flags
    ) -> bool:
        """Relation-first 管线：tags-filter → export → ogr2ogr clip

        用于河流等 multipolygon relation 数据，避免 osmium extract 破坏 relation 结构。
        """
        import time

        bbox_str = f"{west},{south},{east},{north}"

        # Step 1: 从完整 PBF 过滤 relation（使用 r/ 前缀）
        filtered_pbf = os.path.join(temp_dir, f"{base_name}_{tag_type}_full.pbf")
        filter_expr = self.WATER_RELATION_FILTERS.get(tag_type, '')

        if not filter_expr:
            logger.error(f"未知的 relation tag_type: {tag_type}")
            return False

        cmd1 = [
            osmium_path, 'tags-filter',
            pbf_file,
        ] + filter_expr.split() + [
            '-o', filtered_pbf,
            '--overwrite'
        ]

        logger.info(f"Step 1: {osmium_path} tags-filter {filter_expr} (on full PBF)")
        print(f"  [Step 1/3] osmium tags-filter (filtering {tag_type} from full PBF)...")
        print(f"           Command: osmium tags-filter {pbf_file} {filter_expr} -o {filtered_pbf} --overwrite")
        t1 = time.time()
        result1 = subprocess.run(cmd1, capture_output=True, timeout=120, creationflags=flags)
        elapsed1 = time.time() - t1

        if result1.returncode != 0:
            stderr_msg = result1.stderr.decode('utf-8', errors='replace')
            logger.error(f"osmium tags-filter 失败: {stderr_msg}")
            print(f"           FAILED: {stderr_msg}")
            return False

        filtered_size = os.path.getsize(filtered_pbf) / 1024 if os.path.exists(filtered_pbf) else 0
        print(f"           Done in {elapsed1:.1f}s, output: {filtered_size:.1f} KB")

        if filtered_size == 0:
            print(f"           WARNING: Filtered PBF is empty - no {tag_type} relations found!")
            print(f"           Possible causes:")
            print(f"             - No OSM relations match the filter in this region")
            print(f"           Keeping temp file for debugging: {filtered_pbf}")
            return False

        # Step 2: 导出为 GeoJSON（保持完整 relation 结构）
        full_geojson = os.path.join(temp_dir, f"{base_name}_{tag_type}_full.geojson")

        cmd2 = [
            osmium_path, 'export',
            filtered_pbf,
            '-o', full_geojson,
            '-f', 'geojson',
            '--overwrite'
        ]

        logger.info(f"Step 2: {osmium_path} export -f geojson")
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
