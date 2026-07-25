"""LangChain ReAct Agent：自动迭代优化建筑浮雕参数.

工作流：
1. 生成初始浮雕图
2. Qwen-VL 视觉评估（对比参考作品）
3. 根据评估建议调参
4. 重新生成 → 重新评估
5. 循环直到 overall >= 7 或达到最大迭代次数
"""

import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_community.chat_models.tongyi import ChatTongyi
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.prebuilt import create_react_agent

from relief_studio.agent.tools import (
    generate_relief,
    evaluate_image,
    check_against_standard_map,
    get_data_stats,
    init_state,
    _state,
)


SYSTEM_PROMPT = """你是一位城市建筑浮雕地图的艺术总监 AI。你的任务是通过迭代调参，生成尽可能接近参考作品水准的建筑浮雕地图。

## 目标美学（对标 Reso/lution Urban Series）
- 水体 = 纯黑，与陆地形成极端对比
- 建筑 = 白→灰渐变，高度越高越亮
- 建筑纹理致密连续，道路是缝隙/暗线
- 市中心（高楼区）明显高于周围，形成视觉焦点
- 表面质感：石膏/树脂感，不能太粗糙像素化
- 整体是"艺术品"而非"工程图"

## 可调参数及效果
- z_exaggeration (1-8): 高度夸张。越大3D感越强，但过大会失真
- light_azimuth (0-360): 光源方向。315=西北光（默认最佳）
- light_altitude (20-70): 光源仰角。越低阴影越长
- height_gamma (0.3-1.0): 高度映射。越小→高楼更亮、矮楼更灰（对比更强）
- ao_strength (0-0.6): 缝隙暗化。越大→建筑间暗沟越深
- edge_strength (0-0.5): 边缘暗线。越大→轮廓越清晰
- grain_strength (0-0.05): 表面颗粒。越小越光滑
- style: mono_light（白底黑水）或 mono_dark（黑底灰白建筑）

## 工作流程
1. 先用 get_data_stats 了解数据特征
2. 根据数据特征选择初始参数，调用 generate_relief
3. 调用 evaluate_image 评估生成结果（美学，对比参考作品）
4. 调用 check_against_standard_map 检查数据保真度（对比高德标准地图，
   检测缺失/断裂的河流、运河、湖泊）——数据缺口不可能一个个手动修，
   靠这一步自动发现
5. 根据评估反馈调整参数，重新生成
6. 重复直到 overall_aesthetic >= 7 或迭代 4 轮

## 决策规则
- 如果 building_texture 分低（<6）：增加 dilation 或降低 grain
- 如果 height_variation 分低：降低 height_gamma 或增大 z_exaggeration
- 如果 surface_quality 分低：降低 grain_strength
- 如果 water_contrast 分低：检查 style 是否合适
- 如果 water_fidelity 分低（<7）：说明有水体缺失/断裂，这是数据问题
  而非参数问题，在最终报告中明确列出 missing_features（需人工/数据补全）
- 每次最多调 2-3 个参数，避免震荡
"""


def create_relief_agent(model_name: str = "qwen-max"):
    """创建 LangChain ReAct agent.

    Args:
        model_name: 通义千问模型名
            - "qwen-max": 最强推理（用于参数决策）
            - "qwen-vl-max": 视觉模型（用于图像评估，在 tool 内部调用）

    Returns:
        LangGraph agent
    """
    llm = ChatTongyi(
        model=model_name,
        temperature=0.1,  # 低温度 = 确定性决策
        dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
    )

    tools = [generate_relief, evaluate_image, check_against_standard_map, get_data_stats]

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=SYSTEM_PROMPT,
    )

    return agent


def run_agent(
    city_name: str,
    buildings_gdf,
    water_gdf,
    bbox_utm: tuple,
    utm_epsg: int,
    reference_image_path: str | None = None,
    output_dir: str = "",
    max_iterations: int = 4,
    target_score: int = 7,
    bbox_wgs84: tuple | None = None,
) -> dict:
    """运行 agent 完成自动迭代优化.

    Args:
        city_name: 城市名
        buildings_gdf: 建筑 GeoDataFrame (UTM)
        water_gdf: 水体 GeoDataFrame (UTM)
        bbox_utm: UTM bbox
        utm_epsg: UTM EPSG
        reference_image_path: 参考作品图片路径
        output_dir: 输出目录
        max_iterations: 最大迭代轮数
        target_score: 目标分数
        bbox_wgs84: (min_lon, min_lat, max_lon, max_lat)，供标准地图保真度检查使用

    Returns:
        最终结果 dict
    """
    # 初始化状态
    init_state(
        city_name=city_name,
        buildings_gdf=buildings_gdf,
        water_gdf=water_gdf,
        bbox_utm=bbox_utm,
        utm_epsg=utm_epsg,
        reference_image_path=reference_image_path,
        output_dir=output_dir,
        bbox_wgs84=bbox_wgs84,
    )

    # 创建 agent
    agent = create_relief_agent()

    # 构建用户消息
    user_msg = f"""请为 {city_name} 生成建筑浮雕地图。

数据已加载完毕。参考作品路径: {reference_image_path or '无'}
输出目录: {output_dir}
目标: overall_aesthetic >= {target_score}，最多迭代 {max_iterations} 轮。

请开始工作：先查看数据统计，然后生成→评估→调参→再生成，直到达标。"""

    # 运行 agent
    print(f"\n{'='*60}")
    print(f"  Relief Agent 启动 — {city_name}")
    print(f"  目标: score >= {target_score}, max_iter = {max_iterations}")
    print(f"{'='*60}\n")

    result = agent.invoke(
        {"messages": [HumanMessage(content=user_msg)]},
        config={"recursion_limit": max_iterations * 6 + 10},
    )

    # 提取最终结果
    final_msg = result["messages"][-1].content if result["messages"] else ""

    return {
        "city": city_name,
        "iterations": _state["iteration"],
        "history": _state["history"],
        "final_message": final_msg,
        "best_score": max(
            (h["evaluation"].get("overall", 0) for h in _state["history"]),
            default=0,
        ),
    }
