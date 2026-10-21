from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .analytical_agent import (
    AgentUnavailableError,
    ResearchClarification,
    ResearchNoData,
    execute_research,
)
from .config import Settings
from .clarifications import resolve_clarification_answer
from .models import Conversation, ConversationTurn
from .workspace_service import format_report_text_numbers


TERMINAL_STATUSES = {
    "ready",
    "no_data",
    "failed",
    "cancelled",
    "timed_out",
}
ANALYSIS_TASKS: dict[str, asyncio.Task] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _event(
    event_id: int,
    stage: str,
    progress: int,
    data: dict[str, Any] | None = None,
) -> dict:
    return {
        "id": event_id,
        "type": "stage",
        "stage": stage,
        "progress": max(0, min(100, progress)),
        "data": data or {},
        "created_at": _now().isoformat(),
    }


def create_analysis_run(
    session: Session,
    conversation: Conversation,
    question: str,
    *,
    certification_mode: bool = False,
) -> ConversationTurn:
    sequence = (
        session.scalar(
            select(func.max(ConversationTurn.sequence)).where(
                ConversationTurn.conversation_id == conversation.id
            )
        )
        or 0
    ) + 1
    turn = ConversationTurn(
        id=str(uuid.uuid4()),
        conversation_id=conversation.id,
        sequence=sequence,
        role="user",
        turn_type="analysis_run",
        status="queued",
        content=question.strip(),
        plan_json={},
        result_json={},
        clarification_json={},
        planner_json={
            "run_version": 2,
            "progress": 0,
            "stage": "queued",
            "cancel_requested": False,
            "events": [_event(1, "queued", 0)],
            "tool_journal": [],
            "certification_mode": certification_mode,
        },
    )
    session.add(turn)
    conversation.status = "processing"
    conversation.updated_at = _now()
    session.commit()
    return turn


def analysis_run_to_dict(turn: ConversationTurn) -> dict:
    planner = turn.planner_json or {}
    result = dict(turn.result_json or {}) or None
    if result and result.get("answer"):
        result["answer"] = format_report_text_numbers(result["answer"])
    return {
        "run_id": turn.id,
        "turn_id": turn.id,
        "conversation_id": turn.conversation_id,
        "status": turn.status,
        "stage": planner.get("stage", turn.status),
        "progress": planner.get("progress", 0),
        "research_plan": turn.plan_json or None,
        "tool_journal": planner.get("tool_journal", []),
        "clarification": turn.clarification_json or None,
        "result": result,
        "error": planner.get("error"),
        "events_url": f"/api/analysis-runs/{turn.id}/events",
        "created_at": turn.created_at.isoformat(),
        "updated_at": planner.get("updated_at", turn.created_at.isoformat()),
    }


def append_run_event(
    session: Session,
    turn: ConversationTurn,
    stage: str,
    progress: int,
    data: dict[str, Any] | None = None,
) -> None:
    planner = dict(turn.planner_json or {})
    events = list(planner.get("events") or [])
    event_id = int(events[-1].get("id", 0)) + 1 if events else 1
    events.append(_event(event_id, stage, progress, data))
    planner.update(
        {
            "stage": stage,
            "progress": progress,
            "events": events[-100:],
            "updated_at": _now().isoformat(),
        }
    )
    if data and data.get("research_plan"):
        turn.plan_json = data["research_plan"]
    turn.planner_json = planner
    session.commit()


def request_cancel(session: Session, turn: ConversationTurn) -> None:
    if turn.status in TERMINAL_STATUSES:
        return
    planner = dict(turn.planner_json or {})
    planner["cancel_requested"] = True
    planner["updated_at"] = _now().isoformat()
    turn.planner_json = planner
    session.commit()


def _session_factory(bind: Engine) -> sessionmaker:
    return sessionmaker(
        bind=bind,
        autoflush=False,
        expire_on_commit=False,
    )


async def run_analysis(
    session_factory: sessionmaker,
    settings: Settings,
    run_id: str,
) -> None:
    with session_factory() as session:
        turn = session.get(ConversationTurn, run_id)
        if turn is None:
            return
        turn.status = "processing"
        append_run_event(session, turn, "plan", 3)
        conversation_id = turn.conversation_id
        clarification = turn.clarification_json or {}
        clarification_text = str(clarification.get("answer") or "").strip()
        certification_mode = bool(
            (turn.planner_json or {}).get("certification_mode")
        )
        question = turn.content
        if clarification_text:
            question = resolve_clarification_answer(
                question,
                clarification_text,
                clarification_question=str(
                    clarification.get("question") or ""
                ),
                clarification_options=clarification.get("options"),
            )

    async def progress(
        stage: str,
        percent: int,
        data: dict[str, Any] | None,
    ) -> None:
        with session_factory() as event_session:
            current = event_session.get(ConversationTurn, run_id)
            if current is None:
                raise asyncio.CancelledError
            if (current.planner_json or {}).get("cancel_requested"):
                raise asyncio.CancelledError
            append_run_event(event_session, current, stage, percent, data)

    effective_settings = (
        settings.model_copy(update={"llm_max_retries": 0})
        if certification_mode
        else settings
    )
    try:
        execution = await execute_research(
            session_factory,
            effective_settings,
            conversation_id,
            question,
            progress,
            strict_provider=certification_mode,
        )
        with session_factory() as session:
            turn = session.get(ConversationTurn, run_id)
            conversation = session.get(Conversation, conversation_id)
            if turn is None or conversation is None:
                return
            result = dict(execution.result)
            result.update(
                {
                    "query_id": turn.id,
                    "turn_id": turn.id,
                    "conversation_id": conversation.id,
                    "question": turn.content,
                    "status": "ready",
                }
            )
            turn.result_json = result
            turn.plan_json = result["research_plan"]
            turn.status = "ready"
            turn.turn_type = "analysis"
            planner = dict(turn.planner_json or {})
            planner["tool_journal"] = result["execution"]["tool_journal"]
            turn.planner_json = planner
            state = dict(conversation.state_json or {})
            base_plan = result.get("query_plan") or {}
            state.update(
                {
                    "metric_codes": base_plan.get("metric_codes", []),
                    "enterprise_groups": base_plan.get(
                        "enterprise_groups", []
                    ),
                    "territories": base_plan.get("territories", []),
                    "row_codes": base_plan.get("row_codes", []),
                    "period": (
                        (base_plan.get("periods") or [None])[-1]
                    ),
                    "evidence": result.get("evidence", []),
                    "previous_claims": result.get("claims", []),
                    **result.get("context_update", {}),
                }
            )
            conversation.state_json = state
            conversation.status = "draft"
            conversation.updated_at = _now()
            if conversation.title == "Новый диалог":
                title = " ".join(turn.content.split())
                conversation.title = (
                    title if len(title) <= 64 else title[:61].rstrip() + "…"
                )
            append_run_event(
                session,
                turn,
                "done",
                100,
                {
                    "numbers_verified": True,
                    "claims_count": len(result.get("claims", [])),
                    "hypotheses_count": len(
                        result.get("hypotheses", [])
                    ),
                },
            )
    except ResearchClarification as exc:
        with session_factory() as session:
            turn = session.get(ConversationTurn, run_id)
            conversation = (
                session.get(Conversation, turn.conversation_id)
                if turn is not None
                else None
            )
            if turn is None:
                return
            turn.status = "needs_clarification"
            turn.turn_type = "clarification"
            turn.clarification_json = {
                "question": exc.question,
                "options": [
                    {"id": f"option-{index}", "label": label}
                    for index, label in enumerate(exc.options, start=1)
                ],
            }
            if conversation is not None:
                conversation.status = "draft"
            append_run_event(
                session,
                turn,
                "needs_clarification",
                10,
                {"question": exc.question},
            )
    except ResearchNoData as exc:
        _finish_error(
            session_factory,
            run_id,
            "no_data",
            "no_data",
            str(exc),
        )
    except asyncio.TimeoutError:
        _finish_error(
            session_factory,
            run_id,
            "timed_out",
            "timeout",
            (
                "Исследование остановлено по лимиту времени. "
                "Сузьте период, отрасли или регионы и повторите запрос."
            ),
        )
    except asyncio.CancelledError:
        _finish_error(
            session_factory,
            run_id,
            "cancelled",
            "cancelled",
            "Исследование отменено пользователем.",
        )
    except AgentUnavailableError as exc:
        _finish_error(
            session_factory,
            run_id,
            "failed",
            "provider_unavailable",
            str(exc),
        )
    except Exception as exc:
        _finish_error(
            session_factory,
            run_id,
            "failed",
            "analysis_failed",
            str(exc),
        )


def _finish_error(
    session_factory: sessionmaker,
    run_id: str,
    status: str,
    code: str,
    message: str,
) -> None:
    with session_factory() as session:
        turn = session.get(ConversationTurn, run_id)
        if turn is None:
            return
        turn.status = status
        turn.turn_type = (
            status
            if status in {"cancelled", "no_data"}
            else "error"
        )
        turn.result_json = {
            "query_id": turn.id,
            "turn_id": turn.id,
            "conversation_id": turn.conversation_id,
            "question": turn.content,
            "status": status,
            "answer": message,
            "answer_mode": "analytic_agent_v2_error",
            "numbers_verified": True,
            "facts": [],
            "calculations": [],
            "series": [],
            "analysis_sections": [],
            "evidence": [],
            "claims": [],
            "hypotheses": [],
            "warnings": [message],
        }
        planner = dict(turn.planner_json or {})
        planner["error"] = {"code": code, "message": message}
        turn.planner_json = planner
        conversation = session.get(Conversation, turn.conversation_id)
        if conversation is not None:
            conversation.status = "draft"
        append_run_event(
            session,
            turn,
            status,
            100,
            {"error": {"code": code, "message": message}},
        )


def launch_analysis(
    bind: Engine,
    settings: Settings,
    run_id: str,
) -> asyncio.Task:
    factory = _session_factory(bind)
    task = asyncio.create_task(run_analysis(factory, settings, run_id))
    ANALYSIS_TASKS[run_id] = task

    def cleanup(done: asyncio.Task) -> None:
        ANALYSIS_TASKS.pop(run_id, None)
        if not done.cancelled():
            try:
                done.exception()
            except Exception:
                pass

    task.add_done_callback(cleanup)
    return task


def cancel_live_task(run_id: str) -> bool:
    task = ANALYSIS_TASKS.get(run_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True
