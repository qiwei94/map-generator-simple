# -*- coding: utf-8 -*-
"""旅程解析：多照片 EXIF → 带时间轴的轨迹。

纯逻辑模块（无 FastAPI/管线依赖），server.py 调用，pytest 直接可测。

处理链：
    photos → 逐张 (GPS, 拍摄时间) → 时间排序 → 异常点标记（隐含速度）
           → 停留聚类（时序贪心）→ 建议 bbox / 按日期分章
"""
import math
from datetime import datetime, timezone

# ─── 可调常量 ────────────────────────────────────────────────────────
SPEED_LIMIT_KMH = 150.0      # 相邻点隐含速度超过 → 后一点标记 suspect
CLUSTER_RADIUS_M = 400.0     # 距簇心小于该值归入同簇
BBOX_PAD_RATIO = 0.15        # 建议 bbox 外扩比例
BBOX_MIN_SPAN_DEG = 0.018    # 建议 bbox 最小边长（≈2km）
BBOX_SPLIT_SPAN_DEG = 0.35   # 超过该跨度建议按日期拆分成系列


def _to_deg(v):
    return float(v[0]) + float(v[1]) / 60.0 + float(v[2]) / 3600.0


def extract_photo_meta(fp):
    """单张照片 → {"lat", "lon", "time"}（字段缺失为 None）。

    时间优先级：Exif DateTimeOriginal（本地时间）→ GPS UTC 日期/时间戳
    → IFD0 DateTime。返回 time 为 epoch 秒（float）或 None。
    """
    from PIL import Image
    img = Image.open(fp)
    exif = img.getexif()
    out = {"lat": None, "lon": None, "time": None}

    # ── GPS ──
    gps = exif.get_ifd(0x8825)
    if gps and 2 in gps and 4 in gps:
        try:
            lat = _to_deg(gps[2])
            lon = _to_deg(gps[4])
            if str(gps.get(1, "N")).upper() == "S":
                lat = -lat
            if str(gps.get(3, "E")).upper() == "W":
                lon = -lon
            if (-90 <= lat <= 90 and -180 <= lon <= 180
                    and not (lat == 0 and lon == 0)):
                out["lat"], out["lon"] = lat, lon
        except (TypeError, ValueError, ZeroDivisionError, IndexError):
            pass

    # ── 时间 ──
    def parse_dt(s):
        try:
            return datetime.strptime(str(s).strip(), "%Y:%m:%d %H:%M:%S") \
                .timestamp()
        except (ValueError, TypeError):
            return None

    exif_ifd = exif.get_ifd(0x8769)
    t = parse_dt(exif_ifd.get(0x9003)) if exif_ifd else None  # DateTimeOriginal
    if t is None and gps and 29 in gps and 7 in gps:
        # GPSDateStamp "YYYY:MM:DD" + GPSTimeStamp (h, m, s) → UTC
        try:
            d = datetime.strptime(str(gps[29]).strip(), "%Y:%m:%d")
            h, m, s = (float(x) for x in gps[7])
            t = d.replace(hour=int(h), minute=int(m), second=int(s),
                          tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError, IndexError):
            t = None
    if t is None:
        t = parse_dt(exif.get(0x0132))  # IFD0 DateTime（改图会刷新，最后手段）
    out["time"] = t
    return out


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """两点球面距离（米）。"""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) \
        * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def mark_suspects(points):
    """按时间序标记瞬移点（转发图/EXIF 错误混入）。

    points: [{"lat","lon","time",...}]，须已按 time 升序。
    在每个点上补 "suspect": bool。首点视为可信锚。
    """
    prev = None
    for p in points:
        p["suspect"] = False
        if prev is not None:
            dt_s = (p["time"] - prev["time"]) if (
                p["time"] is not None and prev["time"] is not None) else None
            dist = haversine_m(prev["lat"], prev["lon"], p["lat"], p["lon"])
            if dt_s is not None and dt_s > 0:
                if dist / dt_s * 3.6 > SPEED_LIMIT_KMH:
                    p["suspect"] = True
                    continue  # suspect 不做后续锚点
            elif dt_s == 0 and dist > 50000:
                p["suspect"] = True
                continue
        prev = p
    return points


def cluster_stops(points):
    """时序贪心聚类：连续且距簇心 <CLUSTER_RADIUS_M 的点归为一次停留。

    输入须已按时间排序且剔除 suspect。
    返回 [{"lat","lon","count","t_start","t_end","dwell_minutes"}]。
    """
    clusters = []
    for p in points:
        c = clusters[-1] if clusters else None
        if c and haversine_m(c["lat"], c["lon"], p["lat"], p["lon"]) \
                < CLUSTER_RADIUS_M:
            n = c["count"]
            c["lat"] = (c["lat"] * n + p["lat"]) / (n + 1)
            c["lon"] = (c["lon"] * n + p["lon"]) / (n + 1)
            c["count"] = n + 1
            c["photo_names"].append(p.get("name", ""))
            if p["time"] is not None:
                c["t_start"] = min(x for x in (c["t_start"], p["time"])
                                   if x is not None)
                c["t_end"] = max(x for x in (c["t_end"], p["time"])
                                 if x is not None)
        else:
            clusters.append({"lat": p["lat"], "lon": p["lon"], "count": 1,
                             "t_start": p["time"], "t_end": p["time"],
                             "photo_names": [p.get("name", "")]})
    for c in clusters:
        c["dwell_minutes"] = round((c["t_end"] - c["t_start"]) / 60.0, 1) \
            if (c["t_start"] is not None and c["t_end"] is not None) else 0.0
    return clusters


def suggest_bbox(clusters):
    """覆盖全部簇心 + 外扩边距，保证最小跨度。→ [s, w, n, e]"""
    if not clusters:
        return None
    lats = [c["lat"] for c in clusters]
    lons = [c["lon"] for c in clusters]
    s, n = min(lats), max(lats)
    w, e = min(lons), max(lons)
    pad_lat = max((n - s) * BBOX_PAD_RATIO, 0.002)
    pad_lon = max((e - w) * BBOX_PAD_RATIO, 0.002)
    s, n = s - pad_lat, n + pad_lat
    w, e = w - pad_lon, e + pad_lon
    # 最小跨度（太小的模型没内容）
    if (n - s) < BBOX_MIN_SPAN_DEG:
        mid = (n + s) / 2
        s, n = mid - BBOX_MIN_SPAN_DEG / 2, mid + BBOX_MIN_SPAN_DEG / 2
    lon_min = BBOX_MIN_SPAN_DEG / max(
        math.cos(math.radians((s + n) / 2)), 0.2)
    if (e - w) < lon_min:
        mid = (e + w) / 2
        w, e = mid - lon_min / 2, mid + lon_min / 2
    return [round(s, 4), round(w, 4), round(n, 4), round(e, 4)]


def chapters_by_day(clusters):
    """簇按拍摄日期分组 → [{"date", "clusters": [idx...]}]（无时间的归 unknown）。"""
    groups = {}
    for i, c in enumerate(clusters):
        key = datetime.fromtimestamp(c["t_start"]).strftime("%Y-%m-%d") \
            if c["t_start"] is not None else "unknown"
        groups.setdefault(key, []).append(i)
    return [{"date": k, "clusters": v} for k, v in sorted(groups.items())]


NAME_MAX_DIST_M = 300.0   # 簇心到 POI 超过该距离不命名（宁缺毋滥）


def name_clusters(clusters, pois, max_dist_m=NAME_MAX_DIST_M):
    """簇心就近命名：取半径内 (优先级, 距离) 最优的 POI 名。

    pois: [{"name","lat","lon","prio"}]（tools/build_gazetteer.py 产物）。
    命不了名的簇 name=None，前端回退编号——不强行给名字。
    """
    # 粗筛窗口：约 max_dist 对应的经纬度跨度（纬度保守上浮）
    win = max_dist_m / 110574.0 * 1.3
    for c in clusters:
        best_key, best_name = None, None
        for p in pois:
            if abs(p["lat"] - c["lat"]) > win:
                continue
            if abs(p["lon"] - c["lon"]) > win * 2.5:
                continue
            d = haversine_m(c["lat"], c["lon"], p["lat"], p["lon"])
            if d > max_dist_m:
                continue
            key = (p.get("prio", 9), d)
            if best_key is None or key < best_key:
                best_key, best_name = key, p["name"]
        c["name"] = best_name
    return clusters


# ─── 缺口追问（只问照片里没有的信息）──────────────────

GAP_TIME_MIN_H = 4.0     # 相邻停留点间隔超过该时长 → 问中间去了哪
GAP_MAX_QUESTIONS = 4    # 上限：别把三步点击变成五轮对话


def _hhmm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"


def detect_gaps(analysis):
    """从旅程分析结果推导需要追问的缺口。

    四类（按必要性排序）：
      no_gps_all      全部照片无 GPS，无法定位（必答）
      time_gap        时间轴大空档，中间行程缺失
      no_gps_partial  部分照片无 GPS
      unnamed_cluster 停留点地名库未命中

    返回 [{type, question, detail, optional}]；无缺口则返回 []
    （没缺口就一句话也不问）。问句只问事实，不评价不抒情。
    """
    gaps = []
    clusters = analysis.get("clusters") or []
    photos = analysis.get("photos") or []
    no_gps = [p for p in photos if p.get("status") == "no_gps"]

    # 1) 全部无 GPS：没定位就无法生成，必须问
    if not clusters:
        if photos:
            gaps.append({
                "type": "no_gps_all",
                "question": "这些照片在哪拍的？",
                "detail": {"photo_count": len(photos),
                           "photo_names": [p.get("name") for p in photos]},
                "optional": False,
            })
        return gaps

    # 2) 时间轴空档：相邻停留点之间隔了很久，中间往往有未拍照的行程
    for i in range(len(clusters) - 1):
        a, b = clusters[i], clusters[i + 1]
        t_a, t_b = a.get("t_end"), b.get("t_start")
        if t_a is None or t_b is None:
            continue
        hours = (t_b - t_a) / 3600.0
        if hours < GAP_TIME_MIN_H:
            continue
        # 跳过跨夜（晚上回酒店不算行程缺失）
        if datetime.fromtimestamp(t_a).date() != datetime.fromtimestamp(t_b).date():
            continue
        gaps.append({
            "type": "time_gap",
            "question": f"{_hhmm(t_a)}–{_hhmm(t_b)} 之间去了哪？",
            "detail": {"after_cluster": i, "hours": round(hours, 1),
                       "near": [a["lat"], a["lon"]]},
            "optional": True,
        })

    # 3) 部分照片无 GPS
    if no_gps:
        gaps.append({
            "type": "no_gps_partial",
            "question": f"有 {len(no_gps)} 张照片没位置信息，在哪拍的？",
            "detail": {"photo_names": [p.get("name") for p in no_gps],
                       "near": [clusters[0]["lat"], clusters[0]["lon"]]},
            "optional": True,
        })

    # 4) 停留点未命名（地名库没覆盖）——只问停留最久的那一个
    unnamed = [(i, c) for i, c in enumerate(clusters) if not c.get("name")]
    if unnamed:
        i, c = max(unnamed, key=lambda t: t[1].get("dwell_minutes") or 0)
        gaps.append({
            "type": "unnamed_cluster",
            "question": f"第 {i + 1} 个停留点叫什么？",
            "detail": {"cluster": i, "near": [c["lat"], c["lon"]],
                       "count": c.get("count"),
                       "dwell_minutes": c.get("dwell_minutes"),
                       "photo_names": c.get("photo_names", [])},
            "optional": True,
        })

    return gaps[:GAP_MAX_QUESTIONS]


def analyze_journey(photo_metas):
    """主入口：逐张元数据 → 旅程分析结果。

    photo_metas: [{"name", "lat", "lon", "time"}]（lat/lon 为 None 的照片保留
    在 photos 报告里但不参与轨迹）。
    """
    report = []
    valid = []
    for m in photo_metas:
        item = dict(m)
        if m["lat"] is None:
            item["status"] = "no_gps"
        else:
            item["status"] = "ok"
            valid.append(item)
        report.append(item)

    # 无时间戳的点排在最后（保持上传顺序），有时间的按时间排
    valid.sort(key=lambda p: (p["time"] is None, p["time"] or 0))
    mark_suspects(valid)
    for p in valid:
        if p["suspect"]:
            p["status"] = "suspect"
    good = [p for p in valid if not p["suspect"]]

    clusters = cluster_stops(good)
    bbox = suggest_bbox(clusters)
    chapters = chapters_by_day(clusters)
    span_too_big = bool(bbox) and (
        (bbox[2] - bbox[0]) > BBOX_SPLIT_SPAN_DEG
        or (bbox[3] - bbox[1]) > BBOX_SPLIT_SPAN_DEG)
    return {
        "photos": [{k: v for k, v in p.items() if k != "suspect"}
                   for p in report],
        "clusters": clusters,
        "suggested_bbox": bbox,
        "chapters": chapters,
        "suggest_split": span_too_big and len(chapters) > 1,
    }
