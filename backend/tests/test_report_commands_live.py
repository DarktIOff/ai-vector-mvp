from __future__ import annotations

import asyncio
import os

import pytest

from backend.app.config import get_settings
from backend.app.database import SessionLocal
from backend.app.report_service import interpret_report_command


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_REPORT_COMMANDS") != "1",
    reason="Live Qwen-сценарии включаются через RUN_LIVE_REPORT_COMMANDS=1",
)


@pytest.mark.parametrize(
    ("command", "territory"),
    [
        (
            "Собери стандартную справку по 1-МСП по Якутии",
            "Республика Саха (Якутия)",
        ),
        ("Подготовь стандартную справку 1-МСП по Москве", "г. Москва"),
        (
            "Сформируй стандартную справку по Карачаево-Черкесской "
            "Республике по форме 1-МСП",
            "Карачаево-Черкесская Республика",
        ),
    ],
)
def test_live_report_command(command: str, territory: str) -> None:
    with SessionLocal() as session:
        result = asyncio.run(
            interpret_report_command(session, get_settings(), command)
        )
    assert result.status == "ready"
    assert result.report_mode == "region_vs_rf"
    assert result.territory == territory
    assert result.benchmark == "Российская Федерация"
    assert result.old_period_end is not None
    assert result.new_period_end is not None
