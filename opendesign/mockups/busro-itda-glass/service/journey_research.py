"""Source-grounded bus-travel research cards and catalog verification.

The research notes are discovery material, not a substitute for current TAGO
route-stop topology.  Route numbers are intentionally verified as labels only;
the planner still requires exact city/stop identifiers and ordered sequences.
"""

from __future__ import annotations

from pathlib import Path
import json
import re
from typing import Any, Mapping, Sequence


DATA_PATH = Path(__file__).resolve().parent / "data" / "journey_research.json"
MAX_CASES = 50
MAX_ROUTE_LABELS = 64
MAX_LABEL_CHARS = 32
_SAFE_LABEL = re.compile(r"^[0-9A-Za-z가-힣 .+/_-]{1,32}$")


class JourneyResearchError(ValueError):
    """Raised when the curated research catalog is malformed."""


def _text(value: Any, field: str, maximum: int = 240) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise JourneyResearchError(f"{field} is invalid")
    return result


def load_research_cases(path: Path = DATA_PATH) -> tuple[dict[str, Any], ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JourneyResearchError("journey research data is unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise JourneyResearchError("journey research data is invalid")
    if len(payload["cases"]) > MAX_CASES:
        raise JourneyResearchError("journey research catalog is too large")
    policy = payload.get("policy")
    if not isinstance(policy, dict) or int(policy.get("walk_connection_radius_m") or 0) != 300:
        raise JourneyResearchError("journey research walking policy is invalid")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(payload["cases"]):
        if not isinstance(raw, dict):
            raise JourneyResearchError(f"cases[{index}] is invalid")
        case = {
            "id": _text(raw.get("id"), f"cases[{index}].id", 80),
            "title": _text(raw.get("title"), f"cases[{index}].title", 160),
            "region": _text(raw.get("region"), f"cases[{index}].region", 240),
            "evidence_level": _text(raw.get("evidence_level"), f"cases[{index}].evidence_level", 48),
            "from_hint": _text(raw.get("from_hint"), f"cases[{index}].from_hint", 120),
            "to_hint": _text(raw.get("to_hint"), f"cases[{index}].to_hint", 120),
            "route_labels": raw.get("route_labels"),
            "notes": _text(raw.get("notes"), f"cases[{index}].notes", 400),
            "source_title": _text(raw.get("source_title"), f"cases[{index}].source_title", 200),
            "source_url": _text(raw.get("source_url"), f"cases[{index}].source_url", 512),
        }
        if not case["source_url"].startswith("https://"):
            raise JourneyResearchError(f"cases[{index}].source_url must use https")
        labels = case["route_labels"]
        if not isinstance(labels, list) or len(labels) > MAX_ROUTE_LABELS:
            raise JourneyResearchError(f"cases[{index}].route_labels is invalid")
        clean_labels: list[str] = []
        for label in labels:
            value = _text(label, f"cases[{index}].route_labels", MAX_LABEL_CHARS)
            if not _SAFE_LABEL.fullmatch(value):
                raise JourneyResearchError(f"cases[{index}].route_labels contains unsafe text")
            clean_labels.append(value)
        case["route_labels"] = clean_labels
        if "walk_gap_m" in raw:
            try:
                walk_gap = float(raw["walk_gap_m"])
            except (TypeError, ValueError) as exc:
                raise JourneyResearchError(f"cases[{index}].walk_gap_m is invalid") from exc
            if not 0 <= walk_gap <= 100_000:
                raise JourneyResearchError(f"cases[{index}].walk_gap_m is invalid")
            case["walk_gap_m"] = walk_gap
        normalized.append(case)
    return tuple(normalized)


def _label_rows(catalog: Any, labels: Sequence[str]) -> dict[str, dict[str, int]]:
    unique = tuple(dict.fromkeys(labels))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    with catalog.connect() as connection:
        static_rows = connection.execute(
            f"SELECT route_no, COUNT(*) AS count FROM catalog_routes WHERE route_no IN ({placeholders}) GROUP BY route_no",
            unique,
        ).fetchall()
        target_rows = connection.execute(
            f"""
            SELECT t.route_no,
                   COUNT(*) AS target_count,
                   SUM(CASE WHEN a.sequence_id IS NOT NULL THEN 1 ELSE 0 END) AS hydrated_count
              FROM topology_targets t
              LEFT JOIN active_route_sequences a
                ON a.city_code=t.city_code AND a.route_id=t.route_id
             WHERE t.route_no IN ({placeholders})
             GROUP BY t.route_no
            """,
            unique,
        ).fetchall()
    result = {label: {"catalog_route_count": 0, "topology_target_count": 0, "hydrated_route_count": 0} for label in unique}
    for row in static_rows:
        result[str(row["route_no"])] ["catalog_route_count"] = int(row["count"])
    for row in target_rows:
        item = result.setdefault(str(row["route_no"]), {"catalog_route_count": 0, "topology_target_count": 0, "hydrated_route_count": 0})
        item["topology_target_count"] = int(row["target_count"] or 0)
        item["hydrated_route_count"] = int(row["hydrated_count"] or 0)
    return result


def verify_research_cases(catalog: Any, cases: Sequence[Mapping[str, Any]] | None = None) -> tuple[dict[str, Any], ...]:
    rows = tuple(cases) if cases is not None else load_research_cases()
    all_labels = [label for case in rows for label in case.get("route_labels", ())]
    matches = _label_rows(catalog, all_labels)
    verified: list[dict[str, Any]] = []
    for raw in rows:
        labels = [str(label) for label in raw.get("route_labels", ())]
        label_status = []
        for label in labels:
            counts = matches.get(label, {"catalog_route_count": 0, "topology_target_count": 0, "hydrated_route_count": 0})
            if counts["hydrated_route_count"]:
                status = "HYDRATED"
            elif counts["topology_target_count"] or counts["catalog_route_count"]:
                status = "DISCOVERED_OR_STATIC"
            else:
                status = "NOT_FOUND"
            label_status.append({"label": label, "status": status, **counts})
        missing = [item["label"] for item in label_status if item["status"] == "NOT_FOUND"]
        hydrated = [item["label"] for item in label_status if item["status"] == "HYDRATED"]
        evidence = str(raw.get("evidence_level") or "")
        if evidence == "historical_report":
            verification = "RESEARCH_ONLY"
        elif evidence == "planned_report":
            verification = "PLANNED_NEEDS_LIVE_VERIFY"
        elif float(raw.get("walk_gap_m") or 0) > 300:
            verification = "WALK_GAP_OVER_300M"
        elif labels and not missing and len(hydrated) == len(labels):
            verification = "ROUTE_LABELS_HYDRATED"
        elif hydrated or any(item["status"] == "DISCOVERED_OR_STATIC" for item in label_status):
            verification = "ROUTE_LABELS_PARTIAL"
        else:
            verification = "ROUTE_LABELS_NOT_FOUND"
        verified.append({**dict(raw), "verification": verification, "route_label_status": label_status, "missing_route_labels": missing, "hydrated_route_labels": hydrated})
    return tuple(verified)


def research_report(catalog: Any, *, limit: int = 20, evidence_level: str | None = None) -> dict[str, Any]:
    if not 1 <= limit <= MAX_CASES:
        raise JourneyResearchError("limit must be 1..50")
    cases = list(load_research_cases())
    if evidence_level:
        evidence_level = _text(evidence_level, "evidence_level", 48)
        cases = [case for case in cases if case["evidence_level"] == evidence_level]
    verified = list(verify_research_cases(catalog, cases[:limit]))
    hydrated = sum(1 for case in verified if case["verification"] == "ROUTE_LABELS_HYDRATED")
    return {
        "ok": True,
        "source": "CURATED_PUBLIC_TRAVEL_REPORTS_PLUS_CURRENT_TAGO_CATALOG_LABEL_CHECK",
        "count": len(verified),
        "verified_route_label_cases": hydrated,
        "policy": {"walk_connection_radius_m": 300, "route_labels_are_not_ids": True},
        "cases": verified,
    }


__all__ = ["DATA_PATH", "JourneyResearchError", "load_research_cases", "verify_research_cases", "research_report"]
