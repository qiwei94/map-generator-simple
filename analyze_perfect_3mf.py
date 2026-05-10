"""分析5月5日生成的完美西湖3MF文件"""

import sys
import os

sys.path.insert(0, '.')

try:
    import trimesh
    
    file_path = "output/water_only/hangzhou_west_lake_water.3mf"
    
    print("=" * 70)
    print("分析5月5日生成的西湖水体文件")
    print("=" * 70)
    
    if not os.path.exists(file_path):
        print(f"文件不存在: {file_path}")
        sys.exit(1)
    
    file_size = os.path.getsize(file_path) / (1024 * 1024)
    print(f"\n文件: {file_path}")
    print(f"大小: {file_size:.2f} MB")
    
    # 加载 3MF 文件
    print(f"\n加载 3MF 文件...")
    scene = trimesh.load(file_path)
    
    print(f"场景包含: {len(scene.geometry)} 个几何体")
    
    for name, mesh in scene.geometry.items():
        print(f"\n几何体 '{name}':")
        print(f"  顶点数: {len(mesh.vertices)}")
        print(f"  面数: {len(mesh.faces)}")
        print(f"  是否水密: {mesh.is_watertight}")
        print(f"  体积: {mesh.volume:.2f} mm³")
        
        # 分析 Z 轴范围
        z_min = mesh.vertices[:, 2].min()
        z_max = mesh.vertices[:, 2].max()
        print(f"  Z 范围: {z_min:.2f} ~ {z_max:.2f} mm")
        print(f"  高度: {z_max - z_min:.2f} mm")
        
        # 分析 XY 范围
        x_min = mesh.vertices[:, 0].min()
        x_max = mesh.vertices[:, 0].max()
        y_min = mesh.vertices[:, 1].min()
        y_max = mesh.vertices[:, 1].max()
        print(f"  X 范围: {x_min:.2f} ~ {x_max:.2f} mm")
        print(f"  Y 范围: {y_min:.2f} ~ {y_max:.2f} mm")
        print(f"  宽度: {x_max - x_min:.2f} mm")
        print(f"  高度: {y_max - y_min:.2f} mm")
    
    print("\n" + "=" * 70)
    print("分析完成")
    print("=" * 70)
    
except Exception as e:
    print(f"错误: {e}")
    import traceback
    traceback.print_exc()
