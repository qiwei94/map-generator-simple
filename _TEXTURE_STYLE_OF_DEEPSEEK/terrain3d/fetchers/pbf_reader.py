"""读取 PBF 文件获取 OSM 数据

作为 Overpass API 的替代方案，从本地 PBF 文件提取地理数据。
PBF 文件来源：https://download.geofabrik.de/
"""

import logging
import os
from typing import Dict, Optional, Tuple

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon, box
from shapely.ops import unary_union

try:
    import osmium
    OSMIUM_AVAILABLE = True
except ImportError:
    OSMIUM_AVAILABLE = False

logger = logging.getLogger(__name__)


class PBFFeatureExtractor:
    """PBF 文件要素提取器（使用 osmium）"""
    
    def __init__(self):
        self.available = OSMIUM_AVAILABLE
        if not self.available:
            logger.warning("osmium 未安装，PBF 读取功能不可用。运行: pip install osmium")
    
    def extract_features(
        self,
        pbf_file: str,
        tag_type: str,
        bbox: Tuple[float, float, float, float],
    ) -> gpd.GeoDataFrame:
        """从 PBF 文件提取指定类型的要素
        
        Args:
            pbf_file: PBF 文件路径
            tag_type: 要素类型 ('building', 'road', 'water', 'vegetation', 'park', 'wetland')
            bbox: (south, west, north, east) 边界框
        
        Returns:
            GeoDataFrame
        """
        if not self.available:
            return gpd.GeoDataFrame()
        
        if not os.path.exists(pbf_file):
            logger.error(f"PBF 文件不存在: {pbf_file}")
            return gpd.GeoDataFrame()
        
        # 标签过滤器映射
        tag_filters = {
            'building': {'building': True},
            'road': {'highway': True},
            'water': {
                'natural': 'water',
                'waterway': True,
                'landuse': 'reservoir',
                'water': True,
            },
            'vegetation': {
                'landuse': ['forest', 'grass', 'meadow', 'village_green'],
                'natural': ['wood', 'grassland', 'scrub', 'heath'],
            },
            'park': {
                'leisure': ['park', 'garden', 'nature_reserve'],
                'landuse': ['recreation_ground'],
            },
            'wetland': {
                'natural': ['wetland', 'marsh', 'swamp'],
            },
        }
        
        # 几何类型过滤
        valid_geom_types = {
            'building': {'Polygon', 'MultiPolygon'},
            'road': {'LineString', 'MultiLineString'},
            'water': {'Polygon', 'MultiPolygon', 'LineString', 'MultiLineString'},
            'vegetation': {'Polygon', 'MultiPolygon'},
            'park': {'Polygon', 'MultiPolygon'},
            'wetland': {'Polygon', 'MultiPolygon'},
        }
        
        if tag_type not in tag_filters:
            logger.warning(f"不支持的 tag_type: {tag_type}")
            return gpd.GeoDataFrame()
        
        south, west, north, east = bbox
        logger.info(f"从 PBF 文件提取 {tag_type} 数据: {os.path.basename(pbf_file)}")
        logger.info(f"边界框: ({south:.4f}, {west:.4f}, {north:.4f}, {east:.4f})")
        
        # 创建处理器并提取
        handler = OSMFeatureHandler(
            tag_filter=tag_filters[tag_type],
            valid_types=valid_geom_types.get(tag_type),
            bbox=bbox,
        )
        
        try:
            # 注意：使用locations=False，我们手动处理nodes
            # 这样可以确保所有nodes都被存储，不受bbox限制
            handler.apply_file(pbf_file, locations=False)
        except Exception as e:
            logger.error(f"读取 PBF 文件失败: {e}")
            import traceback
            traceback.print_exc()
            return gpd.GeoDataFrame()
        
        if not handler.features:
            logger.info(f"PBF 文件中没有找到 {tag_type} 数据")
            return gpd.GeoDataFrame()
        
        # 转换为 GeoDataFrame
        gdf = gpd.GeoDataFrame(handler.features, geometry='geometry', crs='EPSG:4326')
        
        logger.info(f"从 PBF 提取到 {len(gdf)} 个 {tag_type} 要素")
        return gdf


class OSMFeatureHandler(osmium.SimpleHandler):
    """OSM 要素处理器（继承 osmium.SimpleHandler）"""
    
    def __init__(
        self,
        tag_filter: Dict,
        valid_types: Optional[set] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
    ):
        super().__init__()
        self.tag_filter = tag_filter
        self.valid_types = valid_types
        self.bbox = bbox
        self.nodes = {}  # node_id -> (lat, lon)
        self.ways = {}  # way_id -> {'nodes': [...], 'tags': {...}}
        self.features = []
    
    def node(self, node):
        """存储节点坐标"""
        # 注意：不过滤bbox，因为way可能跨bbox边界
        # 需要在way处理时再过滤坐标
        self.nodes[node.id] = (node.lat, node.lon)
    
    def way(self, way):
        """处理路径（道路、建筑轮廓、水体等）"""
        # 存储way供 relation使用
        coords = []
        for node_ref in way.nodes:
            if node_ref.ref in self.nodes:
                lat, lon = self.nodes[node_ref.ref]
                coords.append((lon, lat))
            
        if len(coords) >= 2:
            self.ways[way.id] = {
                'nodes': coords,
                'tags': dict(way.tags)
            }
            
        # 检查标签是否匹配
        if not self._matches_tag_filter(dict(way.tags)):
            return
            
        if len(coords) < 2:
            return
            
        # 如果有bbox，裁剪坐标到bbox范围内
        if self.bbox:
            south, west, north, east = self.bbox
            coords = self._clip_coords_to_bbox(coords, south, west, north, east)
            
        if len(coords) < 2:
            return
        
        # 判断几何类型
        geometry = self._create_geometry(coords)
        if geometry is None:
            return
        
        # 检查几何类型是否符合要求
        if self.valid_types and geometry.geom_type not in self.valid_types:
            return
        
        # 创建要素
        feature = dict(way.tags)
        feature['geometry'] = geometry
        feature['osm_id'] = way.id
        self.features.append(feature)
    
    def _matches_tag_filter(self, tags: Dict) -> bool:
        """检查标签是否匹配过滤器"""
        for key, value in self.tag_filter.items():
            if key not in tags:
                continue
            if value is True:
                return True  # 只要标签存在即可
            if isinstance(value, (list, set, tuple)):
                if tags[key] in value:
                    return True
            elif tags[key] == value:
                return True
        return False
    
    def _clip_coords_to_bbox(self, coords, south, west, north, east):
        """将坐标列表裁剪到bbox范围内，保留在范围内的连续段"""
        if not coords:
            return []
        
        # 简单过滤：只保留在bbox内的点
        clipped = []
        for lon, lat in coords:
            if south <= lat <= north and west <= lon <= east:
                clipped.append((lon, lat))
        
        return clipped
    
    def _create_geometry(self, coords):
        """根据坐标创建几何对象"""
        if len(coords) < 2:
            return None
        
        # 判断是否为闭合路径（多边形）
        if len(coords) >= 4 and coords[0] == coords[-1]:
            try:
                polygon = Polygon(coords)
                # 验证几何对象是否有效
                if polygon.is_valid:
                    return polygon
                else:
                    # 无效多边形，尝试修复
                    logger.debug(f"创建的多边形无效，尝试修复...")
                    return polygon.buffer(0)  # buffer(0) 可以修复一些简单的拓扑错误
            except Exception as e:
                logger.debug(f"多边形创建失败，回退到线: {e}")
                return LineString(coords)
        else:
            try:
                line = LineString(coords)
                if line.is_valid:
                    return line
                else:
                    return None
            except Exception:
                return None
    
    def relation(self, rel):
        """处理关系（multipolygon等）"""
        # 只处理 multipolygon 类型
        if dict(rel.tags).get('type') != 'multipolygon':
            return
        
        # 检查标签是否匹配
        if not self._matches_tag_filter(dict(rel.tags)):
            return
        
        # 收集 outer 和 inner ways
        outer_ways = []
        inner_ways = []
        
        for member in rel.members:
            if member.type == 'way' and member.ref in self.ways:
                way_data = self.ways[member.ref]
                if member.role == 'outer':
                    outer_ways.append(way_data['nodes'])
                elif member.role == 'inner':
                    inner_ways.append(way_data['nodes'])
        
        if not outer_ways:
            return
        
        # 构建多边形
        try:
            polygons = []
            for outer_coords in outer_ways:
                if len(outer_coords) < 4:
                    continue
                
                # 确保闭合
                if outer_coords[0] != outer_coords[-1]:
                    outer_coords.append(outer_coords[0])
                
                outer_polygon = Polygon(outer_coords)
                if not outer_polygon.is_valid:
                    outer_polygon = outer_polygon.buffer(0)
                
                polygons.append(outer_polygon)
            
            if not polygons:
                return
            
            # 合并所有outer polygons
            if len(polygons) == 1:
                result_polygon = polygons[0]
            else:
                result_polygon = unary_union(polygons)
            
            # 添加inner holes
            if inner_ways and result_polygon.geom_type in ['Polygon', 'MultiPolygon']:
                inner_polygons = []
                for inner_coords in inner_ways:
                    if len(inner_coords) < 4:
                        continue
                    
                    if inner_coords[0] != inner_coords[-1]:
                        inner_coords.append(inner_coords[0])
                    
                    inner_polygon = Polygon(inner_coords)
                    if inner_polygon.is_valid:
                        inner_polygons.append(inner_polygon)
                
                if inner_polygons:
                    # 从outer中减去inner
                    inner_union = unary_union(inner_polygons)
                    result_polygon = result_polygon.difference(inner_union)
            
            # 检查几何类型
            if self.valid_types and result_polygon.geom_type not in self.valid_types:
                return
            
            # 创建要素
            feature = dict(rel.tags)
            feature['geometry'] = result_polygon
            feature['osm_id'] = rel.id
            self.features.append(feature)
            
        except Exception as e:
            logger.debug(f"处理relation失败: {e}")


# 全局提取器实例
_extractor = None


def get_extractor() -> PBFFeatureExtractor:
    """获取全局 PBF 提取器实例"""
    global _extractor
    if _extractor is None:
        _extractor = PBFFeatureExtractor()
    return _extractor


def fetch_from_pbf(
    pbf_file: str,
    tag_type: str,
    south: float,
    west: float,
    north: float,
    east: float,
) -> gpd.GeoDataFrame:
    """从 PBF 文件提取要素的便捷函数
    
    Args:
        pbf_file: PBF 文件路径
        tag_type: 要素类型
        south, west, north, east: 边界框
    
    Returns:
        GeoDataFrame
    """
    extractor = get_extractor()
    return extractor.extract_features(pbf_file, tag_type, (south, west, north, east))
