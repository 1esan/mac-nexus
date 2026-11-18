> **Навигация:** [00 · Мастер-индекс](00_master_index_and_system_map.md) | ← [03 · Стек 1 и Красная книга безопасности](03_cleanup_stack_and_safety_bible.md) | [05 · Справочник HTTP REST и SSE API →](05_http_rest_and_sse_api_reference.md) | [README репозитория](../README.md)

# 04 · Backend, Scanner, Executor и Модель Безопасности

Данный документ описывает внутреннюю архитектуру Python-бэкенда **Mac Nexus** (`mac_nexus` `v1.0.0`): структуру модулей, состояние сервера, механизмы сканирования трёх контуров хранения, многоуровневую модель безопасности, движок `LIVE LOCK` и транзакционный исполнитель операций очистки, телепорта на SSD и перемещения в Корзину.

Все факты, имена символов, пороги и инварианты в этом справочнике сверены с исходным кодом [`__init__.py`](../src/mac_nexus/__init__.py), [`server.py`](../src/mac_nexus/server.py), [`scanner.py`](../src/mac_nexus/scanner.py), [`executor.py`](../src/mac_nexus/executor.py) и набором тестов [`test_mac_nexus.py`](../tests/test_mac_nexus.py).

---

## 1. Обзор архитектуры и принцип Zero-Dependency Backend

Бэкенд Mac Nexus построен **исключительно на стандартной библиотеке Python 3.12+** (`http.server`, `concurrent.futures`, `dataclasses`, `hashlib`, `json`, `os`, `pathlib`, `plistlib`, `queue`, `re`, `shutil`, `subprocess`, `tempfile`, `threading`, `time`, `urllib.parse`, `uuid`). В `src/mac_nexus/` нет ни одной внешней PyPI-зависимости; все системные метрики и действия с инструментами выполняются либо прямыми вызовами ОС (`os.scandir`, `os.statvfs`, `os.lstat`, `shutil`), либо через контролируемые подпроцессы штатных бинарников macOS и Homebrew (`pgrep`, `orca`, `lsof`, `hdiutil`, `diskutil`, `osascript`, `sysctl`, `vm_stat`, `czkawka_cli`, `brew`, `conda`, `uv`, `npm`, `organize`, `open`).

### Ответственность модулей

| Модуль | Версия / Метаданные | Назначение | Ключевые классы и функции |
| :--- | :--- | :--- | :--- |
| [`__init__.py`](../src/mac_nexus/__init__.py) | `APP_NAME = "Mac Nexus"`, `APP_VERSION = "1.0.0"` | Метаданные пакета | `__all__ = ["APP_NAME", "APP_VERSION"]` |
| [`server.py`](../src/mac_nexus/server.py) | `HTTP/1.1` + SSE | Loopback HTTP-сервер, CSRF/Origin-фильтр, раздача `/static/*`, REST и SSE маршруты | `NexusAppContext`, `NexusRequestHandler`, `NexusHTTPServer`, `create_server`, `main` |
| [`scanner.py`](../src/mac_nexus/scanner.py) | 1863 строки | Среда выполнения, снимок ID, телеметрия 3 контуров, 6 параллельных сканеров, `LIVE LOCK`, парсер `czkawka_cli` | `ScanEnvironment`, `ScanSnapshot`, `is_protected_path`, `detect_live_locks`, `get_telemetry`, `scan_system`, `scan_duplicates` |
| [`executor.py`](../src/mac_nexus/executor.py) | 621 строка | Валидация целей, фоновые задания очистки и SHA-256 телепорта, Trash-перемещение, SSE-брокер, аудит-история | `EventBroker`, `HistoryStore`, `move_to_trash`, `teleport_file_sha256`, `start_execute_job`, `start_offload_job`, `trash_duplicate_files`, `open_tool` |

### Компонентная архитектура и потоки данных

```mermaid
flowchart TB
    UI["Cyber-HUD Frontend (static/app.js)"] -->|"HTTP/1.1 + X-Mac-Nexus-Client: 1"| HTTP["NexusHTTPServer (127.0.0.1:8790)"]
    HTTP --> HANDLER["NexusRequestHandler (do_GET / do_POST)"]
    HANDLER --> CTX["NexusAppContext (snapshot_lock + shutdown_event)"]

    CTX -->|"GET /api/telemetry, /api/scan, POST /api/duplicates"| SCAN["scanner.py (ThreadPoolExecutor max_workers=6)"]
    SCAN -->|"Запись ScanTarget / OffloadTarget / DuplicateFileTarget"| SNAP["ScanSnapshot (items_by_id, offload_by_id, dup_files_by_id)"]
    SCAN -->|"pgrep, orca, lsof, hdiutil, diskutil, osascript, sysctl, vm_stat, czkawka_cli"| OS["macOS Kernel / APFS / Homebrew CLI"]

    HANDLER -->|"POST /api/execute, /api/offload, /api/trash-duplicates, /api/open-tool"| EXEC["executor.py (daemon threads nexus-*)"]
    EXEC -->|"Чтение копии ScanSnapshot и повторный LIVE LOCK"| SNAP
    EXEC -->|"Публикация log / progress / reclaimed / done"| BROKER["EventBroker (Queue maxsize=500 на клиента)"]
    BROKER -->|"GET /api/events (text/event-stream)"| UI

    EXEC -->|"Атомарный os.replace"| HIST["HistoryStore (~/.local/state/mac-nexus/history.json)"]
    EXEC -->|"move_to_trash"| TRASH["Корзина (~/.Trash)"]
    EXEC -->|"teleport_file_sha256 (.nexus.tmp + os.fsync + SHA-256)"| SSD["Внешний APFS SSD (/Volumes/secret for mine)"]
```

### Синхронизация состояния в `NexusAppContext`

`NexusAppContext` ([`server.py:48-59`](../src/mac_nexus/server.py#L48-L59)) связывает воедино конфигурацию и живое состояние процесса:
- `env: ScanEnvironment` — конфигурация путей, бинарников и тестовых хуков.
- `static_dir: Path` — канонический resolved-путь к директории статики (по умолчанию `src/mac_nexus/static`).
- `snapshot: ScanSnapshot` — реестр выданных сервером непрозрачных идентификаторов.
- `snapshot_lock: threading.Lock` — защищает чтение и обновление `ScanSnapshot` между потоками.
- `broker: EventBroker` — потокобезопасный брокер SSE-событий.
- `history: HistoryStore` — потокобезопасное хранилище статистики освобождённого места.
- `shutdown_event: threading.Event` — выставляется в `NexusHTTPServer.server_close()` для мгновенного завершения активных SSE-потоков.

При `GET /api/scan` поля `snapshot.items_by_id` и `snapshot.offload_by_id` обновляются методом `.update(...)` под `snapshot_lock`, а при `POST /api/duplicates` обновляется `snapshot.dup_files_by_id`. Перед запуском фоновых воркеров `POST /api/execute` и `POST /api/offload` создают **изолированную копию словарей** `ScanSnapshot` (`dict(...)`) под блокировкой, поэтому повторный скан не ломает уже запущенную фоновую задачу. Операция `POST /api/trash-duplicates` выполняется синхронно под `snapshot_lock` и удаляет (`pop`) перемещённые `file_id` из `dup_files_by_id`.

---

## 2. Среда выполнения `ScanEnvironment` и снимок `ScanSnapshot`

### Конфигурация `ScanEnvironment`

Датакласс `ScanEnvironment` ([`scanner.py:70-106`](../src/mac_nexus/scanner.py#L70-L106)) централизует все пути файловой системы, расположение внешних CLI-утилит и три инъекционные точки для изолированного тестирования без монтирования реального SSD или запуска деструктивных команд:

| Поле | Значение по умолчанию | Назначение |
| :--- | :--- | :--- |
| `home` | `Path.home()` | Домашняя директория пользователя (`~`) |
| `nvme_mount` | `Path("/System/Volumes/Data")` | Точка монтирования Контура 1 (Internal NVMe) |
| `ssd_mount` | `Path("/Volumes/secret for mine")` | Точка монтирования Контура 2 (External APFS SSD) |
| `secondbrain_dir` | `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/SecondBrain` | Контур 3 (SecondBrain iCloud Vault) |
| `projects_dir` | `~/Projects` | Корневой каталог локальных репозиториев разработчика |
| `downloads_dir` | `~/Downloads` | Входящая папка загрузок |
| `desktop_dir` | `~/Desktop` | Рабочий стол (содержит защищённые `.app` и ботов) |
| `trash_dir` | `~/.Trash` | Целевая папка безопасного удаления (Корзина) |
| `state_dir` | `~/.local/state/mac-nexus` | Каталог состояния (XDG state) |
| `digest_path` | `~/.local/state/mac-hygiene/digest_latest.md` | Путь к последнему Воскресному дайджесту |
| `history_path` *(property)* | `self.state_dir / "history.json"` | JSON-журнал накопленного освобождённого объёма |
| `orca_bin` | `/Applications/Orca.app/Contents/Resources/bin/orca` | CLI для опроса активных worktree Orca |
| `czkawka_bin` | `/opt/homebrew/bin/czkawka_cli` | Движок поиска байтовых дубликатов |
| `organize_bin` | `/opt/homebrew/bin/organize` | Утилита тегирования и триажа `~/Downloads` |
| `brew_bin` | `/opt/homebrew/bin/brew` | Homebrew (`brew cleanup --prune=all`) |
| `conda_bin` | `/opt/anaconda3/bin/conda` | Anaconda (`conda clean --all`) |
| `anaconda_python` | `/opt/anaconda3/bin/python3` | Интерпретатор для `python3 -m pip cache purge` |
| `uv_bin` | `~/.local/bin/uv` | Менеджер `uv` (`uv cache prune --ci`) |
| `npm_bin` | `/opt/homebrew/bin/npm` | Node Package Manager (`npm cache clean --force`) |
| `ssd_mount_checker` | `None` (используется `ssd_mount.exists() and os.path.ismount(...)`) | Хук проверки монтирования SSD для `pytest` |
| `cmd_runner` | `None` (используется `subprocess.run`) | Хук перехвата всех внешних команд для `pytest` |
| `now_fn` | `time.time` | Источник текущего Unix-времени (для детерминированных проверок возраста) |

Все вызовы CLI проходят через обёртку `_run_cmd(env, args, timeout=5.0)` ([`scanner.py:164-179`](../src/mac_nexus/scanner.py#L164-L179)), которая при `OSError` или `subprocess.TimeoutExpired` возвращает синтетический `CompletedProcess(..., returncode=1, stdout="", stderr=str(exc))` и никогда не роняет процесс сканирования.

### Модели целей и детерминированная генерация ID (`_stable_id`)

Функция `_stable_id(prefix, key)` ([`scanner.py:182-184`](../src/mac_nexus/scanner.py#L182-L184)) формирует непрозрачный идентификатор длиной `len(prefix) + 1 + 14` символов:

```python
digest = hashlib.sha256(f"{prefix}:{key}".encode("utf-8")).hexdigest()[:14]
return f"{prefix}_{digest}"
```

Используемые префиксы:
- `pkg_<14hex>` — пакетные кэши (`brew`, `pip`, `uv`, `npm`, `nuget`, `conda`);
- `app_<14hex>` — кэши GUI-приложений (`chrome`, `yandex`, `telegram`, `obsidian`, `orca`, `vscode`, `discord`, `notion`, `figma`, `drawio`, `yandex_music`);
- `dl_<14hex>` — пакеты триажа загрузок (`stale_installers`, `stale_screenshots`, `organize_triage_<older_days>`);
- `proj_<14hex>` — артефакты сборки в `~/Projects`;
- `dmg_<14hex>` — призрачные образы `.dmg` (`<dev_entry>:<image_path>`);
- `media_<14hex>` — медиа-кандидаты на телепорт в `offload_by_id` (ключ — `_safe_resolve(fp)`);
- `dupf_<14hex>` — конкретные файлы в группах дубликатов `dup_files_by_id` (ключ — `_safe_resolve(path)`);
- `dup_<12hex>` — идентификатор группы дубликатов (`hash_or_id`).

`ScanSnapshot` ([`scanner.py:155-162`](../src/mac_nexus/scanner.py#L155-L162)) объединяет три словаря с внутренними серверными дескрипторами:

| Словарь в `ScanSnapshot` | Тип значения | Поля дескриптора | Назначение |
| :--- | :--- | :--- | :--- |
| `items_by_id` | `ScanTarget` | `item_id`, `category`, `title`, `action_kind`, `paths`, `tool_name`, `app_key`, `app_label`, `project_path`, `dmg_device`, `dmg_mount_points`, `estimated_bytes`, `locked`, `lock_reason` | Цели очистки Контура 1 (`/api/execute`) |
| `offload_by_id` | `OffloadTarget` | `media_id`, `name`, `source_path`, `rel_path`, `ext`, `size_bytes`, `mtime_ts`, `dest_dir` | Кандидаты SHA-256 телепорта (`/api/offload`) |
| `dup_files_by_id` | `DuplicateFileTarget` | `file_id`, `name`, `path`, `size_bytes`, `mtime_ts`, `group_id`, `is_primary` | Файлы в группах дубликатов (`/api/trash-duplicates`) |

---

## 3. Многоуровневая модель безопасности (5 эшелонов защиты)

Безопасность Mac Nexus строится как эшелонированная оборона: нарушение любого из уровней немедленно останавливает запрос или операцию до касания файловой системы.

### Уровень 1 · Строгая привязка к Loopback (`NexusHTTPServer`)

В конструкторе `NexusHTTPServer.__init__` ([`server.py:356-365`](../src/mac_nexus/server.py#L356-L365)) выполняется жёсткая проверка:

```python
if host != "127.0.0.1":
    raise ValueError(f"Mac Nexus MUST bind strictly to 127.0.0.1 (got {host!r})")
```

Даже при передаче `--host 0.0.0.0` или локального LAN IP через CLI сервер откажется запускаться. На уровне класса выставлены `allow_reuse_address = True` и `daemon_threads = True`, порт по умолчанию — `8790`.

### Уровень 2 · Защита от CSRF, подмены `Origin` и Path Traversal

1. **CSRF-токен и проверка `Origin` (`_check_post_csrf`):**
   Каждый `POST /api/*` запрос проходит проверку [`server.py:125-142`](../src/mac_nexus/server.py#L125-L142):
   - Заголовок `X-Mac-Nexus-Client` (после `.strip()`) должен строго равняться `"1"`. Отсутствие или иное значение даёт `HTTP 403`. Браузерные HTML-формы и простые кросс-доменные `fetch`/`XHR` без CORS-preflight не могут добавить кастомный заголовок.
   - Если заголовок `Origin` присутствует в запросе, `_is_valid_origin(origin)` разбирает его через `urllib.parse.urlsplit` и требует `parsed.scheme == "http"` и `parsed.hostname in {"127.0.0.1", "localhost"}`. Это надёжно блокирует поддельные поддомены вида `http://127.0.0.1.evil.com:8790`, HTTPS-origins и `http://0.0.0.0:8790` (`HTTP 403`).
2. **Ограничение тела запроса (`_read_json_body`):**
   Константа `MAX_JSON_BODY_BYTES = 1024 * 1024` (1 MiB) ограничивает размер JSON-тела; отрицательный или превышающий лимит `Content-Length` отклоняется с `HTTP 413`. Невалидный `Content-Length`, битый UTF-8/JSON или не-словарь (например, JSON-массив) возвращают `HTTP 400`.
3. **Защита статики (`/static/*` и `/`):**
   Метод `_send_static_file` ([`server.py:94-123`](../src/mac_nexus/server.py#L94-L123)):
   - Декодирует URL (`unquote(rel_name).lstrip("/")`) и отклоняет пустое имя, сегмент `..` в `Path(clean_name).parts`, а также любые `/` или `\` (`HTTP 403`).
   - Канонизирует путь `(self.ctx.static_dir / clean_name).resolve()` и проверяет `candidate.relative_to(self.ctx.static_dir)` (`HTTP 403`).
   - Проверяет существование обычного файла (`HTTP 404`) и расширение по белому списку `ALLOWED_STATIC_MIME` (`.html`, `.css`, `.js`, `.svg`, `.png`, `.ico` — иначе `HTTP 403`).
   - Все ответы снабжаются заголовками `Cache-Control: no-store` и `X-Content-Type-Options: nosniff`.

### Уровень 3 · Allowlist серверных Snapshot ID

Ни один мутирующий эндпоинт (`/api/execute`, `/api/offload`, `/api/trash-duplicates`) не принимает файловые пути или команды shell от клиента. HTTP-клиент передаёт только список строковых ID (`item_ids`, `media_ids`, `file_ids`). Любой неизвестный или устаревший ID (равно как попытки передать `"/etc/passwd; rm -rf /"` или `"../../secret"`) вызывает `UnknownTargetError` и немедленный ответ `HTTP 400`. Для поиска дубликатов `target` ограничен словарём из трёх ключей (`downloads`, `desktop`, `ssd_media`), а для `/api/open-tool` `tool` ограничен пятью значениями (`digest`, `pearcleaner`, `daisydisk`, `finder_ssd`, `finder_downloads`).

### Уровень 4 · Инварианты `is_protected_path` и отказ от симлинков

Функция `is_protected_path(path, env, live_worktree_paths)` ([`scanner.py:219-253`](../src/mac_nexus/scanner.py#L219-L253)) проверяет **одновременно** исходный `path` и канонизированный `_safe_resolve(path)` (через `_is_within`, который также проверяет и исходный, и resolved базовый каталог — это исключает обход через твёрдые/промежуточные симлинки или `/var` vs `/private/var`):

| Защищённая зона | Константа / Источник в коде | Правило блокировки |
| :--- | :--- | :--- |
| Бандлы приложений и медиатек | `PROTECTED_BUNDLE_SUFFIXES = (".app", ".photoslibrary")` | Любой компонент пути (`cand.parts`), оканчивающийся на `.app` или `.photoslibrary` (регистронезависимо) |
| Содержимое Корзины | `env.trash_dir` (`~/.Trash`) | Путь равен `~/.Trash` или находится внутри него |
| Боты автоматизации на Рабочем столе | `PROTECTED_DESKTOP_DIRS = ("Бот_автооткликов_Егор", "GPT_подключение_для_бота")` | Любой путь внутри `env.desktop_dir / name` |
| Хранилище знаний SecondBrain | `env.secondbrain_dir` | Всё дерево `SecondBrain` (защита от случайного удаления `node_modules` в `.opencode` или медиа) |
| Живые тяжёлые каталоги на SSD | `SSD_LIVE_HEAVY_DIRS = {"ollama_models", "local-jarvis-heavy", "project-perimeter-heavy", "calisthenics-tracker-heavy"}` | Любой путь внутри `env.ssd_mount / heavy_name` |
| Активные Orca worktrees | `live_worktree_paths` | Динамическое множество путей из `orca worktree ps` |

Дополнительно, символические ссылки (`is_symlink()`) принудительно пропускаются во всех обходах (`_dir_size_and_latest_mtime`, `_secondbrain_stats`, `_scan_*`, `parse_czkawka_compact_json`) и отклоняются в `move_to_trash`, `teleport_file_sha256` и `_clear_cache_dir_contents`.

### Уровень 5 · Движок `LIVE LOCK` (сканирование + повторная проверка перед исполнением)

Механизм `LIVE LOCK` предотвращает повреждение SQLite WAL-журналов, кэшей активных GUI-программ, открытых `.dmg`-образов и проектов, с которыми прямо сейчас работают агенты или локальные серверы.

```mermaid
flowchart LR
    DETECT["detect_live_locks(env)"] --> APPS["11 MONITORED_APPS via pgrep -x"]
    DETECT --> RIDER["JetBrains Rider via pgrep -fi rider"]
    DETECT --> ORCA["orca worktree ps --limit 30 --json (fallback: text)"]
    DETECT --> PORTS["lsof -nP -iTCP:3000,8768 -sTCP:LISTEN + CWD"]
    SCAN_DMG["_scan_ghost_dmgs"] --> LSOF_DMG["lsof &lt;mount_point&gt; + disk &gt;= 6 guard"]

    RECHECK["_recheck_target_lock(target, env, locks)"] --> SYNC["Синхронно в start_execute_job -> HTTP 409"]
    RECHECK --> PER_ITEM["Повторно перед каждой целью в _execute_single_target -> LOG LOCK-GUARD"]
```

#### Источники блокировок в `detect_live_locks(env)`

1. **11 отслеживаемых GUI-приложений (`MONITORED_APPS`, `pgrep -x`):**
   - `chrome` — процесс `Google Chrome` (`Google Chrome`)
   - `yandex` — процесс `Yandex` (`Yandex Browser`)
   - `telegram` — процесс `Telegram` (`Telegram`)
   - `obsidian` — процесс `Obsidian` (`Obsidian`)
   - `orca` — процесс `Orca` (`Orca`)
   - `vscode` — процесс `Code` (`VS Code`)
   - `discord` — процесс `Discord` (`Discord`)
   - `notion` — процесс `Notion` (`Notion`)
   - `figma` — процесс `Figma` (`Figma`)
   - `drawio` — процесс `draw.io` (`draw.io`)
   - `yandex_music` — процессы `Yandex Music` или `Яндекс Музыка` (`Яндекс Музыка`)
   Пока процесс запущен, соответствующий `app_cache` помечается `locked: true`.
2. **JetBrains Rider (`pgrep -fi rider`):**
   При обнаружении процесса `rider_running = True`, что блокирует очистку пакетного кэша `nuget` (`~/.nuget/packages` и `~/.local/share/NuGet/v3-cache`).
3. **Активные worktree Orca (`orca worktree ps`):**
   Сначала выполняется `[orca_bin, "worktree", "ps", "--limit", "30", "--json"]` ([`scanner.py:292-327`](../src/mac_nexus/scanner.py#L292-L327)). Запись считается активной (`is_live`), если выполняется хотя бы одно условие: `liveTerminalCount > 0`, `hasAttachedPty == True`, `status.lower() in {"working", "active"}` или `len(agents) > 0`. Домашний каталог `~` / `resolved_home` принудительно исключается. Если JSON недоступен или некорректен, срабатывает текстовый фоллбэк `parse_orca_worktrees_text` (`orca worktree ps --limit 30`, разбор строк с `live:(\d+)` и следующей строкой-путём).
4. **Живые dev-серверы и боты на портах `3000` и `8768` (`lsof` + корреляция CWD):**
   Выполняется `lsof -nP -iTCP:3000,8768 -sTCP:LISTEN`; для каждого найденного `PID` запрашивается рабочий каталог `lsof -p <pid> -a -d cwd -Fn` (парсится строка `n/...`). В `_find_project_lock(proj_dir, artifact_dir, locks)` ([`scanner.py:1344-1368`](../src/mac_nexus/scanner.py#L1344-L1368)) проект блокируется, если:
   - проект или артефакт пересекается с `live_worktree_repos` (`_is_within(proj_dir, wt_path)`, `_is_within(wt_path, proj_dir)` или `_is_within(artifact_dir, wt_path)`);
   - рабочий каталог активного сервиса на `:3000` или `:8768` (`next-server` и др.) находится внутри/над `proj_dir` либо имеет совпадающее имя каталога (`cwd_path.name == proj_dir.name`, например `calisthenics-tracker` на внешнем SSD или локальном пути).
5. **Открытые файлы на DMG и запрет `disk0..disk5`:**
   В `_parse_hdiutil_images` ([`scanner.py:461-511`](../src/mac_nexus/scanner.py#L461-L511)) разбирается `hdiutil info -plist`. Если среди `system-entities` встречается `/dev/disk(\d+)` с номером `< 6`, **весь образ отбрасывается** (`whole_disk = None; break`), чтобы никогда не задеть системные разделы и физические диски (`disk0..disk5`). Для образов с `disk >= 6` в `_scan_ghost_dmgs` и `_recheck_target_lock` выполняется `lsof <mount_point>`: если возвращается больше 1 строки (заголовок + открытые дескрипторы), образ маркируется как `locked: true`.

#### Двойная проверка (`_recheck_target_lock`)

Даже если клиент получил старый скан до запуска браузера или ворктри, при получении `POST /api/execute` функция `start_execute_job` **синхронно** вызывает `detect_live_locks(env)` и `_recheck_target_lock` для всех выбранных `item_ids` — при наличии блокировки возвращается `HTTP 409 Conflict` без старта фонового потока. Более того, внутри воркера функция `_execute_single_target` повторно вызывает `detect_live_locks(env)` и `_recheck_target_lock` перед каждой отдельной целью; если пользователь запустил приложение уже во время выполнения очереди, цель пропускается с записью `[LOCK-GUARD] Пропуск ...` в SSE.

---

## 4. Подсистемы сканера (`scanner.py`)

### Телеметрия трёх контуров (`get_telemetry`)

Функция `get_telemetry(env)` ([`scanner.py:696-752`](../src/mac_nexus/scanner.py#L696-L752)) формирует снимок состояния всех трёх плоскостей и ресурсов системы:

1. **Plane 1 — Internal NVMe (`planes.nvme`):**
   - `_disk_stats_from_plist_or_statvfs` сначала вызывает `diskutil info -plist <mount>` (ключи `TotalSize`/`APFSContainerSize`, `APFSContainerFree`/`FreeSpace`, `FilesystemType`), а при сбое падает на `os.statvfs`.
   - `_nvme_purgeable_bytes` запускает JXA (`osascript -l JavaScript`) с импортом `Foundation`, сравнивая `NSURLVolumeAvailableCapacityForImportantUsageKey` и `NSURLVolumeAvailableCapacityKey` для `/`. Разница (`important - avail`) и есть очищаемый APFS purgeable-резерв; при `cmd_runner is None` результат кэшируется в `_PURGEABLE_CACHE` на `10.0` секунд.
2. **Plane 2 — External APFS SSD (`planes.ssd`):**
   - `ssd_is_mounted(env)` проверяет монтирование `/Volumes/secret for mine` (`os.path.ismount` или тестовый `ssd_mount_checker`).
   - Возвращает список всех 6 канонических полок `SSD_SHELVES` с флагом `exists`:
     `01_AI_Models_and_Caches`, `02_Project_Heavy`, `03_Software_and_Installers`, `04_Media_Archive`, `90_Cold_Archive`, `99_Backups`.
3. **Plane 3 — SecondBrain iCloud (`planes.secondbrain`):**
   - `_secondbrain_stats` обходит дерево `secondbrain_dir` (игнорируя `.git` и симлинки), подсчитывает количество `.md`-заметок (`md_notes_count`), суммирует размер реальных локальных файлов (пропуская выгруженные в iCloud заглушки с флагом `SF_DATALESS_FLAG = 0x40000000`) и выставляет `has_node_modules_risk = True`, если внутри хранилища обнаружена папка `node_modules`.
4. **Системные метрики (`_system_memory_metrics`):**
   - `cpu_load_1m` (`os.getloadavg()[0]`), `cpu_cores` (`os.cpu_count() or 1`).
   - `ram_total_bytes` (`sysctl -n hw.memsize`, fallback через `SC_PAGE_SIZE * SC_PHYS_PAGES`).
   - `swap_used_bytes` (регулярное выражение по `sysctl -n vm.swapusage`).
   - `ram_used_bytes` из `vm_stat`: `(Pages active + Pages wired down + Pages occupied by compressor) * page_size` (по умолчанию `page_size = 16384`).
   - Вычисление `ram_pressure`:
     - `"critical"` — если `ram_used / ram_total >= 0.88` или `swap_used >= 10 GiB`;
     - `"warning"` — если `ram_used / ram_total >= 0.72` или `swap_used >= 4 GiB`;
     - `"normal"` — во всех остальных случаях.
   - `ghost_dmgs_count`, `orca_live_worktrees` и `history` (`load_history`).

### Полное сканирование системы (`scan_system`)

Функция `scan_system(env, older_days=30)` ограничивает `older_days` диапазоном `[0, 3650]`, снимает `locks = detect_live_locks(env)` и запускает **6 воркеров параллельно** в `ThreadPoolExecutor(max_workers=6)` ([`scanner.py:1624-1673`](../src/mac_nexus/scanner.py#L1624-L1673)):

1. **`pkg_cache` (`_scan_pkg_caches`, [`scanner.py:955-1108`](../src/mac_nexus/scanner.py#L955-L1108)):**

   | `tool` | Сканируемые пути | Условие / Защита | Команда в `executor.py` |
   | :--- | :--- | :--- | :--- |
   | `brew` | `~/Library/Caches/Homebrew` | `env.brew_bin.exists()` при исполнении | `brew cleanup --prune=all` |
   | `pip` | `~/Library/Caches/pip`, `~/.cache/pip` | `env.anaconda_python.exists()` | `<anaconda_python> -m pip cache purge` |
   | `uv` | `~/.cache/uv` | `env.uv_bin.exists()` | `uv cache prune --ci` |
   | `npm` | `~/.npm/_cacache` | `env.npm_bin.exists()` | `npm cache clean --force` |
   | `nuget` | `~/.nuget/packages`, `~/.local/share/NuGet/v3-cache` | Блокируется при `rider_running` | `_clear_cache_dir_contents` |
   | `conda` | `/opt/anaconda3/pkgs` | Pre-check `.conda`/`.tar.bz2` или `pkgs/cache`, затем `conda clean --all --dry-run --json` | `conda clean -y --all` |

2. **`app_cache` (`_scan_app_caches` + `_expand_app_cache_dirs`, [`scanner.py:755-952`](../src/mac_nexus/scanner.py#L755-L952)):**
   Формирует строго ограниченный белый список подкаталогов кэша; каталоги `User/`, `IndexedDB/`, `Cookies`, базы сообщений и терминальные сессии никогда не включаются в `paths`:

   | `app_key` | Процесс `pgrep -x` | Разрешённые каталоги (`~/Library/...`) |
   | :--- | :--- | :--- |
   | `chrome` | `Google Chrome` | `Caches/Google/Chrome/*/Cache`, `Code Cache`, `Chrome for Testing/*/Cache`, `Application Support/Google/Chrome/*/GPUCache`, `DawnGraphiteCache`, `DawnWebGPUCache` |
   | `yandex` | `Yandex` | `Caches/Yandex/YandexBrowser/*/Cache`, `Code Cache`, `Application Support/Yandex/YandexBrowser/ShaderCache`, `*/GPUCache`, `DawnGraphiteCache`, `DawnWebGPUCache` |
   | `telegram` | `Telegram` | `Group Containers/6N38VWS5BX.ru.keepcoder.Telegram/*/temp`, `Containers/ru.keepcoder.Telegram/Data/tmp` |
   | `obsidian` | `Obsidian` | `Application Support/obsidian/{Cache,Code Cache,GPUCache,DawnGraphiteCache,DawnWebGPUCache}` и `Partitions/*/{Cache,Code Cache,GPUCache,Dawn*}` |
   | `orca` | `Orca` | `Caches/orca-updater`, `Application Support/orca/{Cache,Code Cache,GPUCache,Dawn*}` и `Partitions/*/{Cache,Code Cache,GPUCache}` |
   | `vscode` | `Code` | `Application Support/Code/{Cache,Code Cache,GPUCache,CachedData,CachedExtensionVSIXs,DawnGraphiteCache,DawnWebGPUCache}` |
   | `discord` | `Discord` | `Application Support/discord/{Cache,Code Cache,GPUCache,DawnGraphiteCache,DawnWebGPUCache,logs}` |
   | `notion` | `Notion` | `Application Support/Caches/notion-updater`, `Notion/{Cache,Code Cache,GPUCache,Dawn*}`, `Partitions/*/Service Worker/CacheStorage` |
   | `figma` | `Figma` | `Application Support/Figma/DesktopProfile/*/{Cache,Code Cache,GPUCache,Dawn*,Service Worker/CacheStorage}` |
   | `drawio` | `draw.io` | `Caches/draw.io-updater`, `Application Support/draw.io/{Cache,Code Cache,GPUCache}` |
   | `yandex_music` | `Yandex Music` / `Яндекс Музыка` | `Caches/yandexmusic-updater`, `Application Support/YandexMusic/{Cache,Code Cache,GPUCache}` |
3. **`downloads_inbox` (`_scan_downloads_inbox`):**
   Ограниченный обход `~/Downloads` (`max_depth=2`) и `~/Desktop` (`max_depth=1`), пропускающий скрытые файлы/папки (кроме `$RECYCLE.BIN`), симлинки и `is_protected_path`:
   - `stale_installers` (`dl_<14hex>`): файлы `INSTALLER_EXTENSIONS = {".dmg", ".pkg", ".exe", ".iso"}` в `Downloads` старше `7.0` дней → `action_kind: "trash"`.
   - `stale_screenshots` (`dl_<14hex>`): файлы `IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}` в `Downloads` или `Desktop`, чьё имя начинается с `SCREENSHOT_PREFIXES = ("Screenshot", "Screen Shot", "Снимок экрана")`, старше `14.0` дней → `action_kind: "trash"`.
   - `organize_triage_{older_days}` (`dl_<14hex>`): неклассифицированные файлы в `Downloads` старше `older_days` → `action_kind: "organize"`, `size_bytes: 0`, `matched_count = untouched_count`.
4. **`dev_projects` (`_scan_dev_projects`):**
   Сканирует поддиректории верхнего уровня в `~/Projects` (исключая скрытые, симлинки и `secondbrain_dir`). Возраст проекта вычисляется по максимуму из `proj_dir.stat().st_mtime` и `.git/index` mtime без глубокого обхода гигантских `node_modules`. На глубине до 2 уровней (пропуская `.git`) ищутся папки из `PROJECT_ARTIFACT_NAMES = {"node_modules", ".venv", "venv", ".pytest_cache", "__pycache__", "target"}`.
   - **Возрастной барьер:** артефакт включается в выдачу только если проект не изменялся дольше `older_days` (при `older_days > 0` активно изменяемые проекты игнорируются полностью).
   - **Почему не используется `kondo` при скане:** у `kondo` нет флага `--dry-run`, поэтому возраст и размер артефактов вычисляются безопасно средствами Python с проверкой `_find_project_lock`.
   - **Уровень риска (`risk`):** `"low"` для `__pycache__` и `.pytest_cache`, `"medium"` для остальных.
5. **`ghost_dmgs` (`_scan_ghost_dmgs`):**
   Примонтированные `.dmg`-образы с номером устройства `>= 6` и проверкой открытых файлов через `lsof`.
6. **Кандидаты на SSD-телепорт (`_scan_offload_media`):**
   Обходит `~/Desktop` и `~/Downloads` (`os.walk(root, followlinks=False)`), отсекая скрытые каталоги, симлинки, `.app`/`.photoslibrary` и `is_protected_path` (включая ботов и активные worktrees). Отбираются только файлы с расширением `MEDIA_OFFLOAD_EXTENSIONS = {".mov", ".mp4", ".mkv"}` размером строго больше `MIN_OFFLOAD_BYTES = 10 * 1024 * 1024` (10 MiB). Дедупликация по `_safe_resolve`, расчёт пути назначения `<ssd_mount>/04_Media_Archive/Screen_Recordings/<YYYY-MM>/<name>` по месяцу `mtime`, сортировка по убыванию `size_bytes`.

Во всех вычислениях размера каталогов (`_dir_size_and_latest_mtime`) файлы с флагом `SF_DATALESS_FLAG = 0x40000000` пропускаются, чтобы облачные заглушки iCloud не учитывались как локально занимаемые гигабайты на NVMe. Итоговый список `items` сортируется по кортежу `(item["locked"], -item["size_bytes"], item["title"])` — разблокированные и самые крупные цели идут первыми.

### Поиск дубликатов через `czkawka_cli` (`scan_duplicates`)

Функция `scan_duplicates(env, target)` ([`scanner.py:1797-1863`](../src/mac_nexus/scanner.py#L1797-L1863)):
1. Разрешает только три целевых контура: `"downloads"` (`env.downloads_dir`), `"desktop"` (`env.desktop_dir`), `"ssd_media"` (`env.ssd_mount / "04_Media_Archive"`). Иное значение вызывает `ValueError` (`HTTP 400`). При `target == "ssd_media"` и отмонтированном SSD выбрасывает `RuntimeError` (`HTTP 409`).
2. Вызывает `_collect_excluded_dirs(scan_dir, env, live_wt)` для сбора точных путей защищённых директорий (`.app`, `.photoslibrary`, `PROTECTED_DESKTOP_DIRS`, `SSD_LIVE_HEAVY_DIRS`, активные worktrees) и передаёт каждый отдельным флагом `-e <dir>`.
3. Запускает `czkawka_cli dup` (по умолчанию метод сравнения `HASH` и алгоритм `BLAKE3`):
   - `-d <scan_dir>` — корневая директория поиска;
   - `-m 1048576` (`MIN_DUPLICATE_BYTES = 1 MiB`) — игнорирование мелких файлов;
   - `-E "*/*.app/*,*/*.photoslibrary/*,*/.Trash/*"` — дополнительные маски исключения;
   - `-C <tmp_json>` — вывод компактного JSON во временный файл `mac_nexus_dup_*.json`;
   - `-N -M -W` — подавление вывода в консоль (`--do-not-print-results`, `--do-not-print-messages`, `--ignore-error-code-on-found`).
   Коды возврата `0` и `11` считаются штатными; временный файл гарантированно удаляется в `finally`.
4. `parse_czkawka_compact_json` ([`scanner.py:1700-1794`](../src/mac_nexus/scanner.py#L1700-L1794)) повторно фильтрует симлинки и `is_protected_path`, отбрасывает группы с менее чем 2 оставшимися файлами, сортирует каждую группу по `(mtime_ts, len(str(path)), str(path))` и **всегда помечает первый (самый старый) файл флагом `is_primary = True`** («ОРИГИНАЛ / СОХРАНИТЬ»). Избыточный объём считается как `group_size * (len(clean_entries) - 1)`, группы сортируются по убыванию `wasted_bytes`.

---

## 5. Исполнительный движок (`executor.py`)

### Жизненный цикл заданий (`start_execute_job` и `start_offload_job`)

Оба фоновых оркестратора разделены на **синхронную фазу валидации** (в потоке HTTP-запроса) и **асинхронную фоновую фазу** (в daemon-потоке `nexus-<job_id>`):

- `start_execute_job` ([`executor.py:415-479`](../src/mac_nexus/executor.py#L415-L479)):
  1. Проверяет непустоту `item_ids` и наличие каждого ID в `snapshot.items_by_id` (иначе `UnknownTargetError` → `400`).
  2. Вызывает `detect_live_locks(env)` и `_recheck_target_lock` для всех целей (при срабатывании → `LiveLockError` → `409`).
  3. Генерирует `job_id = f"exec_{uuid.uuid4().hex[:10]}"`, запускает поток `nexus-exec_...` и немедленно возвращает `job_id`.
  4. Внутри `_execute_single_target` при `dry_run=True` публикует только событие `[DRY-RUN] Симуляция (...)` без изменений на диске. При `dry_run=False`:
     - `pkg_cache`: `brew cleanup --prune=all`, `anaconda_python -m pip cache purge`, `uv cache prune --ci`, `npm cache clean --force`, `conda clean -y --all`, либо `_clear_cache_dir_contents` для `nuget`. Для `brew`/`pip`/`uv`/`npm` освобождённый объём замеряется как `max(0, before - after)`.
     - `app_cache`: очищает **содержимое** внутри allowlisted папок через `_clear_cache_dir_contents` (без удаления родительских каталогов и без касания `User/`, `IndexedDB/`, `.db`), проверяя `is_protected_path` и игнорируя симлинки.
     - `downloads_inbox`: `action_kind == "trash"` вызывает `move_to_trash` для каждого файла; `action_kind == "organize"` запускает `organize run`.
     - `dev_projects`: повторно проверяет несимлинковость, точное вхождение имени в `PROJECT_ARTIFACT_NAMES`, префикс `_safe_resolve(env.projects_dir) + os.sep` и `not is_protected_path` перед `shutil.rmtree(art_dir, ignore_errors=False)`.
     - `ghost_dmgs`: проверяет `re.fullmatch(r"/dev/disk(\d+)", dev)` и `int(m.group(1)) >= 6` перед `hdiutil detach <dev>`.

### Атомарный SHA-256 телепорт на SSD (`teleport_file_sha256`)

Функция `teleport_file_sha256` ([`executor.py:171-234`](../src/mac_nexus/executor.py#L171-L234)) гарантирует отсутствие потери данных даже при обрыве питания или отключении кабеля SSD:

```mermaid
sequenceDiagram
    participant W as "Worker (start_offload_job)"
    participant NVMe as "Internal NVMe (Source)"
    participant SSD as "External APFS SSD (/Volumes/secret for mine)"

    W->>SSD: "1. Проверка ssd_is_mounted(env) и is_protected_path"
    W->>NVMe: "2. Замер src_size и потоковый sha256_file(src) (блоки 1 MiB)"
    W->>SSD: "3. Запись во временный .&lt;name&gt;.&lt;uuid8&gt;.nexus.tmp"
    W->>SSD: "4. wfh.flush() + os.fsync(wfh.fileno()) + shutil.copystat"
    W->>SSD: "5. Замер dst_size и sha256_file(staging_dest)"
    alt "dst_size != src_size ИЛИ dst_hash != src_hash"
        W->>SSD: "Удаление staging_dest (.nexus.tmp)"
        W-->>NVMe: "Исходник СОХРАНЁН (возврат ошибки целостности)"
    else "Байты и SHA-256 совпали"
        W->>SSD: "6. os.replace(staging_dest, final_dest) (collision-safe имя)"
        W->>SSD: "7. Проверка final_dest.exists() и размера == src_size"
        W->>NVMe: "8. Только теперь src.unlink()"
    end
```

Если `dry_run=True`, телепорт не требует смонтированного SSD на уровне `start_offload_job` (`if not dry_run and not ssd_is_mounted(env): raise LiveLockError(...)`) и лишь возвращает ожидаемый путь `dest_dir / src.name`.

### Безопасное перемещение в Корзину (`move_to_trash` и `trash_duplicate_files`)

- `_unique_dest_path(dest_dir, filename)` ([`executor.py:113-126`](../src/mac_nexus/executor.py#L113-L126)) предотвращает перезапись одноимённых файлов в `~/.Trash` или на SSD: если `dest_dir / filename` уже существует, подбирается имя `{stem}_{unix_ts}_{idx}{suffix}` (`idx` от `1` до `9999`), с резервным суффиксом `uuid4().hex[:8]`.
- `move_to_trash(src, env, live_worktree_paths)` отказывает, если объект не существует, является симлинком (`src.is_symlink()`) или подпадает под `is_protected_path`. После подсчёта размера выполняется `shutil.move(str(src), str(dest))`.
- `trash_duplicate_files` ([`executor.py:555-593`](../src/mac_nexus/executor.py#L555-L593)) проверяет все `file_ids` в `snapshot.dup_files_by_id` и на `is_protected_path`, переносит файлы в `~/.Trash`, удаляет обработанные `file_id` из снапшота и инкрементирует `history`.

### Брокер событий `EventBroker` и журнал `HistoryStore`

- **`EventBroker`** ([`executor.py:42-84`](../src/mac_nexus/executor.py#L42-L84)):
  Поддерживает множество подписчиков SSE. Каждый `subscribe()` выделяет `queue.Queue(maxsize=500)`. Метод `publish(event_type, message, bytes_val=0, job_id=None)` формирует JSON-объект `{"type": event_type, "message": message, "bytes": int(bytes_val), "job_id": job_id, "ts": time.time()}` и рассылает через `put_nowait`. Если очередь конкретного клиента заполнена (500 событий), выполняется кольцевое вытеснение самого старого сообщения (`q.get_nowait()` затем `q.put_nowait(payload)`), благодаря чему медленный клиент никогда не блокирует исполнение фонового задания.
- **`HistoryStore`** ([`executor.py:87-110`](../src/mac_nexus/executor.py#L87-L110)):
  Потокобезопасная (`threading.Lock`) запись кумулятивного счётчика `total_reclaimed_bytes` и отметки `last_run_at` в `env.history_path` (`~/.local/state/mac-nexus/history.json`). Запись идёт во временный файл `.history.{pid}.{tid}.tmp` в `state_dir`, после чего выполняется атомарный `os.replace`.

### Запуск разрешённых инструментов (`open_tool`)

Функция `open_tool(env, tool)` ([`executor.py:596-621`](../src/mac_nexus/executor.py#L596-L621)) поддерживает строго 5 ключей:
- `"digest"` — читает `env.digest_path` (`~/.local/state/mac-hygiene/digest_latest.md`) и возвращает `{"ok": True, "content": ...}` (или заглушку, если дайджест ещё не сгенерирован);
- `"pearcleaner"` — `["open", "-a", "/Applications/Pearcleaner.app"]`;
- `"daisydisk"` — `["open", "-a", "/Applications/DaisyDisk.app"]`;
- `"finder_ssd"` — предварительно проверяет `ssd_is_mounted(env)` (иначе `LiveLockError` → `409`), затем `["open", str(env.ssd_mount)]`;
- `"finder_downloads"` — `["open", str(env.downloads_dir)]`.

Неизвестное значение `tool` вызывает `UnknownTargetError` (`400`), а ненулевой код возврата `open` — `RuntimeError` (`500`).

---

## Связанные документы и модули кода

- Следующий документ: [05 · Справочник HTTP REST и SSE API](05_http_rest_and_sse_api_reference.md)
- Предыдущий документ: [03 · Стек 1 и Красная книга безопасности](03_cleanup_stack_and_safety_bible.md)
- Архитектура и навигация: [00 · Мастер-индекс и карта системы](00_master_index_and_system_map.md), [01 · Трёхконтурная архитектура ФС](01_three_plane_filesystem_architecture.md), [02 · Внутреннее устройство macOS APFS и XDG](02_macos_apfs_xdg_and_env_internals.md), [06 · Cyber-HUD Frontend и Mac Nexus.app](06_cyber_hud_frontend_and_app_bundle.md), [07 · Автоматизация и Runbooks](07_automation_runbooks_and_troubleshooting.md), [08 · Онбординг агентов и аудит](08_agent_onboarding_and_baseline_audit.md)
- Исходный код бэкенда и тесты: [`__init__.py`](../src/mac_nexus/__init__.py), [`server.py`](../src/mac_nexus/server.py), [`scanner.py`](../src/mac_nexus/scanner.py), [`executor.py`](../src/mac_nexus/executor.py), [`test_mac_nexus.py`](../tests/test_mac_nexus.py)
- Инструкции репозитория: [README.md](../README.md), [CLAUDE.md](../CLAUDE.md), [AGENTS.md](../AGENTS.md)
