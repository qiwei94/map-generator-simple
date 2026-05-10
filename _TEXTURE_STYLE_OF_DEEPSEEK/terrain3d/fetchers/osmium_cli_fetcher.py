"""使用 osmium CLI 获取 OSM 数据

高性能方案：使用 osmium 命令行工具，速度提升 10-20倍。

流程：
1. osmium extract - 区域裁剪
2. osmium tags-filter - 标签过滤  
3. osmium export - 导出 GeoJSON
4. Python 读取 GeoJSON → GeoDataFrame
"""

import logging
import os
import subprocess
from typing import Dict, Optional

import geopandas as gpd

logger = logging.getLogger(__name__)

# 默认 PBF 文件目录
DEFAULT_PBF_DIR = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'pbf_cache')

# GeoJSON 缓存目录
DEFAULT_GEOJSON_CACHE = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'cache', 'geojson')


class OsmiumCLIFetcher:
    """使用 osmium CLI 获取 OSM 数据"""
    
    # 标签过滤表达式映射（使用 nwr = node/way/relation）
    TAG_FILTERS = {
        'building': 'nwr/building',
        'road': 'nwr/highway',
        'water': 'nwr/natural=water nwr/water=* nwr/waterway=* nwr/landuse=reservoir',
        'vegetation': 'nwr/landuse=forest,grass,meadow nwr/natural=wood,grassland,scrub',
        'park': 'nwr/leisure=park,garden nwr/landuse=recreation_ground',
        'wetland': 'nwr/natural=wetland,marsh,swamp',
    }
    
    def __init__(self, pbf_dir: str = None, cache_dir: str = None):
        """
        Args:
            pbf_dir: PBF 文件存放目录
            cache_dir: GeoJSON 缓存目录
        """
        self.pbf_dir = pbf_dir or DEFAULT_PBF_DIR
        self.cache_dir = cache_dir or DEFAULT_GEOJSON_CACHE
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # 查找 conda 环境中的工具路径
        self.conda_env_path = self._find_conda_env_path()
        
        # 检查 osmium 是否可用
        self.osmium_available = self._check_tool('osmium')
        
        if not self.osmium_available:
            logger.warning("osmium CLI 未安装。安装: conda install -c conda-forge osmium-tool")
    
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
    
    def _get_cache_path(self, tag_type: str, south: float, west: float, 
                        north: float, east: float) -> str:
        """生成缓存文件路径"""
        cache_key = f"{tag_type}_{south:.2f}_{west:.2f}_{north:.2f}_{east:.2f}"
        return os.path.join(self.cache_dir, f"{cache_key}.geojson")
    
    def fetch_features(
        self,
        tag_type: str,
        south: float,
        west: float,
        north: float,
        east: float,
        pbf_file: str = None,
        region: str = None,
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> gpd.GeoDataFrame:
        """使用 CLI 工具获取 OSM 数据
        
        Args:
            tag_type: 数据类型 ('building', 'road', 'water', 'vegetation', 'park', 'wetland')
            south, west, north, east: 边界框 (WGS84)
            pbf_file: 直接指定 PBF 文件路径
            region: 区域名称 (如 'zhejiang', 'china')
            use_cache: 是否使用缓存
            force_refresh: 强制刷新缓存
        
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
        
        cache_path = self._get_cache_path(tag_type, south, west, north, east)
        
        if use_cache and not force_refresh and os.path.exists(cache_path):
            logger.info(f"使用缓存: {cache_path}")
            return gpd.read_file(cache_path)
        
        logger.info(f"使用 CLI 方式获取 {tag_type} 数据...")
        logger.info(f"边界框: ({south:.4f}, {west:.4f}, {north:.4f}, {east:.4f})")
        print(f"\n  [CLI Pipeline] Starting {tag_type} data extraction...")
        print(f"  Bounding box: ({south:.4f}, {west:.4f}, {north:.4f}, {east:.4f})")
        
        try:
            result = self._run_osmium_pipeline(
                pbf_file, tag_type, south, west, north, east, cache_path
            )
            if result:
                gdf = gpd.read_file(cache_path)
                logger.info(f"CLI 管线完成: {len(gdf)} 条记录")
                print(f"  [CLI Pipeline] Complete: {len(gdf)} features extracted\n")
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
        """执行 osmium 三步管线
        
        Step 1: osmium extract - 区域裁剪
        Step 2: osmium tags-filter - 标签过滤
        Step 3: osmium export - 导出 GeoJSON
        
        Returns:
            True if success
        """
        import time
        
        temp_dir = os.path.join(self.cache_dir, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        
        base_name = os.path.splitext(os.path.basename(pbf_file))[0]
        
        if os.name == 'nt':
            flags = subprocess.CREATE_NO_WINDOW
        else:
            flags = 0
        
        osmium_path = self._get_tool_path('osmium')
        
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
        
        # Check if filtered file has any data
        if filtered_size == 0:
            print(f"           WARNING: Filtered PBF is empty - no {tag_type} features found!")
            print(f"           Possible causes:")
            print(f"             - No OSM data in this area matches the filter")
            print(f"             - Tag filter expression may be too strict")
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
        
        # 清理临时文件（仅成功时清理）
        try:
            os.remove(area_pbf)
            os.remove(filtered_pbf)
        except:
            pass
        
        total_time = elapsed1 + elapsed2 + elapsed3
        logger.info(f"CLI 管道完成: {output_path}")
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


def get_cli_fetcher(pbf_dir: str = None, cache_dir: str = None) -> OsmiumCLIFetcher:
    """获取全局 CLI fetcher 实例"""
    global _cli_fetcher
    if _cli_fetcher is None:
        _cli_fetcher = OsmiumCLIFetcher(pbf_dir, cache_dir)
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
