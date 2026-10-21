import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GeneratorWorkspace } from "./App";
import {
  apiClient,
  backendApi,
  type ReportParameterCatalog,
} from "./api";
import type { DataContext } from "./types";


const catalog: ReportParameterCatalog = {
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
    { name: "г. Москва", aliases: ["Москва"] },
    {
      name: "Карачаево-Черкесская Республика",
      aliases: [],
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

const context: DataContext = {
  form: "1-МСП",
  territory: "Российская Федерация",
  reportingPeriod: "Январь–июнь 2026",
  comparisonPeriod: "Январь–июнь 2025",
  dataVersion: "Контрольная пара",
};


afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.clear();
});


describe("стандартная региональная справка", () => {
  it("fills the form from a command and waits for explicit confirmation", async () => {
    vi.spyOn(backendApi, "reportParameters").mockResolvedValue(catalog);
    vi.spyOn(backendApi, "interpretReportCommand").mockResolvedValue({
      status: "ready",
      report_mode: "region_vs_rf",
      territory: "Республика Саха (Якутия)",
      benchmark: "Российская Федерация",
      old_period_end: "2025-03-31",
      new_period_end: "2026-03-31",
      clarification_question: null,
      no_data_reason: null,
    });
    const generate = vi.spyOn(apiClient, "generateReport").mockResolvedValue({
      id: 77,
      status: "draft",
      old_period_end: "2025-03-31",
      new_period_end: "2026-03-31",
      download_url: "/api/reports/77/download/docx",
      facts_url: "/api/reports/77/facts",
      document_url: "/api/reports/77/document",
      numbers_verified: true,
    });
    const user = userEvent.setup();
    render(
      <GeneratorWorkspace
        context={context}
        onContextChange={() => undefined}
        onOpenReport={() => undefined}
      />,
    );

    await screen.findByRole("option", {
      name: "Республика Саха (Якутия)",
    });
    await user.type(
      screen.getByLabelText("Команда стандартной справки"),
      "Собери стандартную справку по 1-МСП по Якутии",
    );
    await user.click(screen.getByRole("button", { name: "Заполнить форму" }));

    await waitFor(() =>
      expect(screen.getByLabelText("Территория отчёта")).toHaveValue(
        "Республика Саха (Якутия)",
      ),
    );
    expect(screen.getByLabelText("Период сравнения отчёта")).toHaveValue(
      "2025-03-31",
    );
    expect(screen.getByLabelText("Отчётный период отчёта")).toHaveValue(
      "2026-03-31",
    );
    expect(
      screen.getByText(
        "Параметры заполнены. Проверьте их и подтвердите запуск.",
      ),
    ).toBeInTheDocument();
    expect(generate).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "Сформировать отчёт" }),
    );
    await waitFor(() => expect(generate).toHaveBeenCalledOnce());
    expect(generate.mock.calls[0][2]).toEqual({
      report_mode: "region_vs_rf",
      territory: "Республика Саха (Якутия)",
      old_period_end: "2025-03-31",
      new_period_end: "2026-03-31",
    });
  });

  it("keeps manual selectors working when GNIVC LOCAL is unavailable", async () => {
    vi.spyOn(backendApi, "reportParameters").mockResolvedValue(catalog);
    vi.spyOn(backendApi, "interpretReportCommand").mockRejectedValue(
      new Error("GNIVC LOCAL недоступен для интерпретации команды"),
    );
    const user = userEvent.setup();
    render(
      <GeneratorWorkspace
        context={context}
        onContextChange={() => undefined}
        onOpenReport={() => undefined}
      />,
    );
    await screen.findByRole("option", { name: "г. Москва" });
    await user.type(
      screen.getByLabelText("Команда стандартной справки"),
      "Собери справку по Москве",
    );
    await user.click(screen.getByRole("button", { name: "Заполнить форму" }));
    expect(
      await screen.findByText(/Заполните форму вручную/),
    ).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Территория отчёта"), "г. Москва");
    expect(screen.getByLabelText("Территория отчёта")).toHaveValue("г. Москва");
    expect(
      screen.getByRole("button", { name: "Сформировать отчёт" }),
    ).toBeEnabled();
  });
});
