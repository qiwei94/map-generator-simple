# Geofabrik 数据源替代方案

## 背景问题

### 当前痛点
- Overpass API 在远程服务器（特别是中国大陆）访问不稳定或完全不可用
- 新地域的数据无法通过 Overpass API 下载
- 需要稳定的离线数据源支持地图生成项目

### 解决方案
使用 Geofabrik 提供的 OSM 数据提取文件作为数据源，替代或补充 Overpass API。

---

## Geofabrik 简介

### 什么是 Geofabrik？
- 德国公司，提供免费 OSM 数据下载服务
- 将全球数据按大洲、国家、省份层级分割
- **每日更新**

### 数据格式
- **PBF 格式**（推荐）：高效二进制压缩，文件小，处理快
- **GeoPackage 格式**：可直接在 GIS 软件中使用

### 数据内容
✅ **完整包含所有 OSM 数据**：
- 道路（highway=*）
- 建筑（building=*）
- 水体（natural=water, waterway=*）
- 绿地（landuse=forest/grass, natural=wood, leisure=park）
- POI、行政区划等所有标签

---

## 中国数据详情

### 数据层级与大小

| 层级 | 文件大小 | 示例 |
|------|---------|------|
| 全国 | 1.4 GB | china-latest.osm.pbf |
| 省份 | **2-156 MB** | zhejiang-latest.osm.pbf (84 MB) |

### 主要省份数据

| 省份 | 大小 | 说明 |
|------|------|------|
| **浙江** | **84 MB** | ✅ 数据丰富 |
| 江苏 | 72 MB | |
| 广东 | 156 MB | 含港澳 |
| 四川 | 97 MB | |
| 上海 | 24 MB | |
| 北京 | 34 MB | |

### 下载链接

**浙江省：**
```
https://download.geofabrik.de/asia/china/zhejiang-latest.osm.pbf
```

**其他省份：**
```
https://download.geofabrik.de/asia/china/[省份拼音]-latest.osm.pbf
```

---

## 核心工具

### osmium（命令行工具）

**安装：**
```bash
sudo apt-get install osmium-tool  # Linux
brew install osmium               # macOS
```

**常用操作：**

```bash
# 1. 查看文件信息
osmium file-info zhejiang-latest.osm.pbf

# 2. 提取特定区域（杭州）
osmium extract -b 120.0,30.0,120.5,30.5 \
  zhejiang-latest.osm.pbf -o hangzhou.osm.pbf

# 3. 按要素类型提取
osmium tags-filter zhejiang-latest.osm.pbf \
  wr/highway -o roads.osm.pbf
```

### pyosmium（Python 库）

```bash
pip install osmium
```

用于在 Python 代码中直接读取 PBF 文件。

---

## 项目集成思路

### 工作流程

```
1. 下载省份 PBF 文件（如浙江 84 MB）
   ↓
2. 使用 osmium 提取城市级别数据
   ↓
3. 代码通过 pyosmium 读取 PBF 文件
   ↓
4. 转换为 GeoDataFrame
   ↓
5. 输入到现有的地图生成管线
```

### 集成策略

**优先级顺序：**
1. **Tile Cache**（已有）- 最快
2. **City Cache**（已有）- 快
3. **PBF 本地文件**（新增）- 中等，无需网络
4. **Overpass API**（原有）- 最慢，可能不可用

### 核心改动点

1. **新增 PBF 读取模块**
   - 文件：`terrain3d/fetchers/pbf_reader.py`
   - 功能：从 PBF 文件提取道路、建筑、水体、绿地

2. **修改 osm.py**
   - 在 `_tile_cached_fetch()` 中添加 PBF 读取逻辑
   - 作为 Overpass API 失败后的备选方案

3. **配置管理**
   - 环境变量 `OSM_PBF_FILE` 指定 PBF 文件路径
   - 支持多个省份的 PBF 文件

### 代码改动范围

**最小改动：**
- 新增 1 个文件：`pbf_reader.py`（约 100 行）
- 修改 1 个文件：`osm.py`（增加 PBF 读取分支）
- 配置 1 个环境变量

**保持兼容：**
- 不改变现有缓存机制
- 不改变现有 API 接口
- 只是增加一个新的数据源

---

## 优势分析

### ✅ 优点
1. **稳定性**：无需网络，本地文件读取
2. **速度**：本地读取比 API 快得多
3. **完整性**：包含所有 OSM 数据类型
4. **更新灵活**：可随时下载最新数据
5. **成本低**：免费，无调用限制
6. **文件小**：浙江省仅 84 MB

### ⚠️ 注意事项
1. **手动更新**：需要定期下载新数据（建议每周/每月）
2. **初次设置**：需要下载和配置 PBF 文件
3. **内存使用**：处理大文件时需要足够内存（浙江省 84 MB → 处理时约 1-2 GB）

---

## 实施步骤

### Step 1: 下载数据
```bash
wget https://download.geofabrik.de/asia/china/zhejiang-latest.osm.pbf
```

### Step 2: 提取城市数据
```bash
osmium extract -b 120.0,30.0,120.5,30.5 \
  zhejiang-latest.osm.pbf -o hangzhou.osm.pbf
```

### Step 3: 安装依赖
```bash
pip install osmium
sudo apt-get install osmium-tool
```

### Step 4: 代码集成
- 创建 `pbf_reader.py`
- 修改 `osm.py` 添加 PBF 读取逻辑
- 配置环境变量

### Step 5: 测试验证
- 测试 PBF 读取功能
- 验证数据完整性
- 对比 Overpass API 结果

---

## 资源需求

### 存储
- 浙江省 PBF：84 MB
- 提取后城市数据：5-20 MB
- 总计：< 200 MB

### 内存
- PBF 文件读取：1-2 GB
- 正常地图生成：4-8 GB

### 网络
- 初始下载：84 MB（一次性）
- 后续更新：84 MB（每周/每月）

---

## 总结

**核心思路：** 使用 Geofabrik 的省份级别 PBF 文件（浙江 84 MB）作为离线数据源，通过 osmium 工具提取城市数据，在代码中读取 PBF 文件替代 Overpass API 调用。

**关键优势：** 稳定、快速、完整、免费、文件小。

**实施难度：** 低，只需少量代码改动，保持现有架构兼容。
