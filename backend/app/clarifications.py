from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
AFFIRMATIVE_RE = re.compile(
    r"^\s*(?:да|ага|соглас(?:ен|на)|подтверждаю|давайте|давай|"
    r"используй(?:те)?|хочу)\b",
    re.IGNORECASE,
)
PROPOSAL_RE = re.compile(
    r"(?:хотите\s+)?(?:сравнить|использовать|взять|выбрать|заменить\s+на)"
    r"\s+([^?.!]+)",
    re.IGNORECASE,
)


def _option_labels(options: Iterable[Any] | None) -> list[str]:
    labels: list[str] = []
    for option in options or []:
        if isinstance(option, str):
            label = option
        elif isinstance(option, dict):
            label = str(option.get("label") or "")
        else:
            label = ""
        if label.strip():
            labels.append(label.strip())
    return labels


def _period_proposal(
    clarification_question: str,
    options: Iterable[Any] | None,
) -> str | None:
    # Option labels are normally the cleanest representation of the offered
    # replacement and do not contain the unavailable source period.
    labels = _option_labels(options)
    for label in labels:
        if YEAR_RE.search(label):
            return label

    candidates = [
        match.group(1).strip()
        for match in PROPOSAL_RE.finditer(clarification_question)
        if YEAR_RE.search(match.group(1))
    ]
    if candidates:
        return candidates[-1]

    # Some providers formulate the offer as a final sentence without an
    # imperative verb: "Доступны январь–март 2025 и 2026". Use only a
    # sentence containing at least two distinct years so that the unavailable
    # year from the diagnostic sentence cannot become the replacement.
    for sentence in reversed(re.split(r"[?.!]", clarification_question)):
        years = list(dict.fromkeys(YEAR_RE.findall(sentence)))
        if len(years) >= 2:
            return sentence.strip()
    return None


def resolve_clarification_answer(
    original_question: str,
    answer: str,
    *,
    clarification_question: str = "",
    clarification_options: Iterable[Any] | None = None,
) -> str:
    """Apply an accepted period correction without retaining obsolete years.

    Concatenating an affirmative answer with the original question is unsafe:
    a planner still sees the unavailable period in that original text. When a
    period clarification contains a concrete replacement, rebuild the query
    with only the accepted years while preserving its metric and territories.
    """

    clean_answer = answer.strip()
    if not clean_answer:
        return original_question.strip()

    clarification_text = clarification_question.strip()
    period_clarification = bool(
        YEAR_RE.search(clarification_text)
        and re.search(
            r"период|данн|книг|срез|месяц|квартал|год",
            clarification_text,
            re.IGNORECASE,
        )
    )
    answer_has_year = YEAR_RE.search(clean_answer) is not None
    proposal = (
        clean_answer
        if answer_has_year
        else _period_proposal(clarification_text, clarification_options)
        if AFFIRMATIVE_RE.search(clean_answer)
        else None
    )
    if period_clarification and proposal:
        without_obsolete_years = YEAR_RE.sub("", original_question)
        without_obsolete_years = re.sub(
            r"\s+", " ", without_obsolete_years
        ).strip(" .")
        return (
            f"{without_obsolete_years}. Периоды заменить на: {proposal}. "
            "Исходные годы не использовать."
        )
    return (
        f"{original_question.strip()}. Уточнение пользователя: "
        f"{clean_answer}"
    )
