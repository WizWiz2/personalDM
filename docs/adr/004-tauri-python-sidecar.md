# ADR-004: Tauri 2.x как desktop shell + FastAPI Python backend как sidecar

**Статус:** Proposed  
**Дата:** 14 июля 2026  
**Контекст:** product-foundation.md §7–8, §25, §27

---

## Проблема

Архитектура «Личного ДМ» предполагает desktop-приложение, реализованное web-технологиями (React/Vite frontend), с тяжёлым Python-backend (FastAPI, SQLAlchemy, LangGraph/собственный workflow, LLM-оркестрация). Tauri 2.x — предпочтительный desktop shell (§7), но его нативный backend написан на Rust и ориентирован на Rust-плагины. Наш backend — Python.

Вопрос: **как объединить Tauri (или альтернативу) с Python-процессом**, чтобы получить:

- лёгкий установщик;
- управление lifecycle всех процессов (backend, Ollama, ComfyUI);
- кроссплатформенность (Windows, macOS, Linux);
- auto-update;
- доступ к filesystem;
- IPC между shell и backend;
- приемлемый DX для команды, пишущей на Python и TypeScript.

Дополнительно: Python runtime нужно либо bundlить, либо требовать от пользователя. Это решение влияет на размер дистрибутива, скорость старта и сложность CI/CD.

---

## Рассмотренные варианты

### Вариант A: Tauri 2.x shell + Python sidecar process

Tauri запускается как desktop shell (Rust core + WebView). Python backend (FastAPI) запускается как отдельный дочерний процесс (sidecar), которым управляет Tauri через lifecycle API.

**Механизм:**

- Tauri 2.x имеет `shell` plugin с поддержкой sidecar binaries — можно указать путь к исполняемому файлу, который Tauri запустит при старте и убьёт при закрытии.
- Frontend общается с backend по HTTP/WebSocket через `localhost:<port>`.
- Tauri Rust core может выступать прокси (invoke → HTTP → FastAPI) или frontend может обращаться к FastAPI напрямую.
- Для bundling Python runtime: PyInstaller/Nuitka/cx_Freeze собирают Python backend в standalone executable, который кладётся в Tauri bundle как sidecar binary.

**Bundling Python runtime:**

| Способ | Размер | Скорость старта | Сложность CI | Кроссплатформенность |
|--------|--------|----------------|-------------|---------------------|
| **PyInstaller (onefile)** | 80–150 MB | 3–8 сек (распаковка) | Средняя | Win/Mac/Linux, но собирать нужно на каждой платформе |
| **PyInstaller (onedir)** | 80–150 MB | <1 сек | Средняя | Аналогично |
| **Nuitka** | 60–120 MB | <1 сек (нативная компиляция) | Высокая (требует C compiler) | Win/Mac/Linux |
| **Embedded Python** | 30–50 MB + venv | <1 сек | Высокая (ручная сборка) | Только Windows (embeddable zip), Mac/Linux нужны другие подходы |
| **Требовать Python** | 0 MB | мгновенно | Низкая | Все, но UX страдает |

**Плюсы:**

- Самый лёгкий shell (~5–10 MB Tauri vs ~150+ MB Electron). Итого с Python sidecar: ~90–160 MB.
- Tauri 2.x — стабильная экосистема с auto-updater, system tray, notification, dialog и прочими плагинами.
- Нативный WebView (Edge/WebKit/WebKitGTK) — не тащим Chromium.
- Sidecar API в Tauri позволяет управлять lifecycle: запуск, остановка, перезапуск, мониторинг stdout/stderr.
- Чёткое разделение: Tauri = shell + native features, Python = вся доменная логика.
- Frontend может работать и без Tauri (в браузере) — это важно для LAN-режима и раннего Docker-этапа.
- Можно дополнительно запускать Ollama и ComfyUI как sidecar processes через тот же механизм.
- Tauri поддерживает deep linking, custom protocols, clipboard, file dialogs — всё нативное.
- Security: Tauri CSP по умолчанию строже, чем Electron.

**Минусы:**

- **Два мира** — Rust и Python. Если нужна нативная функциональность вне стандартных Tauri плагинов, придётся писать Rust. Для нашего случая это маловероятно (нам хватит shell + filesystem + sidecar management), но стоит учитывать.
- **Bundling Python** добавляет сложность в CI/CD: нужно собирать sidecar для каждой платформы отдельно (PyInstaller/Nuitka не кросскомпилируют).
- **Размер sidecar** может превысить размер самого Tauri shell в 10–15 раз. PyInstaller для FastAPI + SQLAlchemy + numpy/ML зависимости может выдать 150+ MB.
- **Startup time**: при PyInstaller onefile — пользователь ждёт 3–8 секунд пока распакуется temporary directory. Onedir решает это, но усложняет обновление.
- **Port collision**: sidecar слушает `localhost:port` — нужна логика поиска свободного порта и передачи его frontend'у.
- **Health check**: frontend должен ждать, пока backend поднимется (readiness probe).
- **WebView ограничения**: нет DevTools в production, различия рендеринга между Edge/WebKit/WebKitGTK. На Linux WebKitGTK может отставать по поддержке CSS/JS.
- **Auto-update** покрывает только Tauri shell. Для обновления Python sidecar нужен свой механизм или пересборка всего bundle.

**IPC:**

```
Frontend (WebView)
  ├── HTTP REST → FastAPI (CRUD, queries)
  ├── WebSocket → FastAPI (streaming LLM, real-time updates)
  └── Tauri invoke → Rust core (native OS features: file dialogs, keychain, tray)
```

**Управление sidecar processes:**

```
Tauri Rust Core
  ├── Python Backend sidecar (FastAPI) — managed lifecycle
  ├── Ollama sidecar (optional) — managed lifecycle
  ├── ComfyUI sidecar (optional) — managed lifecycle  
  └── Health monitor — periodic pings, restart on crash
```

---

### Вариант B: Electron + Python sidecar

Electron предоставляет полную среду Chromium + Node.js. Python backend запускается как child process из Node.js main process.

**Плюсы:**

- Самая зрелая экосистема desktop web-приложений. Огромное количество документации, примеров, ready-made решений.
- Chromium гарантирует идентичное поведение на всех платформах — нет проблем с WebKitGTK.
- DevTools доступны всегда, включая production (можно отключить).
- `electron-builder` или `electron-forge` — отлаженные toolchains для сборки, подписи, auto-update.
- Node.js main process может управлять child processes, IPC через stdin/stdout или HTTP.
- Огромное количество npm-пакетов для native features.
- `electron-store` для настроек, `keytar` для keychain — всё есть.
- Более предсказуемый рендеринг UI.

**Минусы:**

- **Размер**: Chromium + Node.js = 150–200 MB minimum. С Python sidecar — 300–400 MB. Для local-first приложения это «Гравитационная аномалия» — bundle тяжелее чёрной дыры `node_modules`.
- **RAM**: Chromium + Node.js + Python backend = 300–600 MB RAM при простое. Для пользователей с 8–16 GB RAM (целевая аудитория: геймеры с локальными LLM) это ощутимо.
- **Security**: Electron исторически имеет более широкую attack surface. Node.js main process имеет полный доступ к FS — нужен аккуратный preload/contextBridge.
- **Производительность**: Chromium потребляет больше ресурсов, чем нативный WebView.
- **Два рантайма JS**: Node.js (main) + Chromium (renderer) — это уже сложно, а с Python backend — три рантайма.
- **Восприятие**: в сообществе Electron имеет репутацию «тяжёлого» решения. Для продукта, позиционирующегося как local-first и лёгкого — это может подорвать доверие.

**IPC:**

```
Renderer (Chromium)
  ├── IPC → Main Process (Node.js) → child_process.spawn → Python Backend
  ├── HTTP/WS → Python Backend (direct)
  └── contextBridge → native features
```

---

### Вариант C: Neutralinojs + Python sidecar

Neutralinojs — легковесный фреймворк, использующий нативный WebView (как Tauri), но с JavaScript API вместо Rust.

**Плюсы:**

- Очень лёгкий: ~2–5 MB для shell.
- Нативный WebView — малое потребление RAM.
- JavaScript API для native features — не нужно знать Rust.
- Extensions API для запуска sidecar processes.
- Кроссплатформенный.

**Минусы:**

- **Незрелая экосистема**: маленькое community, мало плагинов, мало production-примеров крупных приложений.
- **Ограниченные native API**: нет полноценного keychain access, нет system tray (экспериментальный), нет auto-updater из коробки.
- **Extensions API** для sidecar — менее отлажен, чем Tauri shell plugin.
- **WebView ограничения** — те же, что у Tauri (WebKitGTK на Linux).
- **Risk**: проект может потерять maintainer (маленькая команда). Для продукта с расчётом на годы — это серьёзный риск.
- **Нет встроенного bundler**: нужно самому организовывать сборку и подпись.
- **Документация** — значительно беднее, чем у Tauri или Electron.
- **Нет deep linking**, custom protocol handlers — ограничены возможности интеграции с ОС.

---

### Вариант D: Нет desktop shell — чистый web-сервер + браузер

Python backend запускается самостоятельно (как systemd service, docker compose, или bat/sh скрипт). Frontend отдаётся как static files через FastAPI/nginx. Пользователь открывает `http://localhost:<port>`.

**Плюсы:**

- **Максимальная простота**: нет shell, нет bundling, нет IPC overhead.
- **Кроссплатформенность** бесплатна — браузер есть везде.
- **LAN-режим** — бесплатен. Просто открой URL с другого устройства.
- **Минимальная зависимость**: только Python + requirements. Docker compose для полного стека.
- **DX**: самый простой вариант для разработки. `uvicorn main:app --reload` и всё.
- **Идеален для раннего этапа** (§7: «docker compose up → браузер»).
- **Обновление**: `pip install --upgrade` или `docker pull`.
- **CI/CD**: простейший — Docker image или Python wheel.
- **RAM**: только Python backend + браузер (который уже открыт).

**Минусы:**

- **Нет нативной интеграции**: нет system tray, нет file dialogs (только через `<input type="file">`), нет keyboard shortcuts вне браузера, нет native notifications.
- **Нет auto-start**: пользователь должен вручную запускать сервер.
- **Нет auto-update** UI из коробки.
- **UX**: «запусти команду → открой браузер → введи URL» — это не desktop-приложение, а developer tool. Для non-technical пользователя это барьер.
- **Конкурентное восприятие**: «это web-сервер, а не приложение» — ощущение незавершённости.
- **Lifecycle management**: кто запускает/останавливает Ollama и ComfyUI? Пользователь вручную? Отдельный supervisor?
- **Нет keychain**: API-ключи придётся хранить в файле или env vars.
- **Port management**: пользователь может уже использовать порт, нет elegant fallback.
- **Нет single-window experience**: пользователь может случайно закрыть вкладку.

**Тем не менее**: этот вариант не исключает desktop shell позже. Frontend, написанный для этого режима, можно обернуть в Tauri без изменений.

---

### Вариант E: Wails (Go) + Python sidecar

Wails — Go-фреймворк для desktop web-приложений с нативным WebView. Wails Go backend может запускать Python как child process.

**Плюсы:**

- Лёгкий shell (~5–10 MB), нативный WebView.
- Go backend может работать как proxy/orchestrator для Python sidecar.
- Go лучше подходит для системной логики (process management, file operations), чем Rust (проще, быстрее компилируется).
- Кроссплатформенный.
- Более зрелый, чем Neutralinojs.
- Go bindings для native features (file dialogs, menus, events).

**Минусы:**

- **Три языка**: Go (shell) + TypeScript (frontend) + Python (backend). Это больше, чем у Tauri (Rust + TypeScript + Python), потому что Go в нашем стеке больше нигде не используется, тогда как Rust хотя бы «бесплатен» внутри Tauri.
- **Меньшая экосистема**, чем Tauri: меньше плагинов, меньше community momentum.
- **Auto-updater**: менее зрелый, чем у Tauri.
- **Keychain**: нет встроенного — нужна Go-библиотека.
- **Sidecar management**: нет встроенного API — нужно писать самому на Go (`os/exec`).
- **WebView ограничения** — те же, что у Tauri.
- **Документация и примеры**: заметно меньше, чем у Tauri.
- **Wails v3** (в разработке) может сломать совместимость с v2 — риск миграции.
- **Community тренд**: Tauri набирает momentum быстрее, чем Wails.

---

## Сравнительная таблица

| Критерий | A: Tauri + Python | B: Electron + Python | C: Neutralino + Python | D: Web-only | E: Wails + Python |
|----------|:-:|:-:|:-:|:-:|:-:|
| **Размер bundle** | ~100–170 MB | ~300–400 MB | ~90–160 MB | ~0 (pip/docker) | ~100–170 MB |
| **RAM idle** | ~100–200 MB | ~300–600 MB | ~100–200 MB | ~50–100 MB (+браузер) | ~100–200 MB |
| **Кроссплатформенность** | ✅ (WebView различия) | ✅ (единый Chromium) | ✅ (WebView различия) | ✅ (браузер) | ✅ (WebView различия) |
| **Auto-update** | ✅ (tauri-updater) | ✅ (electron-updater) | ❌ (вручную) | ❌ | ⚠️ (менее зрелый) |
| **Native features** | ✅✅ (plugin system) | ✅✅✅ (npm ecosystem) | ⚠️ (ограничено) | ❌ | ✅ (Go bindings) |
| **Sidecar management** | ✅ (shell plugin) | ✅ (child_process) | ⚠️ (extensions API) | ❌ | ⚠️ (вручную на Go) |
| **Keychain** | ✅ (tauri-plugin-os) | ✅ (keytar) | ❌ | ❌ | ⚠️ (Go lib) |
| **Security** | ✅✅ (strict CSP) | ⚠️ (wider surface) | ✅ | ⚠️ (сеть) | ✅ |
| **DX** | Хорошо (Rust minimal) | Отлично (JS everywhere) | Средне | Отлично | Средне (Go) |
| **Зрелость** | ✅ (v2 stable) | ✅✅✅ (>10 лет) | ⚠️ (niche) | N/A | ⚠️ (v2, v3 в разработке) |
| **Community** | Крупное, растущее | Огромное | Маленькое | N/A | Среднее |
| **LAN fallback** | ✅ (frontend = web) | ✅ (frontend = web) | ✅ | ✅ (native) | ✅ |

---

## Стратегия bundling Python runtime

Независимо от выбора shell, вопрос упаковки Python runtime остаётся ключевым.

### Подход 1: PyInstaller (onedir)

Рекомендуется как стартовый. Собирает Python + зависимости + скомпилированные .pyc в папку, которая кладётся в Tauri bundle как sidecar.

- Плюсы: зрелый инструмент, хорошая документация, работает с большинством Python-пакетов.
- Минусы: сборка платформозависима (нужен CI runner для каждой ОС). Размер 80–150 MB для нашего стека.

### Подход 2: Nuitka (compiled)

Компилирует Python в C, затем в нативный бинарник.

- Плюсы: быстрее старт, меньший размер, нет распаковки temporary files.
- Минусы: сложнее CI (нужен C compiler), не все пакеты совместимы, дольше сборка.

### Подход 3: Embedded Python + venv

На Windows: `python-3.12-embed-amd64.zip` (~15 MB) + `pip install` в venv. На Mac/Linux: аналогично через standalone Python builds (python-build-standalone от Gregory Szorc).

- Плюсы: полный контроль, можно обновлять зависимости без пересборки всего.
- Минусы: сложная первоначальная настройка, нужно поддерживать создание venv на каждой платформе.

### Подход 4: Требовать Python у пользователя

- Плюсы: нулевой overhead, пользователь управляет версией.
- Минусы: «установите Python 3.12+» — это барьер для non-technical пользователей, conflicting system Python, разные пути на разных ОС.

### Рекомендация по bundling

Для MVP: **PyInstaller (onedir)**. Это наиболее предсказуемый путь. В будущем можно мигрировать на Nuitka для уменьшения размера и ускорения старта, когда CI/CD pipeline стабилизируется.

При этом ранний этап разработки (Docker/localhost) не требует bundling вообще — разработчик запускает `uvicorn` напрямую.

---

## Рекомендация

**Рекомендуемая стратегия: двухфазная.**

**Фаза 1 (Этапы 0–2 roadmap): Вариант D — Web-only.**

- `docker compose up` или `uvicorn` + `npm run dev`.
- Браузер на `localhost`.
- Максимальная скорость разработки.
- Нет overhead на shell, bundling, sidecar management.
- Frontend пишется как обычное SPA — React + Vite.
- Можно использовать с LAN.

**Фаза 2 (Этап 3+): Вариант A — Tauri 2.x + Python sidecar (PyInstaller).**

- Обернуть тот же frontend в Tauri.
- Добавить sidecar management для Python backend.
- Добавить native features: keychain, file dialogs, tray, auto-update.
- Добавить process management для Ollama/ComfyUI.

Эта стратегия позволяет:

- не платить за сложность desktop shell на ранних этапах;
- не ограничивать себя: frontend остаётся web-совместимым;
- перейти к Tauri, когда doменная логика стабилизируется;
- сохранить web-only mode как fallback для Docker/LAN/server.

Electron (Вариант B) рекомендуется как запасной план на случай, если WebView на Linux (WebKitGTK) окажется слишком проблемным для нашего UI. Но по состоянию на 2026 год, Tauri 2.x достаточно зрелый.

---

## Последствия

### При выборе Tauri + Python sidecar:

1. **CI/CD** усложняется: нужно собирать Python sidecar (PyInstaller) + Tauri bundle для каждой платформы (Windows, macOS x86/ARM, Linux). Минимум 4 CI runners.
2. **Размер дистрибутива**: ~100–170 MB. Для desktop-приложения — приемлемо. Для сравнения: SillyTavern с Node.js — ~200 MB, Foundry VTT — ~150 MB.
3. **Startup sequence**: Tauri start → spawn Python sidecar → wait for health check → show UI. Нужна splash screen или loading indicator.
4. **Error handling**: если Python sidecar крашится, Tauri должен показать диагностику и предложить перезапуск. Нужна стратегия graceful degradation.
5. **Auto-update**: два компонента обновляются по-разному. Tauri updater обновляет shell + frontend. Python sidecar нужно обновлять отдельно (или пересобирать весь bundle).
6. **LAN-режим**: Python backend слушает на 0.0.0.0 — нужна аутентификация и CORS. Tauri IPC не распространяется на LAN — только HTTP/WS API.
7. **Developer experience**: разработчик запускает Tauri dev mode (hot reload frontend) + отдельно uvicorn (hot reload backend). Это два процесса, но привычная схема для fullstack разработки.

### При web-only (Фаза 1):

1. Process management (Ollama, ComfyUI) — ответственность пользователя или отдельного скрипта/supervisor.
2. Нет нативных file dialogs — используем `<input type="file">` и drag-and-drop.
3. Нет keychain — API-ключи хранятся в encrypted config файле (см. ADR-005).

---

## Связанные решения

- **ADR-005**: Хранение секретов. Выбор shell влияет на доступность OS Keychain.
- **ADR-001**: Local-first desktop. Этот ADR уточняет, как именно local-first реализуется технически.
- **§7 product-foundation**: Формат приложения.
- **§8 product-foundation**: Предлагаемый стек.
- **§27 product-foundation**: Уточнение local-first архитектуры.
