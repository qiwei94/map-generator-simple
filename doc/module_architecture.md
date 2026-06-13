# 模块架构设计

## 术语

| 术语 | 含义 |
|------|------|
| data_server | 数据存储 ECS（现有 8.136.0.235, 134GB），存 PBF/DEM/pipeline_cache |
| process_server | 执行运算 ECS（待购 4c16g），跑 pipeline + API + AI 调用 |
| OSS | 阿里云对象存储，存最终产物（PNG/GLB/3MF）+ 静态网页 |
| pipeline_cache | 中间产物缓存（geodata/layers/output），存在 data_server 上 |

## 云上部署架构

### V1: 初期验证（<10 单/天）

```
                    ┌──────────────────────┐
                    │       用户 / AI       │
                    └──────────┬───────────┘
                               │ HTTPS
                               ▼
                    ┌──────────────────────┐
                    │   process_server     │
                    │   (4c16g, 40GB SSD)  │
                    │                      │
                    │  ┌────────────────┐  │
                    │  │  FastAPI       │  │
                    │  │  M6:AI 调用    │  │
                    │  │  M7:Orchestration│ │
                    │  │  Redis (本机)  │  │
                    │  └───────┬────────┘  │
                    │          │           │
                    │  ┌───────▼────────┐  │
                    │  │  Pipeline      │  │
                    │  │  M1→M5→M2→M3  │  │
                    │  └───────┬────────┘  │
                    └──────────┼───────────┘
                     NFS mount │ (同VPC, <1ms)
                    ┌──────────▼───────────┐          ┌────────────┐
                    │   data_server        │          │    OSS     │
                    │   (2c2g, 134GB)      │          │            │
                    │                      │   输出    │  成品展示   │
                    │  pbf_cache/    62GB  │ ───────▶ │  PNG/GLB   │
                    │  dem_cache/    43GB  │          │  静态网页   │
                    │  pipeline_cache/     │          │  CDN 加速   │
                    │    geodata/          │          └────────────┘
                    │    layers/           │
                    │    output/           │
                    └──────────────────────┘
```

**特点：**
- process_server 无状态，挂了换一台不丢数据
- 所有持久化数据在 data_server
- 最终产物推到 OSS，用户从 CDN 访问

### V2: 小规模运营（10-100 单/天）

```
                    ┌──────────────────────┐
                    │       用户 / AI       │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   API Gateway / SLB   │  ← 负载均衡
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │process_server│ │process_server│ │process_server│
    │     #1       │ │     #2       │ │     #3       │
    │  API + Worker│ │  Worker only │ │  Worker only │
    └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
           │                │                │
           └────────────────┼────────────────┘
                            │ NFS/NAS mount
                    ┌───────▼──────────────┐
                    │   data_server        │
                    │   (NAS 替代 NFS)     │
                    │                      │
                    │  pbf_cache/          │     ┌────────────┐
                    │  dem_cache/          │     │    OSS     │
                    │  pipeline_cache/     │────▶│  成品+CDN  │
                    └──────────────────────┘     └────────────┘
                                                      │
                    ┌──────────────────────┐           │
                    │   Redis (独立实例)    │           │
                    │   任务队列 + 缓存     │           │
                    └──────────────────────┘           │
                    ┌──────────────────────┐           │
                    │   RDS MySQL          │◀──────────┘
                    │   订单 + 用户 + 任务  │
                    └──────────────────────┘
```

**V1→V2 升级点：**
- data_server 的 NFS → NAS（支持多节点并发读写）
- 多台 process_server 竞争消费 Redis 队列
- SQLite → RDS MySQL
- Redis 独立实例
- SLB 负载均衡 API 请求

### V3: 规模化（100+ 单/天）

```
                    ┌──────────────────────┐
                    │       用户 / AI       │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │     API Gateway       │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   API Server (ECS)    │  ← 轻量，只接请求
                    │   FastAPI + M6 + M7  │
                    └──────────┬───────────┘
                               │ 任务入队
                    ┌──────────▼───────────┐
                    │   RocketMQ / Redis    │  ← 消息队列
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │process_server│ │process_server│ │  FC (函数计算) │
    │  ECS Worker  │ │  ECS Worker  │ │  弹性 Worker  │
    └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
           │                │                │
           └────────────────┼────────────────┘
                            │
              ┌─────────────┼──────────────┐
              ▼                            ▼
    ┌──────────────────┐        ┌────────────────┐
    │   OSS (数据源)    │        │   OSS (产物)   │
    │  PBF + DEM       │        │  PNG/GLB/3MF   │
    │  pipeline_cache  │        │  CDN 加速      │
    └──────────────────┘        └────────────────┘
              │
    ┌─────────▼────────┐
    │   RDS + Redis    │
    └──────────────────┘
```

**V2→V3 升级点：**
- data_server 退役 → 数据全部迁移到 OSS（无限扩展）
- process_server 按需 + FC 弹性混合（高峰弹出，低谷缩回）
- pipeline_cache 也上 OSS（跨节点共享无上限）
- API 和 Worker 彻底分离

## 弹性与扩展性分析

### 弹性（应对流量波动）

| 阶段 | 瓶颈 | 弹性方案 | 扩容时间 |
|------|------|---------|---------|
| V1 | process_server 单点 | 手动升配（停机 5min） | 分钟级 |
| V2 | process_server 固定数量 | 手动加减 ECS + 队列自动分配 | 10 分钟 |
| V3 | 无固定瓶颈 | FC 自动弹性 0→N，ECS 按 CPU 阈值伸缩组 | 秒级 |

### 扩展性（应对数据/功能增长）

| 扩展方向 | V1 | V2 | V3 |
|----------|-----|-----|-----|
| 加更多城市数据 | data_server 扩盘 | NAS 按量扩 | OSS 无限 |
| 加新 pipeline 功能 | 改代码重启 | 滚动更新 Worker | 更新函数版本 |
| 加新 AI 模型 | process_server 调 API | 同 V1 | 独立 AI 微服务 |
| 加新输出格式 | M3 加 exporter | 同 V1 | 同 V1 |
| 多地域部署 | ❌ | ❌ | OSS 跨域复制 + 多 region Worker |

### 缓存层演进

| 阶段 | 缓存位置 | 共享范围 | 淘汰策略 |
|------|---------|---------|---------|
| V1 | data_server 本地目录 | 单 process_server | 手动清理 |
| V2 | NAS 共享目录 | 多 process_server | LRU (定时脚本) |
| V3 | OSS + 本地 SSD 热缓存 | 全局 | OSS 生命周期规则自动淘汰 |

### 各阶段成本估算

| 阶段 | 月成本 | 支撑量 |
|------|--------|--------|
| V1 | ¥500-800 | <10 单/天 |
| V2 | ¥2000-4000 | 10-100 单/天 |
| V3 | ¥5000-15000 (按量弹性) | 100-1000 单/天 |

## 模块总览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                    M7: Orchestration (编排)                        │  │
│  │  控制流转、循环次数、收敛判断、批量调度、队列管理                       │  │
│  └───────────┬───────────────────────────────────────────────────────┘  │
│              │                                                          │
│  ┌───────────▼───────────────────────────────────────────────────────┐  │
│  │                                                                    │  │
│  │   ┌──────────┐                                                    │  │
│  │   │ M6: AI   │  用户照片/文字/对话                                  │  │
│  │   │ (理解层)  │──────────────────┐                                 │  │
│  │   └──────────┘                   │                                 │  │
│  │        │                         │                                 │  │
│  │        │ {bbox, city, style}     │ 反馈: {score, issues, overrides}│  │
│  │        ▼                         │                                 │  │
│  │   ┌──────────┐    params    ┌────┴─────┐                          │  │
│  │   │ M1: Data │◄────────────│M5: Params │◄─── 用户反馈             │  │
│  │   │ (数据获取) │             │ (参数中枢) │     "左边多点绿"          │  │
│  │   └────┬─────┘             └────┬──────┘                          │  │
│  │        │                        │                                  │  │
│  │        │ GeoDataFrames          │ pipeline_params.json             │  │
│  │        │ + elevation            │                                  │  │
│  │        ▼                        ▼                                  │  │
│  │   ┌─────────────────────────────────────┐                          │  │
│  │   │         M2: Pipeline (生成)          │                          │  │
│  │   │  terrain/buildings/roads/water/veg   │                          │  │
│  │   └──────────────────┬──────────────────┘                          │  │
│  │                      │                                             │  │
│  │                      │ meshes dict (trimesh)                       │  │
│  │                      ▼                                             │  │
│  │   ┌─────────────────────────────────────┐                          │  │
│  │   │      M3: Export + Preview            │                          │  │
│  │   │  3MF / GLB / PNG / metadata          │                          │  │
│  │   └──────────┬──────────┬───────────────┘                          │  │
│  │              │          │                                          │  │
│  │              │          │                                          │  │
│  │      ┌───────▼──┐  ┌───▼────────┐                                 │  │
│  │      │ M6: AI   │  │ M4: Web    │                                 │  │
│  │      │ 审图评分   │  │ 用户预览    │                                 │  │
│  │      └───────┬──┘  └───┬────────┘                                 │  │
│  │              │          │                                          │  │
│  │              └────┬─────┘                                          │  │
│  │                   │ 反馈 (不满意)                                   │  │
│  │                   ▼                                                │  │
│  │              ┌──────────┐                                          │  │
│  │              │M5: Params│ ← 接收反馈，修正参数，触发重跑              │  │
│  │              └──────────┘                                          │  │
│  │                                                                    │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                          ↑ 循环直到满意                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

## 核心闭环

```
初始: M6(定位) → M1(取数) → M5(选参) → M2(生成) → M3(输出)
                                                        │
审图: M3 ──PNG──▶ M6(AI审图) ──反馈──▶ M5(调参) ──▶ M2(重跑) → M3
                     │
用户: M3 ──GLB──▶ M4(Web预览) ──反馈──▶ M5(调参) ──▶ M2(重跑) → M3
                                                        │
终止: AI评分 ≥ 阈值 且 (用户确认 或 批量模式自动通过)
      或 达到最大迭代次数 (默认3轮)
```

## 模块接口定义

### M1: Data (数据获取)

```
输入:
  bbox: (south, west, north, east)  # WGS84
  pbf_path: str                     # PBF 文件路径
  dem_dir: str                      # DEM 缓存目录

输出:
  buildings_gdf: GeoDataFrame       # 建筑多边形 + height/levels 属性
  roads_gdf: GeoDataFrame           # 道路线 + highway type
  water_gdf: GeoDataFrame           # 水体多边形/线
  vegetation_gdf: GeoDataFrame      # 绿地多边形
  elevation_grid: ndarray           # 高程网格 (NxN)
  width_m, height_m: float          # UTM 投影后的区域尺寸
  area_km2: float                   # 面积
  scale: float                      # 比例尺

不做: 定位、坐标猜测、参数决策
```

### M2: Pipeline (几何生成)

```
输入:
  data: M1 输出 (GeoDataFrames + elevation)
  params: pipeline_params.json (来自 M5)

输出:
  meshes: {
    "terrain": Trimesh,
    "buildings": Trimesh,
    "landmarks": Trimesh,
    "roads": Trimesh,
    "water": Trimesh,
    "vegetation": Trimesh,
    "block_base": Trimesh
  }
  layers: LayerPolygons (用于 PNG 渲染)

不做: 数据获取、参数选择、文件输出
冻结: 核心几何逻辑不动
```

### M3: Export + Preview (输出+预览)

```
输入:
  meshes: dict (来自 M2)
  layers: LayerPolygons (来自 M2, 用于 PNG)
  output_config: {format: [3mf, glb, png], quality: high/web, lod_target: int}

输出:
  files: {
    "3mf": path,        # 打印用高精度
    "glb": path,        # Web 3D 预览 (降面后)
    "png": path,        # 2D 俯视预览
    "metadata": path    # 生成参数+统计
  }

不做: 评判质量、决定是否重跑
```

### M4: Web (展示)

```
输入:
  cities_index: [{city, country, png_url, glb_url, metadata_url}, ...]
  单城市详情: glb_url + png_url + metadata

输出:
  静态网页 (HTML + CSS + model-viewer)
  用户操作事件: {type: "feedback", text: "...", city: "..."}

不做: 生成、存储、AI 调用
```

### M5: Params (参数中枢)

```
输入 (初始):
  city_profile: CityProfile (从 M1 数据分析得出)
  user_preferences: {style, mood, highlights} (来自 M6)

输入 (迭代):
  feedback: {
    source: "ai" | "user",
    score: float,
    issues: ["水体不完整", "建筑过密"],
    suggested_overrides: {z_gamma: 0.35, ...}
  }
  current_params: 上一轮参数
  iteration: int

输出:
  pipeline_params.json: {
    z_gamma, terrain_thickness, building_mode, road_tier,
    water_detail, vegetation_min_area, style, ...
  }
  decision_report: {每个参数的 reason}

不做: 执行生成、获取数据、调用 AI API
```

### M6: AI (理解层)

```
功能 1 — 定位:
  输入: 照片(EXIF/视觉) 或 文字描述 或 城市名
  输出: {bbox, city_name, country, pbf_name}

功能 2 — 意图理解:
  输入: 用户对话 (多轮)
  输出: {style, mood, highlights, preferences}

功能 3 — 审图:
  输入: PNG 图片 + 当前 params + city_profile
  输出: {
    score: 0-10,
    passed: bool,
    issues: ["...", "..."],
    suggested_overrides: {param: value, ...}
  }

功能 4 — 反馈解析:
  输入: 用户自然语言反馈 ("左边多点绿", "建筑太密了")
  输出: suggested_overrides (同审图格式)

不做: 执行 pipeline、管理数据文件
```

### M7: Orchestration (编排)

```
输入:
  mode: "single" | "batch" | "interactive"
  cities: [city_config, ...] (batch 模式)
  user_session: session_id (interactive 模式)

职责:
  - 控制 M6→M1→M5→M2→M3 的执行顺序
  - 管理审图-调参-重跑循环 (max_iterations, convergence)
  - 批量模式: 断点续跑、超时、错误处理、进度
  - 交互模式: 等待用户反馈、推送通知
  - 队列管理 (Redis)

输出:
  task_status: {id, state, progress, result_urls}

不做: 具体的生成/AI/数据逻辑 (只调度)
```

## 可行性预判（M6 功能 5）

在用户确认下单之前，M6 需要快速评估"能不能做、做出来效果如何"：

```
用户输入需求
    │
    ▼
M6: 可行性预判 (1-2秒内返回)
    ├── 数据覆盖度检查（bbox 内建筑/道路/水体数量级）
    ├── 风格匹配度（当前能力 vs 用户期望）
    ├── 物理可行性（打印精度 vs 细节需求）
    └── 输出: feasibility_report
            │
            ├── ✅ 可做 → 继续流程
            ├── ⚠️ 有限制 → 告知限制 + 展示降级预览 → 用户决定是否继续
            └── ❌ 做不了 → 诚实说明原因 + 推荐替代方案
```

### "做不到"的场景及应对

| 做不到的原因 | 例子 | 产品策略 |
|-------------|------|---------|
| 数据缺失 | 小镇没有 OSM 建筑数据 | 告知覆盖度，降级出图（只有路网+地形），展示降级预览让用户判断 |
| 超出能力边界 | "把我的照片印在模型上" | 明确说做不到，推荐替代方案 |
| 风格不支持 | "想要赛博朋克风" | 说明可选风格，展示最近似效果 |
| 质量不达标 | 3 轮调参后 AI 评分仍低 | 人工介入 或 诚实告知"该区域效果有限" |
| 物理限制 | "建筑要 0.1mm 细节" | 解释工艺限制，建议放大比例尺 |
| 区域数据不均 | bbox 边缘有数据，中心空白 | 建议调整范围，指出"往东移 2km 数据更丰富" |

### 接口定义

```
M6 功能 5 — feasibility_check:
  输入:
    bbox: (south, west, north, east)
    user_request: {style, mood, highlights, special_requirements}
  
  输出:
    {
      feasible: "yes" | "partial" | "no",
      coverage: {buildings: 85%, roads: 92%, water: 40%, vegetation: 60%},
      estimated_quality: "high" | "medium" | "low",
      limitations: ["该区域建筑数据稀疏，效果可能单薄"],
      alternatives: [
        {action: "shift_bbox", suggestion: "往东移2km", new_bbox: [...]},
        {action: "change_style", suggestion: "该区域适合 terrain-first 风格"},
        {action: "enlarge_area", suggestion: "扩大到 10km 范围效果更好"}
      ],
      degraded_preview: png_url  // 可选：快速低质量预览
    }
```

### 核心原则

- **诚实优先**：宁可告知限制让用户自己判断，不硬装出图后翻车
- **给替代方案**：每次说"不行"的时候必须带一个"但你可以..."
- **快速反馈**：预判在 1-2 秒内完成（只查数据量级，不跑 pipeline）
- **降级预览**：partial 情况下可以出一张低质量快速预览，让用户直观感受

### 实现路径

1. M1 提供轻量接口 `quick_stats(bbox)` — 只统计 OSM 要素数量，不做完整提取
2. M5 提供 `estimate_quality(stats, style)` — 根据数据量预判出图质量等级
3. M6 综合上述 + 用户请求 → 生成 feasibility_report
4. M7 在启动 pipeline 之前强制调用 feasibility_check，non-feasible 直接拦截

## 模块独立性

| 模块 | 可独立运行？ | 独立测试方式 |
|------|------------|------------|
| M1 | ✅ | 给 bbox → 出 GeoDataFrame，验证字段完整 |
| M2 | ✅ | 给固定 GeoDataFrame + params → 出 meshes，跑 validator |
| M3 | ✅ | 给固定 meshes → 出文件，检查文件完整性 |
| M4 | ✅ | 给 JSON 列表 → 出静态网页，浏览器验证 |
| M5 | ✅ | 给 CityProfile / feedback → 出 params，单元测试 |
| M6 | ✅ | 给照片/文字 → 出 JSON，mock API 测试 |
| M7 | ✅ | mock 所有模块接口 → 验证编排逻辑 |

## 文件归属

```
project/
├── m1_data/                    # M1: Data
│   ├── fetcher.py              # bbox → GeoDataFrames
│   ├── dem_reader.py           # bbox → elevation_grid
│   └── cache.py                # PBF/DEM 路径管理
├── m2_pipeline/                # M2: Pipeline (现有 _TEXTURE_STYLE_OF_DEEPSEEK/)
│   ├── (现有冻结文件不动)
│   └── run.py                  # 统一入口: data+params → meshes
├── m3_export/                  # M3: Export
│   ├── to_3mf.py              # meshes → 3MF
│   ├── to_glb.py              # meshes → GLB (含 LOD)
│   ├── to_png.py              # layers → PNG
│   └── metadata.py            # 生成 metadata.json
├── m4_web/                     # M4: Web
│   ├── index.html
│   ├── city.html
│   └── assets/
├── m5_params/                  # M5: Params (现有 auto_params/)
│   ├── profile.py             # 数据 → CityProfile
│   ├── resolver.py            # profile + feedback → params
│   └── feedback.py            # 解析反馈、合并 overrides
├── m6_ai/                      # M6: AI
│   ├── locate.py              # 照片/文字 → bbox
│   ├── understand.py          # 对话 → preferences
│   ├── review.py              # PNG → 评分+反馈
│   └── parse_feedback.py      # 用户文字 → overrides
├── m7_orchestration/           # M7: Orchestration
│   ├── single.py              # 单次生成流程
│   ├── batch.py               # 批量生成
│   ├── interactive.py         # 交互式 (含循环)
│   └── queue.py               # Redis 任务管理
├── cities.json                 # 城市配置
└── tools/                      # 运维工具 (manage_pbf/dem 等)
```
