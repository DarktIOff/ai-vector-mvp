from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .manifest import CapabilityManifest
from .verifier import VerificationIssue


ABSOLUTE_PATH_RE = re.compile(r"/(?:Users|home|var|private|tmp)/[^\s\"']+")
SENSITIVE_KEYS = {
    "absolute_path",
    "path",
    "sql",
    "api_key",
    "source_cell",
    "source_sheet",
}


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(record, ensure_ascii=False, default=str) + "\n"
            )


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Persist one completed case immediately for crash-safe resume."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(record, ensure_ascii=False, default=str) + "\n"
        )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize(item)
            for key, item in value.items()
            if key.casefold() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return ABSOLUTE_PATH_RE.sub("<redacted-path>", value)
    return value


def anonymized_replay(record: dict[str, Any]) -> dict[str, Any]:
    case = record.get("case") or {}
    public_id = "replay-" + hashlib.sha256(
        str(case.get("case_id", "")).encode("utf-8")
    ).hexdigest()[:12]
    response = record.get("response") or {}
    result = response.get("result") or {}
    return _sanitize(
        {
            "replay_id": public_id,
            "case_id": case.get("case_id"),
            "history": [
                turn.get("question") for turn in case.get("turns", [])
            ],
            "expected_filters": (record.get("oracle") or {}).get("filters"),
            "actual_filters": result.get("query_plan"),
            "numeric_differences": (record.get("verdict") or {}).get(
                "numeric_differences", []
            ),
            "issues": (record.get("verdict") or {}).get("issues", []),
            "tool_journal": (result.get("execution") or {}).get(
                "tool_journal", []
            ),
            "semantic_query": (result.get("execution") or {}).get(
                "semantic_query"
            ),
            "shadow_diff": (result.get("execution") or {}).get(
                "shadow_diff"
            ),
            "provider": (result.get("execution") or {}).get("provider"),
            "model": (result.get("execution") or {}).get("model"),
            "duration_ms": (result.get("execution") or {}).get(
                "duration_ms"
            ),
            "reproduce": (
                "analytic-certification --case "
                + str(case.get("case_id", ""))
            ),
        }
    )


def coverage_summary(
    records: list[dict[str, Any]],
    manifest: CapabilityManifest,
    *,
    selected_count: int,
    source_audit: dict[str, Any] | None = None,
    metamorphic_issues: list[VerificationIssue] | None = None,
) -> dict[str, Any]:
    completed = [record for record in records if record.get("verdict")]
    passed = [
        record
        for record in completed
        if (record.get("verdict") or {}).get("passed") is True
    ]
    categories = Counter(
        (record.get("case") or {}).get("category", "unknown")
        for record in completed
    )
    failures = Counter(
        issue.get("code", "unknown")
        for record in completed
        for issue in (record.get("verdict") or {}).get("issues", [])
    )
    root_cause_map = {
        "query_plan_filter_mismatch": "entity_resolution_or_context",
        "missing_analysis": "planning",
        "missing_tool": "planning",
        "forbidden_entity": "context_or_security",
        "silent_substitution": "availability_or_period",
        "numeric_mismatch": "calculation",
        "distribution_mismatch": "calculation",
        "missing_evidence_row": "calculation_or_completeness",
        "invalid_claim_evidence": "text_synthesis",
        "claim_number_without_evidence": "text_synthesis",
        "claim_entity_without_evidence": "text_synthesis",
        "invalid_hypothesis": "text_synthesis",
        "missing_chart": "visualization",
        "chart_without_series": "visualization",
        "chart_evidence_mismatch": "visualization",
        "missing_table": "visualization",
        "table_entities_mismatch": "visualization",
        "provider_error": "provider",
        "provider_fallback": "provider",
        "provider_retry": "provider",
        "model_mismatch": "provider",
        "no_model_calls": "provider",
        "runner_error": "runner_transport",
        "cleanup_failed": "runner_cleanup",
    }
    root_causes = Counter(
        root_cause_map.get(issue.get("code"), "other")
        for record in completed
        for issue in (record.get("verdict") or {}).get("issues", [])
    )
    covered_metrics = {
        code
        for record in completed
        for turn in (record.get("case") or {}).get("turns", [])
        for code in turn.get("metric_codes", [])
    }
    covered_rows = {
        int(code)
        for record in completed
        for turn in (record.get("case") or {}).get("turns", [])
        for code in turn.get("row_codes", [])
    }
    covered_territories = {
        name
        for record in completed
        for turn in (record.get("case") or {}).get("turns", [])
        for name in turn.get("territories", [])
    }
    meta_issues = metamorphic_issues or []
    source_status = (
        "not_run"
        if source_audit is None
        else "passed"
        if source_audit.get("passed") is True
        else "failed"
    )
    source_passed = source_audit is None or source_status == "passed"
    release_passed = (
        len(completed) == selected_count
        and len(passed) == selected_count
        and not meta_issues
        and source_passed
    )
    return {
        "selected": selected_count,
        "completed": len(completed),
        "passed": len(passed),
        "failed": len(completed) - len(passed),
        "first_attempt_pass_rate": (
            len(passed) / selected_count if selected_count else 0
        ),
        "release_passed": release_passed,
        "categories": dict(sorted(categories.items())),
        "failure_clusters": dict(failures.most_common()),
        "root_cause_clusters": dict(root_causes.most_common()),
        "catalog": {
            "metrics": {
                "covered": len(covered_metrics),
                "total": len(manifest.metrics),
            },
            "rows": {
                "covered": len(covered_rows),
                "total": len(manifest.rows),
            },
            "territories": {
                "covered": len(covered_territories),
                "total": len(manifest.territories),
            },
        },
        "metamorphic_issues": [item.to_dict() for item in meta_issues],
        "source_audit_status": source_status,
        "source_audit_passed": (
            None if source_audit is None else source_status == "passed"
        ),
        "data_fingerprint": manifest.data_fingerprint,
    }


def write_junit(path: Path, records: list[dict[str, Any]]) -> None:
    failures = sum(
        not (record.get("verdict") or {}).get("passed", False)
        for record in records
    )
    suite = ET.Element(
        "testsuite",
        {
            "name": "analytic-certification",
            "tests": str(len(records)),
            "failures": str(failures),
            "errors": "0",
        },
    )
    for record in records:
        case = record.get("case") or {}
        verdict = record.get("verdict") or {}
        testcase = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": str(case.get("category", "certification")),
                "name": str(case.get("case_id", "unknown")),
                "time": str(float(record.get("duration_ms", 0)) / 1000),
            },
        )
        if not verdict.get("passed", False):
            failure = ET.SubElement(
                testcase,
                "failure",
                {"message": "analytic certification failed"},
            )
            failure.text = json.dumps(
                verdict.get("issues", []), ensure_ascii=False, indent=2
            )
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def write_html_report(
    path: Path,
    records: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> None:
    rows = []
    for case_number, record in enumerate(records, start=1):
        case = record.get("case") or {}
        verdict = record.get("verdict") or {}
        response = record.get("response") or {}
        result = response.get("result") or {}
        expected = (record.get("oracle") or {}).get("filters", {})
        actual = result.get("query_plan", {})
        issue_text = "\n".join(
            f"[{item.get('code')}] {item.get('message')}"
            for item in verdict.get("issues", [])
        )
        history = "\n".join(
            f"{index}. {item.get('question')}"
            for index, item in enumerate(case.get("turns", []), start=1)
        )
        passed = bool(verdict.get("passed"))
        status = "PASS" if passed else "FAIL"
        status_class = "pass" if passed else "fail"
        journal = (result.get("execution") or {}).get("tool_journal", [])
        planner_diagnostics = {
            "semantic_query": (result.get("execution") or {}).get(
                "semantic_query"
            ),
            "shadow_diff": (result.get("execution") or {}).get(
                "shadow_diff"
            ),
            "tool_journal": journal,
        }
        rows.append(
            "<tr class='{}'><td class='case-number'>{}</td><td><span class='status {}'>{}</span></td><td><code>{}</code><details><summary>Вопрос и история</summary><pre>{}</pre></details></td>"
            "<td><pre>{}</pre></td><td><pre>{}</pre></td><td><pre>{}</pre></td>"
            "<td>{}<br><code>{}</code><br>{} ms<details><summary>Журнал инструментов</summary><pre>{}</pre></details></td><td><code>analytic-certification --case {}</code></td></tr>".format(
                status_class,
                case_number,
                status_class,
                status,
                html.escape(str(case.get("case_id"))),
                html.escape(history),
                html.escape(json.dumps(expected, ensure_ascii=False, indent=2)),
                html.escape(json.dumps(actual, ensure_ascii=False, indent=2)),
                html.escape(issue_text or "—"),
                html.escape(str((result.get("execution") or {}).get("provider", "—"))),
                html.escape(str((result.get("execution") or {}).get("model", "—"))),
                html.escape(str((result.get("execution") or {}).get("duration_ms", "—"))),
                html.escape(
                    json.dumps(
                        planner_diagnostics, ensure_ascii=False, indent=2
                    )
                ),
                html.escape(str(case.get("case_id"))),
            )
        )
    document = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Analytic certification</title>
<style>
body{{font:14px system-ui;margin:24px;color:#17202a}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccd1d1;padding:8px;vertical-align:top}} th{{position:sticky;top:0;background:#f4f6f7}}
tr.fail{{background:#fff1f0}} tr.pass{{background:#f1fff4}} pre{{white-space:pre-wrap;max-width:36rem}}
code{{word-break:break-all}} .summary{{display:flex;gap:24px;margin:16px 0;align-items:center}}
.status{{display:inline-block;min-width:3.4rem;padding:4px 8px;border-radius:999px;color:#fff;font-weight:800;text-align:center}}
.status.pass{{background:#16803a}} .status.fail{{background:#c62828}}
.case-number{{font-weight:800;text-align:right;white-space:nowrap}}
</style></head><body><h1>Сертификация аналитических вопросов</h1>
<div class="summary"><strong>Пройдено: {coverage.get('passed')}/{coverage.get('selected')}</strong>
<strong>Release verdict: <span class="status {'pass' if coverage.get('release_passed') else 'fail'}">{'PASS' if coverage.get('release_passed') else 'FAIL'}</span></strong>
<strong>Source audit: {html.escape(str(coverage.get('source_audit_status', 'not_run')).upper())}</strong></div>
<pre>{html.escape(json.dumps(coverage, ensure_ascii=False, indent=2))}</pre>
<table><thead><tr><th>№</th><th>Итог</th><th>Сценарий</th><th>Ожидаемые фильтры</th><th>Фактические фильтры</th><th>Расхождения</th><th>Провайдер</th><th>Воспроизведение</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""
    path.write_text(document, encoding="utf-8")


def materialize_artifacts(
    run_dir: Path,
    records: list[dict[str, Any]],
    manifest: CapabilityManifest,
    *,
    selected_count: int,
    source_audit: dict[str, Any] | None = None,
    metamorphic_issues: list[VerificationIssue] | None = None,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(run_dir / "results.jsonl", records)
    coverage = coverage_summary(
        records,
        manifest,
        selected_count=selected_count,
        source_audit=source_audit,
        metamorphic_issues=metamorphic_issues,
    )
    write_json(run_dir / "coverage.json", coverage)
    if source_audit is not None:
        write_json(run_dir / "source-audit.json", source_audit)
    write_junit(run_dir / "junit.xml", records)
    write_html_report(run_dir / "report.html", records, coverage)
    replay_dir = run_dir / "replays"
    replay_dir.mkdir(exist_ok=True)
    failed_case_ids = {
        str((record.get("case") or {}).get("case_id", "unknown"))
        for record in records
        if not (record.get("verdict") or {}).get("passed", False)
    }
    for stale in replay_dir.glob("*.json"):
        if stale.stem not in failed_case_ids:
            stale.unlink()
    for record in records:
        if not (record.get("verdict") or {}).get("passed", False):
            case_id = (record.get("case") or {}).get("case_id", "unknown")
            write_json(replay_dir / f"{case_id}.json", anonymized_replay(record))
    return coverage
