"""读取 PBF 文件获取 OSM 数据

作为 Overpass API 的替代方案，从本地 PBF 文件提取地理数据。
PBF 文件来源：https://download.geofabrik.de/

水体处理策略（同 right_xihu.py）：
- 使用 area() + WKBFactory 只提取多边形
- 不使用 way()/relation() 处理水体，避免 LineString buffer 导致宽度不准确
"""

import logging
import os
from typing import Dict, Optional, Tuple

import geopandas as gpd
import pandas as pd
import shapely.wkb as wkblib
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
            'water': {'Polygon', 'MultiPolygon'},  # 水体只提取多边形（保留原始宽度）
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
        
        # 对于水体类型，使用 area-only 策略（同 right_xihu.py）
        use_area_only = (tag_type == 'water')
        
        # 创建处理器并提取
        handler = OSMFeatureHandler(
            tag_filter=tag_filters[tag_type],
            valid_types=valid_geom_types.get(tag_type),
            bbox=bbox,
            use_area_only=use_area_only,
        )
        
        try:
            handler.apply_file(pbf_file, locations=True)
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
    """OSM 要素处理器（继承 osmium.SimpleHandler）
    
    水体处理使用 area() + WKBFactory（同 right_xihu.py）：
    - 只处理 area，获得多边形（保留河流原始宽度）
    - 不使用 way()/relation() 处理水体，避免 LineString buffer
    """
    
    def __init__(
        self,
        tag_filter: Dict,
        valid_types: Optional[set] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        use_area_only: bool = False,
    ):
        super().__init__()
        self.tag_filter = tag_filter
        self.valid_types = valid_types
        self.bbox = bbox
        self.use_area_only = use_area_only
        self.nodes = {}  # node_id -> (lat, lon)
        self.ways = {}  # way_id -> {'nodes': [...], 'tags': {...}}
        self.features = []
        
        # WKB factory for area processing (同 right_xihu.py)
        if use_area_only and OSMIUM_AVAILABLE:
            self.wkb_factory = osmium.geom.WKBFactory()
        else:
            self.wkb_factory = None
    
    def node(self, node):
        """存储节点坐标（用于非水体类型的 way 处理）"""
        if node.location.valid():
            self.nodes[node.id] = (node.location.lat, node.location.lon)
    
    def area(self, a):
        """处理 area（封闭的 way 或 relation）
        
        使用 WKBFactory 将 OSM area 转换为 Shapely 多边形，
        保留河流等水体的原始宽度（同 right_xihu.py 逻辑）。
        """
        if not self.use_area_only or self.wkb_factory is None:
            return
        
        # 获取标签
        tags = {}
        try:
            for tag in a.tags:
                tags[tag.k] = tag.v
        except Exception:
            pass
        
        # 检查标签是否匹配
        if not self._matches_tag_filter(tags):
            return
        
        # 转换几何体（使用 WKBFactory，同 right_xihu.py）
        try:
            wkb = self.wkb_factory.create_multipolygon(a)
            geom = wkblib.loads(wkb, hex=True)
            
            # BBOX 过滤（检查几何是否与边界框相交）
            if self.bbox:
                south, west, north, east = self.bbox
                bounds = geom.bounds
                if not (bounds[0] <= east and bounds[2] >= west and
                        bounds[1] <= north and bounds[3] >= south):
                    return
            
            # 检查几何类型
            if self.valid_types and geom.geom_type not in self.valid_types:
                return
            
            # 跳过空几何
            if geom.is_empty:
                return
            
            # 创建要素
            osm_id = a.orig_id()
            feature = dict(tags)
            feature['geometry'] = geom
            feature['osm_id'] = osm_id
            self.features.append(feature)
            
        except Exception as e:
            logger.debug(f"area 处理失败 (id={a.orig_id()}): {e}")
    
    def way(self, way):
        """处理路径（道路、建筑轮廓等，水体不使用此方法）"""
        # 如果只使用 area（水体），跳过 way 处理
        if self.use_area_only:
            return
        
        # 非水体类型：使用原有的 way 处理逻辑
        coords = []
        for n in way.nodes:
            if n.location.valid():
                coords.append((n.lon, n.lat))
        
        if len(coords) >= 2:
            self.ways[way.id] = {
                'nodes': coords,
                'tags': {tag.k: tag.v for tag in way.tags}
            }
            
        if not self._matches_tag_filter({tag.k: tag.v for tag in way.tags}):
            return
            
        if len(coords) < 2:
            return
        
        geometry = self._create_geometry(coords)
        if geometry is None:
            return
        
        if self.bbox:
            south, west, north, east = self.bbox
            bbox_poly = box(west, south, east, north)
            try:
                geometry = geometry.intersection(bbox_poly)
                if geometry.is_empty:
                    return
            except Exception:
                return
        
        if self.valid_types and geometry.geom_type not in self.valid_types:
            return
        
        feature = {tag.k: tag.v for tag in way.tags}
        feature['geometry'] = geometry
        feature['osm_id'] = way.id
        self.features.append(feature)
    
    def relation(self, rel):
        """处理关系（multipolygon 等，水体不使用此方法）"""
        # 如果只使用 area（水体），跳过 relation 处理
        if self.use_area_only:
            return
        
        tags = {tag.k: tag.v for tag in rel.tags}
        
        if tags.get('type') != 'multipolygon':
            return
        
        if not self._matches_tag_filter(tags):
            return
        
        outer_ways = []
        inner_ways = []
        
        for member in rel.members:
            if member.type == 'w' and member.ref in self.ways:
                way_data = self.ways[member.ref]
                if member.role == 'outer':
                    outer_ways.append(way_data['nodes'])
                elif member.role == 'inner':
                    inner_ways.append(way_data['nodes'])
        
        if not outer_ways:
            return
        
        try:
            polygons = []
            for outer_coords in outer_ways:
                if len(outer_coords) < 4:
                    continue
                coords_list = list(outer_coords)
                if coords_list[0] != coords_list[-1]:
                    coords_list.append(coords_list[0])
                outer_polygon = Polygon(coords_list)
                if not outer_polygon.is_valid:
                    outer_polygon = outer_polygon.buffer(0)
                polygons.append(outer_polygon)
            
            if not polygons:
                return
            
            if len(polygons) == 1:
                result_polygon = polygons[0]
            else:
                result_polygon = unary_union(polygons)
            
            if inner_ways and result_polygon.geom_type in ['Polygon', 'MultiPolygon']:
                inner_polygons = []
                for inner_coords in inner_ways:
                    coords_list = list(inner_coords)
                    if len(coords_list) < 4:
                        continue
                    if coords_list[0] != coords_list[-1]:
                        coords_list.append(coords_list[0])
                    inner_polygon = Polygon(coords_list)
                    if inner_polygon.is_valid:
                        inner_polygons.append(inner_polygon)
                
                if inner_polygons:
                    inner_union = unary_union(inner_polygons)
                    result_polygon = result_polygon.difference(inner_union)
            
            if self.bbox:
                south, west, north, east = self.bbox
                bbox_poly = box(west, south, east, north)
                result_polygon = result_polygon.intersection(bbox_poly)
                if result_polygon.is_empty:
                    return
            
            if self.valid_types and result_polygon.geom_type not in self.valid_types:
                return
            
            feature = tags
            feature['geometry'] = result_polygon
            feature['osm_id'] = rel.id
            self.features.append(feature)
            
        except Exception as e:
            logger.debug(f"处理 relation 失败: {e}")
    
    def _matches_tag_filter(self, tags: Dict) -> bool:
        """检查标签是否匹配过滤器"""
        for key, value in self.tag_filter.items():
            if key not in tags:
                continue
            if value is True:
                return True
            if isinstance(value, (list, set, tuple)):
                if tags[key] in value:
                    return True
            elif tags[key] == value:
                return True
        return False
    
    def _create_geometry(self, coords):
        """根据坐标创建几何对象"""
        if len(coords) < 2:
            return None
        
        if len(coords) >= 4 and coords[0] == coords[-1]:
            try:
                polygon = Polygon(coords)
                if polygon.is_valid:
                    return polygon
                else:
                    logger.debug(f"创建的多边形无效，尝试修复...")
                    return polygon.buffer(0)
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
