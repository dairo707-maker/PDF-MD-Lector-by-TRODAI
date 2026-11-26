# Estado de Relevo Multi-IA — PDF/MD Lector v0.1.0

> **Proposito de este archivo:** Punto de control vivo para que **Claude Code** (o cualquier otra IA) pueda retomar el desarrollo exactamente donde quedo ante un cierre o limite de cuota (`429`), sin repetir trabajo ya validado ni perder decisiones arquitectonicas.
>
> Lee primero [`CLAUDE.md`](../CLAUDE.md) para las reglas no negociables del proyecto y comandos en OneDrive.

- **Ultima actualizacion:** `2026-11-26T20:50Z`
- **Base validada previa:** `v0.2.0` (`M7`, GitHub Release `v0.2.0`)
- **Version entregada:** `0.3.0` (`PDF/MD Lector · by TRODAI`)

---

## 1. Mapa de Hitos (M1–M8 + Pista E2E)

| Hito | Alcance principal | Estado actual | Notas / Dependencias |
|---|---|---|---|
| **M1** | Fundacion, dependencias `pyproject.toml`, `path_guard`, DPAPI `secrets_store`, `TempManager`, `CrashGuard`, `SingleInstanceLock`, `redaction`, `metadata_sanitizer`, `feedback_reporter` + `deploy/feedback-worker/`, `NotificationCenter`, `TaskController`, `LocalUsageInsights` | **COMPLETED** (Gate Iteracion 5 **PASS**: 14/14 prototipo + 25/25 M1 + 54/54 E2E nucleo M1; Auditor CLEAN) | Ver `tests/test_m1_foundation_security.py` |
| **Pista E2E** | Infraestructura y bateria de pruebas de caja opaca Tiers 1–4 (`TEST_INFRA.md`, fixtures sinteticos, `tests/e2e/`) | **COMPLETED** (`419/419` E2E incluidos en corrida total `515 passed`) | `TEST_READY.md` cubierto al 100 % |
| **M2** | `ToolRegistry` (44 `tool_id`), parametros DPI/calidad e idioma original, `ocr_engine` (WinRT/Tesseract/RapidOCR), `capture_engine` (QScreen/WIA/QCamera), `invoice_engine` (DIAN UBL 2.1 + Factur-X EN 16931) | **COMPLETED** (M2-A `17/17` + M2-B `15/15` + 100 % E2E `fa06..fa10`) | Ver `tests/test_m2_tools_ocr_invoices.py` y `tests/test_m2b_ocr_capture_invoices.py` |
| **M3** | `markdown_engine` + `md_security` (`nh3`, bloqueo imagenes remotas, `MD <-> PDF`), PDFium hit-testing, `pdf_annotations` ISO 32000 + `pdf_canvas` (`QTabletEvent`/`QTouchEvent` "Escribir tactil"), `md_tab_page` (3 modos), `viewer_view` mixto `.pdf`/`.md`, `dictionary_panel` estilo Kindle opcional/cerrable/desacoplable | **COMPLETED** (M3 `8/8` unit/regresion + 100 % E2E `fa11..fa16`) | Ver `tests/test_m3_markdown_annotations_dict.py` |
| **M4** | SVGs poligonales, `AdaptiveButton`, `DialogLocalizer`, `TourDialog`, `updater.py` (24h, `Solo offline`, SHA-256, banner solido), `HubView` (`Acciones Rapidas` dinamicas + `232x104`), `ToolWorkspace`, dialogos (`Invoice`, `Preferences`, `Notifications`, `Support`, `Diagnostics`, `AboutUpdater`), Theme Studio (Hub + canvas exterior), `MainWindow`/`SplashScreen`/`app.py` (< 300 ms) | **COMPLETED** (`tests/test_m4_ui_updater.py` + `fa01/03/04/05/17/18/19/20` + arranque `273.56 ms`) | Verificaciones visuales aprobadas por Neon en `final_screenshots/` |
| **M5** | Windows 11 `HKCU` + CLI, `browser-extension/` (MV3) + `native_messaging.py`, API REST perezosa (`127.0.0.1` + Bearer), Panel IA (BM25 + BYOK + MCP cliente) + `mcp_server.py` (`pdf-md-lector-mcp`), `scripts/build_exe.py`, `scripts/sign_local.ps1`, `packaging/installer.iss`, `.github/workflows/build-release.yml`, docs OSS y auditoria multi-IA | **COMPLETED** (`tests/test_m5_integrations_ai_mcp.py` + `tests/test_m5_packaging_docs.py` + `fa21..fa25`) | `sign_local.ps1 -DryRun` exit `0` |
| **M6** | Pase completo E2E Tiers 1–4 + suite unitaria (`515/515`), compilacion real `build_exe.py` + `SHA256SUMS.txt` + prueba Splash `.exe`, `sign_local.ps1 -DryRun`, capturas reales claro/oscuro (`980x650`, `1200x760`, `1366x720`), Git local `main` + commits semanticos + `git notes` + tag `v0.1.0` | **COMPLETED** (`515 passed`, build + smoke + capturas + Git local `v0.1.0`) | Sin remoto ni `git push` |
| **M7 (`v0.2.0`)** | Fixes `ToolCard.mousePressEvent` (`shiboken6.isValid`) y `DocumentRagIndex.index_pdf` (`pypdf`), compuerta hardware IA Local (`ai_hardware.py`) + API BYOK / MCP Oficial legal, diccionario morfologico + `suggest_fuzzy` + Wiktionary opt-in cacheado, notas adhesivas arrastrables con tarjeta flotante + `NotesPanel` (`/Subj`), autoguardado y recuperacion ante cierre inesperado (`autosave.py`), actualizacion directa GitHub Releases y publicacion publica `v0.2.0` | **COMPLETED** (`test_m7_v020_features`: `8 passed`; `test_tier1_features`: `189 passed`; Tiers 2–4 + smoke: `230 passed`; `build_exe.py` firmado `Valid`) | Repo publico MIT + GitHub Release `v0.2.0` |
| **M8 (`v0.3.0`)** | Botones flotantes sin recuadros e iconos centrados (`32x32` `iconOnly="true"`, cero recortes, `FlowContainer`), TopBar/Visor depurados sin duplicados, idioma fijo desde instalador (`read_installer_language`), Wiktionary por defecto con parser wikitext `w/api.php` + fallback REST `en.wiktionary.org` + sugerencias `opensearch` + cache SQLite, busqueda en documento (`Ctrl+F`) con resaltado PDFium, impresion del sistema (`Ctrl+P` `PrintPdfDialog` con `QPrinterInfo`), Modo Presentacion (`F5` / `Ctrl+L` + compatibilidad con apuntadores inalambricos + HUD inferior ocultable), listado de atajos (`F1` `ShortcutsDialog`), movimiento reactivo al puntero exclusivo en Hero del Hub (`motion_engine.py`), release firmado `v0.3.0` | **COMPLETED** (`test_m1/m3/m4/m5/m6/m7/m8/prototype`: `85 passed in 147.91s`; E2E `test_tier1_features` + `v0.3.0`; `FIRST_FRAME_MS=62.36`; live Wiktionary ES/EN OK; capturas `v4/` verificadas) | GitHub Release `v0.3.0` |

---

## 2. Inventario Actual Observado en el Workspace

### Nucleo (`src/pdf_lector/core/`)
- `src/pdf_lector/core/path_guard.py`
- `src/pdf_lector/core/win32_bridge.py`
- `src/pdf_lector/core/secrets_store.py`
- `src/pdf_lector/core/temp_manager.py`
- `src/pdf_lector/core/crash_guard.py`
- `src/pdf_lector/core/single_instance.py`
- `src/pdf_lector/core/redaction.py`
- `src/pdf_lector/core/metadata_sanitizer.py`
- `src/pdf_lector/core/feedback_reporter.py`
- `src/pdf_lector/core/notifications.py`
- `src/pdf_lector/core/task_control.py`
- `src/pdf_lector/core/usage_insights.py`
- `src/pdf_lector/core/bookmarks_store.py`
- `src/pdf_lector/core/dictionary_engine.py`
- `src/pdf_lector/core/pdf_engine.py`
- `src/pdf_lector/core/i18n.py`

### Configuracion, Tema, UI y entrada
- Raiz del proyecto: `VERSION` (`0.1.0`), `pyproject.toml`, `uv.lock`
- `src/pdf_lector/app.py`, `src/pdf_lector/__init__.py`
- `src/pdf_lector/theme/`: `palette.py`, `theme_profile.py`, `motion_engine.py`
- `src/pdf_lector/ui/`: `main_window.py`, `splash_screen.py`, `widgets.py`, `svg_icons.py`, `hub_view.py`, `viewer_view.py`, `dictionary_panel.py`, `theme_studio.py`
- `deploy/feedback-worker/`: `worker.js`, `wrangler.toml` (worker Cloudflare de soporte sin cuenta GitHub)

### Pruebas (`tests/`) y Documentos de Infraestructura E2E
- `tests/test_prototype_theme_motion_splash.py` (linea base `v0.1.0-alpha.2`)
- `tests/test_m1_foundation_security.py` (pruebas M1)
- `tests/e2e/` (`419` casos recolectados = `7` smoke + `189` Tier 1 + `171` Tier 2 + `34` Tier 3 + `18` Tier 4):
  - `tests/e2e/__init__.py`
  - `tests/e2e/conftest.py`
  - `tests/e2e/fixtures.py`
  - `tests/e2e/test_fixtures_smoke.py`
  - `tests/e2e/test_tier1_features.py`
  - `tests/e2e/test_tier2_boundaries.py`
  - `tests/e2e/test_tier3_pairwise.py`
  - `tests/e2e/test_tier4_workloads.py`
- Metadatos Teamwork E2E (solo lectura/referencia):
  - `C:\Users\datg2\.gemini\antigravity\scratch\teamwork\303cf05e-a44a-4016-9147-e5dcb495c9a4\.agents\teamwork\TEST_INFRA.md`
  - `C:\Users\datg2\.gemini\antigravity\scratch\teamwork\303cf05e-a44a-4016-9147-e5dcb495c9a4\.agents\teamwork\TEST_READY.md`

---

## 3. Protocolo de Reanudacion para Claude Code

Si tomas el relevo tras una interrupcion o limite de cuota:

1. **Leer contexto:** Lee `CLAUDE.md`, este archivo (`docs/HANDOFF_STATUS.md`) y, si sigue disponible en disco, el plan maestro en:
   `C:\Users\datg2\.gemini\antigravity\scratch\teamwork\303cf05e-a44a-4016-9147-e5dcb495c9a4\.agents\teamwork\PROJECT.md`
   (junto con `sub_orch_*\GATE_STATUS.md` y `sub_orch_*\progress.md` para conocer los ultimos hallazgos puntuales).
2. **Inspeccionar estado real del codigo y tests:**
   ```powershell
   uv sync --extra dev --link-mode=copy
   uv run --no-sync pytest tests/test_prototype_theme_motion_splash.py tests/test_m1_foundation_security.py -v
   ```
   Revisa `git status` si ya existe `.git` y lista `src/` y `tests/` para confirmar exactamente que archivos ya fueron completados tras esta ultima marca de tiempo.
3. **No reescribir lo que ya pasa:** Verifica el primer hito no marcado como `COMPLETED` en la tabla superior y continua desde ese hito respetando los contratos de interfaz de `PROJECT.md`.
4. **Actualizar este archivo al cerrar cada hito:** Cada vez que completes o verifiques un hito (`M1`–`M6`), actualiza la tabla de estado, la lista de archivos modificados y el resultado exacto de `pytest` aqui en `docs/HANDOFF_STATUS.md`.
5. **Seguridad GitHub:** No crees repositorio remoto ni ejecutes `git push` sin autorizacion explicita del usuario tras revisar las verificaciones finales de `M6`.

---

## 4. Bitacora de Hitos

- **2026-11-24 16:15 (-05):** Archivo inicializado durante Iteracion 2 de `M1` y `Pista E2E` (con nucleo M1 redactado y suites E2E Tiers 1–4 en depuracion). Pendiente completar M1 gate, E2E `TEST_READY.md` y ejecutar M2–M6.
- **2026-11-24 16:30 (-05) — Cierre de Hito `Pista E2E` -> `COMPLETED`:**
  - **Gate E2E — Iteracion 3: PASS** (`auditor_i3_1` = `CLEAN`, `reviewer_i3_1` = `APPROVE`, `reviewer_i3_2` = `APPROVE`, `challenger_i3_1` = `APPROVE`, `challenger_i3_2` = `APPROVE`).
  - **Resultados exactos de verificacion (`$env:QT_QPA_PLATFORM='offscreen'`):**
    - `uv run --no-sync python -m compileall -q tests/e2e` -> exit `0`
    - `uv run --no-sync pytest --collect-only tests/e2e -q --strict-markers` -> `419 tests collected` (`7` smoke + `189` Tier 1 + `171` Tier 2 + `34` Tier 3 + `18` Tier 4 = `412` Tiers 1–4 >= umbral `391`)
    - `uv run --no-sync pytest -v tests/e2e/test_fixtures_smoke.py` -> `7 passed`
    - `uv run --no-sync pytest -v tests/test_prototype_theme_motion_splash.py` -> `14 passed` (standalone)
    - `uv run --no-sync pytest -v tests/e2e/test_tier1_features.py tests/e2e/test_tier2_boundaries.py -k "fa01 or fa02 or fa03 or fa04 or fa05"` -> `54 passed, 8 failed` (`54/54` nucleo M1 pasando; `8` brechas estrictamente dependientes de M4: `diagnostics_dialog`, `notifications_drawer`, `support_dialog`, `HubView.quick_action_tool_ids`)
  - **Rutas exactas de artefactos E2E:**
    - `C:\Users\datg2\.gemini\antigravity\scratch\teamwork\303cf05e-a44a-4016-9147-e5dcb495c9a4\.agents\teamwork\TEST_INFRA.md`
    - `C:\Users\datg2\.gemini\antigravity\scratch\teamwork\303cf05e-a44a-4016-9147-e5dcb495c9a4\.agents\teamwork\TEST_READY.md`
    - `c:\Users\datg2\OneDrive - Fundación Mamonal\A S\IP DAIRO\2026\PDF Lector by TRODAI\tests\e2e\` (`__init__.py`, `conftest.py`, `fixtures.py`, `test_fixtures_smoke.py`, `test_tier1_features.py`, `test_tier2_boundaries.py`, `test_tier3_pairwise.py`, `test_tier4_workloads.py`)
  - **Pasos pendientes exactos para ejecucion E2E en M6:**
    1. Esperar a completar los hitos de implementacion `M2`–`M5`.
    2. Ejecutar `tests/e2e/` secuencialmente por nivel (`Tier 1 -> Tier 2 -> Tier 3 -> Tier 4`) y la bateria completa mediante `$env:QT_QPA_PLATFORM='offscreen'; uv run --no-sync pytest tests/e2e -v` hasta lograr 100 % (`419/419`) con codigo de salida `0`, y posteriormente ejecutar el endurecimiento adversarial `Tier 5`.
    3. Aplicar/verificar los 3 puntos de sincronizacion de contratos: (a) `PROJECT.md` `path_guard` `validate_local_* -> tuple[bool, str]`, (b) `MainWindow.open_document_path -> bool`, y (c) opt-in de pruebas loopback (`allow_localhost=True`) en `SupportDialog` (M4) para `feedback_reporter.submit_feedback_endpoint` (conservando `allow_localhost=False` por defecto en produccion).
- **2026-11-24 22:18 (-05) — Cierre de Hito `M1` -> `COMPLETED`:**
  - **Gate M1 — Iteracion 5: PASS** (`teamwork_auditor_m1_r5` = `CLEAN`, `teamwork_reviewer_m1_r5_1` = `APPROVE`, `teamwork_reviewer_m1_r5_2` = `APPROVE`, `teamwork_challenger_m1_r5_1` = `CONFIRMED`, `teamwork_challenger_m1_r5_2` = `CONFIRMED`).
  - **Resultados exactos de verificacion:**
    - `uv run --no-sync pytest tests/test_prototype_theme_motion_splash.py tests/test_m1_foundation_security.py -v` -> `39 passed in 41.00s` (exit `0`, Primary Reviewer 1; Auditor M1 `tests/test_m1_foundation_security.py`: `25 passed in 25.23s`, exit `0`).
    - `$env:QT_QPA_PLATFORM="offscreen"; uv run --no-sync pytest tests/e2e/test_tier1_features.py tests/e2e/test_tier2_boundaries.py -k "fa01 or fa02 or fa03 or fa04 or fa05" -v` -> `54 passed, 8 failed` (`54/54` nucleo M1 pasando; `8` objetivos UI aun no implementados pertenecientes estrictamente a M4: `diagnostics_dialog`, `notifications_drawer`, `support_dialog`, `HubView.quick_action_tool_ids`).
    - `node --check deploy/feedback-worker/worker.js` -> exit `0`.
    - Arranque Fase 1 (`QT_QPA_PLATFORM="offscreen"`): `70.80–81.44 ms` (Reviewer 1) y `60.08–74.08 ms` (Auditor) — todos `< 300.0 ms`.
  - **Archivos M1 creados/actualizados:** `VERSION`, `pyproject.toml`, `uv.lock`, `src/pdf_lector/__init__.py`, `src/pdf_lector/core/{path_guard,win32_bridge,secrets_store,temp_manager,crash_guard,single_instance,redaction,metadata_sanitizer,feedback_reporter,notifications,task_control,usage_insights}.py`, `deploy/feedback-worker/{worker.js,wrangler.toml}`, `tests/test_m1_foundation_security.py`.
  - **Nota tecnica explicita para M4 (rendimiento real Windows y cableado):** Aunque `QT_QPA_PLATFORM="offscreen"` pasa `< 300.0 ms` (`~55–81 ms`), el perfilado en Windows real (`QT_QPA_PLATFORM="windows"`) identifico **`QFontDatabase.families()` consumiendo `596.0 ms`** en `src/pdf_lector/ui/splash_screen.py:27` (mas llamadas redundantes a `hasFamily()` / doble `StatusTag.apply_theme()` en `src/pdf_lector/ui/widgets.py:161,179` y `src/pdf_lector/ui/splash_screen.py:182-185`, y `load_theme_profile(check_media_exists=True)` en `src/pdf_lector/app.py:149` / `src/pdf_lector/theme/theme_profile.py:215`). **M4 DEBE optimizar estos puntos en archivos de su propiedad junto con el cableado de `SingleInstanceLock` + `CrashGuard` en `src/pdf_lector/app.py`.**
  - **Siguientes pasos exactos:** `M2` (`ToolRegistry`, 44 herramientas, parametros DPI/idioma, OCR, Captura, Facturas DIAN/Factur-X) y `M3` (`markdown_engine`, `md_security`, anotaciones/tinta ISO 32000, visor mixto, diccionario estilo Kindle) quedan desbloqueados para ejecucion inmediata.
- **2026-11-26 10:55 (-05) — Cierre Final `M4`, `M5`, `M6` (`v0.1.0`) -> `COMPLETED`:**
  - **Bateria completa (`$env:QT_QPA_PLATFORM="offscreen"; uv run --no-sync pytest tests/ -q --tb=short`):** **`515 passed in 2658.27s`** (`exit 0`, `0` fallos, `0` omitidas — incluye unitarias `M1`–`M5`, prototipo base y los `419` casos E2E Tiers 1–4).
  - **Auditoria estatica y arranque Fase 1:** `STATIC_VIOLATIONS: []` (cero emojis Unicode, cero `QtWebEngine`, cero `super().__lt__()`, cero `font-weight: 600`/`DemiBold`), `SVG_VIOLATIONS: []` (100 % poligonales) y `PHASE1_MS=273.56` (`< 300 ms`).
  - **Compilacion real Windows 11 (`uv run --no-sync python scripts/build_exe.py` -> `BUILD_EXIT=0`):**
    - `dist/PDF-MD-Lector-Portable.exe` (SHA-256 `1df9aa038c2953001375fb45ed384233a80547b8e4a2050126ee32482415347d`, firmado `Valid`)
    - `dist/PDF-MD-Lector/PDF-MD-Lector.exe` (paquete `--onedir` instalado por Setup: arranque real total **`1,743.93 ms`** / `FIRST_FRAME_MS=63.85 ms` vs `39,746.60 ms` en `--onefile` — 22x mas rapido; firmado `Valid`)
    - `dist/PDF-MD-Lector-Setup.exe` (SHA-256 `43725e98a4c2dc4130460d1d7f0c9fed1289254cf98b432d79c5f76c066f57c3`, firmado `Valid`, bitmaps HiDPI `100%/150%/200%` renderizados desde `pdf_lector_logo.svg` con antialiasing 4x)
    - `dist/SHA256SUMS.txt`
    - Prueba en vivo de arranque empaquetado (`PDF-MD-Lector.exe`, `PDF_LECTOR_BENCH_STARTUP=1`, `PDF_LECTOR_BENCH_EXIT=1`): `EXIT=0`, Splash progresivo `5% -> 100%`.
    - Firma local gratuita dry-run (`scripts/sign_local.ps1 -DryRun`): `SIGN_DRY_EXIT=0` (sin modificar almacenes de certificados).
  - **Revision visual final (`final_screenshots/`):** 10 capturas claro/oscuro (`980x650`, `1200x760`, `1366x720`, Visor mixto `.pdf`/`.md` con Diccionario estilo Kindle cerrado y abierto, y Theme Studio) aprobadas sin recortes ni violaciones de identidad TRODAI.
  - **Git local:** Inicializado en rama `main` con 6 commits semanticos en espanol, `git notes` de trazabilidad y etiqueta anotada `v0.1.0` (sin repositorio remoto ni `git push`).
- **2026-11-26 13:50 (-05) — Hito M6 UX/UI Estilo Adobe, Rendimiento 60 FPS, Chat IA, Herramientas con Cola y Diccionario -> `COMPLETED`:**
  - **Archivos creados/actualizados:**
    - `assets/icons/{pin,detach,comment,eraser,thumbs,send}.svg` (6 nuevos SVGs 100 % poligonales)
    - `src/pdf_lector/core/{recent_docs,ai_chat_history,i18n,dictionary_engine}.py`
    - `src/pdf_lector/theme/{motion_engine,palette}.py`
    - `src/pdf_lector/ui/{ai_panel,dictionary_panel,hub_view,main_window,pdf_canvas,preferences_dialog,theme_studio,tool_workspace,viewer_view}.py`
    - `scripts/build_exe.py`, `tests/test_m6_ux_refinements.py`
  - **Mejoras clave implementadas y verificadas:**
    1. **Paneles Miniaturas e Indice/Marcadores independientes (sin pestana "Ambos"):** ocultables por defecto al abrir PDF, cada uno con boton propio de **Cerrar (`X`)** y **Desacoplar** a ventana flotante independiente; mantienen compatibilidad con `set_rail_mode(...)`.
    2. **Visor PDF estilo Adobe:** panel lateral izquierdo colapsable automaticamente a cinta (`40px`) al abrir PDF; cinta vertical derecha fija; barra superior de 1 fila solo con iconos y tooltips por defecto (opcion *"Mostrar etiquetas de texto en botones de barras"* en Preferencias).
    3. **Popover contextual automatico al seleccionar texto:** barra estatica de anotaciones oculta por defecto (accesible desde tinta en cinta derecha); al seleccionar/resaltar texto aparece popover flotante con resaltado en 6 colores, nota adhesiva, comentario, copia y busqueda en diccionario.
    4. **Scroll vertical continuo multipagina:** pila continua de paginas con renderizado perezoso sincronizado con `Pagina X / N`, miniaturas y preferencias (`continuous_scroll`).
    5. **Chat IA moderno (`AskDocumentPanel`):** entrada abajo y conversacion arriba con fecha y hora `dd/mm/yyyy HH:mm`, historial persistente por PDF/Markdown, exportar `.md`/`.txt`/`.json`, limpiar historial, contraste Dark/Light corregido y boton cerrar (`close_requested`) funcional.
    6. **Abiertos recientemente y Favoritos en Inicio (`RecentDocumentsStore`):** listado ordenado por fecha/hora con boton Fijar/Favorito (fijados arriba), abrir en 1 clic y limpiar.
    7. **Rendimiento 60 FPS en Theme Studio:** blur piramidal bilineal cacheado sin recrear `QMovie` GIF/WebP, coalescencia de sliders (`45ms`), skip de reconstruccion QSS global cuando no cambia `base_mode`, y build `--onedir` en staging `%TEMP%`.
    8. **Apartado de Herramientas + Cola + Fix `.exe` congelado:** vista inline con previsualizacion multipagina de PDF/MD/imagen, cola por lotes multi-archivo (`Subir/Bajar/Quitar/Limpiar`), solicitud obligatoria de ubicacion de salida y `--collect-submodules pdf_lector` en `scripts/build_exe.py` (`PYZ.pyz` `11/11` modulos empaquetados).
    9. **Diccionario offline reparado y copiable:** corregido `TypeError` en `i18n.t(key, lang, /)`, lexico ampliado a >180 terminos ES + >60 EN, texto seleccionable con raton/teclado y boton de copiado al portapapeles.
  - **Resultados exactos de verificacion:**
    - `tests/test_m6_ux_refinements.py` + `tests/test_prototype_theme_motion_splash.py` + `tests/test_m3_markdown_annotations_dict.py` + `tests/test_m4_ui_updater.py` + `tests/test_m5_integrations_ai_mcp.py`: `40 passed in 25.99s` (luego `tests/test_m6_ux_refinements.py` + `tests/test_m4_ui_updater.py`: `14 passed in 11.86s` tras ajuste visual de `PreferencesDialog`).
    - `tests/e2e/test_tier1_features.py` + `tests/e2e/test_tier3_pairwise.py`: `223 passed in 404.80s`.
    - `tests/e2e/test_tier2_boundaries.py` + `tests/test_m2_tools_ocr_invoices.py`: `188 passed in 76.64s`.
    - Compilacion y firma local (`$env:PDF_LECTOR_SIGN_LOCAL="1"; uv run --no-sync python scripts/build_exe.py --setup-only` + portable previo): `dist/PDF-MD-Lector/PDF-MD-Lector.exe` (`Valid`, arranque `FIRST_FRAME_MS=54.49`, GUI wall `604.13 ms`, `11/11` modulos clave en `PYZ.pyz`), `dist/PDF-MD-Lector-Portable.exe` (`Valid`, SHA-256 `9f19468b198de1e0448af6db8577d621ee4386c6fd937a7572caab984c5d9e3a`), `dist/PDF-MD-Lector-Setup.exe` (`Valid`, SHA-256 `ad8ef848a56ae36ca8d3a9c2be14f475e4c38d53400feb6f3ffbc2ceb71e8ace`).
- **2026-11-26 14:42 (-05) — Hito M7 (`v0.2.0`) -> `COMPLETED`:**
  - **Archivos creados:** `src/pdf_lector/core/{ai_hardware,autosave}.py`, `src/pdf_lector/ui/notes_panel.py`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `tests/test_m7_v020_features.py`.
  - **Archivos actualizados:** `VERSION` (`0.2.0`), `pyproject.toml`, `packaging/installer.iss`, `src/pdf_lector/__init__.py`, `src/pdf_lector/api/schemas.py`, `src/pdf_lector/app.py`, `src/pdf_lector/core/{ai_rag,dictionary_engine,pdf_annotations}.py`, `src/pdf_lector/ui/{about_updater_dialog,ai_panel,dictionary_panel,main_window,pdf_canvas,viewer_view,widgets}.py`, `CHANGELOG.md`, `SECURITY.md`, `tests/{test_m1_foundation_security,test_m4_ui_updater,test_m5_packaging_docs}.py`, `tests/e2e/test_tier1_features.py`.
  - **Resultados exactos de verificacion:**
    - `tests/test_m7_v020_features.py`: `8 passed in 5.81s`
    - `tests/e2e/test_tier1_features.py`: `189 passed in 136.55s`
    - `tests/e2e/test_fixtures_smoke.py tests/e2e/test_tier2_boundaries.py tests/e2e/test_tier3_pairwise.py tests/e2e/test_tier4_workloads.py`: `230 passed in 423.61s`
    - Checks estaticos (`capture_v3.py`): `STATIC_CHECKS_OK: all SVGs 100% polygonal, 0 Unicode emojis.`, `FIRST_FRAME_MS=36.33`
    - Compilacion + firma local (`$env:PDF_LECTOR_SIGN_LOCAL="1"; uv run --no-sync python scripts/build_exe.py`): `PDF-MD-Lector-Setup.exe` (`Valid`, SHA-256 `689bb6991f91efd730827611f26cff36c079abf3e7dff9aecbe7f9dcada6cc01`), `PDF-MD-Lector-Portable.exe` (`Valid`, SHA-256 `84f0869d4ab3ca480148816cdcf88647325db8d2656aca75fc58a631a96af4df`).


