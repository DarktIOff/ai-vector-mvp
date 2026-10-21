import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AnalysisWorkspace, App, isStandardReportCommand } from "./App";
import {
  apiClient,
  backendApi,
  type ConversationSummary,
  type ReportParameterCatalog,
  type ReportSpecification,
} from "./api";

const reportCatalog: ReportParameterCatalog = {
  report_modes: [
    {
      code: "region_vs_rf",
      name: "Регион и РФ",
      benchmark: "Российская Федерация",
    },
    { code: "national", name: "Российская Федерация", benchmark: null },
  ],
  territories: [
    {
      name: "Республика Саха (Якутия)",
      aliases: ["Якутия", "Саха"],
    },
  ],
  periods: [
    {
      period_end: "2025-03-31",
      label: "январь–март 2025 года",
      series_kind: "cumulative_ytd",
      months_count: 3,
    },
    {
      period_end: "2026-03-31",
      label: "январь–март 2026 года",
      series_kind: "cumulative_ytd",
      months_count: 3,
    },
    {
      period_end: "2025-06-30",
      label: "январь–июнь 2025 года",
      series_kind: "cumulative_ytd",
      months_count: 6,
    },
    {
      period_end: "2026-06-30",
      label: "январь–июнь 2026 года",
      series_kind: "cumulative_ytd",
      months_count: 6,
    },
  ],
  comparable_pairs: [
    {
      old_period_end: "2025-03-31",
      new_period_end: "2026-03-31",
      old_label: "январь–март 2025 года",
      new_label: "январь–март 2026 года",
    },
  ],
  recommended_pair: {
    old_period_end: "2025-03-31",
    new_period_end: "2026-03-31",
    old_label: "январь–март 2025 года",
    new_label: "январь–март 2026 года",
  },
  national_legacy_pair: {
    old_period_end: "2025-06-30",
    new_period_end: "2026-06-30",
    old_label: "январь–июнь 2025 года",
    new_label: "январь–июнь 2026 года",
  },
  required_benchmark: "Российская Федерация",
};

describe("ИИ-ВЕКТОР", () => {
  it("recognizes only explicit standard-report supercommands", () => {
    expect(
      isStandardReportCommand("Собери мне отчёт по региону Якутия"),
    ).toBe(true);
    expect(isStandardReportCommand("Сформируй отчёт по стране")).toBe(true);
    expect(
      isStandardReportCommand("Подготовь стандартную справку 1-МСП по Москве"),
    ).toBe(true);
    expect(
      isStandardReportCommand("Сравни поступления Якутии и России"),
    ).toBe(false);
    expect(
      isStandardReportCommand("Собери отчёт по результатам этого анализа"),
    ).toBe(false);
  });

  it.each([
    {
      command: "Собери мне отчёт по региону Якутия",
      specification: {
        status: "ready",
        report_mode: "region_vs_rf",
        territory: "Республика Саха (Якутия)",
        benchmark: "Российская Федерация",
        old_period_end: "2025-03-31",
        new_period_end: "2026-03-31",
        clarification_question: null,
        no_data_reason: null,
      } satisfies ReportSpecification,
      expectedMode: "region_vs_rf",
    },
    {
      command: "Сформируй отчёт по стране",
      specification: {
        status: "ready",
        report_mode: "national",
        territory: "Российская Федерация",
        benchmark: null,
        old_period_end: "2025-06-30",
        new_period_end: "2026-06-30",
        clarification_question: null,
        no_data_reason: null,
      } satisfies ReportSpecification,
      expectedMode: "national",
    },
  ])(
    "routes the chat supercommand '$command' to a prefilled generator",
    async ({ command, specification, expectedMode }) => {
      localStorage.clear();
      vi.spyOn(backendApi, "conversations").mockResolvedValue([]);
      vi.spyOn(backendApi, "scenarios").mockResolvedValue([]);
      vi.spyOn(backendApi, "reportParameters").mockResolvedValue(reportCatalog);
      const interpret = vi
        .spyOn(backendApi, "interpretReportCommand")
        .mockResolvedValue(specification);
      const createConversation = vi.spyOn(backendApi, "createConversation");
      const generate = vi.spyOn(apiClient, "generateReport");

      try {
        const user = userEvent.setup();
        render(<App />);
        await user.type(
          screen.getByLabelText("Введите вопрос или задачу"),
          command,
        );
        await user.click(
          screen.getByRole("button", { name: "Отправить запрос" }),
        );

        expect(
          await screen.findByRole("heading", {
            name: "Формирование отчёта 1-МСП",
          }),
        ).toBeInTheDocument();
        await waitFor(() => expect(interpret).toHaveBeenCalledOnce());
        expect(interpret.mock.calls[0][0]).toBe(command);
        await waitFor(() =>
          expect(screen.getByLabelText("Режим отчёта")).toHaveValue(
            expectedMode,
          ),
        );
        expect(
          await screen.findByText(
            "Параметры заполнены. Проверьте их и подтвердите запуск.",
          ),
        ).toBeInTheDocument();
        expect(createConversation).not.toHaveBeenCalled();
        expect(generate).not.toHaveBeenCalled();
      } finally {
        vi.restoreAllMocks();
        localStorage.clear();
      }
    },
  );

  it("routes the same supercommand from an open analytical conversation", async () => {
    vi.spyOn(backendApi, "conversation").mockResolvedValue({
      id: "conversation-route-test",
      title: "Рабочий диалог",
      status: "draft",
      pinned: false,
      archived: false,
      context: "Поступления МСП",
      created_at: "2026-08-02T00:00:00",
      updated_at: "2026-08-02T00:00:00",
      project_id: null,
      turns_count: 0,
      state: {},
      turns: [],
      project: null,
    });
    const createTurn = vi.spyOn(backendApi, "createConversationTurn");
    const openStandardReport = vi.fn();

    try {
      const user = userEvent.setup();
      render(
        <AnalysisWorkspace
          activeConversationId="conversation-route-test"
          onNavigate={() => undefined}
          onShowInspector={() => undefined}
          onToast={() => undefined}
          onResult={() => undefined}
          onOpenStandardReport={openStandardReport}
        />,
      );
      const composer = await screen.findByPlaceholderText(
        "Задайте уточняющий вопрос или продолжите анализ…",
      );
      await user.type(composer, "Собери мне отчёт по региону Якутия");
      await user.click(
        screen.getByRole("button", { name: "Отправить запрос" }),
      );

      expect(openStandardReport).toHaveBeenCalledWith(
        "Собери мне отчёт по региону Якутия",
      );
      expect(createTurn).not.toHaveBeenCalled();
    } finally {
      vi.restoreAllMocks();
    }
  });

  it("renders the start workspace", () => {
    render(<App />);
    expect(screen.getByText("Добрый день, Геворг")).toBeInTheDocument();
    expect(
      screen.getByText("Сформировать отчёт 1-МСП"),
    ).toBeInTheDocument();
  });

  it("uses the real MVP mode by default", () => {
    render(<App />);
    expect(screen.getByText("РАБОЧИЙ MVP")).toBeInTheDocument();
    expect(screen.getByText("Локальные книги 1-МСП")).toBeInTheDocument();
  });

  it("returns to the start workspace from the brand button", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(
      screen.getByText("Сформировать отчёт 1-МСП").closest("button")!,
    );
    expect(
      screen.getByRole("heading", { name: "Формирование отчёта 1-МСП" }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", {
        name: "Перейти на главную страницу ИИ-ВЕКТОР",
      }),
    );
    expect(screen.getByText("Добрый день, Геворг")).toBeInTheDocument();
  });

  it("permanently deletes an old conversation from the sidebar", async () => {
    const conversation: ConversationSummary = {
      id: "old-conversation",
      title: "Старый диалог",
      status: "draft",
      pinned: false,
      archived: false,
      context: "Поступления МСП",
      created_at: "2026-01-01T00:00:00",
      updated_at: "2026-01-01T00:00:00",
      project_id: null,
      turns_count: 2,
    };
    vi.spyOn(backendApi, "conversations")
      .mockResolvedValueOnce([conversation])
      .mockResolvedValueOnce([]);
    vi.spyOn(backendApi, "scenarios").mockResolvedValue([]);
    const deleteConversation = vi
      .spyOn(backendApi, "deleteConversation")
      .mockResolvedValue({
        id: conversation.id,
        deleted: true,
      });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    try {
      const user = userEvent.setup();
      render(<App />);
      expect(await screen.findByText("Старый диалог")).toBeInTheDocument();

      await user.click(
        screen.getByRole("button", {
          name: "Удалить диалог Старый диалог",
        }),
      );

      expect(confirm).toHaveBeenCalledOnce();
      await waitFor(() =>
        expect(deleteConversation).toHaveBeenCalledWith(
          conversation.id,
        ),
      );
      await waitFor(() =>
        expect(screen.queryByText("Старый диалог")).not.toBeInTheDocument(),
      );
      expect(await screen.findByText("Диалог удалён")).toBeInTheDocument();
    } finally {
      vi.restoreAllMocks();
    }
  });
});
