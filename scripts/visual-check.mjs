import { chromium } from "playwright-core";

const executablePath =
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const baseUrl = "http://127.0.0.1:4173/";
const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ["--no-sandbox", "--disable-gpu"],
});

const page = await browser.newPage({
  viewport: { width: 1600, height: 1000 },
  colorScheme: "light",
});
const errors = [];

page.on("console", (message) => {
  if (message.type() === "error") {
    errors.push(`console: ${message.text()}`);
  }
});
page.on("pageerror", (error) => errors.push(`page: ${error.message}`));

await page.goto(baseUrl, { waitUntil: "networkidle" });
await page.screenshot({ path: "/tmp/ai-vector-home-light.png" });

await page
  .getByRole("button", { name: "Проверить и импортировать данные" })
  .click();
await page.getByRole("heading", { name: "Источники формы 1-МСП" }).waitFor();
await page.getByRole("button", { name: /Сканировать каталог/ }).click();
await page.getByText("166").first().waitFor({ timeout: 30_000 });
await page.screenshot({ path: "/tmp/ai-vector-data.png" });

await page.getByRole("button", { name: "Новый диалог" }).first().click();
await page
  .getByRole("button", { name: "Найти неблагоприятные показатели" })
  .click();
await page.getByText("Краткий вывод").waitFor();
await page.getByText("Понято как").waitFor();
await page.getByText("Подтверждённые факты").waitFor();
await page.screenshot({ path: "/tmp/ai-vector-analysis.png" });

await page.getByRole("button", { name: "Новый диалог" }).first().click();
await page.getByRole("button", { name: "Сформировать отчёт 1-МСП" }).click();
await page.getByRole("heading", { name: "Формирование отчёта 1-МСП" }).waitFor();
await page.getByRole("button", { name: "Сформировать отчёт" }).click();
await page.getByRole("button", { name: /Открыть проект отчёта/ }).waitFor({
  timeout: 15_000,
});
await page.getByRole("button", { name: /Открыть проект отчёта/ }).click();
await page.getByRole("button", { name: /Отправить на проверку/ }).click();
await page.getByText("Требует проверки").first().waitFor();
await page.screenshot({ path: "/tmp/ai-vector-report.png" });

await page.reload({ waitUntil: "networkidle" });
await page.getByRole("button", { name: "Создать отчёт по шаблону" }).click();
await page.getByRole("heading", { name: "Шаблоны отчётов" }).waitFor();
await page.getByRole("button", { name: /Использовать/ }).first().click();
await page.getByRole("heading", { name: "Проверка Word-шаблона" }).waitFor();
await page
  .locator(".validation-item.outdated")
  .getByRole("button", { name: "Заменить" })
  .click();
await page.getByRole("button", { name: "Создать тег" }).click();
await page.getByRole("button", { name: "Сохранить шаблон" }).waitFor();
await page.screenshot({ path: "/tmp/ai-vector-template.png" });

await page.setViewportSize({ width: 800, height: 900 });
await page.reload({ waitUntil: "networkidle" });
await page.screenshot({ path: "/tmp/ai-vector-tablet.png" });

console.log(
  JSON.stringify(
    {
      title: await page.title(),
      viewport: page.viewportSize(),
      errors,
      screenshots: [
        "/tmp/ai-vector-home-light.png",
        "/tmp/ai-vector-data.png",
        "/tmp/ai-vector-analysis.png",
        "/tmp/ai-vector-report.png",
        "/tmp/ai-vector-template.png",
        "/tmp/ai-vector-tablet.png",
      ],
    },
    null,
    2,
  ),
);

await browser.close();

if (errors.length > 0) {
  process.exitCode = 1;
}
