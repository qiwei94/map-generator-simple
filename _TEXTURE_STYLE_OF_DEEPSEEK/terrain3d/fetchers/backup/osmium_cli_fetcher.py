"""使用 osmium CLI + ogr2ogr 获取 OSM 数据

高性能方案：使用命令行工具替代 Python osmnx，速度提升 5-10倍。

流程：
1. osmium extract - 区域裁剪
2. osmium tags-filter - 标签过滤  
3. ogr2ogr - 格式转换 GeoJSON
4. Python 读取 GeoJSON
"""

import logging
import os
import subprocess
import json
import shutil
from typing import Dict, Optional, Tuple

import geopandas as gpd

logger = logging.getLogger(__name__)

# 默认 PBF 文件目录
DEFAULT_PBF_DIR = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'pbf_cache')

# GeoJSON 缓存目录
DEFAULT_GEOJSON_CACHE = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'cache', 'geojson')


class OsmiumCLIFetcher:
    """使用 osmium CLI + ogr2ogr 获取 OSM 数据"""
    
    # 标签过滤表达式映射
    TAG_FILTERS = {
        'building': 'a/building',
        'road': 'w/highway',
        'water': 'a/natural=water wr/waterway a/water a/landuse=reservoir',
        'vegetation': 'a/landuse=forest,grass,meadow a/natural=wood,grassland,scrub',
        'park': 'a/leisure=park,garden a/landuse=recreation_ground',
        'wetland': 'a/natural=wetland,marsh,swamp',
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
        
        # 检查工具是否可用
        self.osmium_available = self._check_tool('osmium')
        self.ogr2ogr_available = self._check_tool('ogr2ogr')
        
        if not self.osmium_available:
            logger.warning("osmium CLI 未安装。安装: conda install -c conda-forge osmium-tool")
        if not self.ogr2ogr_available:
            logger.warning("ogr2ogr 未安装。安装: conda install -c conda-forge gdal")
    
    def _check_tool(self, tool_name: str) -> bool:
        """检查命令行工具是否可用"""
        try:
            result = subprocess.run([tool_name, '--version'], 
                                    capture_output=True, timeout=5)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    
    def _find_pbf_file(self, region: str) -> Optional[str]:
        """查找区域对应的 PBF 文件"""
        # 常见文件名模式
        patterns = [
            f"{region}-latest.osm.pbf",
            f"{region}.osm.pbf",
            f"{region}_latest.osm.pbf",
        ]
        
        for pattern in patterns:
            path = os.path.join(self.pbf_dir, pattern)
            if os.path.exists(path):
                return path
        
        # 搜索目录
        for f in os.listdir(self.pbf_dir):
            if f.endswith('.pbf') and region.lower() in f.lower():
                return os.path.join(self.pbf_dir, f)
        
        return None
    
    def _get_cache_path(self, tag_type: str, south: float, west: float, 
                        north: float, east: float) -> str:
        """生成缓存文件路径"""
        # 使用坐标作为缓存键
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
        if not self.osmium_available or not self.ogr2ogr_available:
            logger.error("osmium 或 ogr2ogr 未安装，无法使用 CLI 方式")
            return gpd.GeoDataFrame()
        
        # 查找 PBF 文件
        if pbf_file is None:
            if region:
                pbf_file = self._find_pbf_file(region)
            else:
                logger.error("必须指定 pbf_file 或 region")
                return gpd.GeoDataFrame()
        
        if pbf_file is None or not os.path.exists(pbf_file):
            logger.error(f"PBF 文件不存在: {pbf_file}")
            return gpd.GeoDataFrame()
        
        # 检查缓存
        cache_path = self._get_cache_path(tag_type, south, west, north, east)
        
        if use_cache and not force_refresh and os.path.exists(cache_path):
            logger.info(f"使用缓存: {cache_path}")
            return gpd.read_file(cache_path)
        
        # 执行 CLI 命令
        logger.info(f"使用 CLI 方式获取 {tag_type} 数据...")
        logger.info(f"边界框: ({south:.4f}, {west:.4f}, {north:.4f}, {east:.4f})")
        
        try:
            result = self._run_osmium_pipeline(
                pbf_file, tag_type, south, west, north, east, cache_path
            )
            if result:
                return gpd.read_file(cache_path)
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
        """执行 osmium + ogr2ogr 管道
        
        Returns:
            True if success
        """
        # 创建临时目录
        temp_dir = os.path.join(self.cache_dir, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        
        base_name = os.path.splitext(os.path.basename(pbf_file))[0]
        
        # Step 1: 区域裁剪
        area_pbf = os.path.join(temp_dir, f"{base_name}_area.pbf")
        bbox_str = f"{west},{south},{east},{north}"
        
        cmd1 = [
            'osmium', 'extract',
            '-b', bbox_str,
            '--strategy', 'complete_ways',
            pbf_file,
            '-o', area_pbf
        ]
        
        logger.info(f"Step 1: osmium extract -b {bbox_str}")
        result1 = subprocess.run(cmd1, capture_output=True, timeout=60)
        
        if result1.returncode != 0:
            logger.error(f"osmium extract 失败: {result1.stderr.decode()}")
            return False
        
        # Step 2: 标签过滤
        filtered_pbf = os.path.join(temp_dir, f"{base_name}_{tag_type}.pbf")
        filter_expr = self.TAG_FILTERS.get(tag_type, '')
        
        if not filter_expr:
            logger.error(f"未知的 tag_type: {tag_type}")
            return False
        
        cmd2 = [
            'osmium', 'tags-filter',
            area_pbf,
        ] + filter_expr.split() + [
            '-o', filtered_pbf
        ]
        
        logger.info(f"Step 2: osmium tags-filter {filter_expr}")
        result2 = subprocess.run(cmd2, capture_output=True, timeout=60)
        
        if result2.returncode != 0:
            logger.error(f"osmium tags-filter 失败: {result2.stderr.decode()}")
            return False
        
        # Step 3: 格式转换
        cmd3 = [
            'ogr2ogr',
            '-f', 'GeoJSON',
            output_path,
            filtered_pbf
        ]
        
        logger.info(f"Step 3: ogr2ogr -f GeoJSON")
        result3 = subprocess.run(cmd3, capture_output=True, timeout=120)
        
        if result3.returncode != 0:
            logger.error(f"ogr2ogr 失败: {result3.stderr.decode()}")
            return False
        
        # 清理临时文件
        try:
            os.remove(area_pbf)
            os.remove(filtered_pbf)
        except:
            pass
        
        logger.info(f"CLI 管道完成: {output_path}")
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