详细拆解了参考模型的设计，我有以下发现：

一个模型的数据来源于：水体、建筑、道路、植被，其中非常关键的是植被，这一点之前都被忽略了。

参考模型位置：
- 芝加哥：/Users/zhangqiwei/Desktop/city_demo/芝加哥/芝加哥25Km城市肌理P.3mf
- 杭州：/Users/zhangqiwei/Desktop/city_demo/杭州/杭州25Km城市肌理P.3mf

## 模型分解--芝加哥
## 固定比例尺设计

无论城市实际范围多大，生成的 3D 打印模型始终固定为 **196mm × 196mm**（内部跨度），以适配 Bambu Lab 200mm 打印平台（留 2mm 边距）。

### 比例尺计算

比例尺由 `config.py` 中的 `compute_scale()` 函数计算：

```python
INTERNAL_SPAN_MM = 196.0       # 模型 XY 内部跨度（固定值）
scale = 196.0 / max(width_m, height_m)
```

其中 `width_m` 和 `height_m` 是城市在 UTM 坐标系下的实际宽度和高度（单位：米）。

### 典型比例

| 城市范围 | 实际大小 | 比例尺 (mm/m) | 模型大小 |
|---------|---------|--------------|---------|
| 25km × 25km | 625 km² | 0.00784 | 196mm × 196mm |
| 10km × 10km | 100 km² | 0.01960 | 196mm × 196mm |
| 5km × 5km | 25 km² | 0.03920 | 196mm × 196mm |

### 影响范围

该比例尺影响所有几何组件的构建：
- **地形**：高程缩放、网格坐标转换
- **建筑**：实际高度（米）→ 模型高度（毫米）
- **道路**：实际宽度（米）→ 模型宽度（毫米）
- **水系**：水体范围缩放
- **植被**：绿地范围缩放

所有组件共享同一个 `scale` 因子，确保模型内各元素的相对比例与真实世界一致。比例尺在流水线 Stage 0 计算完成后，传入后续所有 Stage 使用。
<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777951240956-1b350ff7-00c0-4c1d-98a3-311a4169843d.png" width="1090" title="" crop="0,0,1,1" id="u875bb56a" class="ne-image">

模型的各个对象（从左往右）：

### 图一：完整模型
完整模型是各个组件按Z轴装配之后的结果，也就是完整模型本身。



### 图二：植被
<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777951709001-438f5b6e-a45d-4d14-b4d0-f7dae060076e.png" width="1410" title="" crop="0,0,1,1" id="uc9d73df3" class="ne-image">

右下角的圆圈内明显是公园，也就是地图的这一部分：

<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777951961339-3041d85d-a31a-4cb8-9efb-adf030488303.png" width="2208" title="" crop="0,0,1,1" id="uffec288c" class="ne-image">

还需要注意的是，植被模型似乎单独挖空了河流的部分（植被截图左边的红圈和下方真实地图的红圈），也可能是植被数据天然就空出了河流的部分，要结合数据做判断。

<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777952153266-4bf0be72-ed72-47b9-9786-1e6e8cca4ee2.png" width="1364" title="" crop="0,0,1,1" id="u92930ead" class="ne-image">



### 图三：底板+水体雕刻（河流等是突出来的）
<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777952246325-8a52515e-0d2f-49c2-be54-5fefc256a3a0.png" width="1066" title="" crop="0,0,1,1" id="u7f8bdffd" class="ne-image">

此图可见，芝加哥的底板上，将河流部分单独雕刻了出来，可以和地形上的镂空完美结合在一起。

### 图四：地形+道路+水体镂空
<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777952712899-6d49b807-95dc-45c6-8cdd-6f3eec0bba63.png" width="764" title="" crop="0,0,1,1" id="u756f531f" class="ne-image">

这个对象看似简单，实则暗藏玄机，首先是地形的数据，上面可以看到起伏，虽然芝加哥比较平坦，但还是能看到高低的差异。其次，河流的部分被镂空了，可以跟对象三：底板+水体雕刻，完美嵌合，这里要考虑下工艺是怎么实现的，因为会涉及到z轴高度的问题，是否是先做成地形图，再切割出水体部分，水体的部分再嵌在底板上（并用同一种颜色），需要数据验证。



### 图五：建筑物
<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777954352850-ad97023c-d611-4476-92bb-2f5de8a43cd2.png" width="715" title="" crop="0,0,1,1" id="u9947fe25" class="ne-image">

建筑物，osm数据的渲染，核心的一点是跟基础底板的z轴关系，我初步看下来，似乎是以底板基础高度为0，嵌在了地形里？这一点需要数据分析。





## 模型分解--杭州
<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777951275502-1006e9c0-cb4d-4048-bdb1-4fa2bfb3cd48.png" width="1148" title="" crop="0,0,1,1" id="udbd4a60e" class="ne-image">

### 图一：完整模型
完整模型是各个组件按Z轴装配之后的结果，也就是完整模型本身。



### 图二：植被
这是模型的植被部分：

<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777953499945-3d74a8f5-50be-4937-a488-a8acaf4ee680.png" width="630" title="" crop="0,0,1,1" id="ud3f425a5" class="ne-image">

这是杭州的卫星图，红圈的部分是植被，可见还是能跟模型对得上的，除了西溪湿地，西溪湿地作者做了加强展示（在地形中用灰色表现了）：

<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777953588436-0b86255e-d8bb-4e94-9c6f-9a354723809b.png" width="1240" title="" crop="0,0,1,1" id="uac0eca3d" class="ne-image">



### 图三：底板+水体雕刻（河流等是突出来的）
<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777953831755-5093be5e-48a6-4ecf-ab81-aab3f4609f90.png" width="784" title="" crop="0,0,1,1" id="ucdb8a32e" class="ne-image">

可以看到图中，钱塘江、西湖 和其他水体是高于底板的，被雕刻了出来。这部分可以完美跟地形部分镶嵌。



### 图四：地形+道路+水体镂空
<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777953981650-ff2c74c0-f236-4b4c-9c41-8ae344480f1b.png" width="1380" title="" crop="0,0,1,1" id="uda735078" class="ne-image">

暗藏玄机的对象四，地形+道路+水体镂空：

1. 首先是地形的数据，很明显看到两座山体，这应该就是高程数据的渲染
2. 其次是水体镂空部分，跟对象三：底板+水体雕刻，可以完美契合，这里同样需要数据支撑，看看底板+水体的高度部分是怎么做的。
3. 最重要的玄机是图中的红圈部分，这里可以看到是钱塘江上的几座桥，似乎道路数据融入了地形，这是如何实现的？需要数据支撑，似乎道路数据并没有被刻意强调，只是融入了灰色的对象四中。



### 图五：建筑
<img src="https://cdn.nlark.com/yuque/0/2026/png/529106/1777954274388-a820e75f-ade8-40f2-a195-edda6e50c26d.png" width="711" title="" crop="0,0,1,1" id="u79eebbf2" class="ne-image">

建筑部分似乎就是osm的渲染，核心的一点是跟基础底板的z轴关系，这里需要进行数据分析来确认工艺，跟芝加哥类似的问题。





## 解决思路：
1. 先用实际数据，回答上面几个分析的几个问题。
2. 增加植被部分的数据获取。
3. 将各个部分，渲染出单独的模型，检验是否符合预期，特别是：
    1. 对象三：
        1. 水体雕刻的部分如何搞定？高度如何确定？
        2. 对象三：水体明显对小面积的部分进行了过滤，我们也要一个根据面积过滤掉小水体的filter，需要确定阈值。
    2. 对象四：
        1. 高程数据、镂空水体、融合道路, 镂空时如何封闭mesh? 
        2. 道路融合时如何做到尽可能开销低？是否只做水面部分？
4. 每个对象要单独能生成3mf文件，以便确认是否符合预期
5. 最后考虑，如何进行四个五个对象的装配。



## 环境配置

**Python版本**: 3.9+（推荐 3.10/3.11）

**依赖文件**: `requirements.txt`

**激活虚拟环境**:
```bash
cd /Users/zhangqiwei/Desktop/map_generator_final
source venv/bin/activate
```

**退出虚拟环境**:
```bash
deactivate
```

**核心依赖**: numpy, trimesh, shapely, geopandas, osmnx, pyproj, scipy, rich



