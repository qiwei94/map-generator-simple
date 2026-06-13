"""Auto-parameter system for city-adaptive map generation.

Public API:
    detect_city_profile()  — detect features from GeoDataFrames
    resolve_params()       — rules engine: CityProfile → ResolvedParams
    save_decision_report() — write param_decision.json
    ai_review_png()        — AI vision review loop (requires ANTHROPIC_API_KEY)
    ai_art_direction()     — AI style strategy for new cities
"""

from .city_profile import CityProfile, detect_city_profile
from .param_resolver import ResolvedParams, resolve_params, explain_decisions
from .decision_report import save_decision_report

__all__ = [
    "CityProfile",
    "detect_city_profile",
    "ResolvedParams",
    "resolve_params",
    "explain_decisions",
    "save_decision_report",
]
