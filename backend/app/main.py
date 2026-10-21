from __future__ import annotations

import asyncio
import json
import hashlib
import os
import shutil
import time
import uuid
from calendar import monthrange
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .calculations import (
    build_report_data,
    find_receipts_workbook,
    json_ready,
)
from .analysis_runs import (
    TERMINAL_STATUSES,
    analysis_run_to_dict,
    append_run_event,
    cancel_live_task,
    create_analysis_run,
    launch_analysis,
    request_cancel,
)
from .catalog import resolved_metric_details, seed_catalog
from .config import get_settings
from .database import SessionLocal, get_db, init_database
from .importer import import_root, scan_root
from .llm import LLMProviderError, configured_provider, safe_status
from .models import (
    AnalysisProject,
    AnalysisProjectBlock,
    ImportBatch,
    AiCallLog,
    Conversation,
    ConversationTurn,
    MetricDefinition,
    MspFact,
    ReportRun,
    RowDefinition,
    SchemaVersion,
    SourceFile,
    Scenario,
    ValidationResult,
)
from .query_service import (
    available_periods,
    build_query_plan,
    execute_plan,
)
from .planner import plan_question, validate_plan
from .reporting import (
    extract_report_document,
    generate_report,
    generate_report_from_data,
    update_report_document,
)
from .report_service import (
    build_region_vs_rf_report_data,
    interpret_report_command,
    report_available_parameters,
    validate_region_report_parameters,
)
from .schemas import (
    AnalysisRunCreate,
    AiTestRequest,
    ClarificationAnswer,
    ConversationCreate,
    ConversationPatch,
    ConversationTurnCreate,
    ImportRequest,
    NarrativeRequest,
    ProjectBlockCreate,
    ProjectPatch,
    QueryPlan,
    QueryRequest,
    ReportDocumentPatch,
    ReportGenerateRequest,
    ReportInterpretRequest,
    ScenarioCreate,
    ScenarioRunRequest,
)
from .synthesis_service import synthesize_project
from .workspace_service import (
    PROJECT_PRESENTATION_VERSION,
    add_project_block,
    answer_clarification,
    conversation_detail,
    conversation_summary,
    create_conversation_turn,
    ensure_project,
    project_to_dict,
    remove_project_block,
    scenario_steps,
    scenario_to_dict,
    update_project,
    write_project_docx,
)
from .workbooks import (
    classify_workbook,
    extract_as_of_date,
    open_reader,
    period_from_as_of,
    source_files,
)


settings = get_settings()
app = FastAPI(
    title="ИИ-ВЕКТОР API",
    version="0.3.0",
    description=(
        "Детерминированный импорт и evidence-backed аналитический агент "
        "по форме 1-МСП"
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

QUERY_RESULTS: dict[str, dict] = {}
CONVERSATIONS: dict[str, list[dict]] = {}


DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document"
)


def _immutable_docx_response(
    path: Path,
    *,
    filename: str | None = None,
) -> FileResponse:
    """Serve a per-request snapshot so Content-Length cannot race a rewrite."""

    snapshot = path.with_name(
        f".{path.stem}.download-{uuid.uuid4().hex}{path.suffix}"
    )
    shutil.copyfile(path, snapshot)
    return FileResponse(
        snapshot,
        media_type=DOCX_MEDIA_TYPE,
        filename=filename or path.name,
        background=BackgroundTask(snapshot.unlink, missing_ok=True),
    )


@app.on_event("startup")
def startup() -> None:
    init_database()
    with SessionLocal() as session:
        seed_catalog(session)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "ИИ-ВЕКТОР",
        "version": app.version,
        "data_root_available": settings.msp_history_root.is_dir(),
        "automsp_root_available": settings.automsp_root.is_dir(),
    }


@app.post("/api/conversations")
def conversation_create(
    request: ConversationCreate,
    session: Session = Depends(get_db),
) -> dict:
    conversation = Conversation(
        id=str(uuid.uuid4()),
        title=(request.title or "Новый диалог").strip() or "Новый диалог",
    )
    session.add(conversation)
    session.commit()
    return conversation_detail(conversation)


@app.get("/api/conversations")
def conversation_list(
    search: str = "",
    pinned: bool | None = None,
    include_archived: bool = False,
    session: Session = Depends(get_db),
) -> list[dict]:
    statement = select(Conversation)
    if not include_archived:
        statement = statement.where(Conversation.archived.is_(False))
    if pinned is not None:
        statement = statement.where(Conversation.pinned.is_(pinned))
    if search.strip():
        statement = statement.where(
            Conversation.title.ilike(f"%{search.strip()}%")
        )
    statement = statement.order_by(
        Conversation.pinned.desc(),
        Conversation.updated_at.desc(),
    )
    return [
        conversation_summary(item)
        for item in session.scalars(statement).all()
    ]


@app.get("/api/conversations/{conversation_id}")
def conversation_get(
    conversation_id: str,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "Диалог не найден")
    return conversation_detail(conversation)


@app.patch("/api/conversations/{conversation_id}")
def conversation_patch(
    conversation_id: str,
    request: ConversationPatch,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "Диалог не найден")
    changes = request.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if field == "title" and isinstance(value, str):
            value = value.strip()
        setattr(conversation, field, value)
    conversation.updated_at = datetime.utcnow()
    session.commit()
    return conversation_detail(conversation)


@app.delete("/api/conversations/{conversation_id}")
def conversation_archive(
    conversation_id: str,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "Диалог не найден")
    conversation.archived = True
    conversation.updated_at = datetime.utcnow()
    session.commit()
    return {"id": conversation.id, "archived": True}


@app.delete("/api/conversations/{conversation_id}/permanent")
def conversation_delete_permanently(
    conversation_id: str,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "Диалог не найден")
    if conversation.status == "processing":
        raise HTTPException(
            409,
            "Нельзя удалить диалог, пока выполняется анализ",
        )
    linked_scenarios = session.scalars(
        select(Scenario).where(
            Scenario.source_conversation_id == conversation_id
        )
    ).all()
    for scenario in linked_scenarios:
        scenario.source_conversation_id = None
    session.delete(conversation)
    session.commit()
    return {"id": conversation_id, "deleted": True}


@app.post("/api/conversations/{conversation_id}/turns")
async def conversation_turn_create(
    conversation_id: str,
    request: ConversationTurnCreate,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.archived:
        raise HTTPException(404, "Диалог не найден")
    try:
        return await create_conversation_turn(
            session, settings, conversation, request.question
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post(
    "/api/conversations/{conversation_id}/analysis-runs",
    status_code=202,
)
async def analysis_run_create(
    conversation_id: str,
    request: AnalysisRunCreate,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.archived:
        raise HTTPException(404, "Диалог не найден")
    turn = create_analysis_run(
        session,
        conversation,
        request.question,
        certification_mode=request.certification_mode,
    )
    launch_analysis(session.get_bind(), settings, turn.id)
    return {
        "run_id": turn.id,
        "turn_id": turn.id,
        "status": "queued",
        "events_url": f"/api/analysis-runs/{turn.id}/events",
    }


@app.get("/api/analysis-runs/{run_id}")
def analysis_run_get(
    run_id: str,
    session: Session = Depends(get_db),
) -> dict:
    turn = session.get(ConversationTurn, run_id)
    if turn is None or turn.turn_type not in {
        "analysis_run",
        "analysis",
        "clarification",
        "error",
        "cancelled",
        "no_data",
    }:
        raise HTTPException(404, "Аналитический запуск не найден")
    return analysis_run_to_dict(turn)


@app.get("/api/analysis-runs/{run_id}/events")
def analysis_run_events(
    run_id: str,
    after: int = 0,
    session: Session = Depends(get_db),
) -> StreamingResponse:
    turn = session.get(ConversationTurn, run_id)
    if turn is None:
        raise HTTPException(404, "Аналитический запуск не найден")
    bind = session.get_bind()

    async def events():
        cursor = max(0, after)
        heartbeat_at = time.monotonic()
        while True:
            with Session(bind=bind) as event_session:
                current = event_session.get(ConversationTurn, run_id)
                if current is None:
                    return
                stored = list(
                    (current.planner_json or {}).get("events") or []
                )
                pending = [
                    item
                    for item in stored
                    if int(item.get("id", 0)) > cursor
                ]
                status = current.status
            for item in pending:
                cursor = int(item.get("id", cursor))
                payload = json.dumps(
                    item, ensure_ascii=False, default=str
                )
                yield (
                    f"id: {cursor}\n"
                    f"event: stage\n"
                    f"data: {payload}\n\n"
                )
                heartbeat_at = time.monotonic()
            if status in TERMINAL_STATUSES or status == "needs_clarification":
                return
            if time.monotonic() - heartbeat_at >= 15:
                yield ": heartbeat\n\n"
                heartbeat_at = time.monotonic()
            await asyncio.sleep(0.2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/analysis-runs/{run_id}/cancel", status_code=202)
def analysis_run_cancel(
    run_id: str,
    session: Session = Depends(get_db),
) -> dict:
    turn = session.get(ConversationTurn, run_id)
    if turn is None:
        raise HTTPException(404, "Аналитический запуск не найден")
    if turn.status in TERMINAL_STATUSES:
        return analysis_run_to_dict(turn)
    request_cancel(session, turn)
    cancelled_live = cancel_live_task(run_id)
    if not cancelled_live:
        turn.status = "cancelled"
        turn.turn_type = "cancelled"
        turn.result_json = {
            "query_id": turn.id,
            "turn_id": turn.id,
            "conversation_id": turn.conversation_id,
            "question": turn.content,
            "status": "cancelled",
            "answer": "Исследование отменено пользователем.",
            "answer_mode": "analytic_agent_v2_error",
            "numbers_verified": True,
            "facts": [],
            "calculations": [],
            "series": [],
            "analysis_sections": [],
            "evidence": [],
            "claims": [],
            "hypotheses": [],
            "warnings": [],
        }
        append_run_event(
            session,
            turn,
            "cancelled",
            100,
            {"cancelled": True},
        )
    return {
        "run_id": run_id,
        "status": "cancelling" if cancelled_live else "cancelled",
        "task_interrupted": cancelled_live,
    }


@app.post(
    "/api/analysis-runs/{run_id}/clarification",
    status_code=202,
)
async def analysis_run_clarification(
    run_id: str,
    request: ClarificationAnswer,
    session: Session = Depends(get_db),
) -> dict:
    turn = session.get(ConversationTurn, run_id)
    if turn is None:
        raise HTTPException(404, "Аналитический запуск не найден")
    if turn.status != "needs_clarification":
        raise HTTPException(409, "Запуск не ожидает уточнения")
    clarification = dict(turn.clarification_json or {})
    answer = (request.text or "").strip()
    if request.option_id:
        selected = next(
            (
                item
                for item in clarification.get("options", [])
                if item.get("id") == request.option_id
            ),
            None,
        )
        if selected is None:
            raise HTTPException(422, "Вариант уточнения не найден")
        answer = str(selected.get("label") or "").strip()
    if not answer:
        raise HTTPException(422, "Введите уточнение или выберите вариант")
    clarification["answer"] = answer
    turn.clarification_json = clarification
    turn.status = "queued"
    turn.turn_type = "analysis_run"
    planner = dict(turn.planner_json or {})
    planner.update(
        {
            "cancel_requested": False,
            "error": None,
            "stage": "queued",
            "progress": 0,
        }
    )
    turn.planner_json = planner
    append_run_event(
        session,
        turn,
        "clarification_received",
        0,
        {"answer_received": True},
    )
    launch_analysis(session.get_bind(), settings, turn.id)
    return {
        "run_id": turn.id,
        "turn_id": turn.id,
        "status": "queued",
        "events_url": f"/api/analysis-runs/{turn.id}/events",
    }


@app.post(
    "/api/conversations/{conversation_id}/turns/{turn_id}/clarification"
)
async def conversation_turn_clarification(
    conversation_id: str,
    turn_id: str,
    request: ClarificationAnswer,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    turn = session.get(ConversationTurn, turn_id)
    if (
        conversation is None
        or turn is None
        or turn.conversation_id != conversation_id
    ):
        raise HTTPException(404, "Ход диалога не найден")
    try:
        return await answer_clarification(
            session,
            settings,
            conversation,
            turn,
            option_id=request.option_id,
            text=request.text,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/conversations/{conversation_id}/project")
def conversation_project_get(
    conversation_id: str,
    create: bool = True,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "Диалог не найден")
    project = (
        ensure_project(session, conversation)
        if create
        else conversation.project
    )
    if project is None:
        raise HTTPException(404, "Проект ещё не создан")
    return project_to_dict(project) or {}


@app.post("/api/conversations/{conversation_id}/project/blocks")
def conversation_project_add_block(
    conversation_id: str,
    request: ProjectBlockCreate,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    turn = session.get(ConversationTurn, request.turn_id)
    if conversation is None or turn is None:
        raise HTTPException(404, "Диалог или ход не найден")
    try:
        return add_project_block(session, conversation, turn)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete(
    "/api/conversations/{conversation_id}/project/blocks/{block_id}"
)
def conversation_project_remove_block(
    conversation_id: str,
    block_id: str,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, conversation_id)
    block = session.get(AnalysisProjectBlock, block_id)
    if (
        conversation is None
        or conversation.project is None
        or block is None
        or block.project_id != conversation.project.id
    ):
        raise HTTPException(404, "Блок проекта не найден")
    return remove_project_block(session, conversation.project, block)


@app.patch("/api/analysis-projects/{project_id}")
def analysis_project_patch(
    project_id: str,
    request: ProjectPatch,
    session: Session = Depends(get_db),
) -> dict:
    project = session.get(AnalysisProject, project_id)
    if project is None:
        raise HTTPException(404, "Аналитический проект не найден")
    try:
        return update_project(
            session,
            project,
            base_version=request.base_version,
            title=request.title,
            ordered_block_ids=request.ordered_block_ids,
            changed_text_blocks=request.changed_text_blocks,
        )
    except RuntimeError as exc:
        if str(exc) == "version_conflict":
            raise HTTPException(
                409,
                "Проект уже изменён. Обновите страницу и повторите.",
            ) from exc
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/analysis-projects/{project_id}/synthesize")
async def analysis_project_synthesize(
    project_id: str,
    session: Session = Depends(get_db),
) -> dict:
    project = session.get(AnalysisProject, project_id)
    if project is None:
        raise HTTPException(404, "Аналитический проект не найден")
    if not project.blocks:
        raise HTTPException(422, "В проекте ещё нет аналитических результатов")
    return await synthesize_project(session, settings, project)


@app.get("/api/analysis-projects/{project_id}/download/docx")
async def analysis_project_download(
    project_id: str,
    session: Session = Depends(get_db),
):
    project = session.get(AnalysisProject, project_id)
    if project is None:
        raise HTTPException(404, "Аналитический проект не найден")
    document = project.document_json or {}
    if project.blocks and (
        document.get("presentation_version")
        != PROJECT_PRESENTATION_VERSION
        or not document.get("synthesis_mode")
    ):
        await synthesize_project(session, settings, project)
    path = write_project_docx(project, settings)
    session.commit()
    return _immutable_docx_response(path)


@app.post("/api/scenarios")
def scenario_create(
    request: ScenarioCreate,
    session: Session = Depends(get_db),
) -> dict:
    conversation = session.get(Conversation, request.conversation_id)
    if conversation is None:
        raise HTTPException(404, "Диалог не найден")
    steps = scenario_steps(conversation)
    if not steps:
        raise HTTPException(422, "В диалоге ещё нет готовых аналитических ходов")
    scenario = Scenario(
        id=str(uuid.uuid4()),
        title=(request.title or f"Сценарий — {conversation.title}").strip(),
        source_conversation_id=conversation.id,
        steps_json=steps,
    )
    session.add(scenario)
    session.commit()
    return scenario_to_dict(scenario)


@app.get("/api/scenarios")
def scenario_list(session: Session = Depends(get_db)) -> list[dict]:
    scenarios = session.scalars(
        select(Scenario).order_by(Scenario.updated_at.desc())
    ).all()
    return [scenario_to_dict(item) for item in scenarios]


@app.post("/api/scenarios/{scenario_id}/run")
async def scenario_run(
    scenario_id: str,
    request: ScenarioRunRequest,
    session: Session = Depends(get_db),
) -> dict:
    scenario = session.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(404, "Сценарий не найден")
    conversation = Conversation(
        id=str(uuid.uuid4()),
        title=f"{scenario.title} — запуск",
    )
    session.add(conversation)
    session.commit()
    results = []
    periods = available_periods(session)
    latest_year = max(item.year for item in periods) if periods else datetime.now().year
    automatic_parameters = {
        "latest_year": str(latest_year),
        **{
            f"latest_year_minus_{offset}": str(latest_year - offset)
            for offset in range(1, 24)
        },
        **request.parameters,
    }
    for step in scenario.steps_json or []:
        question = str(step.get("question", ""))
        for key, value in automatic_parameters.items():
            question = question.replace("{{" + key + "}}", value)
        if not question.strip():
            continue
        try:
            result = await create_conversation_turn(
                session, settings, conversation, question
            )
        except ValueError as exc:
            result = {"status": "failed", "answer": str(exc)}
        results.append(result)
        if result.get("status") != "ready":
            break
    return {
        "scenario_id": scenario.id,
        "conversation": conversation_detail(conversation),
        "results": results,
    }


@app.post("/api/data/scan")
def data_scan() -> dict:
    if not settings.msp_history_root.is_dir():
        raise HTTPException(
            404, "Каталог исторических данных не найден. Проверьте MSP_HISTORY_ROOT."
        )
    return scan_root(settings.msp_history_root)


@app.post("/api/data/import")
def data_import(
    request: ImportRequest, session: Session = Depends(get_db)
) -> dict:
    paths = None
    if request.paths:
        paths = []
        root = settings.msp_history_root.resolve()
        allowed_roots = {root, settings.automsp_root.resolve()}
        for raw_path in request.paths:
            path = Path(raw_path).expanduser().resolve()
            if not any(path.is_relative_to(item) for item in allowed_roots):
                raise HTTPException(400, f"Путь вне разрешённых каталогов: {path}")
            if not path.is_file():
                raise HTTPException(404, f"Файл не найден: {path}")
            paths.append(path)
    batch = import_root(
        session,
        settings.msp_history_root,
        paths=paths,
        include_regional_metadata=request.include_regional_metadata,
    )
    return batch_to_dict(batch)


@app.get("/api/data/imports")
def data_imports(session: Session = Depends(get_db)) -> list[dict]:
    batches = session.scalars(
        select(ImportBatch).order_by(ImportBatch.id.desc()).limit(50)
    ).all()
    return [batch_to_dict(batch) for batch in batches]


@app.get("/api/data/imports/{batch_id}")
def data_import_detail(
    batch_id: int, session: Session = Depends(get_db)
) -> dict:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Запуск импорта не найден")
    files = session.scalars(
        select(SourceFile).where(SourceFile.batch_id == batch_id)
    ).all()
    return {
        **batch_to_dict(batch),
        "files": [source_to_dict(source) for source in files],
    }


@app.get("/api/data/schemas")
def data_schemas(session: Session = Depends(get_db)) -> list[dict]:
    schemas = session.scalars(
        select(SchemaVersion).order_by(SchemaVersion.numeric_column_count)
    ).all()
    return [
        {
            "id": item.id,
            "fingerprint": item.fingerprint,
            "form_name": item.form_name,
            "edition_text": item.edition_text,
            "numeric_column_count": item.numeric_column_count,
            "status": item.status,
        }
        for item in schemas
    ]


@app.get("/api/data/quarantine")
def data_quarantine(session: Session = Depends(get_db)) -> list[dict]:
    files = session.scalars(
        select(SourceFile)
        .where(SourceFile.status == "quarantined")
        .order_by(SourceFile.id.desc())
    ).all()
    return [source_to_dict(source) for source in files]


@app.post("/api/data/quarantine/{source_id}/approve-mapping")
def approve_mapping(
    source_id: int, session: Session = Depends(get_db)
) -> dict:
    source = session.get(SourceFile, source_id)
    if source is None:
        raise HTTPException(404, "Источник не найден")
    raise HTTPException(
        409,
        "Автоматическое утверждение неизвестной схемы запрещено. "
        "Добавьте проверенный адаптер и повторите импорт.",
    )


@app.get("/api/catalog/metrics")
def catalog_metrics(session: Session = Depends(get_db)) -> list[dict]:
    metrics = session.scalars(
        select(MetricDefinition).order_by(MetricDefinition.report_column_number)
    ).all()
    return [
        {
            "id": item.id,
            "semantic_code": item.semantic_code,
            "report_column_number": item.report_column_number,
            "name_ru": item.name_ru,
            "unit": item.unit,
            "aliases": item.aliases_json,
            "status": item.status,
        }
        for item in metrics
    ]


@app.get("/api/catalog/rows")
def catalog_rows(session: Session = Depends(get_db)) -> list[dict]:
    rows = session.scalars(
        select(RowDefinition).order_by(RowDefinition.base_row_code)
    ).all()
    return [
        {
            "id": item.id,
            "base_row_code": item.base_row_code,
            "name_ru": item.name_ru,
            "okved_text": item.okved_text,
            "hierarchy_level": item.hierarchy_level,
            "aggregation_expression": item.aggregation_expression,
        }
        for item in rows
    ]


@app.get("/api/catalog/territories")
def catalog_territories(session: Session = Depends(get_db)) -> list[dict]:
    from .models import Territory

    territories = session.scalars(select(Territory).order_by(Territory.name_ru)).all()
    return [
        {
            "id": item.id,
            "name_ru": item.name_ru,
            "territory_type": item.territory_type,
            "official_code": item.official_code,
        }
        for item in territories
    ]


@app.get("/api/catalog/periods")
def catalog_periods(session: Session = Depends(get_db)) -> list[dict]:
    rows = session.execute(
        select(
            MspFact.period_end,
            SourceFile.as_of_date,
            SourceFile.months_count,
            SourceFile.series_kind,
            SourceFile.status,
            func.count(MspFact.id),
        )
        .join(SourceFile)
        .group_by(
            MspFact.period_end,
            SourceFile.as_of_date,
            SourceFile.months_count,
            SourceFile.series_kind,
            SourceFile.status,
        )
        .order_by(MspFact.period_end)
    ).all()
    return [
        {
            "period_end": row[0].isoformat(),
            "as_of_date": row[1].isoformat() if row[1] else None,
            "months_count": row[2],
            "series_kind": row[3],
            "status": row[4],
            "facts_count": row[5],
        }
        for row in rows
    ]


@app.get("/api/catalog/sources/{fact_id}")
def catalog_source(fact_id: int, session: Session = Depends(get_db)) -> dict:
    fact = session.get(MspFact, fact_id)
    if fact is None:
        raise HTTPException(404, "Факт не найден")
    return {
        "fact_id": fact.id,
        "file": Path(fact.source_file.absolute_path).name,
        "absolute_path": fact.source_file.absolute_path,
        "sheet": fact.source_sheet,
        "cell": fact.source_cell,
        "row_code": fact.source_row_code,
        "report_column": fact.metric.report_column_number,
        "report_column_name": fact.metric.name_ru,
        "section": fact.section,
        "territory": fact.territory.name_ru,
        "period_start": fact.period_start.isoformat(),
        "period_end": fact.period_end.isoformat(),
        "as_of_date": fact.as_of_date.isoformat(),
        "raw_value": str(fact.value),
        "unit": fact.unit,
        "is_derived": fact.is_derived,
        "lineage": fact.lineage_json,
        "read_only": True,
    }


@app.post("/api/query/plan")
async def query_plan(
    request: QueryRequest, session: Session = Depends(get_db)
) -> dict:
    conversation_id = request.conversation_id or str(uuid.uuid4())
    context = CONVERSATIONS.setdefault(conversation_id, [])
    try:
        planned = await plan_question(
            session,
            settings,
            request.question,
            available_periods(session),
            context,
        )
        plan = planned.plan
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if planned.status == "needs_clarification":
        context.append(
            {
                "question": request.question,
                "pending_question": request.question,
                "clarification_question": planned.clarification_question,
                "clarification_options": planned.clarification_options,
            }
        )
        if len(context) > 12:
            del context[:-12]
    return {
        "question": request.question,
        "conversation_id": conversation_id,
        "status": planned.status,
        "plan": plan.model_dump(mode="json") if plan is not None else None,
        "resolved_metrics": resolved_metric_details(
            session, plan.metric_codes
        )
        if plan is not None
        else [],
        "planner_mode": planned.mode,
        "warnings": planned.warnings,
        "requires_confirmation": False,
        "clarification_question": planned.clarification_question,
        "clarification_options": planned.clarification_options,
    }


@app.post("/api/query/execute")
def query_execute(plan: QueryPlan, session: Session = Depends(get_db)) -> dict:
    try:
        return execute_plan(session, plan)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/query")
async def query(
    request: QueryRequest, session: Session = Depends(get_db)
) -> dict:
    conversation_id = request.conversation_id or str(uuid.uuid4())
    context = CONVERSATIONS.setdefault(conversation_id, [])
    try:
        periods = available_periods(session)
        if request.plan is not None:
            plan = validate_plan(session, request.plan, periods)
            planner_mode = "confirmed"
            planner_warnings: list[str] = []
        else:
            planned = await plan_question(
                session,
                settings,
                request.question,
                periods,
                context,
            )
            plan = planned.plan
            planner_mode = planned.mode
            planner_warnings = planned.warnings
            if planned.status == "needs_clarification":
                query_id = str(uuid.uuid4())
                result = {
                    "query_id": query_id,
                    "conversation_id": conversation_id,
                    "question": request.question,
                    "status": "needs_clarification",
                    "query_plan": None,
                    "resolved_metrics": [],
                    "facts": [],
                    "calculations": [],
                    "series": [],
                    "visualization": None,
                    "warnings": planner_warnings,
                    "answer": planned.clarification_question
                    or "Уточните запрос.",
                    "answer_mode": "clarification",
                    "numbers_verified": True,
                    "clarification_question": planned.clarification_question,
                    "clarification_options": planned.clarification_options,
                }
                context.append(
                    {
                        "question": request.question,
                        "pending_question": request.question,
                        "clarification_question": (
                            planned.clarification_question
                        ),
                        "clarification_options": (
                            planned.clarification_options
                        ),
                    }
                )
                QUERY_RESULTS[query_id] = result
                return result
            if plan is None:
                raise ValueError("Планировщик не вернул план")
        result = execute_plan(session, plan)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    query_id = str(uuid.uuid4())
    result["query_id"] = query_id
    result["conversation_id"] = conversation_id
    result["question"] = request.question
    result["planner_mode"] = planner_mode
    result["warnings"] = [
        *planner_warnings,
        *result.get("warnings", []),
    ]
    QUERY_RESULTS[query_id] = result
    context.append(
        {
            "question": request.question,
            "plan": plan.model_dump(mode="json"),
        }
    )
    if len(context) > 12:
        del context[:-12]
    return result


@app.get("/api/query/{query_id}/events")
def query_events(query_id: str) -> StreamingResponse:
    if query_id not in QUERY_RESULTS:
        raise HTTPException(404, "Запрос не найден")
    result = QUERY_RESULTS[query_id]

    async def events():
        for event in (
            {"type": "plan", "data": result["query_plan"]},
            {"type": "facts", "data": result["facts"]},
            {"type": "answer", "data": result["answer"]},
            {"type": "done", "data": {"numbers_verified": True}},
        ):
            yield f"event: {event['type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/query/{query_id}/cancel")
def query_cancel(query_id: str) -> dict:
    return {"query_id": query_id, "status": "cancelled"}


def _control_files(
    old_year: int, new_year: int, months_count: int
) -> tuple[Path, Path]:
    candidates: dict[tuple[int, int], Path] = {}
    for path in source_files(settings.automsp_root):
        try:
            reader = open_reader(path)
            kind = classify_workbook(path, reader.sheet_names)
            if kind != "msp_summary":
                reader.close()
                continue
            as_of, _ = extract_as_of_date(reader)
            reader.close()
            if as_of is None:
                continue
            _, _, report_year, months = period_from_as_of(as_of)
            candidates[(report_year, months)] = path
        except Exception:
            continue
    old_path = candidates.get((old_year, months_count))
    new_path = candidates.get((new_year, months_count))
    if old_path is None or new_path is None:
        raise HTTPException(
            422,
            f"Не найдена сопоставимая пара 1-МСП за {old_year}/{new_year}, "
            f"месяцев: {months_count}",
        )
    return old_path, new_path


def _national_request_parameters(
    request: ReportGenerateRequest,
) -> tuple[int, int, int]:
    if request.old_period_end is None or request.new_period_end is None:
        return request.old_year, request.new_year, request.months_count
    old_period = request.old_period_end
    new_period = request.new_period_end
    if old_period >= new_period:
        raise HTTPException(422, "Периоды должны идти в хронологическом порядке")
    if old_period.month != new_period.month:
        raise HTTPException(
            422,
            "Накопительные периоды должны иметь одинаковый месяц окончания",
        )
    return old_period.year, new_period.year, new_period.month


@app.get("/api/reports/1-msp/parameters")
@app.get("/api/reports/1-msp/options")
def report_parameters(session: Session = Depends(get_db)) -> dict:
    return report_available_parameters(session)


@app.post("/api/reports/1-msp/interpret")
async def report_interpret(
    request: ReportInterpretRequest,
    session: Session = Depends(get_db),
) -> dict:
    try:
        specification = await interpret_report_command(
            session, settings, request.command
        )
    except LLMProviderError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            502,
            f"GNIVC LOCAL вернул некорректную спецификацию отчёта: {exc}",
        ) from exc
    return specification.model_dump(mode="json")


@app.post("/api/reports/1-msp/preview")
def report_preview(
    request: ReportGenerateRequest,
    session: Session = Depends(get_db),
) -> dict:
    if request.report_mode == "region_vs_rf":
        try:
            territory, old_period, new_period = (
                validate_region_report_parameters(
                    session,
                    territory=request.territory,
                    old_period_end=request.old_period_end,
                    new_period_end=request.new_period_end,
                )
            )
            data = build_region_vs_rf_report_data(
                session,
                settings.msp_codes_file,
                territory=territory,
                old_period_end=old_period,
                new_period_end=new_period,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        parameters = {
            "report_mode": "region_vs_rf",
            "territory": territory,
            "benchmark": "Российская Федерация",
            "old_period_end": old_period.isoformat(),
            "new_period_end": new_period.isoformat(),
        }
    else:
        old_year, new_year, months_count = _national_request_parameters(request)
        old_path, new_path = _control_files(old_year, new_year, months_count)
        receipts = find_receipts_workbook(settings.automsp_root)
        data = build_report_data(
            old_path,
            new_path,
            receipts,
            settings.msp_codes_file,
            old_year=old_year,
            new_year=new_year,
            months_count=months_count,
        )
        parameters = {
            "report_mode": "national",
            "territory": "Российская Федерация",
            "old_year": old_year,
            "new_year": new_year,
            "months_count": months_count,
        }
    return {
        "status": "ready",
        "parameters": parameters,
        "data": json_ready(data),
    }


@app.post("/api/reports/1-msp/generate")
def report_generate(
    request: ReportGenerateRequest, session: Session = Depends(get_db)
) -> dict:
    if request.report_mode == "region_vs_rf":
        try:
            territory, old_period, new_period = validate_region_report_parameters(
                session,
                territory=request.territory,
                old_period_end=request.old_period_end,
                new_period_end=request.new_period_end,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        old_year = old_period.year
        new_year = new_period.year
        months_count = new_period.month
        parameters = {
            **request.model_dump(mode="json"),
            "territory": territory,
            "old_period_end": old_period.isoformat(),
            "new_period_end": new_period.isoformat(),
        }
    else:
        old_year, new_year, months_count = _national_request_parameters(request)
        old_path, new_path = _control_files(old_year, new_year, months_count)
        old_period = datetime(
            old_year,
            months_count,
            monthrange(old_year, months_count)[1],
        ).date()
        new_period = datetime(
            new_year,
            months_count,
            monthrange(new_year, months_count)[1],
        ).date()
        parameters = request.model_dump(mode="json")
    run = ReportRun(
        report_type=(
            "1-msp-region-vs-rf"
            if request.report_mode == "region_vs_rf"
            else "1-msp"
        ),
        status="processing",
        parameters_json=parameters,
        old_period_end=old_period,
        new_period_end=new_period,
    )
    session.add(run)
    session.commit()
    try:
        if request.report_mode == "region_vs_rf":
            data = build_region_vs_rf_report_data(
                session,
                settings.msp_codes_file,
                territory=territory,
                old_period_end=old_period,
                new_period_end=new_period,
            )
            docx_path, json_path, snapshot = generate_report_from_data(
                settings.msp_report_template,
                settings.app_data_dir / "reports",
                data,
                old_year=old_year,
                new_year=new_year,
                months_count=months_count,
            )
        else:
            docx_path, json_path, snapshot = generate_report(
                settings.msp_report_template,
                settings.app_data_dir / "reports",
                old_path,
                new_path,
                find_receipts_workbook(settings.automsp_root),
                settings.msp_codes_file,
                old_year=old_year,
                new_year=new_year,
                months_count=months_count,
            )
        run.status = "draft"
        run.output_docx_path = str(docx_path.resolve())
        run.output_json_path = str(json_path.resolve())
        run.facts_snapshot_json = snapshot
        run.finished_at = datetime.utcnow()
        session.commit()
    except ValueError as exc:
        run.status = "failed"
        run.finished_at = datetime.utcnow()
        run.parameters_json = {
            **run.parameters_json,
            "error": str(exc),
        }
        session.commit()
        raise HTTPException(422, f"Не удалось сформировать отчёт: {exc}") from exc
    except Exception as exc:
        run.status = "failed"
        run.finished_at = datetime.utcnow()
        run.parameters_json = {
            **run.parameters_json,
            "error": str(exc),
        }
        session.commit()
        raise HTTPException(500, f"Не удалось сформировать отчёт: {exc}") from exc
    return report_to_dict(run)


@app.get("/api/reports/{report_id}")
def report_get(report_id: int, session: Session = Depends(get_db)) -> dict:
    run = session.get(ReportRun, report_id)
    if run is None:
        raise HTTPException(404, "Отчёт не найден")
    return report_to_dict(run)


@app.get("/api/reports/{report_id}/facts")
def report_facts(report_id: int, session: Session = Depends(get_db)) -> dict:
    run = session.get(ReportRun, report_id)
    if run is None:
        raise HTTPException(404, "Отчёт не найден")
    return run.facts_snapshot_json


def _report_document(
    run: ReportRun, session: Session
) -> dict:
    snapshot = dict(run.facts_snapshot_json or {})
    model = snapshot.get("report_document")
    if isinstance(model, dict):
        return model
    if not run.output_docx_path:
        raise HTTPException(404, "Файл отчёта не найден")
    path = Path(run.output_docx_path)
    if not path.is_file():
        raise HTTPException(404, "Файл отчёта отсутствует на диске")
    model = extract_report_document(path, version=1)
    snapshot["report_document"] = model
    run.facts_snapshot_json = snapshot
    session.commit()
    return model


def _write_snapshot_file(run: ReportRun) -> None:
    if not run.output_json_path:
        return
    target = Path(run.output_json_path)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                run.facts_snapshot_json,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


@app.get("/api/reports/{report_id}/document")
def report_document_get(
    report_id: int, session: Session = Depends(get_db)
) -> dict:
    run = session.get(ReportRun, report_id)
    if run is None:
        raise HTTPException(404, "Отчёт не найден")
    return _report_document(run, session)


@app.patch("/api/reports/{report_id}/document")
def report_document_patch(
    report_id: int,
    request: ReportDocumentPatch,
    session: Session = Depends(get_db),
) -> dict:
    run = session.get(ReportRun, report_id)
    if run is None:
        raise HTTPException(404, "Отчёт не найден")
    current = _report_document(run, session)
    if int(current.get("version", 1)) != request.base_version:
        raise HTTPException(
            409,
            "Документ уже изменён. Обновите страницу и повторите правку.",
        )
    if not request.changed_text_blocks:
        return current
    if not run.output_docx_path:
        raise HTTPException(404, "Файл отчёта не найден")
    try:
        updated = update_report_document(
            Path(run.output_docx_path),
            current,
            request.changed_text_blocks,
        )
    except KeyError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    snapshot = dict(run.facts_snapshot_json or {})
    snapshot["report_document"] = updated
    run.facts_snapshot_json = snapshot
    session.commit()
    _write_snapshot_file(run)
    return updated


@app.get("/api/reports/{report_id}/download/docx")
def report_download(report_id: int, session: Session = Depends(get_db)):
    run = session.get(ReportRun, report_id)
    if run is None or not run.output_docx_path:
        raise HTTPException(404, "Файл отчёта не найден")
    path = Path(run.output_docx_path)
    if not path.is_file():
        raise HTTPException(404, "Файл отчёта отсутствует на диске")
    return _immutable_docx_response(path)


@app.post("/api/reports/{report_id}/improve-narrative")
async def improve_narrative(
    report_id: int,
    request: NarrativeRequest,
    session: Session = Depends(get_db),
) -> dict:
    run = session.get(ReportRun, report_id)
    if run is None:
        raise HTTPException(404, "Отчёт не найден")
    status = await safe_status(settings)
    if not status["narrative_improvement_enabled"]:
        return {
            "report_id": report_id,
            "status": "deterministic_fallback",
            "message": "Улучшение стиля ИИ отключено политикой данных или провайдер не настроен.",
        }
    return {
        "report_id": report_id,
        "status": "not_applied",
        "message": "Для реальных данных требуется явное организационное разрешение.",
    }


@app.post("/api/reports/{report_id}/submit")
def report_submit(report_id: int, session: Session = Depends(get_db)) -> dict:
    run = session.get(ReportRun, report_id)
    if run is None:
        raise HTTPException(404, "Отчёт не найден")
    if run.status != "draft":
        raise HTTPException(409, "Отправить можно только черновик")
    run.status = "needs_review"
    session.commit()
    return report_to_dict(run)


@app.post("/api/reports/{report_id}/approve")
def report_approve(report_id: int, session: Session = Depends(get_db)) -> dict:
    run = session.get(ReportRun, report_id)
    if run is None:
        raise HTTPException(404, "Отчёт не найден")
    if run.status != "needs_review":
        raise HTTPException(409, "Утвердить можно только отчёт на проверке")
    run.status = "approved"
    session.commit()
    return report_to_dict(run)


@app.post("/api/reports/{report_id}/request-revision")
def report_request_revision(
    report_id: int, session: Session = Depends(get_db)
) -> dict:
    run = session.get(ReportRun, report_id)
    if run is None:
        raise HTTPException(404, "Отчёт не найден")
    if run.status != "needs_review":
        raise HTTPException(409, "Вернуть можно только отчёт на проверке")
    run.status = "revision_requested"
    session.commit()
    return report_to_dict(run)


@app.get("/api/ai/status")
async def ai_status() -> dict:
    return await safe_status(settings)


@app.post("/api/ai/test")
async def ai_test(
    request: AiTestRequest, session: Session = Depends(get_db)
) -> dict:
    started = time.perf_counter()
    provider = configured_provider(settings)
    input_hash = hashlib.sha256(request.text.encode("utf-8")).hexdigest()
    try:
        status = await provider.healthcheck()
        if status["status"] != "available":
            session.add(
                AiCallLog(
                    provider=provider.name,
                    model=provider.model,
                    purpose="synthetic_healthcheck",
                    status=status["status"],
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    input_hash=input_hash,
                    token_usage_json=getattr(provider, "last_usage", {}),
                    error_code=status.get("error_code"),
                )
            )
            session.commit()
            return status
        response = await provider.complete(
            [{"role": "user", "content": request.text}],
            max_tokens=64,
        )
        used_fallback = bool(getattr(provider, "last_fallback", False))
        session.add(
            AiCallLog(
                provider=provider.name,
                model=provider.model,
                purpose="synthetic_healthcheck",
                status="fallback" if used_fallback else "success",
                duration_ms=int((time.perf_counter() - started) * 1000),
                input_hash=input_hash,
                token_usage_json=getattr(provider, "last_usage", {}),
                error_code="deterministic_fallback" if used_fallback else None,
            )
        )
        session.commit()
        return {
            **status,
            "synthetic_response": response,
            "fallback": used_fallback,
        }
    except LLMProviderError as exc:
        session.add(
            AiCallLog(
                provider=provider.name,
                model=provider.model,
                purpose="synthetic_healthcheck",
                status="failed",
                duration_ms=int((time.perf_counter() - started) * 1000),
                input_hash=input_hash,
                token_usage_json=getattr(provider, "last_usage", {}),
                error_code=exc.code,
            )
        )
        session.commit()
        raise HTTPException(503, {"code": exc.code, "message": str(exc)}) from exc


def batch_to_dict(batch: ImportBatch) -> dict:
    return {
        "id": batch.id,
        "started_at": batch.started_at.isoformat(),
        "finished_at": batch.finished_at.isoformat()
        if batch.finished_at
        else None,
        "status": batch.status,
        "source_root": batch.source_root,
        "files_found": batch.files_found,
        "files_imported": batch.files_imported,
        "files_quarantined": batch.files_quarantined,
        "errors_count": batch.errors_count,
    }


def source_to_dict(source: SourceFile) -> dict:
    return {
        "id": source.id,
        "name": Path(source.absolute_path).name,
        "absolute_path": source.absolute_path,
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "file_format": source.file_format,
        "workbook_kind": source.workbook_kind,
        "as_of_date": source.as_of_date.isoformat() if source.as_of_date else None,
        "period_end": source.period_end.isoformat() if source.period_end else None,
        "months_count": source.months_count,
        "series_kind": source.series_kind,
        "schema_version_id": source.schema_version_id,
        "status": source.status,
        "warnings": source.warnings_json,
    }


def report_to_dict(run: ReportRun) -> dict:
    return {
        "id": run.id,
        "report_type": run.report_type,
        "status": run.status,
        "parameters": run.parameters_json,
        "old_period_end": run.old_period_end.isoformat()
        if run.old_period_end
        else None,
        "new_period_end": run.new_period_end.isoformat()
        if run.new_period_end
        else None,
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_by": run.created_by,
        "download_url": f"/api/reports/{run.id}/download/docx"
        if run.output_docx_path
        else None,
        "facts_url": f"/api/reports/{run.id}/facts"
        if run.facts_snapshot_json
        else None,
        "document_url": f"/api/reports/{run.id}/document"
        if run.output_docx_path
        else None,
        "numbers_verified": bool(run.facts_snapshot_json),
    }
    ProjectBlockCreate,
    ProjectPatch,
