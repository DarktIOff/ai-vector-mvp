import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TrendSeriesChart } from "./App";
import type { QuerySeries } from "./api";
import fixture from "./test/generated-certification-oracle.json";


describe("сертификационное отображение oracle", () => {
  it("renders every oracle point in the chart's accessible table", () => {
    const series = fixture.series as QuerySeries;
    render(<TrendSeriesChart series={[series]} />);

    expect(
      screen.getByLabelText("Линейный график многолетней динамики"),
    ).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(
      series.points.length + 1,
    );
    for (const point of series.points) {
      expect(table).toHaveTextContent(point.label);
      const billions = Number(point.value) / 1_000_000;
      const formatted = new Intl.NumberFormat("ru-RU", {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        }).format(billions);
      expect(table.textContent?.replace(/\s/gu, " ")).toContain(
        formatted.replace(/\s/gu, " "),
      );
    }
  });

  it("keeps oracle periods in chronological order", () => {
    const series = fixture.series as QuerySeries;
    render(<TrendSeriesChart series={[series]} />);
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.textContent)).toEqual(
      series.points.map((point) =>
        expect.stringContaining(point.label),
      ),
    );
  });
});
