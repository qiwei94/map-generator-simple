"""Persistent, source-aware building height storage.

The generation pipeline consumes small bounding boxes repeatedly.  Remote
height providers should therefore be treated as import sources, not runtime
dependencies: raw responses stay on disk and normalized observations are
indexed in SQLite for later spatial reuse.

The store deliberately keeps observations from different providers separate.
Choosing a winning height remains a deterministic pipeline decision; ingesting
new data never silently overwrites another source's value.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
from typing import Iterable, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _bbox_tuple(bbox: Sequence[float]) -> Tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("bbox must be (south, west, north, east)")
    south, west, north, east = map(float, bbox)
    if south > north or west > east:
        raise ValueError("invalid bbox ordering")
    return south, west, north, east


class BuildingHeightStore:
    """SQLite repository for raw-query metadata and normalized heights."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;

                CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_coverage (
                    id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_release TEXT NOT NULL DEFAULT 'unknown',
                    south REAL NOT NULL,
                    west REAL NOT NULL,
                    north REAL NOT NULL,
                    east REAL NOT NULL,
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    raw_path TEXT,
                    raw_sha256 TEXT,
                    retrieved_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(source, source_release, south, west, north, east)
                );
                CREATE INDEX IF NOT EXISTS idx_coverage_source
                    ON source_coverage(source, source_release);

                CREATE TABLE IF NOT EXISTS height_observations (
                    id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_feature_id TEXT NOT NULL,
                    source_release TEXT NOT NULL DEFAULT 'unknown',
                    height_m REAL NOT NULL,
                    num_floors REAL,
                    height_kind TEXT NOT NULL DEFAULT 'estimated',
                    confidence REAL,
                    name TEXT,
                    geom_wkb BLOB NOT NULL,
                    minx REAL NOT NULL,
                    miny REAL NOT NULL,
                    maxx REAL NOT NULL,
                    maxy REAL NOT NULL,
                    source_url TEXT,
                    retrieved_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(source, source_release, source_feature_id)
                );
                CREATE INDEX IF NOT EXISTS idx_height_source_feature
                    ON height_observations(source, source_feature_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS height_observations_rtree
                    USING rtree(id, minx, maxx, miny, maxy);

                CREATE TABLE IF NOT EXISTS landmark_heights (
                    qid TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    height_m REAL,
                    label TEXT,
                    confidence REAL,
                    source_url TEXT,
                    retrieved_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS request_cache (
                    source TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT,
                    response_path TEXT,
                    retrieved_at TEXT NOT NULL,
                    expires_at TEXT,
                    error TEXT,
                    PRIMARY KEY(source, request_key)
                );
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO store_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    @staticmethod
    def file_sha256(path: Optional[str]) -> Optional[str]:
        if not path or not os.path.isfile(path):
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def register_coverage(
        self,
        source: str,
        bbox: Sequence[float],
        *,
        source_release: str = "unknown",
        observation_count: int = 0,
        raw_path: Optional[str] = None,
        metadata: Optional[Mapping] = None,
    ) -> None:
        south, west, north, east = _bbox_tuple(bbox)
        absolute_raw = os.path.abspath(raw_path) if raw_path else None
        checksum = self.file_sha256(absolute_raw)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_coverage(
                    source, source_release, south, west, north, east,
                    observation_count, raw_path, raw_sha256, retrieved_at,
                    metadata_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_release, south, west, north, east)
                DO UPDATE SET
                    observation_count=excluded.observation_count,
                    raw_path=COALESCE(excluded.raw_path, source_coverage.raw_path),
                    raw_sha256=COALESCE(excluded.raw_sha256,
                                        source_coverage.raw_sha256),
                    retrieved_at=excluded.retrieved_at,
                    metadata_json=excluded.metadata_json
                """,
                (source, source_release, south, west, north, east,
                 int(observation_count), absolute_raw, checksum, _utcnow(),
                 _json(metadata)),
            )

    def covers_bbox(self, source: str, bbox: Sequence[float]) -> bool:
        south, west, north, east = _bbox_tuple(bbox)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM source_coverage
                WHERE source=? AND south<=? AND west<=? AND north>=? AND east>=?
                LIMIT 1
                """,
                (source, south, west, north, east),
            ).fetchone()
        return row is not None

    def put_observations(
        self,
        observations: Iterable[Mapping],
        *,
        source: str,
        source_release: str = "unknown",
        source_url: Optional[str] = None,
    ) -> int:
        """Upsert WGS84 observations and return the number accepted."""
        rows = []
        for item in observations:
            geom = item.get("geometry")
            height = item.get("height_m")
            if geom is None or getattr(geom, "is_empty", True):
                continue
            try:
                height = float(height)
            except (TypeError, ValueError):
                continue
            if not (0 < height <= 2000):
                continue
            minx, miny, maxx, maxy = map(float, geom.bounds)
            feature_id = str(item.get("source_feature_id") or "").strip()
            geom_wkb = bytes(geom.wkb)
            if not feature_id:
                feature_id = hashlib.sha256(geom_wkb).hexdigest()
            rows.append((
                source, feature_id, source_release, height,
                item.get("num_floors"),
                str(item.get("height_kind") or "estimated"),
                item.get("confidence"), item.get("name"), geom_wkb,
                minx, miny, maxx, maxy,
                item.get("source_url") or source_url, _utcnow(),
                _json(item.get("metadata")),
            ))

        if not rows:
            return 0

        with self.connect() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO height_observations(
                        source, source_feature_id, source_release, height_m,
                        num_floors, height_kind, confidence, name, geom_wkb,
                        minx, miny, maxx, maxy, source_url, retrieved_at,
                        metadata_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, source_release, source_feature_id)
                    DO UPDATE SET
                        height_m=excluded.height_m,
                        num_floors=excluded.num_floors,
                        height_kind=excluded.height_kind,
                        confidence=excluded.confidence,
                        name=excluded.name,
                        geom_wkb=excluded.geom_wkb,
                        minx=excluded.minx, miny=excluded.miny,
                        maxx=excluded.maxx, maxy=excluded.maxy,
                        source_url=excluded.source_url,
                        retrieved_at=excluded.retrieved_at,
                        metadata_json=excluded.metadata_json
                    """,
                    row,
                )
                obs_id = conn.execute(
                    """
                    SELECT id FROM height_observations
                    WHERE source=? AND source_release=? AND source_feature_id=?
                    """,
                    (row[0], row[2], row[1]),
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT OR REPLACE INTO height_observations_rtree(
                        id, minx, maxx, miny, maxy
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (obs_id, row[9], row[11], row[10], row[12]),
                )
        return len(rows)

    def query_bbox(self, source: str, bbox: Sequence[float]):
        """Return observations intersecting bbox as a WGS84 GeoDataFrame."""
        import geopandas as gpd
        from shapely import wkb

        south, west, north, east = _bbox_tuple(bbox)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT h.* FROM height_observations AS h
                JOIN height_observations_rtree AS r ON r.id=h.id
                WHERE h.source=?
                  AND r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=?
                ORDER BY h.retrieved_at DESC, h.id DESC
                """,
                (source, east, west, north, south),
            ).fetchall()
        # A feature can exist in more than one release.  Prefer the latest
        # retrieved observation without destroying the older source record.
        seen = set()
        records = []
        for row in rows:
            key = row["source_feature_id"]
            if key in seen:
                continue
            seen.add(key)
            records.append({
                "source_feature_id": key,
                "source_release": row["source_release"],
                "height": row["height_m"],
                "num_floors": row["num_floors"],
                "height_kind": row["height_kind"],
                "confidence": row["confidence"],
                "name": row["name"],
                "geometry": wkb.loads(row["geom_wkb"]),
            })
        if not records:
            return gpd.GeoDataFrame(
                columns=["source_feature_id", "source_release", "height",
                         "num_floors", "height_kind", "confidence", "name",
                         "geometry"],
                geometry="geometry", crs="EPSG:4326",
            )
        return gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")

    def put_landmark(
        self,
        qid: str,
        *,
        status: str,
        height_m: Optional[float] = None,
        label: Optional[str] = None,
        confidence: Optional[float] = None,
        source_url: Optional[str] = None,
        raw: Optional[Mapping] = None,
    ) -> None:
        qid = str(qid).upper().strip()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO landmark_heights(
                    qid, status, height_m, label, confidence, source_url,
                    retrieved_at, raw_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(qid) DO UPDATE SET
                    status=excluded.status,
                    height_m=excluded.height_m,
                    label=excluded.label,
                    confidence=excluded.confidence,
                    source_url=excluded.source_url,
                    retrieved_at=excluded.retrieved_at,
                    raw_json=excluded.raw_json
                """,
                (qid, status, height_m, label, confidence, source_url,
                 _utcnow(), _json(raw)),
            )

    def get_landmarks(self, qids: Sequence[str]) -> dict:
        normalized = sorted({str(q).upper().strip() for q in qids if q})
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM landmark_heights WHERE qid IN ({placeholders})",
                normalized,
            ).fetchall()
        return {row["qid"]: dict(row) for row in rows}

    def put_request(
        self,
        source: str,
        request_key: str,
        *,
        status: str,
        response_json=None,
        response_path: Optional[str] = None,
        expires_at: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO request_cache(
                    source, request_key, status, response_json, response_path,
                    retrieved_at, expires_at, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, request_key) DO UPDATE SET
                    status=excluded.status,
                    response_json=excluded.response_json,
                    response_path=excluded.response_path,
                    retrieved_at=excluded.retrieved_at,
                    expires_at=excluded.expires_at,
                    error=excluded.error
                """,
                (source, request_key, status,
                 _json(response_json) if response_json is not None else None,
                 response_path, _utcnow(), expires_at, error),
            )
