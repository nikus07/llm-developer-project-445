# AI-оператор для сайта мобильного оператора связи

[![hexlet-check](https://github.com/nikus07/llm-developer-project-445/actions/workflows/hexlet-check.yml/badge.svg)](https://github.com/nikus07/llm-developer-project-445/actions)

Соберите агентный чат для сайта мобильного оператора. Он отвечает абоненту на вопросы о тарифе, списаниях и правилах со ссылками на базу знаний, а смену тарифа, услуг и контактов проводит через карточку подтверждения. По пути освоите оркестрацию нескольких агентов на OpenAI Agents SDK, MCP-серверы с JWT, контракты действий с идемпотентностью, журнал аудита, права через политики Dogwood и guardrail-сервис, при отказе которого действия закрываются.

Учебный проект Хекслета: https://ru.hexlet.io/programs/llm-developer
Как это должно работать: https://files.hexlet.app/a/i3355x

## Стек

- Python

## Установка

Нужны Python 3.12, Docker и OpenAI-совместимая модель (например, LM Studio).

```bash
git clone https://github.com/nikus07/llm-developer-project-445.git
cd llm-developer-project-445
python3.12 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt                          # или make install
cp .env.example .env                                     # заполнить LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
```

## Использование

Пять процессов и Postgres (каждый в своём терминале; без `make` — команды из `Makefile`):

```bash
make infra && make migrate && make seed   # docker compose up -d; python -m core.db migrate; python -m core.seed
make stand       # :8100  issuer, read-MCP, REST-моки, веб-чат
make actions     # :8220  action-MCP (JWT)
make pdp         # :9100  политики Dogwood
make guardrail   # :8300  guardrail-сервис
make api         # :8000  оркестратор
```

Веб-чат: http://127.0.0.1:8100 (anna — смена через карточку, boris — только чтение, olga — оператор).
Проверки: `make smoke` (54, без модели), `make scenarios` (11 диалогов на живой модели),
`python scripts/acceptance.py` (чек-лист приёмки), `python scripts/incident.py` (цепочка хода по журналам).
Отчёт приёмки: [ACCEPTANCE.md](ACCEPTANCE.md).

---

<details>
<summary>Автоматические тесты Хекслета</summary>

Тесты запускаются на каждый коммит. За запуск отвечает файл `.github/workflows/hexlet-check.yml` — не удаляйте и не переименовывайте ни его, ни репозиторий.

</details>

## О Хекслете

[Хекслет](https://ru.hexlet.io/) — школа программирования: авторские программы обучения с практикой, поддержкой наставников и реальными проектами, которые остаются в резюме. Этот репозиторий — один из таких проектов.
