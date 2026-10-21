from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import MspFact
from scripts.audit_semantic_mapping import audit_national, audit_regional


def audit_all_imported_sources(session: Session) -> dict[str, Any]:
    """Audit all 1.09M facts plus every usable regional source column."""

    expected_facts = int(session.scalar(select(func.count(MspFact.id))) or 0)
    national = audit_national(include_all_imported=True).as_dict()
    regional = audit_regional().as_dict()
    audited_facts = int(national["counts"].get("database_facts", 0))
    fact_count_matches = audited_facts == expected_facts
    mapping_issue_count = sum(national["issues"].values()) + sum(
        regional["issues"].values()
    )
    return {
        "expected_imported_facts": expected_facts,
        "audited_imported_facts": audited_facts,
        "fact_count_matches": fact_count_matches,
        "semantic_mapping_issues": mapping_issue_count,
        "passed": fact_count_matches and mapping_issue_count == 0,
        "national": national,
        "regional": regional,
    }
