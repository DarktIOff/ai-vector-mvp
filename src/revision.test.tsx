import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AnalysisSections,
  AnalysisWorkspace,
  AutoResizeReportText,
  EdgeSafeAxisTick,
  LiveFactResults,
  LiveReportWorkspace,
  ReportChartBlockView,
  ReportLoadingScreen,
  synthesisNoticeFromResult,
  TrendSeriesChart,
} from "./App";
import {
  backendApi,
  type QueryResult,
  type QuerySeries,
  type ReportChartBlock,
  type ReportDocument,
} from "./api";


afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.clear();
});


describe("многолетняя динамика", () => {
  it("keeps the complete final date inside the chart edge", () => {
    render(
      <svg>
        <EdgeSafeAxisTick
          x={300}
          y={20}
          index={3}
          visibleTicksCount={4}
          payload={{ value: "30-06-2026" }}
        />
      </svg>,
    );
    const finalDate = screen.getByText("30-06-2026");
    expect(finalDate).toHaveTextContent("30-06-2026");
    expect(finalDate).toHaveAttribute("text-anchor", "end");
  });

  it("shows every point in an accessible table", () => {
    const series: QuerySeries[] = [
      {
        key: "series-1",
        label: "НДС · Российская Федерация · все МСП",
        metric_code: "receipts_vat_total",
        territory: "Российская Федерация",
        enterprise_group: "all_msp",
        row_code: 1010,
        unit: "тыс. руб.",
        points: [2022, 2023, 2024, 2025, 2026].map((year, index) => ({
          period_end: `${year}-06-30`,
          label: `январь–июнь ${year}`,
          value: String((index + 1) * 1_000_000),
          fact_id: `fact-${year}`,
        })),
        changes: [],
        overall_change: {
          from_period: "2022-06-30",
          to_period: "2026-06-30",
          difference: "4000000",
          growth_rate: "500",
        },
        minimum: {
          period_end: "2022-06-30",
          label: "январь–июнь 2022",
          value: "1000000",
        },
        maximum: {
          period_end: "2026-06-30",
          label: "январь–июнь 2026",
          value: "5000000",
        },
      },
    ];
    render(<TrendSeriesChart series={series} />);
    expect(
      screen.getByLabelText("Линейный график многолетней динамики"),
    ).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("январь–июнь 2022");
    expect(screen.getByRole("table")).toHaveTextContent("январь–июнь 2026");
    expect(screen.getAllByRole("row")).toHaveLength(6);
  });

  it("normalizes multi-industry dynamics and renders shared context once", async () => {
    const makeSeries = (
      key: string,
      industry: string,
      rowCode: number,
      values: [number, number],
    ): QuerySeries => ({
      key,
      label:
        "Поступления в консолидированный бюджет · Московская область · " +
        `все МСП · ${industry} - всего`,
      metric_code: "receipts_budget_total",
      territory: "Московская область",
      enterprise_group: "all_msp",
      row_code: rowCode,
      unit: "тыс. руб.",
      points: values.map((value, index) => ({
        period_end: `${2025 + index}-03-31`,
        label: `${2025 + index}-03-31`,
        value: String(value),
        fact_id: `${key}-${index}`,
      })),
      changes: [],
      overall_change: {
        from_period: "2025-03-31",
        to_period: "2026-03-31",
        difference: String(values[1] - values[0]),
        growth_rate: String((values[1] / values[0] - 1) * 100),
      },
      minimum: {
        period_end: "2025-03-31",
        label: "2025-03-31",
        value: String(Math.min(...values)),
      },
      maximum: {
        period_end: "2026-03-31",
        label: "2026-03-31",
        value: String(Math.max(...values)),
      },
    });
    const user = userEvent.setup();
    render(
      <TrendSeriesChart
        series={[
          makeSeries("series-1", "Строительство", 1270, [
            1_000_000, 700_000,
          ]),
          makeSeries("series-2", "Торговля", 1130, [
            50_000_000, 55_000_000,
          ]),
        ]}
      />,
    );

    expect(
      screen.getByRole("heading", {
        name: "Динамика по выбранным отраслям",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(
        "Поступления в консолидированный бюджет · Московская область · все МСП",
      ),
    ).toHaveLength(1);
    expect(
      screen.queryByText(
        "Поступления в консолидированный бюджет · Московская область · " +
          "все МСП · Строительство - всего",
      ),
    ).not.toBeInTheDocument();
    const changeMode = screen.getByRole("button", {
      name: "Изменение, %",
    });
    const absoluteMode = screen.getByRole("button", { name: "Объём" });
    expect(changeMode).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByText("Наведите на точку графика для другого периода"),
    ).toBeInTheDocument();

    await user.click(absoluteMode);
    expect(absoluteMode).toHaveAttribute("aria-pressed", "true");
    expect(changeMode).toHaveAttribute("aria-pressed", "false");
  });
});


describe("региональный рост выбранных отраслей", () => {
  it("shows average, median and weighted growth instead of one total", () => {
    render(
      <AnalysisSections
        sections={[
          {
            kind: "regional_average_growth",
            title: "Средний рост по регионам",
            rows: [
              {
                entity: "Строительство",
                growth_rate: "12.5",
                median_growth_rate: "10.2",
                weighted_growth_rate: "11.8",
                regions_count: 85,
                minimum_region: {
                  entity: "Регион А",
                  growth_rate: "-4.1",
                },
                maximum_region: {
                  entity: "Регион Б",
                  growth_rate: "35.7",
                },
              },
            ],
            methodology: "Сопоставлены одинаковые периоды.",
          },
        ]}
      />,
    );
    expect(screen.getByRole("table")).toHaveTextContent("Строительство");
    expect(screen.getByRole("columnheader", {
      name: "Средний прирост",
    })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", {
      name: "Медиана",
    })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", {
      name: "Взвешенный прирост",
    })).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("85");
    expect(screen.getByRole("table")).toHaveTextContent("Регион Б");
  });

  it("shows the industry once in the table title", () => {
    render(
      <AnalysisSections
        sections={[
          {
            kind: "regional_industry_growth",
            title: "Лидеры регионов по росту — Строительство",
            rows: [
              {
                entity: "Строительство — Москва",
                industry: "Строительство",
                region: "Москва",
                growth_rate: "18.2",
              },
              {
                entity: "Строительство — Республика Татарстан",
                industry: "Строительство",
                region: "Республика Татарстан",
                growth_rate: "15.4",
              },
            ],
            methodology: "Сопоставлены одинаковые периоды.",
          },
        ]}
      />,
    );
    const table = screen.getByRole("table");
    expect(
      screen.getByRole("heading", {
        name: "Лидеры регионов по росту — Строительство",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "Регион" }),
    ).toBeInTheDocument();
    expect(table).toHaveTextContent("Москва");
    expect(table).toHaveTextContent("Республика Татарстан");
    expect(table).not.toHaveTextContent("Строительство");
  });
});


describe("служебное разрешение сущностей", () => {
  const resolvedSection = {
    kind: "resolved_entities",
    title: "Разрешённые сущности",
    rows: [
      {
        kind: "metric",
        code: "receipts_federal_taxes_total",
        name: "Федеральные налоги и сборы",
        unit: "тыс. руб.",
      },
    ],
    methodology: "Поиск только по утверждённым справочникам.",
  };

  it("hides the internal section in an ordinary analytical answer", () => {
    render(<AnalysisSections sections={[resolvedSection]} />);
    expect(screen.queryByText("Разрешённые сущности")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders catalog entities with their actual fields", () => {
    render(
      <AnalysisSections
        sections={[resolvedSection]}
        showResolvedEntities
      />,
    );
    const table = screen.getByRole("table");
    expect(table).toHaveTextContent("Показатель");
    expect(table).toHaveTextContent("receipts_federal_taxes_total");
    expect(table).toHaveTextContent("Федеральные налоги и сборы");
    expect(table).toHaveTextContent("тыс. руб.");
    expect(
      screen.queryByRole("columnheader", { name: "Текущее значение" }),
    ).not.toBeInTheDocument();
  });
});


describe("подтверждённые факты", () => {
  it("is collapsed by default and expands on request", async () => {
    const result: Pick<QueryResult, "facts" | "calculations"> = {
      facts: [
        {
          fact_id: "fact-1",
          metric_code: "receipts_vat_total",
          metric: "НДС",
          value: "1000000",
          unit: "тыс. руб.",
          period_end: "2026-06-30",
          section: "1",
          enterprise_group: "all_msp",
          territory: "Российская Федерация",
          row_code: 1010,
          source: {
            file: "source.xlsx",
            sheet: "Лист 1",
            cell: "B10",
          },
        },
      ],
      calculations: [],
    };
    const user = userEvent.setup();
    render(
      <LiveFactResults
        result={result}
        onShowSources={() => undefined}
      />,
    );
    const title = screen.getByText("Подтверждённые факты");
    const details = title.closest("details");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("Показать")).toBeInTheDocument();

    await user.click(title);

    expect(details).toHaveAttribute("open");
    expect(screen.getByText("НДС")).toBeInTheDocument();
    expect(screen.getByText("Скрыть")).toBeInTheDocument();
  });
});


describe("формирование аналитического отчёта", () => {
  it("shows an animated matrix-style status screen", () => {
    const { container } = render(
      <ReportLoadingScreen stage="Формируем связный текст" />,
    );
    expect(
      screen.getByRole("status", {
        name: "Формирование аналитического отчёта",
      }),
    ).toHaveTextContent("Собираем аналитический отчёт");
    expect(screen.getByText("Формируем связный текст")).toBeInTheDocument();
    expect(
      screen.getByText("АНАЛИТИЧЕСКИЙ ОТЧЁТ // СБОРКА"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("ANALYTIC REPORT // BUILD"),
    ).not.toBeInTheDocument();
    expect(container.querySelectorAll(".report-matrix-column")).toHaveLength(
      22,
    );
  });

  it("expands editable text blocks to their full content height", () => {
    const previous = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "scrollHeight",
    );
    Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", {
      configurable: true,
      get: () => 180,
    });
    try {
      render(
        <AutoResizeReportText
          blockId="summary"
          text={"Первая строка\nВторая строка"}
          editable
          onChange={() => undefined}
        />,
      );
      expect(screen.getByLabelText("Текстовый блок summary")).toHaveStyle({
        height: "180px",
      });
    } finally {
      if (previous) {
        Object.defineProperty(
          HTMLTextAreaElement.prototype,
          "scrollHeight",
          previous,
        );
      } else {
        delete (HTMLTextAreaElement.prototype as unknown as {
          scrollHeight?: number;
        }).scrollHeight;
      }
    }
  });

  it("explains when the AI rebuild fell back to deterministic text", () => {
    expect(
      synthesisNoticeFromResult({
        used_ai: false,
        message:
          "ИИ-провайдер временно отключён после ошибок соединения.",
        project: { version: 6 },
      }),
    ).toEqual({
      tone: "warning",
      title: "ИИ не использован",
      message:
        "ИИ-провайдер временно отключён после ошибок соединения. " +
        "Версия проекта 6.",
    });
  });

  it("uses the neutral GNIVC LOCAL name after an editorial rebuild", () => {
    expect(
      synthesisNoticeFromResult({
        used_ai: true,
        message:
          "Отчёт отредактирован в GNIVC LOCAL по проверенным агрегатам.",
        project: { version: 7 },
      }),
    ).toEqual({
      tone: "success",
      title: "Отчёт отредактирован в GNIVC LOCAL",
      message:
        "Отчёт отредактирован в GNIVC LOCAL по проверенным агрегатам. " +
        "Версия проекта 7.",
    });
  });

  it("renders generated chat series as a compact report chart", () => {
    const block: ReportChartBlock = {
      id: "project-chart",
      type: "chart",
      title: "Динамика по выбранным отраслям",
      context:
        "Поступления в консолидированный бюджет · Московская область · все МСП",
      mode: "change",
      unit: "тыс. руб.",
      editable: false,
      series: [
        {
          key: "construction",
          label: "Строительство",
          unit: "тыс. руб.",
          points: [
            {
              period_end: "2025-06-30",
              label: "январь–июнь 2025",
              value: "1000000",
            },
            {
              period_end: "2026-06-30",
              label: "январь–июнь 2026",
              value: "900000",
            },
          ],
        },
        {
          key: "trade",
          label: "Торговля",
          unit: "тыс. руб.",
          points: [
            {
              period_end: "2025-06-30",
              label: "январь–июнь 2025",
              value: "2000000",
            },
            {
              period_end: "2026-06-30",
              label: "январь–июнь 2026",
              value: "2400000",
            },
          ],
        },
      ],
    };
    render(<ReportChartBlockView block={block} />);

    expect(
      screen.getByLabelText(
        "График: Динамика по выбранным отраслям",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Поступления в консолидированный бюджет · Московская область · все МСП",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Строительство")).toBeInTheDocument();
    expect(screen.getByText("Торговля")).toBeInTheDocument();
    expect(screen.getByText("% к первому периоду")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Линейный график нескольких периодов"),
    ).toBeInTheDocument();
  });

  it("uses grouped bars for an industry comparison in one period", () => {
    const block: ReportChartBlock = {
      id: "industry-bars",
      type: "chart",
      title: "Отраслевое сравнение",
      context: "Московская область · все МСП",
      chart_type: "bar",
      mode: "absolute",
      unit: "тыс. руб.",
      editable: false,
      series: [
        {
          key: "construction",
          label: "Строительство",
          unit: "тыс. руб.",
          points: [
            {
              period_end: "2026-06-30",
              label: "январь–июнь 2026",
              value: "1000000",
            },
          ],
        },
        {
          key: "trade",
          label: "Торговля",
          unit: "тыс. руб.",
          points: [
            {
              period_end: "2026-06-30",
              label: "январь–июнь 2026",
              value: "2000000",
            },
          ],
        },
      ],
    };
    render(<ReportChartBlockView block={block} />);

    expect(
      screen.getByLabelText("Столбчатая диаграмма одного периода"),
    ).toBeInTheDocument();
    expect(screen.queryByText("% к первому периоду")).not.toBeInTheDocument();
  });
});


describe("уточнение запроса", () => {
  it("renders the planner clarification without executing facts", async () => {
    vi.spyOn(backendApi, "planQuery").mockResolvedValue({
      question: "Покажи значение",
      conversation_id: "conversation-test",
      status: "needs_clarification",
      plan: null,
      resolved_metrics: [],
      planner_mode: "deterministic",
      warnings: [],
      requires_confirmation: false,
      clarification_question: "Какой именно показатель нужен?",
      clarification_options: ["НДС", "Страховые взносы"],
    });
    render(
      <AnalysisWorkspace
        onNavigate={() => undefined}
        onShowInspector={() => undefined}
        onToast={() => undefined}
        onResult={() => undefined}
      />,
    );
    expect(
      await screen.findByText("Какой именно показатель нужен?"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "НДС" })).toBeInTheDocument();
  });

  it("submits a typed confirmation to the pending clarification", async () => {
    vi.spyOn(backendApi, "conversation").mockResolvedValue({
      id: "conversation-clarification",
      title: "Проверка периода",
      status: "draft",
      pinned: false,
      archived: false,
      context: "Новый аналитический диалог",
      created_at: "2026-08-02T10:00:00",
      updated_at: "2026-08-02T10:00:00",
      project_id: null,
      turns_count: 0,
      state: {},
      turns: [],
      project: null,
    });
    vi.spyOn(backendApi, "createConversationTurn").mockResolvedValue({
      query_id: "turn-clarification",
      turn_id: "turn-clarification",
      conversation_id: "conversation-clarification",
      question: "Сравни поступления за 2022 и 2026",
      status: "needs_clarification",
      query_plan: null,
      resolved_metrics: [],
      facts: [],
      calculations: [],
      series: [],
      visualization: null,
      warnings: [],
      answer: "Хотите использовать 2025 и 2026?",
      answer_mode: "clarification",
      numbers_verified: true,
      clarification_question: "Хотите использовать 2025 и 2026?",
      clarification_options: [],
    });
    const readyResult: QueryResult = {
      query_id: "turn-clarification",
      turn_id: "turn-clarification",
      conversation_id: "conversation-clarification",
      question: "Сравни поступления за 2022 и 2026",
      status: "ready",
      query_plan: {
        intent: "compare",
        metric_codes: ["receipts_budget_total"],
        enterprise_groups: ["all_msp"],
        territories: ["Ленинградская область", "Свердловская область"],
        row_codes: [1010],
        periods: ["2025-03-31", "2026-03-31"],
        operations: ["value", "difference", "growth_rate", "sources"],
        group_by: [],
        ranking_criteria: [],
        limit: null,
      },
      resolved_metrics: [],
      facts: [],
      calculations: [],
      series: [],
      visualization: null,
      warnings: [],
      answer: "Сравнение выполнено за 2025 и 2026 годы.",
      answer_mode: "deterministic",
      numbers_verified: true,
    };
    const answerClarification = vi
      .spyOn(backendApi, "answerClarification")
      .mockResolvedValue(readyResult);
    vi.spyOn(backendApi, "createAnalysisRun").mockResolvedValue({
      run_id: "turn-clarification",
      turn_id: "turn-clarification",
      conversation_id: "conversation-clarification",
      status: "queued",
      progress: 0,
      result: null,
      events_url: "/api/analysis-runs/turn-clarification/events",
    });
    vi.spyOn(backendApi, "waitForAnalysisRun")
      .mockResolvedValueOnce({
        run_id: "turn-clarification",
        turn_id: "turn-clarification",
        conversation_id: "conversation-clarification",
        status: "needs_clarification",
        progress: 10,
        clarification: {
          question: "Хотите использовать 2025 и 2026?",
          options: [],
        },
        result: null,
        events_url: "/api/analysis-runs/turn-clarification/events",
      })
      .mockResolvedValueOnce({
        run_id: "turn-clarification",
        turn_id: "turn-clarification",
        conversation_id: "conversation-clarification",
        status: "ready",
        progress: 100,
        result: readyResult,
        events_url: "/api/analysis-runs/turn-clarification/events",
      });
    const clarifyAnalysisRun = vi
      .spyOn(backendApi, "clarifyAnalysisRun")
      .mockResolvedValue({
        run_id: "turn-clarification",
        turn_id: "turn-clarification",
        conversation_id: "conversation-clarification",
        status: "queued",
        progress: 0,
        result: null,
        events_url: "/api/analysis-runs/turn-clarification/events",
      });
    const user = userEvent.setup();
    render(
      <AnalysisWorkspace
        activeConversationId="conversation-clarification"
        onNavigate={() => undefined}
        onShowInspector={() => undefined}
        onToast={() => undefined}
        onResult={() => undefined}
      />,
    );
    const composer = screen.getByPlaceholderText(
      "Задайте уточняющий вопрос или продолжите анализ…",
    );
    await user.type(composer, "Сравни поступления за 2022 и 2026");
    await user.click(screen.getByRole("button", { name: "Отправить запрос" }));
    expect(
      await screen.findByText("Хотите использовать 2025 и 2026?"),
    ).toBeInTheDocument();

    await user.type(composer, "да");
    await user.click(screen.getByRole("button", { name: "Отправить запрос" }));

    await waitFor(() => {
      expect(
        answerClarification.mock.calls.length
        + clarifyAnalysisRun.mock.calls.length,
      ).toBe(1);
    });
    if (clarifyAnalysisRun.mock.calls.length) {
      expect(clarifyAnalysisRun).toHaveBeenCalledWith(
        "turn-clarification",
        { text: "да" },
        expect.anything(),
      );
    } else {
      expect(answerClarification).toHaveBeenCalledWith(
        "conversation-clarification",
        "turn-clarification",
        { text: "да" },
        expect.anything(),
      );
    }
    expect(
      await screen.findByText("Сравнение выполнено за 2025 и 2026 годы."),
    ).toBeInTheDocument();
  });
});


describe("сохранённое рабочее пространство", () => {
  it("offers report generation after loading an old conversation", async () => {
    const oldResult: QueryResult = {
      query_id: "turn-old",
      turn_id: "turn-old",
      conversation_id: "conversation-old",
      question: "Какая динамика?",
      status: "ready",
      query_plan: null,
      resolved_metrics: [],
      facts: [],
      calculations: [],
      series: [],
      warnings: [],
      answer: "Проверенный ответ.",
      answer_mode: "analytic_agent_v2",
      numbers_verified: true,
    };
    vi.spyOn(backendApi, "conversation").mockResolvedValue({
      id: "conversation-old",
      title: "Старый диалог",
      status: "draft",
      pinned: false,
      archived: false,
      context: "Поступления МСП",
      created_at: "2026-07-20T10:00:00",
      updated_at: "2026-07-29T10:00:00",
      project_id: "project-old",
      turns_count: 1,
      state: {},
      turns: [
        {
          id: "turn-old",
          sequence: 1,
          question: "Какая динамика?",
          status: "ready",
          turn_type: "analysis",
          plan: null,
          result: oldResult,
          clarification: null,
          created_at: "2026-07-20T10:00:00",
        },
      ],
      project: null,
    });
    const openProject = vi.fn();
    const user = userEvent.setup();
    render(
      <AnalysisWorkspace
        activeConversationId="conversation-old"
        onNavigate={() => undefined}
        onShowInspector={() => undefined}
        onToast={() => undefined}
        onResult={() => undefined}
        onOpenProject={openProject}
      />,
    );

    expect(await screen.findByText("Проверенный ответ.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Добавить в отчёт" }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Сформировать отчёт" }),
    );
    expect(openProject).toHaveBeenCalledWith("conversation-old");
  });

  it("creates a persistent turn and wires project/scenario actions", async () => {
    vi.spyOn(backendApi, "conversation").mockResolvedValue({
      id: "conversation-live",
      title: "Новый диалог",
      status: "draft",
      pinned: false,
      archived: false,
      context: "Новый аналитический диалог",
      created_at: "2026-07-29T10:00:00",
      updated_at: "2026-07-29T10:00:00",
      project_id: null,
      turns_count: 0,
      state: {},
      turns: [],
      project: null,
    });
    const readyResult: QueryResult = {
      query_id: "turn-live",
      turn_id: "turn-live",
      conversation_id: "conversation-live",
      question: "В каком регионе лучше всего",
      status: "ready",
      query_plan: {
        intent: "rank",
        metric_codes: ["receipts_budget_total"],
        enterprise_groups: ["all_msp"],
        territories: ["Российская Федерация"],
        row_codes: [1010],
        periods: ["2026-03-31"],
        operations: ["value", "difference", "growth_rate", "sources"],
        group_by: ["territory"],
        analysis_dimension: "territory",
        ranking_criteria: ["value", "growth_rate"],
        limit: 5,
      },
      resolved_metrics: [],
      facts: [],
      calculations: [],
      series: [],
      analysis_sections: [
        {
          kind: "ranking_value",
          title: "Лидеры по текущему абсолютному значению",
          rows: [
            {
              entity: "Москва",
              current: "1000000",
              previous: "900000",
              difference: "100000",
              growth_rate: "11.1",
            },
          ],
          methodology: "Проверенный накопительный срез.",
        },
      ],
      visualization: null,
      warnings: [],
      answer: "Лидер по абсолютному значению — Москва.",
      answer_mode: "deterministic",
      numbers_verified: true,
    };
    vi.spyOn(backendApi, "createConversationTurn").mockResolvedValue(
      readyResult,
    );
    vi.spyOn(backendApi, "createAnalysisRun").mockResolvedValue({
      run_id: "turn-live",
      turn_id: "turn-live",
      conversation_id: "conversation-live",
      status: "queued",
      progress: 0,
      result: null,
      events_url: "/api/analysis-runs/turn-live/events",
    });
    vi.spyOn(backendApi, "waitForAnalysisRun").mockResolvedValue({
      run_id: "turn-live",
      turn_id: "turn-live",
      conversation_id: "conversation-live",
      status: "ready",
      progress: 100,
      result: readyResult,
      events_url: "/api/analysis-runs/turn-live/events",
    });
    const addBlock = vi
      .spyOn(backendApi, "addProjectBlock")
      .mockResolvedValue({
        block_id: "block-live",
        added: true,
        project: {
          id: "project-live",
          conversation_id: "conversation-live",
          title: "Проект",
          version: 2,
          blocks_count: 1,
          document: {
            version: 2,
            blocks: [],
            paragraph_count: 0,
            table_count: 0,
          },
          download_url: null,
          created_at: "2026-07-29T10:00:00",
          updated_at: "2026-07-29T10:00:00",
        },
      });
    const saveScenario = vi
      .spyOn(backendApi, "saveScenario")
      .mockResolvedValue({
        id: "scenario-live",
        title: "Сценарий — Новый диалог",
        source_conversation_id: "conversation-live",
        steps: [],
        created_at: "2026-07-29T10:00:00",
        updated_at: "2026-07-29T10:00:00",
      });
    const user = userEvent.setup();
    render(
      <AnalysisWorkspace
        activeConversationId="conversation-live"
        onNavigate={() => undefined}
        onShowInspector={() => undefined}
        onToast={() => undefined}
        onResult={() => undefined}
      />,
    );
    expect(
      await screen.findByText("Новый аналитический диалог"),
    ).toBeInTheDocument();
    await user.type(
      screen.getByPlaceholderText(
        "Задайте уточняющий вопрос или продолжите анализ…",
      ),
      "В каком регионе лучше всего",
    );
    await user.click(screen.getByRole("button", { name: "Отправить запрос" }));
    expect(
      await screen.findByText("Лидер по абсолютному значению — Москва."),
    ).toBeInTheDocument();
    expect(screen.getByText("Москва")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Добавить в отчёт" }),
    );
    expect(addBlock).toHaveBeenCalledWith(
      "conversation-live",
      "turn-live",
    );
    await user.click(
      screen.getByRole("button", { name: "Сохранить сценарий" }),
    );
    expect(saveScenario).toHaveBeenCalledWith("conversation-live");
  });
});


describe("канонический Web-документ", () => {
  it("renders the full document and ignores a Strict Mode abort", async () => {
    const blocks: ReportDocument["blocks"] = [
      ...Array.from({ length: 20 }, (_, index) => ({
        id: `paragraph-${index}`,
        type: "paragraph" as const,
        text: `Абзац ${index + 1}`,
        style: index === 0 ? "Heading 1" : "Normal",
        editable: true,
      })),
      ...Array.from({ length: 4 }, (_, index) => ({
        id: `table-${index}`,
        type: "table" as const,
        rows: [[`Таблица ${index + 1}`], ["Расчётная ячейка"]],
        editable: false as const,
      })),
    ];
    const documentModel: ReportDocument = {
      version: 1,
      blocks,
      paragraph_count: 20,
      table_count: 4,
    };
    sessionStorage.setItem("ai-vector-report-id", "77");
    let requests = 0;
    vi.spyOn(backendApi, "reportDocument").mockImplementation(
      (_reportId, signal) => {
        requests += 1;
        if (requests > 1) {
          return Promise.resolve(documentModel);
        }
        return new Promise((_resolve, reject) => {
          signal?.addEventListener(
            "abort",
            () =>
              reject(
                new DOMException("Fetch is aborted", "AbortError"),
              ),
            { once: true },
          );
        });
      },
    );
    render(
      <StrictMode>
        <LiveReportWorkspace
          role="Аналитик"
          status="draft"
          setStatus={() => undefined}
          onBack={() => undefined}
          onToast={() => undefined}
        />
      </StrictMode>,
    );
    await waitFor(() =>
      expect(screen.getAllByRole("textbox")).toHaveLength(20),
    );
    expect(screen.getAllByRole("table")).toHaveLength(4);
    expect(screen.getByDisplayValue("Абзац 20")).toBeInTheDocument();
    expect(screen.getByText("Таблица 4")).toBeInTheDocument();
    expect(screen.queryByText("Fetch is aborted")).not.toBeInTheDocument();
    expect(requests).toBeGreaterThanOrEqual(2);
  });
});
