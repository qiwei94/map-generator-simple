# 旅程回忆 · Journey Relief Studio

> 把一段旅程，凝固成一块独一无二的 3D 浮雕。

上传旅行照片 → 自动还原轨迹与停留点 → 挑选风格 → 生成可 3D 打印的城市浮雕模型。
不是地图生成器，而是**回忆的实体化**：聊天式挖掘经历，3D 实体承载情绪，每一件都只属于它的主人。

---

## 产品理念

- **聊天式入口**：回忆藏在脑子里，只有对话能低成本挖出来。系统只在「照片里没有的信息」上追问（在哪拍的？中间去了哪？这个停留点叫什么？），不替用户抒情。
- **克制的文案**：地名、日期、坐标就是全部，留白交给记忆。模型上不写一个形容词。
- **3D 实体**：数字回忆看完即忘，实体浮雕占据物理空间、可赠予、不可复制。
- **隐私优先**：照片仅用于读取 GPS 坐标标记地点，**不以任何形式落盘或上传第三方**，解析完即从内存丢弃。

---

## 核心能力

### 选位置（Step 1）
- **照片定位**：单张读 EXIF GPS 落点；多张自动聚类成旅程轨迹（时序贪心 + 异常点过滤）。
- **缺口追问**：照片无 GPS / 时间轴空档 / 停留点未命名时，inline 追问，**带相关照片缩略图**（可点开放大图）。
- **批量补名**：支持「第一张在鱼尾狮、第二张在博物馆」一句话按序号对应多张无 GPS 照片。
- **地名搜索**：景点目录 → 高德 POI → Nominatim 三级检索，全球任意地名可达。
- **照片上限 10 张**：同一地点传太多无意义，前端截断 + 后端硬限。

### 定取景（Step 2）
- 卫星底图（Esri World Imagery）+ 红色取景框，5 / 10 / 15 km 三挡。
- 旅途名字由用户自己写，系统不代填。

### 挑风格（Step 3）
- 为任意区域动态生成 4 种风格画廊（默认 / 饱满街区 / 精细刻画 / 极简留白），参数差异拉到边界，肉眼可辨。

### 预览与产出（Step 4–5）
- **2D 双图**：旅程纪念平面图 + 轨迹标注图，上下排列，置于 3D 预览上方。
- **3D 正视图**：浮雕立起正对画面，相机锁定不能转到底座。
- **标注方式**：不插大头针，而是把标注点附近**最高处染红**（建筑红屋顶、山红山顶）。
- **封闭底座**：地形补裙边 + 平底，产出 watertight 实体。
- **落地后检**：导出前强制检查所有图层贴地，悬浮即拒绝导出。

---

## 数据驱动原则

不同城市/景点的 OSM 数据质量差异巨大，**写死阈值泛化效果差**，故采用数据源优先级链：

- **水体**：卫星多边形（高德）> OSM Polygon > OSM `width` 标签 > 自适应 buffer（最后手段）。有真实形状时跳过 LineString 猜宽度。
- **建筑高度**：动态覆盖率分档（`height_coverage ≥ 30%` 才跳过非验证建筑），替代写死开关。

---

## 目录结构

```
generate_city.py              # 当前正式西湖 25KM 3MF 入口
generate_city_legacy.py       # 旧通用入口（Web/draft/任意 bbox 兼容）
_TEXTURE_STYLE_OF_DEEPSEEK/   # 几何预处理 + 渲染核心
  _layer_preprocess.py        #   图层预处理（BL/BO/WL/VO/roads…）
  render_glb.py               #   draft GLB 导出 + 落地后检 + 染红标注
  render_png.py               #   2D 诊断图
  config.py                   #   渲染参数表
  auto_params/                #   规则引擎参数自适应
aesthetic/                    # 美学评审 + 风格画廊生成
  presets.py / rerun_harness.py / review_render.py / metrics.py
webapp/                       # 旅程回忆 Studio（FastAPI 轻后端 + 响应式前端）
  server.py                   #   API：cities / gallery / styles / generate / journey / geocode / fetch-pbf
  journey.py                  #   旅程解析：EXIF / 聚类 / 缺口检测 / 地名命名（纯逻辑，可单测）
  static/                     #   index.html / app.js / style.css + vendor（model-viewer, leaflet 本地自托管）
tools/
  gen_area_gallery.py         #   为任意 bbox 生成风格画廊
  batch_generate_gallery.py   #   预设城市画廊批量生成 + STYLE_VARIANTS 定义
  build_gazetteer.py          #   PBF → 离线地名表
data/
  landmarks/catalog.json      #   52 景点目录（含取景框）
  pbf_coverage.json           #   80 区域 PBF 覆盖表
  gazetteer/                  #   离线地名表（开箱即用）
tests/                        # pytest 套件（journey / gaps / gazetteer / geocode / landmarks / pbf_coverage / glb_postcheck …）
```

---

## 快速开始

### 1. 环境
```bash
pip install -r requirements.txt
```
Python ≥ 3.9。`manifold3d` 保证 watertight 输出；`Pillow` 读 EXIF；`fastapi/uvicorn` 跑 Web 服务。

### 2. 数据准备
管线需要 OSM 的 `.osm.pbf` 与高程 DEM。本地 `pbf_cache/` 放对应区域 PBF 即可生成。
项目支持**三态数据可用性**：本地就绪 / 远端可拉取 / 无数据。配置一台数据源服务器后，缺失区域可在页面一键 `scp` 拉取（拉一次永久复用）。

### 3. 启动 Studio
```bash
python webapp/server.py            # 默认 8787 端口
STUDIO_PORT=9000 python webapp/server.py   # 自定义端口
```
- 本机：`http://127.0.0.1:8787`
- 手机同 WiFi：用 `ipconfig` 查 WLAN 的 IPv4 访问（注意排除代理虚拟网卡地址）。

### 4. 测试
```bash
pytest tests/ -m "not slow"        # 离线套件（网络用例标 slow 默认跳过）
```

### 5. 西湖 25KM CLI 与 PNG 预览

专用西湖脚本可在生成 3MF 的同一次运行中输出彩色诊断图、干净俯视图和高度图：

```bash
python generate_city.py \
  --elevation-file /path/to/westlake_dem.tif \
  --png \
  --review-png
```

所有产物写入 `output/westlake_cli/`。`--png` 输出
`westlake_cli_preview.png`；`--review-png` 输出
`westlake_cli_topdown.png` 和 `westlake_cli_height.png`。

城市基底可用同一入口做三档对比；默认仍为既有的 `textured`，避免静默改变当前正确输出：

```bash
python generate_city.py --block-base-mode off
python generate_city.py --block-base-mode flat
python generate_city.py --block-base-mode textured
```

三档分别表示不生成城市基底、生成无纹理平面基底、生成现有语义 Z 纹理基底。
输出文件名包含 `_block-off`、`_block-flat` 或 `_block-textured`，可以并排保留。

城市基底默认退让模型外圈 2mm：外圈街区整块移除，紧邻的过渡带仅保留具有建筑覆盖的街区。3mm 更明显；设为 0 可恢复旧版铺满边缘的行为：

```bash
python generate_city.py --block-base-mode flat --block-base-edge-retreat-mm 3
python generate_city.py --block-base-mode flat --block-base-edge-retreat-mm 0
```

---

## 计算与部署架构（实测结论）

- **本地计算，服务器只做数据仓库**。实测：服务器光做 osmium 提取就比本地跑完整 full 还慢，且内存不足跑不动 full 3MF。
- 管线**纯单线程**，多核只提升并发不加速单任务；真实业务负载（甜区尺寸）内存峰值约 3.4–4 GB。
- 产品形态规划：线上设计服务 + 打印外包，多租户订单制，`spec_hash` 幂等缓存复用产物。

---

## 后续路线图

按优先级与性价比排序：

1. **部署上线**：把 Studio 部署到云服务器（API + 静态），打通外网访问与 HTTPS，这是从「本地 demo」到「可用产品」的第一步。
2. **数据覆盖**：当前本地 7 个 PBF 区域、远端 80 个区域。按需把高频景点区域拉到本地/对象存储，扩大「立即可生成」范围；DEM 同理。
3. **VLM 看图认地名**（B 层）：无 GPS 照片让视觉模型看图给候选地名，用户打字确认——让输入真正成为「辅助判据」。守住规矩：VLM 只出地名文本，坐标走 geocode 确定性查询；加缓存避免重复花费。
4. **算力扩容**：当并发上来再买计算节点（worker 与 API 分离 + 任务队列）。学生阶段单量小，先不烧钱。
5. **订单/支付闭环**：制作码模式对接淘宝/闲鱼，或自建轻量支付。
6. **收集反馈**：先小范围（朋友圈/旅行社群）放真实旅程照片试用，观察「卡在哪一步」「追问是否够用」「风格是否选得下去」，用真实数据驱动迭代，而非闭门调参。

> 取舍原则：标品养活现金流、定制抬高品牌；但纯定制盘子小、获客贵，且长尾订制交付的是不确定性。**先验证「有人愿意为回忆付费」，再谈规模。**

---

## 许可

个人项目，仅供学习与作品集展示。地图数据 © OpenStreetMap 贡献者，高程 © Copernicus DEM，卫星底图 © Esri。
