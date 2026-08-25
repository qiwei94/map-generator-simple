"""Pure progress parsing shared by the API and remote workers."""
from __future__ import annotations

import re


_FULL_STAGES = (
    ("Exported:", 98, "正在整理与验证交付文件"),
    ("[Stage 9]", 95, "正在导出可打印 3MF"),
    ("[Stage 8.5]", 91, "正在构建街区底座"),
    ("[Stage 8]", 88, "正在构建绿地与地表层"),
    ("[Stage 7]", 85, "正在构建水体结构"),
    ("[Stage 6]", 82, "正在构建道路结构"),
    ("[Stage 5]", 78, "正在构建建筑结构"),
    ("[Stage 4.8]", 76, "正在生成可旋转 3D 预览"),
    ("[Stage 4.7]", 74, "正在检查视觉构图"),
    ("[Stage 4.65]", 72, "正在渲染高清俯视图"),
    ("[Stage 4.6]", 66, "正在准备可打印水体与诊断图"),
    ("[preprocess] Total:", 63, "道路、水体与街区预处理已经完成"),
    ("[preprocess] _compute_block_base:", 60, "正在切分城市街区"),
    ("[preprocess] subtraction+filter:", 57, "正在清理图层重叠与细碎结构"),
    ("[preprocess] road_continuity:", 54, "正在恢复道路骨架连续性"),
    ("[Stage 4.5]", 50, "正在整理道路、建筑与水体图层"),
    ("[Stage 4]", 40, "正在构建地形与水体底层"),
    ("[Stage 3e]", 35, "正在分析城市结构与打印参数"),
    ("[Stage 3d]", 32, "正在读取地表信息"),
    ("[Stage 3c]", 29, "正在提取道路网络"),
    ("[Stage 3b]", 25, "正在提取建筑与街区"),
    ("[Stage 3]", 20, "正在提取绿地与植被"),
    ("[Stage 2]", 15, "正在提取湖泊、河流与岸线"),
    ("[Stage 1b]", 10, "正在读取高程数据"),
    ("[Stage 1]", 7, "正在建立取景坐标与边界"),
    ("[Stage 0]", 4, "正在检查运行环境"),
)

_STYLE_STAGES = (
    ("contact sheet:", 98, "正在整理风格方案"),
    ("/minimal]", 96, "四种风格已经渲染完成"),
    ("/dense_detail]", 91, "正在渲染第 4 种风格"),
    ("/block_fill]", 84, "正在渲染第 3 种风格"),
    ("/baseline]", 77, "正在渲染第 2 种风格"),
    ("[harness] prepared", 69, "正在渲染第 1 种风格"),
    ("[Tile Cache] vegetation:", 57, "正在提取绿地与地表信息"),
    ("[Tile Cache] water:", 42, "正在提取湖泊、河流与岸线"),
    ("[Tile Cache] road:", 25, "正在提取道路网络"),
    ("[Tile Cache] building:", 9, "正在提取建筑与街区"),
    ("[area-gallery]", 4, "正在准备所选区域的地图数据"),
)

_FAST_DRAFT_STAGES = (
    ("[glb] exported:", 97, "正在整理可旋转预览"),
    ("[postcheck] PASS", 92, "正在检查图层与地形接触关系"),
    ("[glb] block_base:", 76, "正在构建轻量 3D 图层"),
    ("[cache HIT] preprocess", 62, "已复用风格图层，准备 3D 预览"),
    ("[fast-draft] reopening", 18, "正在载入刚才选定的风格数据"),
)


def _counter_detail(log_tail: str) -> tuple[int | None, int | None,
                                             str | None]:
    patterns = (
        (r"\[amap\] fetched\s+(\d+)/(\d+)\s+tiles", "地图瓦片"),
        (r"\[amap\] only fetched\s+(\d+)/(\d+)\s+tiles", "地图瓦片"),
    )
    for pattern, noun in patterns:
        matches = list(re.finditer(pattern, log_tail, re.IGNORECASE))
        if matches:
            current, total = map(int, matches[-1].groups())
            return current, total, f"{noun} {current}/{total}"
    built = list(re.finditer(
        r"BlockBase\([^)]*\):\s*(\d+)\s+built", log_tail,
        re.IGNORECASE))
    if built:
        current = int(built[-1].group(1))
        return current, None, f"已构建 {current} 个街区块"
    return None, None, None


def progress_from_log(job: dict, log_tail: str) -> dict:
    """Return monotonic-friendly stage progress derived from real markers."""
    status = job.get("status")
    if status in ("starting", "pending"):
        return {
            "progress_pct": 2,
            "stage_code": "queued",
            "stage_label": "等待兼容的计算节点开始处理",
            "progress_source": "queue",
        }
    if status == "done":
        return {
            "progress_pct": 100,
            "stage_code": "done",
            "stage_label": ("风格方案已经生成" if job.get("mode") == "styles"
                            else "模型与交付文件已经生成"),
            "progress_source": "completion",
        }
    if status == "failed":
        return {
            "progress_pct": int(job.get("progress_pct") or 0),
            "stage_code": "failed",
            "stage_label": "生成未完成",
            "progress_source": "completion",
        }

    if job.get("fast_draft"):
        stages = _FAST_DRAFT_STAGES
    elif job.get("mode") == "styles":
        stages = _STYLE_STAGES
    else:
        stages = _FULL_STAGES

    current = int(job.get("progress_pct") or 3)
    stage_code = str(job.get("stage_code") or "preparing")
    label = str(job.get("stage_label") or "正在准备地图与高程数据")
    for marker, progress, marker_label in stages:
        if marker in log_tail:
            if progress >= current:
                current, label = progress, marker_label
                stage_code = marker.strip("[]:").lower().replace(" ", "_")
            break
    stage_current, stage_total, detail = _counter_detail(log_tail)
    result = {
        "progress_pct": max(3, min(99, current)),
        "stage_code": stage_code,
        "stage_label": label,
        "progress_source": "pipeline_markers",
    }
    if stage_current is not None:
        result["stage_current"] = stage_current
    if stage_total is not None:
        result["stage_total"] = stage_total
    if detail:
        result["stage_detail"] = detail
    return result
