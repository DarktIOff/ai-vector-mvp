# ИИ-ВЕКТОР — рабочий MVP 1-МСП

Локальное приложение для импорта, анализа и формирования отчёта по форме
1-МСП. Реализация следует `REAL_MVP_IMPLEMENTATION_BRIEF_ИИ-ВЕКТОР.md`:
официальные числа рассчитываются детерминированно, каждый факт связан с
исходной книгой/листом/ячейкой, а языковая модель не используется как
калькулятор.

Для передачи разработчикам и переносимого запуска в новом окружении см.
`DEVELOPER_HANDOFF.md`: исходные данные, локальная база и ключи в Git не входят.
Примеры ниже с абсолютным путём к Python относятся к исходной рабочей машине;
в новом окружении используйте активированную `.venv` и переменную
`AI_VECTOR_PYTHON`, как описано в инструкции передачи.

## Что реализовано

- инвентаризация 166 исторических `.xls/.xlsx` за 2022–2026 годы;
- адаптеры для настоящих `.xls`, `.xlsx` и двух OOXML-книг с ошибочным
  расширением `.xls`;
- версионируемые схемы граф, каталог 44 показателей, строк и территорий;
- импорт сводных книг в SQLite с SHA-256, периодом, ячейкой и исходным
  значением; неизвестные схемы и физически повреждённые книги помещаются
  в карантин;
- потоковый адаптер книг «В разрезе субъектов РФ» / «Раздел 1–2»:
  выбирает лист по строке или ОКВЭД, территорию по строке и показатель
  по номеру графы; источником результата остаётся конкретная ячейка;
- LLM-планировщик запросов на русском с проверяемым JSON-планом:
  распознаёт периоды, кварталы, номер графы, показатель, группу МСП,
  строку и код ОКВЭД; при недоступности провайдера используется
  детерминированный резерв;
- детерминированное выполнение плана: значение, сравнение, источник,
  микро, отдельный квартал из накопительных данных и производный
  Раздел III (`Раздел I − Раздел II`);
- воспроизведение расчётов `AutoMSP.bas` на контрольной паре
  01.07.2025/01.07.2026;
- заполнение настоящего `template.docx`, включая динамическую таблицу
  по 20 отраслям, и выгрузка Word;
- HTTP API, экран «Данные», видимый план запроса, паспорт источника и
  статус ИИ-провайдера;
- рабочее пространство на SQLite: чаты, ходы, уточнения, закрепление,
  аналитические проекты и сценарии сохраняются после перезапуска;
- многоходовый контекст беседы наследует показатель, период, группу,
  территорию и ОКВЭД; рейтинги по уровню и росту и диагностика
  региональных выбросов рассчитываются детерминированно;
- каждый чат имеет собственный проект: выбранные ответы, таблицы и графики
  формируют единый Web/Word-документ;
- явный отдельный демо-режим; по умолчанию frontend работает с backend.

## Запуск одной командой

Из каталога проекта:

```bash
./run_mvp.sh
```

Скрипт одновременно запускает backend и frontend, а `Ctrl+C` корректно
останавливает оба процесса. При первом запуске либо после изменения
зависимостей:

```bash
./run_mvp.sh --install
```

Путь к Python по умолчанию:
`/Users/Melchior@Magi/Virtual Environment/gen_venv/bin/python`. При
необходимости его можно переопределить:

```bash
AI_VECTOR_PYTHON=/другой/путь/python ./run_mvp.sh
```

После запуска откройте `http://localhost:4173`.

## Ручной запуск

Python-команды проекта выполняются в указанном окружении:

```bash
'/Users/Melchior@Magi/Virtual Environment/gen_venv/bin/python' -m pip install -r requirements.txt
```

Терминал 1 — backend:

```bash
PYTHONPATH=. '/Users/Melchior@Magi/Virtual Environment/gen_venv/bin/python' -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Терминал 2 — frontend:

```bash
npm install
npm run dev
```

Откройте `http://localhost:4173`. Vite проксирует `/api` на
`http://127.0.0.1:8000`.

На экране «Данные»:

1. Нажмите «Сканировать каталог».
2. Проверьте число готовых и карантинных книг.
3. Нажмите «Импортировать готовые».

Для быстрой проверки анализа достаточно контрольной пары из `rd/AutoMSP`;
она может быть импортирована через API:

```bash
curl -X POST http://127.0.0.1:8000/api/data/import \
  -H 'Content-Type: application/json' \
  -d '{"paths":["rd/AutoMSP/53778127_1-МСП.xlsx","rd/AutoMSP/75638291_1-МСП.xlsx"]}'
```

## Проверки

```bash
PYTHONPATH=. '/Users/Melchior@Magi/Virtual Environment/gen_venv/bin/python' -m pytest
npm test
npm run build
```

Python-тесты проверяют состав архива, Unicode-нормализацию, ошибочные
расширения, деление на ноль, контрольные суммы AutoMSP, координаты источников,
региональный oracle Якутии и отсутствие незаполненных маркеров в итоговом
`.docx`. Налоговая таблица физически содержит пять колонок, а заголовок
«Приложения» всегда начинается с новой страницы, в том числе после правки.

Для live-проверки интерпретации трёх команд (Якутия, Москва и
Карачаево-Черкесская Республика) при доступном Qwen:

```bash
RUN_LIVE_REPORT_COMMANDS=1 PYTHONPATH=. \
  '/Users/Melchior@Magi/Virtual Environment/gen_venv/bin/python' \
  -m pytest backend/tests/test_report_commands_live.py
```

## Сертификация аналитических вопросов

Release-контур отделён от production-планировщика. Он строит capability
manifest непосредственно из импортированных фактов и метаданных книг,
считает эталонные значения собственным `Decimal`-oracle, а пользовательский
путь проверяет через настоящий асинхронный `analysis-runs` API. Для
сертификационного запуска API автоматически отключает повторы и запрещает
deterministic fallback; обычные пользовательские запросы сохраняют прежний
контракт.

Перед live-прогоном backend должен работать на `127.0.0.1:8000`, а в `.env`
должны быть настроены `LLM_PROVIDER=gnivc_qwen`,
`GNIVC_QUERY_MODEL=qwen3.6-35b`, передача агрегатов и доступ к локальной сети
GNIVC. После подключения к локальной сети сначала можно независимо проверить
само соединение с моделью (backend для этой команды не нужен):

```bash
./analytic-certification --network-check
```

Команда выполняет реальный healthcheck и четыре одновременных JSON-запроса к
`qwen3.6-35b`. Повторы и fallback отключены; текст ответов, API-ключи и иные
секреты не печатаются. Успех — код возврата `0`, `"passed": true` и четыре
элемента `parallel` со статусом `success`.

Команды live-сертификации:

```bash
./analytic-certification --tier smoke
./analytic-certification --tier full
./analytic-certification --case metric-reference-313d4033d545f5bb
./analytic-certification --resume
./analytic-certification --tier full --shard 1/4
```

`smoke` содержит ровно 48 сценариев. Полный каталог содержит не менее 1 200
одиночных вопросов, 120 диалогов по 2–5 ходов и 100 негативных сценариев.
Точный версионируемый перечень находится в
`backend/certification/corpus/questions.md`, ручные регрессии — в
`regressions.yaml`. После изменения данных они обновляются без live-вызовов:

```bash
./analytic-certification --materialize
```

Отдельный полный аудит всех импортированных фактов и исходных колонок:

```bash
./analytic-certification --audit-sources
```

Результаты создаются в `artifacts/analytic-certification/<run-id>/`:
`results.jsonl`, `coverage.json`, `report.html`, `junit.xml`, health/source
audit и обезличенные replay-файлы с минимальной командой воспроизведения.
Full release verdict равен PASS только при прохождении каждого выбранного
сценария с первой попытки, отсутствии fallback/retry и успешном source audit.

## Демо-режим

Демо не включается автоматически при ошибке backend. Для отдельного
прототипного режима запустите:

```bash
VITE_DEMO_MODE=true npm run dev
```

## Конфигурация и ИИ

Скопируйте `.env.example` в `.env`, если нужны нестандартные пути или
провайдер. Без ключей используется безопасный `deterministic/rules-v1`.
Поддержаны адаптеры Z.AI и GNIVC Qwen, но отправка реальных данных наружу
запрещена по умолчанию (`ALLOW_EXTERNAL_LLM_DATA=false`).

Для Z.AI задайте в `.env`:

```dotenv
LLM_PROVIDER=zai
LLM_QUERY_MODEL=glm-5.1
ZAI_API_KEY=ваш_ключ
LLM_DATA_POLICY=metadata_only
```

Для локального GNIVC GPUStack используйте:

```dotenv
LLM_PROVIDER=gnivc_qwen
GNIVC_BASE_URL=https://api.ai.gnivc.ru/v1
GNIVC_API_KEY=ваш_ключ
GNIVC_QUERY_MODEL=qwen3.6-35b
GNIVC_NARRATIVE_MODEL=qwen3.5-122b
LLM_DATA_POLICY=aggregates
ALLOW_EXTERNAL_LLM_DATA=true
```

`qwen3.6-35b` строит исследовательский план, а `qwen3.5-122b`
формирует итоговые claims и narrative как более сильная judge-модель.
Адаптер автоматически отключает Qwen thinking через
`chat_template_kwargs.enable_thinking=false` и `/no_think`, заменяет
неподдерживаемую нулевую температуру на `0.2` и не отправляет
Z.AI-специфичный параметр `thinking`.

Кнопка «Пересобрать отчёт с GigaChat» использует отдельную редакционную модель
на том же GNIVC OpenAI-compatible endpoint и не меняет модель аналитического
чата. Дополнительный ключ или OAuth не нужны:

```dotenv
GNIVC_BASE_URL=https://api.ai.gnivc.ru/v1
GNIVC_API_KEY=ваш_ключ
REPORT_SYNTHESIS_PROVIDER=gnivc_gigachat
REPORT_SYNTHESIS_MODEL=GigaChat-Max
LLM_DATA_POLICY=aggregates
ALLOW_EXTERNAL_LLM_DATA=true
```

GigaChat получает исключительно проверенные отображаемые агрегаты и контекст
формулировок; исходные книги, ячейки и evidence ему не передаются. Gateway
получает строгую OpenAI-compatible `json_schema`, после чего backend повторно
проверяет полную редакционную схему, все числа и block_id. Qwen-специфичные `/no_think`,
`chat_template_kwargs` и `thinking` этой модели не передаются. При ошибке
модели сохраняется детерминированная версия.

GNIVC доступен только из разрешённой локальной сети/VPN. После переключения
сети проверьте конфигурацию из обычного Terminal:

```bash
PYTHONDONTWRITEBYTECODE=1 \
  '/Users/Melchior@Magi/Virtual Environment/gen_venv/bin/python' \
  -B -m backend.check_provider
```

Проверка не выводит ключ или ответ модели. Успешный результат содержит
`"status": "available"`. После изменения `.env` перезапустите backend,
поскольку настройки и экземпляр провайдера кэшируются процессом.

В режиме `metadata_only` агенту доступны только инструменты поиска по
справочникам и проверки плана. Значения из отчётов, вычисления и ссылки на
исходные ячейки остаются в backend. Однозначный проверенный план выполняется
автоматически; при неоднозначности интерфейс задаёт один уточняющий вопрос и
продолжает тот же диалог.

Для synthesis итогов аналитического проекта можно явно разрешить передачу
только проверенных агрегатов:

```dotenv
LLM_DATA_POLICY=aggregates
ALLOW_EXTERNAL_LLM_DATA=true
```

Thinking используется только этим synthesis-вызовом. Планировщик работает
со структурированными действиями при `temperature=0` и с отключённым
thinking. Файлы, ячейки и первичные строки во внешний synthesis не
передаются; каждое число итогового текста проверяется по снимку агрегатов.

Помимо числовых запросов, аналитический чат понимает вопросы о составе
загруженных данных, например:

- `Для какого периода можно сравнить Калининград и Москву по НДС?`
- `Какие регионы доступны?`
- `Какие налоги и показатели доступны?`
- `Какие коды ОКВЭД доступны?`

Ответ строится по фактически зарегистрированным источникам и справочникам,
а не по знаниям языковой модели. Для регионального сравнения календарь
дополнительно проверяется по наличию выбранного показателя и территорий в
книгах «В разрезе субъектов РФ».

Ключи не возвращаются из `/api/ai/status`, не хранятся во frontend и не
должны попадать в репозиторий. Файл `rd/models_and_api_calls.txt` приложению
не требуется.

Основные API:

- `POST /api/data/scan`, `POST /api/data/import`;
- `GET /api/catalog/periods`, `GET /api/catalog/sources/{fact_id}`;
- `POST /api/query/plan`, `POST /api/query`;
- `GET|POST /api/conversations`, `PATCH /api/conversations/{id}`;
- `POST /api/conversations/{id}/turns`;
- `POST /api/conversations/{id}/turns/{turn_id}/clarification`;
- `POST /api/conversations/{id}/analysis-runs` — асинхронный
  многошаговый аналитический агент (`202 Accepted`);
- `GET /api/analysis-runs/{id}` и
  `GET /api/analysis-runs/{id}/events` (SSE);
- `POST /api/analysis-runs/{id}/cancel` и
  `POST /api/analysis-runs/{id}/clarification`;
- `GET /api/conversations/{id}/project`;
- `POST /api/conversations/{id}/project/blocks`;
- `POST /api/scenarios`, `POST /api/scenarios/{id}/run`;
- `GET /api/reports/1-msp/parameters` — территории, общие накопительные
  периоды и рекомендуемая пара;
- `POST /api/reports/1-msp/interpret` — model-first заполнение параметров
  формы без автоматического запуска;
- `POST /api/reports/1-msp/preview`, `POST /api/reports/1-msp/generate` —
  национальный режим и стандартная справка `region_vs_rf`;
- `GET /api/reports/{id}/document`, `PATCH /api/reports/{id}/document`;
- `GET /api/reports/{id}/download/docx`;
- `GET /api/ai/status`, `POST /api/ai/test`.

Swagger UI доступен по `http://127.0.0.1:8000/docs`.

## Многошаговый аналитический агент V2

Новый маршрут строит операционный `ResearchPlan`, пакетно запускает
типизированные аналитические инструменты и формирует `AnalysisResult` с
`evidence`, проверенными `claims` и отдельно маркированными `hypotheses`.
Модель не получает SQL, имена таблиц, абсолютные пути или первичные строки:
наружу передаются только агрегаты, сущности, периоды и обезличенные
идентификаторы источников.

Ограничения одного запуска: не более четырёх обращений к модели, двенадцати
инструментов, четырёх параллельных инструментов, 50 строк на инструмент,
250 строк в контексте модели и 115 секунд. Старые синхронные endpoints
остаются доступными как совместимый детерминированный fallback.

Для постепенного включения Web задайте обе переменные и перезапустите
backend/frontend:

```dotenv
ANALYTIC_AGENT_V2=true
VITE_ANALYTIC_AGENT_V2=true
```

`ANALYTIC_AGENT_V2` фиксирует серверный rollout-флаг, а
`VITE_ANALYTIC_AGENT_V2` переключает Web с синхронного хода на
`analysis-runs` и SSE-прогресс.

Корпус из 60 запросов и сквозные V2-проверки находятся в
`backend/tests/test_analytic_agent_v2.py`. Модуль
`backend/app/agent_evaluation.py` считает аналитическое покрытие, долю
evidence-ссылок, проверку чисел и маркировку гипотез; модель допускается при
покрытии не ниже 90%, а при равном качестве выбирается более быстрая.
