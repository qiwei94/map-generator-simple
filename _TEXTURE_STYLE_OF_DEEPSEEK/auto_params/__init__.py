"""Auto-parameter system for city-adaptive map generation.

Public API:
    detect_city_profile()  — detect features from GeoDataFrames
    resolve_params()       — rules engine: CityProfile → ResolvedParams
    save_decision_report() — write param_decision.json
    ai_review_png()        — AI vision review loop (requires ANTHROPIC_API_KEY)
    ai_art_direction()     — AI style strategy for new cities
    PreferenceStore        — user preference learning (JSONL log)
"""

from .city_profile import CityProfile, detect_city_profile
from .param_resolver import ResolvedParams, resolve_params, explain_decisions
from .decision_report import save_decision_report
from .ai_review import ai_review_png, ReviewResult
from .ai_art_direction import ai_art_direction
from .preference_store import PreferenceStore, PreferenceRecord

__all__ = [
    "CityProfile",
    "detect_city_profile",
    "ResolvedParams",
    "resolve_params",
    "explain_decisions",
    "save_decision_report",
    "ai_review_png",
    "ReviewResult",
    "ai_art_direction",
    "PreferenceStore",
    "PreferenceRecord",
]
