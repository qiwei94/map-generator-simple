"""Cached Wikidata height lookup for OSM-tagged landmark buildings.

Only buildings already carrying an OSM ``wikidata=Q...`` identity are queried.
The small Wikibase API response, successful height, and negative result are all
persisted so normal generation does not depend on repeated network requests.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .building_height_store import BuildingHeightStore


logger = logging.getLogger(__name__)
_API_URL = "https://www.wikidata.org/w/api.php"
_SPARQL_URL = "https://query.wikidata.org/sparql"
_ENTITY_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
_QID_RE = re.compile(r"\bQ\d+\b", re.IGNORECASE)
_STORE_FILENAME = "building_heights.sqlite3"
_UNIT_TO_METERS = {
    "Q11573": 1.0,       # metre
    "Q174728": 0.01,     # centimetre
    "Q174789": 0.001,    # millimetre
    "Q3710": 0.3048,     # foot
    "Q218593": 0.0254,   # inch
    "Q828224": 1000.0,   # kilometre
}


def _qid(value) -> Optional[str]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    match = _QID_RE.search(str(value))
    return match.group(0).upper() if match else None


def _height_from_entity(entity: dict) -> Optional[float]:
    claims = entity.get("claims", {}).get("P2048", [])
    claims = sorted(
        claims,
        key=lambda claim: {"preferred": 0, "normal": 1, "deprecated": 2}.get(
            claim.get("rank"), 1),
    )
    for claim in claims:
        if claim.get("rank") == "deprecated":
            continue
        value = (
            claim.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        )
        try:
            amount = float(value.get("amount"))
        except (TypeError, ValueError):
            continue
        unit_qid = str(value.get("unit", "")).rsplit("/", 1)[-1]
        factor = _UNIT_TO_METERS.get(unit_qid)
        if factor is None:
            continue
        height_m = amount * factor
        if 0 < height_m <= 2000:
            return height_m
    return None


def _label_from_entity(entity: dict) -> Optional[str]:
    labels = entity.get("labels", {})
    for language in ("zh-cn", "zh", "en"):
        value = labels.get(language, {}).get("value")
        if value:
            return value
    for label in labels.values():
        if isinstance(label, dict) and label.get("value"):
            return label["value"]
    return None


def _fetch_entities(
    qids: Sequence[str], *, session=None, timeout: float = 20.0,
) -> Dict[str, dict]:
    """Fetch entities in bounded Wikibase API batches (maximum 50 ids)."""
    import requests

    client = session or requests.Session()
    entities: Dict[str, dict] = {}
    for offset in range(0, len(qids), 50):
        batch = list(qids[offset:offset + 50])
        response = client.get(
            _API_URL,
            params={
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "claims|labels",
                "languages": "zh-cn|zh|en",
                "format": "json",
                "formatversion": "2",
            },
            headers={
                "User-Agent": "map-generator-simple/height-cache "
                              "(building height enrichment)"
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        entities.update(payload.get("entities", {}))
    return entities


def prefetch_wikidata_landmarks(
    qids: Sequence[str],
    *,
    cache_dir: str,
    session=None,
    timeout: float = 20.0,
) -> Tuple[dict, dict]:
    """Fetch uncached landmark entities and return a summary plus all records.

    Queries are deliberately split into independent 50-entity requests. A
    failed batch is recorded without discarding successful batches, while
    both height hits and missing-height entities become durable cache rows.
    """
    normalized = sorted({_qid(qid) for qid in qids if _qid(qid)})
    os.makedirs(cache_dir, exist_ok=True)
    store = BuildingHeightStore(os.path.join(cache_dir, _STORE_FILENAME))
    cached_before = store.get_landmarks(normalized)
    missing = [qid for qid in normalized if qid not in cached_before]
    batch_count = 0
    errors = []

    for offset in range(0, len(missing), 50):
        batch = missing[offset:offset + 50]
        batch_count += 1
        request_key = hashlib.sha256("|".join(batch).encode("ascii")).hexdigest()
        try:
            entities = _fetch_entities(
                batch, session=session, timeout=timeout)
            store.put_request(
                "wikidata", request_key, status="ok", response_json=entities)
            for qid in batch:
                entity = entities.get(qid, {})
                height_m = _height_from_entity(entity)
                store.put_landmark(
                    qid,
                    status="ok" if height_m is not None else "missing",
                    height_m=height_m,
                    label=_label_from_entity(entity),
                    confidence=0.9 if height_m is not None else None,
                    source_url=_ENTITY_URL.format(qid=qid),
                    raw=entity,
                )
        except Exception as exc:
            store.put_request(
                "wikidata", request_key, status="error", error=str(exc))
            errors.append({"qids": batch, "error": str(exc)})
            logger.warning("Wikidata height lookup batch failed: %s", exc)

    records = store.get_landmarks(normalized)
    summary = {
        "qid_count": len(normalized),
        "cached_before": len(cached_before),
        "api_requested": len(missing),
        "api_batches": batch_count,
        "api_errors": len(errors),
        "height_hits": sum(
            1 for row in records.values() if row.get("status") == "ok"),
        "negative_cached": sum(
            1 for row in records.values() if row.get("status") == "missing"),
        "unresolved": len(normalized) - len(records),
        "errors": errors,
    }
    return summary, records


def _fetch_sparql_heights(
    qids: Sequence[str], *, session=None, timeout: float = 30.0,
) -> Tuple[dict, dict]:
    """Return normalized P2048 values and the compact SPARQL response."""
    import requests

    values = " ".join(f"wd:{qid}" for qid in qids)
    query = (
        "SELECT ?item ?height WHERE { VALUES ?item { " + values +
        " } ?item wdt:P2048 ?height . }")
    client = session or requests.Session()
    response = client.get(
        _SPARQL_URL,
        params={"query": query, "format": "json"},
        headers={
            "User-Agent": "map-generator-simple/height-cache "
                          "(building height enrichment)"
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    heights = {}
    for binding in payload.get("results", {}).get("bindings", []):
        qid = _qid(binding.get("item", {}).get("value"))
        try:
            height_m = float(binding.get("height", {}).get("value"))
        except (TypeError, ValueError):
            continue
        if qid and 0 < height_m <= 2000:
            heights[qid] = max(height_m, heights.get(qid, 0.0))
    return heights, payload


def prefetch_wikidata_landmarks_sparql(
    qids: Sequence[str],
    *,
    cache_dir: str,
    session=None,
    timeout: float = 30.0,
    batch_size: int = 200,
    delay_s: float = 0.15,
) -> Tuple[dict, dict]:
    """Discover P2048 in compact batches, then hydrate only positive hits."""
    normalized = sorted({_qid(qid) for qid in qids if _qid(qid)})
    os.makedirs(cache_dir, exist_ok=True)
    store = BuildingHeightStore(os.path.join(cache_dir, _STORE_FILENAME))
    cached_before = store.get_landmarks(normalized)
    missing = [qid for qid in normalized if qid not in cached_before]
    errors = []
    positive_qids = set()
    sparql_batches = 0

    batch_size = max(1, min(int(batch_size), 500))
    for offset in range(0, len(missing), batch_size):
        batch = missing[offset:offset + batch_size]
        sparql_batches += 1
        request_key = hashlib.sha256("|".join(batch).encode("ascii")).hexdigest()
        try:
            heights, payload = _fetch_sparql_heights(
                batch, session=session, timeout=timeout)
            store.put_request(
                "wikidata_sparql", request_key, status="ok",
                response_json=payload)
            positive_qids.update(heights)
            for qid in batch:
                if qid not in heights:
                    store.put_landmark(
                        qid, status="missing",
                        source_url=_ENTITY_URL.format(qid=qid),
                        raw={"provider": "wikidata_sparql", "property": "P2048",
                             "matched": False},
                    )
                    continue
                store.put_landmark(
                    qid,
                    status="ok",
                    height_m=heights[qid],
                    confidence=0.9,
                    source_url=_ENTITY_URL.format(qid=qid),
                    raw={
                        "provider": "wikidata_sparql", "property": "P2048",
                        "height_m": heights[qid],
                    },
                )
        except Exception as exc:
            store.put_request(
                "wikidata_sparql", request_key, status="error", error=str(exc))
            errors.append({"qids": batch, "error": str(exc)})
            logger.warning("Wikidata SPARQL height batch failed: %s", exc)
        if delay_s and offset + batch_size < len(missing):
            time.sleep(delay_s)

    records = store.get_landmarks(normalized)
    hydrate_qids = sorted(
        qid for qid, row in records.items()
        if row.get("status") == "ok" and not row.get("label")
    )
    hydration_errors = []
    hydrated = 0
    deferred = 0
    for offset in range(0, len(hydrate_qids), 50):
        batch = hydrate_qids[offset:offset + 50]
        request_key = hashlib.sha256("|".join(batch).encode("ascii")).hexdigest()
        try:
            entities = _fetch_entities(
                batch, session=session, timeout=timeout)
            store.put_request(
                "wikidata", request_key, status="ok", response_json=entities)
            for qid in batch:
                entity = entities.get(qid, {})
                existing = records[qid]
                store.put_landmark(
                    qid,
                    status="ok",
                    height_m=_height_from_entity(entity) or existing["height_m"],
                    label=_label_from_entity(entity),
                    confidence=0.9,
                    source_url=_ENTITY_URL.format(qid=qid),
                    raw=entity or {
                        "provider": "wikidata_sparql", "property": "P2048",
                        "height_m": existing["height_m"],
                    },
                )
                hydrated += 1
        except Exception as exc:
            store.put_request(
                "wikidata", request_key, status="error", error=str(exc))
            hydration_errors.append({"qids": batch, "error": str(exc)})
            logger.warning("Wikidata positive entity hydration failed: %s", exc)
            if "429" in str(exc):
                deferred = len(hydrate_qids) - offset
                break
        if delay_s and offset + 50 < len(hydrate_qids):
            time.sleep(max(delay_s, 0.5))

    records = store.get_landmarks(normalized)
    summary = {
        "qid_count": len(normalized),
        "cached_before": len(cached_before),
        "api_requested": len(missing),
        "api_batches": sparql_batches,
        "api_errors": len(errors),
        "positive_discovered": len(positive_qids),
        "entity_qids": len(hydrate_qids),
        "entity_hydrated": hydrated,
        "entity_hydration_errors": len(hydration_errors),
        "entity_hydration_deferred": deferred,
        "height_hits": sum(
            1 for row in records.values() if row.get("status") == "ok"),
        "negative_cached": sum(
            1 for row in records.values() if row.get("status") == "missing"),
        "unresolved": len(normalized) - len(records),
        "errors": errors,
        "hydration_errors": hydration_errors,
    }
    return summary, records


def load_wikidata_heights(
    buildings_gdf,
    *,
    cache_dir: str,
    auto_fetch: bool = False,
    session=None,
) -> Tuple[pd.Series, pd.Series]:
    """Return cached/fetched landmark heights aligned to ``buildings_gdf``."""
    heights = pd.Series(np.nan, index=buildings_gdf.index, dtype=float)
    labels = pd.Series(np.nan, index=buildings_gdf.index, dtype=object)
    if buildings_gdf is None or buildings_gdf.empty or "wikidata" not in buildings_gdf:
        return heights, labels

    os.makedirs(cache_dir, exist_ok=True)
    store = BuildingHeightStore(os.path.join(cache_dir, _STORE_FILENAME))
    row_qids = buildings_gdf["wikidata"].apply(_qid)
    qids = sorted({qid for qid in row_qids if qid})
    cached = store.get_landmarks(qids)
    missing = [qid for qid in qids if qid not in cached]

    env_auto = os.environ.get("WIKIDATA_HEIGHT_AUTO_FETCH", "").strip()
    if env_auto:
        auto_fetch = env_auto.lower() in {"1", "true", "yes", "on"}

    if missing and auto_fetch:
        _, cached = prefetch_wikidata_landmarks(
            qids, cache_dir=cache_dir, session=session)

    for idx, qid in row_qids.items():
        if not qid:
            continue
        record = cached.get(qid)
        if not record or record.get("status") != "ok":
            continue
        height_m = record.get("height_m")
        if height_m is not None:
            heights.loc[idx] = float(height_m)
        if record.get("label"):
            labels.loc[idx] = record["label"]

    logger.info(
        "Wikidata landmark heights: %d tagged, %d height hits, %d cached negatives",
        len(qids), int(heights.notna().sum()),
        sum(1 for row in cached.values() if row.get("status") == "missing"),
    )
    return heights, labels
