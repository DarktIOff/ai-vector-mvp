# Техническое руководство

## Структура проекта

| Каталог | Содержимое |
| --- | --- |
| `src/` | React-интерфейс, клиент API, компоненты и frontend-тесты |
| `backend/app/` | FastAPI, импорт, расчёты, аналитический агент и отчёты |
| `backend/tests/` | Backend-тесты и интеграционные проверки |
| `backend/certification/` | Корпус вопросов, независимые эталоны и сертификация |
| `scripts/` | Аудит данных, визуальная проверка и упаковка исходников |
| `rd/` | Локальные исходные книги и шаблоны; исключён из Git |
| `var/` | SQLite, журналы и созданные документы; исключён из Git |
| `artifacts/` | Результаты проверок; исключён из Git |

Frontend использует React, TypeScript, Vite и Recharts. Backend построен
на FastAPI, SQLAlchemy и SQLite. Импорт поддерживает несколько схем формы
1-МСП, включая общероссийские и региональные книги.

## Путь данных

1. Сканирование определяет формат книги, период и схему показателей.
2. Импорт сохраняет факты вместе с контрольной суммой файла и координатами
   исходной ячейки. Неизвестные схемы и повреждённые книги попадают в карантин.
3. Планировщик сопоставляет вопрос с показателями, территориями, периодами,
   группами МСП и отраслевыми строками.
4. Аналитические инструменты выполняют расчёты и возвращают ссылки на источники.
5. Результат отображается в чате, сохраняется в проекте или используется
   при формировании Word-документа.

Чаты, ходы, проекты и сценарии сохраняются в SQLite. Фоновые аналитические
задачи выполняются в процессе backend.

## Раздельный запуск

Подготовка окружения описана в [README](README.md#установка-и-запуск).
В первом терминале активируйте окружение и запустите API:

```bash
source .venv/bin/activate
python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Во втором терминале:

```bash
npm run dev
```

Vite проксирует `/api` на `http://127.0.0.1:8000`.
После изменения `.env` перезапустите backend: настройки и провайдеры
кэшируются в процессе.

## Источники и хранение

Основные настройки в `.env`:

```dotenv
MSP_HISTORY_ROOT=./rd/1_msp_db
AUTOMSP_ROOT=./rd/AutoMSP
MSP_REPORT_TEMPLATE=./rd/AutoMSP/template.docx
MSP_CODES_FILE=./rd/AutoMSP/Коды.xlsx
APP_DATA_DIR=./var
DATABASE_URL=sqlite:///./var/ai_vector.db
```

Для анализа нужны импортированные факты. Для генерации отчёта дополнительно
нужны соответствующие контрольные книги, справочник кодов и Word-шаблон.
Пустая база создаётся при запуске; импорт выполняется через экран «Данные»
или API. Состав доступных периодов и территорий зависит от загруженных книг.

`.gitignore` исключает исходные каталоги, Excel-файлы, базы, `.env`, окружения,
сборки и результаты запусков. Тесты и тестовые корпуса содержат эталонные
числа; они входят в исходный код.

## Провайдеры моделей

Для работы без внешнего провайдера:

```dotenv
LLM_PROVIDER=deterministic
REPORT_SYNTHESIS_PROVIDER=deterministic
```

Пример настройки Z.AI:

```dotenv
LLM_PROVIDER=zai
LLM_QUERY_MODEL=glm-5.1
LLM_NARRATIVE_MODEL=glm-5.1
ZAI_API_KEY=
LLM_DATA_POLICY=metadata_only
```

Пример настройки GNIVC:

```dotenv
LLM_PROVIDER=gnivc_qwen
GNIVC_BASE_URL=https://api.ai.gnivc.ru/v1
GNIVC_API_KEY=
GNIVC_QUERY_MODEL=qwen3.6-35b
GNIVC_NARRATIVE_MODEL=qwen3.5-122b
```

Ключи заполняются в локальном `.env`. Доступ к GNIVC требует разрешённой сети
или VPN. Проверка соединения из активированного окружения:

```bash
python -m backend.check_provider
```

`LLM_DATA_POLICY=metadata_only` ограничивает модель метаданными.
Для аналитических вызовов с передачей агрегатов нужны обе настройки:

```dotenv
LLM_DATA_POLICY=aggregates
ALLOW_EXTERNAL_LLM_DATA=true
```

Выбор провайдера сам по себе не включает передачу реальных значений.

Редакционная обработка отчёта настраивается отдельно от аналитического чата:

```dotenv
REPORT_SYNTHESIS_PROVIDER=gnivc_gigachat
REPORT_SYNTHESIS_MODEL=GigaChat-Max
```

Она использует `GNIVC_BASE_URL` и `GNIVC_API_KEY`. Backend проверяет
возвращённую структуру и числа; при ошибке сохраняет детерминированную версию.

## Асинхронный аналитический агент

Для переключения интерфейса на `analysis-runs` и SSE-прогресс задайте:

```dotenv
ANALYTIC_AGENT_V2=true
VITE_ANALYTIC_AGENT_V2=true
```

Перезапустите frontend и backend. Агент строит план исследования, вызывает
типизированные инструменты и возвращает результаты с источниками,
проверенными выводами и отдельно обозначенными гипотезами.

Таймаут отдельного вызова модели задаётся `LLM_TIMEOUT_SECONDS`.
`ANALYTIC_PLANNER_EMERGENCY_MAX_STEPS` ограничивает зацикливание планировщика;
это не обычный лимит числа вызовов для анализа. Актуальные настройки
и значения по умолчанию находятся в `backend/app/config.py`.

## API

Интерактивная схема: <http://127.0.0.1:8000/docs>.

| Назначение | Основные маршруты |
| --- | --- |
| Сканирование и импорт | `POST /api/data/scan`, `POST /api/data/import` |
| Периоды и источники | `GET /api/catalog/periods`, `GET /api/catalog/sources/{fact_id}` |
| План и запрос | `POST /api/query/plan`, `POST /api/query` |
| Чаты | `GET/POST /api/conversations`, `POST /api/conversations/{id}/turns` |
| Аналитический запуск | `POST /api/conversations/{id}/analysis-runs` |
| Состояние и прогресс | `GET /api/analysis-runs/{id}`, `GET /api/analysis-runs/{id}/events` |
| Отмена и уточнение | `POST /api/analysis-runs/{id}/cancel`, `POST /api/analysis-runs/{id}/clarification` |
| Проекты | `GET /api/conversations/{id}/project`, `POST /api/conversations/{id}/project/blocks` |
| Параметры отчёта | `GET /api/reports/1-msp/parameters`, `POST /api/reports/1-msp/interpret` |
| Формирование отчёта | `POST /api/reports/1-msp/preview`, `POST /api/reports/1-msp/generate` |
| Документ | `GET/PATCH /api/reports/{id}/document`, `GET /api/reports/{id}/download/docx` |
| Состояние модели | `GET /api/ai/status`, `POST /api/ai/test` |

## Тесты и сертификация

```bash
python -m pytest
npm test -- --exclude 'handoff/**'
npm run build
```

Frontend-тесты работают с локальными фикстурами. Полный backend-набор зависит
от исходных книг и импортированного каталога. Сейчас не все такие проверки
помечены как интеграционные: отсутствие данных может приводить к падению
тестов вместо пропуска. Для автономного запуска потребуется отделить
интеграционные проверки и добавить синтетические наборы данных.

Для live-проверки команд отчёта при настроенном Qwen:

```bash
RUN_LIVE_REPORT_COMMANDS=1 python -m pytest backend/tests/test_report_commands_live.py
```

Сертификация использует независимый `Decimal`-эталон и проверяет
пользовательский путь через асинхронный API. После загрузки данных:

```bash
./analytic-certification --materialize
./analytic-certification --audit-sources
```

Live-прогон требует работающий backend, настроенный GNIVC Qwen и режим
передачи агрегатов:

```bash
./analytic-certification --network-check
./analytic-certification --tier smoke
./analytic-certification --tier full
./analytic-certification --resume
```

Корпус вопросов находится в `backend/certification/corpus/`.
Результаты записываются в `artifacts/analytic-certification/<run-id>/`:
JSONL, HTML, JUnit, покрытие и файлы воспроизведения. В сертификационном
режиме повторы и резервный разбор отключены; результат PASS требует
прохождения каждого выбранного сценария и аудита источников.

## Ограничения

- Нет аутентификации и разграничения доступа.
- SQLite, файловое хранилище и фоновые задачи привязаны к одному экземпляру backend.
- Для многопользовательского развёртывания нужны миграции, мониторинг
  и отдельная организация хранения и выполнения задач.
- Поддержка форматов зависит от схемы книги; результаты сканирования
  и карантин нужно проверять перед импортом.
