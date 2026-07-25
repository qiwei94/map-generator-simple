"""Agent 工具集：生成、评估、调参.

每个工具封装为 LangChain @tool，供 ReAct agent 调用。
"""

import base64
import json
import os
import sys
from typing import Optional

from langchain_core.tools import tool

# 确保项目根目录在 path 中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ─── 全局状态（agent 运行期间共享）───────────────────────────────────────
_state = {
    "city_name": "",
    "buildings_gdf": None,
    "water_gdf": None,
    "bbox_utm": None,
    "bbox_wgs84": None,
    "utm_epsg": None,
    "relief_data": None,
    "reference_image_path": None,
    "output_dir": "",
    "iteration": 0,
    "history": [],  # [(params, score, feedback)]
}


def init_state(
    city_name: str,
    buildings_gdf,
    water_gdf,
    bbox_utm: tuple,
    utm_epsg: int,
    reference_image_path: Optional[str],
    output_dir: str,
    bbox_wgs84: Optional[tuple] = None,
):
    """初始化 agent 运行状态."""
    _state["city_name"] = city_name
    _state["buildings_gdf"] = buildings_gdf
    _state["water_gdf"] = water_gdf
    _state["bbox_utm"] = bbox_utm
    _state["bbox_wgs84"] = bbox_wgs84
    _state["utm_epsg"] = utm_epsg
    _state["reference_image_path"] = reference_image_path
    _state["output_dir"] = output_dir
    _state["iteration"] = 0
    _state["history"] = []
    _state["relief_data"] = None


# ─── Tool 1: 生成浮雕图 ─────────────────────────────────────────────────


@tool
def generate_relief(
    z_exaggeration: float = 3.0,
    light_azimuth: float = 315.0,
    light_altitude: float = 45.0,
    height_gamma: float = 0.55,
    ao_strength: float = 0.35,
    edge_strength: float = 0.25,
    grain_strength: float = 0.02,
    dilation_iterations: int = 2,
    style: str = "mono_light",
) -> str:
    """用指定参数生成城市建筑浮雕图。

    Args:
        z_exaggeration: 高度夸张系数(1-8)，越大3D感越强
        light_azimuth: 光源方位角(0-360度)，315=西北光
        light_altitude: 光源仰角(20-70度)
        height_gamma: 高度→亮度映射gamma(0.3-1.0)，越小高楼越亮
        ao_strength: 环境光遮蔽强度(0-0.6)，越大缝隙越暗
        edge_strength: 边缘暗化强度(0-0.5)
        grain_strength: 表面颗粒纹理(0-0.05)
        dilation_iterations: 建筑膨胀迭代(0-4)，越大建筑越连续
        style: 渲染风格 mono_light/mono_dark

    Returns:
        生成结果描述（含文件路径和统计信息）
    """
    from relief_studio.relief_map import build_relief_heightmap
    from relief_studio.renderer import render_relief

    _state["iteration"] += 1
    iter_n = _state["iteration"]

    # 构建高度图（如果还没有或 dilation 变了）
    if _state["relief_data"] is None:
        _state["relief_data"] = build_relief_heightmap(
            _state["buildings_gdf"],
            _state["water_gdf"],
            _state["bbox_utm"],
            grid_size=2048,
            height_cap=400.0,
            height_floor=2.0,
        )

    # 渲染
    out_path = os.path.join(
        _state["output_dir"],
        f"{_state['city_name']}_iter{iter_n:02d}.png",
    )

    render_relief(
        _state["relief_data"],
        out_path,
        city_name=_state["city_name"],
        style=style,
        z_exaggeration=z_exaggeration,
        light_azimuth=light_azimuth,
        light_altitude=light_altitude,
        height_gamma=height_gamma,
        ao_strength=ao_strength,
        edge_strength=edge_strength,
        grain_strength=grain_strength,
        output_size_px=2048,
    )

    params = {
        "z_exaggeration": z_exaggeration,
        "light_azimuth": light_azimuth,
        "light_altitude": light_altitude,
        "height_gamma": height_gamma,
        "ao_strength": ao_strength,
        "edge_strength": edge_strength,
        "grain_strength": grain_strength,
        "style": style,
    }

    return json.dumps({
        "status": "success",
        "iteration": iter_n,
        "output_path": out_path,
        "params": params,
        "building_coverage": f"{(_state['relief_data']['heightmap'] > 0).mean()*100:.1f}%",
        "water_coverage": f"{_state['relief_data']['water_mask'].mean()*100:.1f}%",
    }, ensure_ascii=False)


# ─── Tool 2: AI 视觉评估 ────────────────────────────────────────────────


@tool
def evaluate_image(image_path: str) -> str:
    """用 Qwen-VL 视觉模型评估生成的浮雕图质量。

    将生成图与参考作品对比，从5个维度打分(1-10)并给出改进建议。

    Args:
        image_path: 要评估的 PNG 图片路径

    Returns:
        JSON 格式的评估结果（分数+问题+建议）
    """
    import dashscope
    from dashscope import MultiModalConversation

    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return json.dumps({"error": "DASHSCOPE_API_KEY not set"})

    # 构建消息
    content = []

    # 参考图（如果有）
    ref_path = _state.get("reference_image_path")
    if ref_path and os.path.exists(ref_path):
        content.append({"image": f"file://{ref_path}"})
        content.append({"text": "上面是参考作品（目标效果）。"})

    # 待评估图
    content.append({"image": f"file://{image_path}"})
    content.append({"text": f"""上面是我生成的{_state['city_name']}建筑浮雕地图。

请对比参考作品，从以下维度评分(1-10)：
1. water_contrast: 水体与陆地的对比是否鲜明（水体应该是纯黑，陆地是灰白）
2. building_texture: 建筑纹理是否致密连续（不能太稀疏也不能太糊）
3. height_variation: 高度层次是否明显（市中心应该明显高于郊区）
4. surface_quality: 表面质感是否接近石膏/树脂（不能太粗糙/像素化）
5. overall_aesthetic: 整体美观度，是否接近参考作品的艺术感

然后给出最多3条最关键的改进建议，每条指明要调哪个参数、往哪个方向调。

严格输出JSON（不要markdown代码块）：
{{"scores": {{"water_contrast": N, "building_texture": N, "height_variation": N, "surface_quality": N, "overall_aesthetic": N}}, "issues": ["问题1", "问题2"], "suggestions": [{{"param": "参数名", "direction": "increase/decrease", "reason": "原因"}}]}}"""})

    messages = [{"role": "user", "content": content}]

    try:
        response = MultiModalConversation.call(
            model="qwen-vl-max",
            messages=messages,
            api_key=api_key,
        )

        if response.status_code == 200:
            raw = response.output.choices[0].message.content[0]["text"]
            # 解析 JSON
            result = _parse_evaluation(raw)
            # 记录历史
            _state["history"].append({
                "iteration": _state["iteration"],
                "image_path": image_path,
                "evaluation": result,
            })
            return json.dumps(result, ensure_ascii=False)
        else:
            return json.dumps({"error": f"API error: {response.code} {response.message}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─── Tool 3: 标准地图保真度检查 ────────────────────────────────


@tool
def check_against_standard_map(generated_image_path: str) -> str:
    """将生成的浮雕图与高德标准地图对比，自动检测缺失/断裂的水体要素。

    这是数据完整性的"保真度检查"（解决"数据缺口不可能一个个手动修"的问题）：
    标准地图里有、但浮雕图里缺失/断裂的河流、运河、湖泊，通常意味着
    OSM 源数据有缺口，而非渲染参数问题。

    Args:
        generated_image_path: 生成的浮雕图 PNG 路径

    Returns:
        JSON：water_fidelity 评分 + 缺失要素列表 + 标准地图来源
    """
    from dashscope import MultiModalConversation
    from relief_studio.standard_map import get_standard_map

    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return json.dumps({"error": "DASHSCOPE_API_KEY not set"})

    bbox_wgs84 = _state.get("bbox_wgs84")
    if not bbox_wgs84:
        return json.dumps({"error": "bbox_wgs84 not in state (请在 init_state 传入)"})

    # 获取地理对齐的标准地图
    std_path = os.path.join(
        _state["output_dir"], f"{_state['city_name']}_standard_map.png"
    )
    path, source = get_standard_map(bbox_wgs84, std_path)
    if path is None:
        return json.dumps({"error": "无法获取标准地图（网络不可用且无本地缓存）"})

    content = [
        {"image": f"file://{path}"},
        {"text": "上面是标准地图（高德底图，蓝色为水体）。"},
        {"image": f"file://{generated_image_path}"},
        {"text": f"""上面是生成的{_state['city_name']}建筑浮雕地图（黑色为水体，灰白为建筑）。
两张图是同一区域，方向一致（上北下南）。

请对比两张图，找出【标准地图中明显存在、但浮雕地图中缺失或断裂】的水体要素：
- 河流/运河是否中断、缺失某一段？
- 湖泊/大型水面是否缺失？
- 水系整体形状是否严重不符？

对每个发现，说明：要素类型、大致位置（如"中部偏东，南北向"）、问题描述。
同时给一个 water_fidelity 评分(1-10)：10=水系完全一致，1=大面积缺失。

严格输出JSON（不要markdown代码块）：
{{"water_fidelity": N, "missing_features": [{{"type": "河流/运河/湖泊", "location": "位置描述", "problem": "问题描述"}}], "note": "总体评价"}}"""},
    ]

    messages = [{"role": "user", "content": content}]
    try:
        response = MultiModalConversation.call(
            model="qwen-vl-max", messages=messages, api_key=api_key
        )
        if response.status_code == 200:
            raw = response.output.choices[0].message.content[0]["text"]
            result = _parse_fidelity(raw)
            result["standard_map_source"] = source
            result["standard_map_path"] = path
            return json.dumps(result, ensure_ascii=False)
        return json.dumps({"error": f"API error: {response.code} {response.message}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ─── Tool 4: 获取数据统计 ────────────────────────────────────────────────


@tool
def get_data_stats() -> str:
    """获取当前城市数据的统计信息（建筑数量、高度分布、覆盖率等）。

    Returns:
        数据统计 JSON
    """
    import numpy as np

    gdf = _state["buildings_gdf"]
    if gdf is None:
        return json.dumps({"error": "no data loaded"})

    heights = gdf["height"].dropna()
    relief = _state["relief_data"]

    stats = {
        "city": _state["city_name"],
        "building_count": len(gdf),
        "height_stats": {
            "mean": round(float(heights.mean()), 1),
            "median": round(float(heights.median()), 1),
            "p90": round(float(heights.quantile(0.9)), 1),
            "max": round(float(heights.max()), 1),
        },
        "coverage": {
            "building_pct": round(float((relief["heightmap"] > 0).mean() * 100), 1) if relief else None,
            "water_pct": round(float(relief["water_mask"].mean() * 100), 1) if relief else None,
        },
        "iterations_so_far": _state["iteration"],
        "history_scores": [
            h["evaluation"].get("scores", {}).get("overall_aesthetic", "?")
            for h in _state["history"]
        ],
    }
    return json.dumps(stats, ensure_ascii=False)


# ─── 辅助函数 ────────────────────────────────────────────────────────────


def _parse_fidelity(raw: str) -> dict:
    """解析 Qwen-VL 的保真度检查输出."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    try:
        data = json.loads(text)
        return {
            "water_fidelity": data.get("water_fidelity", 0),
            "missing_features": data.get("missing_features", []),
            "note": data.get("note", ""),
        }
    except json.JSONDecodeError:
        return {"water_fidelity": 0, "missing_features": [], "note": "parse failed", "raw": raw[:500]}


def _parse_evaluation(raw: str) -> dict:
    """解析 Qwen-VL 的评估输出."""
    text = raw.strip()
    # 去掉可能的 markdown fence
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        data = json.loads(text)
        return {
            "scores": data.get("scores", {}),
            "issues": data.get("issues", []),
            "suggestions": data.get("suggestions", []),
            "overall": data.get("scores", {}).get("overall_aesthetic", 0),
        }
    except json.JSONDecodeError:
        return {
            "scores": {},
            "issues": ["Failed to parse evaluation"],
            "suggestions": [],
            "overall": 0,
            "raw": raw[:500],
        }
