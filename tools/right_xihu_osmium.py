import subprocess
import os
import sys
import shutil

# === 配置 ===
pbf_file = r'C:\Users\kiwi\OneDrive\Desktop\pbf\zhejiang.osm.pbf'
output_dir = r'C:\Users\kiwi\OneDrive\Desktop\pbf'

# 西湖 25km 范围边界框
BBOX = "119.89,30.03,120.41,30.48"  # West,South,East,North

def find_osmium():
    """查找 osmium CLI 可执行文件"""
    # 先尝试直接调用（可能在 PATH 中）
    for cmd in ['osmium', 'osmium.exe']:
        if shutil.which(cmd):
            return cmd
    
    # 尝试从当前 Python 环境的 Scripts 目录查找
    python_scripts = os.path.join(sys.prefix, 'Scripts')
    if os.path.isdir(python_scripts):
        exe_path = os.path.join(python_scripts, 'osmium.exe')
        if os.path.isfile(exe_path):
            return exe_path
    
    # 尝试 conda 环境路径
    conda_prefix = os.environ.get('CONDA_PREFIX')
    if conda_prefix:
        exe_path = os.path.join(conda_prefix, 'bin', 'osmium')
        if os.path.isfile(exe_path):
            return exe_path
        exe_path = os.path.join(conda_prefix, 'Scripts', 'osmium.exe')
        if os.path.isfile(exe_path):
            return exe_path
    
    return None

def run_osmium_command(args, description=""):
    """运行 osmium 命令"""
    osmium_exe = find_osmium()
    if not osmium_exe:
        raise RuntimeError("osmium CLI not found. Install with: conda install -c conda-forge osmium-tools")
    
    cmd = [osmium_exe] + args
    print(f"Running: {' '.join(cmd)}")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        raise RuntimeError(f"osmium command failed: {description}")
    
    if result.stdout:
        print(result.stdout)
    
    return result

def main():
    print("=" * 60)
    print("Extracting water features using osmium CLI...")
    print(f"BBOX: {BBOX}")
    print("=" * 60)
    
    # Step 1: 按边界框裁剪区域
    area_file = os.path.join(output_dir, 'westlake_area.osm.pbf')
    print(f"\n[Step 1] Extracting area by bbox...")
    run_osmium_command([
        'extract',
        '-b', BBOX,
        pbf_file,
        '-o', area_file
    ], "Extract area by bbox")
    
    file_size = os.path.getsize(area_file)
    print(f"Area file size: {file_size / 1024:.1f} KB")
    
    # Step 2: 过滤水体标签
    water_file = os.path.join(output_dir, 'cli_westlake_water.osm.pbf')
    print(f"\n[Step 2] Filtering water features...")
    run_osmium_command([
        'tags-filter',
        area_file,
        'nwr', 'natural=water',
        'nwr', 'water=*',
        'nwr', 'waterway=*',
        'nwr', 'landuse=reservoir',
        '-o', water_file
    ], "Filter water tags")
    
    file_size = os.path.getsize(water_file)
    print(f"Water file size: {file_size / 1024:.1f} KB")
    
    # Step 3: 导出为 GeoJSON
    geojson_file = os.path.join(output_dir, 'cli_westlake_water_25km.geojson')
    print(f"\n[Step 3] Exporting to GeoJSON...")
    run_osmium_command([
        'export',
        water_file,
        '-o', geojson_file
    ], "Export to GeoJSON")
    
    file_size = os.path.getsize(geojson_file)
    print(f"\nSaved to: {geojson_file}")
    print(f"File size: {file_size / 1024:.1f} KB ({file_size / 1024 / 1024:.2f} MB)")
    
    # 清理临时文件（可选）
    print(f"\n[Cleanup] Removing temporary files...")
    for tmp in [area_file, water_file]:
        if os.path.exists(tmp):
            os.remove(tmp)
            print(f"Removed: {tmp}")
    
    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)

if __name__ == '__main__':
    main()
