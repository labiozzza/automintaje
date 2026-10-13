# AGENTS.md — automontage (transcriber + автопостинг)

Отдельный проект opencode. Общие правила (token economy, gstack) — в глобальном
`~/.config/opencode/AGENTS.md`; здесь только специфика этого репозитория.

## Что это
Единый автоматический процесс: монтаж видео + подготовка платформенных версий +
публикация. Ядро — FastAPI в `app.py`, фронт — `templates/index.html`, движок
монтажа — `montage/`. Состояние задач — `montage_work/<job_id>/`.

## Окружение
- Python: `/Library/Frameworks/Python.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python`
- Тесты: `unittest` (НЕ pytest). Запуск из корня репозитория:
  `".../Python" -m unittest discover -p "test_*.py"`
- Python 3.9: `list[...]`/`dict[...]` можно, `X | None` — нельзя.
- Сервер: `uvicorn app:app` на `127.0.0.1:8000`, без `--reload`. После правок
  `app.py` рестарт обязателен: `lsof -nP -iTCP:8000 -sTCP:LISTEN`.

## Безопасность публикаций (критично)
- НИКОГДА не вызывать публикацию в реальные аккаунты из тестов. Мокать
  `publish_job` / `process_publish`; e2e не должны делать POST `/publish`.
- Секреты (токены, ID) — в `config/secrets.json` (в .gitignore, не коммитить).
- Публикуемый медиа-репозиторий GitHub задаётся `GITHUB_REPO_DIR` в
  `config/secrets.json` (отдельный репозиторий, не этот).

## Проверки перед коммитом
- Python: `python -m py_compile <файлы>`.
- JS: извлечь инлайновый `<script>` без `src` и прогнать `node --check`.
- Playwright-сюиты (`test_*_ui.py`, `test_layout.py`, `test_publish_progress.py`)
  НЕ входят в `unittest discover` — запускать отдельно.

## Прочее
- Абсолютные пути этого проекта: корень `~/Projects/automontage`.
- Не удалять реальные публикации и файлы из медиа-репозитория без явного разрешения.
