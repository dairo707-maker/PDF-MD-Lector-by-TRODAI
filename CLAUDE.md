# CLAUDE.md — Guia de Contexto, Auditoria y Relevo Multi-IA

Este repositorio implementa **PDF/MD Lector** (`PDF/MD Lector · by TRODAI`, licencia MIT), un lector y editor dual **100 % local y privado** de archivos **PDF** y **Markdown (`.md` / `.markdown`)** con suite modular de herramientas PDF para **Windows 11**.

Este documento permite que **Claude Code**, otros modelos de IA o desarrolladores humanos auditen el repositorio o retomen el trabajo inmediatamente desde el ultimo hito completado.

> **Estado vivo del proyecto:** Consulta primero [`docs/HANDOFF_STATUS.md`](docs/HANDOFF_STATUS.md) para ver que hitos (`M1`–`M6` y `E2E`) estan completados, que archivos existen y exactamente por donde continuar.

---

## 1. Identidad, Rutas y Comandos Obligatorios

- **Producto visible:** `PDF/MD Lector` (`PDF/MD Lector · by TRODAI`)
- **Version objetivo:** `0.1.0` (evolucionando desde la base validada `v0.1.0-alpha.2`)
- **Workspace local:** `c:\Users\datg2\OneDrive - Fundación Mamonal\A S\IP DAIRO\2026\PDF Lector by TRODAI`
- **Repositorio hermano (referencia visual, UX PySide6 y assets OFL):** `c:\Users\datg2\OneDrive - Fundación Mamonal\A S\IP DAIRO\2026\App optimización PC-Almacenamiento`
- **Identificadores compatibles con Windows y GitHub (sin `/`):**
  - Repo objetivo (NO crear ni hacer push todavia): `github.com/dairo707-maker/PDF-MD-Lector-by-TRODAI`
  - Ejecutables: `PDF-MD-Lector-Portable.exe`, `PDF-MD-Lector-Setup.exe`
  - `AppUserModelID`: `TRODAI.PDFMDLector`
  - ProgIDs Windows (`HKCU`): `TRODAI.PDFMDLector.PDF` (`.pdf`), `TRODAI.PDFMDLector.MD` (opcional `.md` / `.markdown`)
  - Native Messaging Host: `com.trodai.pdf_md_lector`
  - Servidor MCP (`stdio`): `pdf-md-lector-mcp`
  - Datos locales por usuario: `%LOCALAPPDATA%\TRODAI\PDFLector\` (`logs\`, `reports\`, `secrets.json`, `usage_insights.json`, `bookmarks.json`, `theme_profile.json`, `dictionary\`)

### Comandos en OneDrive (Windows 11 / PowerShell)
Este workspace vive dentro de **OneDrive**, donde los hardlinks fallan. Usa **siempre** `--link-mode=copy` y `--no-sync`:

```powershell
# Sincronizar entorno Python 3.13 + PySide6
uv sync --extra dev --link-mode=copy

# Ejecutar toda la bateria de pruebas automatizadas
uv run --no-sync pytest -v

# Lanzar la aplicacion en vivo
uv run --no-sync python -m pdf_lector.app
```

---

## 2. Reglas No Negociables

### A. Regla Critica de Git y GitHub
- Inicializar y mantener **solo repositorio Git local** en la rama `main` con commits semanticos en espanol, `git notes` y etiqueta anotada `v0.1.0` al finalizar.
- **PROHIBIDO** crear el repositorio remoto o ejecutar `git push` hasta que el usuario apruebe explicitamente los resultados finales verificados.

### B. Privacidad y Seguridad de los Usuarios
1. **100 % local por defecto y cero telemetria oculta.** Ninguna conexion externa sin accion o consentimiento explicito del usuario.
2. **Validacion estricta en disco local (`src/pdf_lector/core/path_guard.py`):** bloquear rutas UNC (`\\...`, `//...`, `\\?\UNC\`), esquemas de red (`http`, `https`, `ftp`, `file`, `smb`, `javascript`, `data`), *path traversal* (`..`), unidades de red remotas y placeholders cloud-only en banners de temas, glosarios `.json`, enlaces/imagenes Markdown y APIs locales.
3. **Markdown seguro (`core/md_security.py` + `core/markdown_engine.py`):** renderizado via `markdown-it-py` + sanitizador estricto `nh3` + `QTextBrowser` (`setOpenExternalLinks(False)` + `loadResource()` controlado). Bloquear por defecto imagenes remotas y scripts/HTML inseguro; evitar dependencias pesadas de `QtWebEngine*`.
4. **Secretos con Windows DPAPI (`core/secrets_store.py`):** claves BYOK, Bearer token de la API local y tokens opcionales cifrados con `CryptProtectData` en `crypt32.dll`.
5. **Tachado y sanitizacion reales:** `core/redaction.py` debe destruir texto/objetos subyacentes en flujos PDF y XObjects (no cubrir solo con rectangulos); `core/metadata_sanitizer.py` debe limpiar `/Info`, `/Metadata` (XMP), `/PieceInfo` y actualizaciones incrementales.
6. **Privacidad en anotaciones e insights:** autor de comentarios configurable (valor por defecto `Anónimo`; **nunca** exponer automaticamente el nombre de usuario de Windows sin consentimiento). `LocalUsageInsights` guarda solo contadores agregados, nunca texto ni rutas confidenciales.
7. **Anonimizacion estricta en reportes (`core/feedback_reporter.py`):** reemplazar usuario de Windows, nombre de equipo, rutas personales/OneDrive, correos e IPs por marcadores neutros; mostrar siempre vista previa completa y editable antes de enviar; cero envios silenciosos.

### C. Identidad Visual TRODAI, UI/UX y Seguridad PySide6 / Shiboken6
- **Iconos y marca:** `assets/icons/pdf_lector_logo.svg` y `pdf_lector.ico` (colores solidos 100 % opacos) para el lector; `assets/icons/trodai_sentinel.svg` (`brand.svg`) y wordmark monolitico `TRODAI` (nunca resaltar "AI") estrictamente como sello `by TRODAI`.
- **SVGs 100 % poligonales y cero emojis Unicode:** prohibido usar emojis Unicode en UI/codigo y prohibido `<circle>`, `<ellipse>` o curvas `C/S/Q/T/A` en SVG (solo `M/L/Z`, `<polygon>`, `<polyline>`, `<rect>`, `stroke-linecap="square"`, `stroke-linejoin="miter"`, `stroke-miterlimit="4"`).
- **Tipografia y geometria:** `Space Grotesk` (pesos `300`, `400`, `500`, `700`; **nunca `600`**) + `JetBrains Mono`. `border-radius: 0px` global (unica excepcion `6px` en `QWidget#TopBar`), DWM `DWMWCP_DONOTROUND`.
- **Ajustes UI aprobados en vivo:**
  - Cero acentos de color en fondos/bordes de seleccion ni cuadros de color en Theme Studio.
  - `StatusTag` 100 % opacos (prohibido `rgba(` en badges).
  - Todas las tarjetas `ToolCard` del Hub de tamano uniforme `232x104`.
  - Ventana Windows 11 auto-ajustada al area util con Minimizar/Maximizar/Cerrar siempre visibles + Pantalla Completa (`F11` / `Esc`).
  - Arranque en 2 fases (`< 300 ms` al primer fotograma desde codigo fuente) con SplashScreen temporal estilo Adobe + carga perezosa de motores pesados.
- **Reglas criticas PySide6 / Shiboken6 (heredadas de WinOptiAI):**
  - Nunca llamar `super().__lt__()` en overrides de widgets/items Qt.
  - Nunca destruir widgets hijos de forma sincrona dentro de slots `clicked`; diferir con `QTimer.singleShot(0, ...)` + `w.hide(); w.deleteLater()`, y validar widgets vivos con `shiboken6.isValid(...)`.
  - Usar `Qt.QueuedConnection` para senales entre workers y UI; llamar `CoInitializeEx` en hilos COM (WIA/Office); limpiar variables `_MEIPASS*` antes de lanzar subprocesos hijos (p. ej. actualizacion desacoplada).

---

## 3. Alcance Funcional Resumen (v0.1.0)

1. **Suite modular PDF (~38 herramientas en 8 categorias):**
   - **Acciones Rapidas dinamicas:** 6 iniciales por defecto (`Firmar PDF`, `Unir PDF`, `Eliminar páginas PDF`, `Desbloquear PDF`, `Convertir desde PDF`, `Comprimir PDF`) que se reordenan automaticamente con las 6 mas usadas segun `LocalUsageInsights` (restablecidas al limpiar historial).
   - **Organizar y Editar (20)**, **Convertir y OCR (5 + `MD <-> PDF` + WinRT OCR / Tesseract / RapidOCR opcional)**, **Crear (Incl. Captura `QScreen` + Escaner WIA / Camara `QCamera`)**, **Facturas duales (Colombia DIAN UBL 2.1 XML+NIT+IVA/INC+CUFE+QR e Internacional Factur-X/ZUGFeRD/XRechnung EN 16931 PDF/A-3)**, **Optimizar y Reparar**, **Seguridad y Privacidad**, **Ver y Comprobar**.
   - **Salida e idioma configurables:** en todas las herramientas aplicables, permitir ajustar **resolucion de salida (DPI / calidad)** e **idioma del archivo original** (`Auto`, `es`, `en`, `pt`, `fr`, `de`, `it`, multi).
2. **Visor mixto PDF + Markdown y anotaciones ISO 32000:**
   - Pestanas mixtas `.pdf` y `.md`/`.markdown`; 3 modos Markdown (`Lectura | Dividido | Editor`); switch de 3 modos en rail izquierdo (`Miniaturas | Índice y Marcadores | Ambos`).
   - Anotaciones nativas ISO 32000 (`/Highlight` 6 colores, `/Text`, `/Ink` con `QTabletEvent` + rechazo de palma + modo 1-clic "Escribir tactil" con `QTouchEvent` manteniendo scroll/zoom 2 dedos, `/FreeText`/`/Popup` con autor e hilos, `Guardar` con respaldo + `Guardar como` + `Aplanar PDF`).
   - **Diccionario Offline SQLite estilo Kindle:** inicia **cerrado por defecto**, se abre al seleccionar una palabra con accion **`Buscar en diccionario`** o desde su pestana/boton en el Visor, permite **desacoplarse en ventana flotante**, volver a **acoplarse** y **cerrarse**; ES/EN incluidos + Gestor de Idiomas + glosarios `.json`.
3. **Actualizaciones, CrashGuard, Soporte sin GitHub y Firma gratuita (`$0`):**
   - `updater.py`: consulta GitHub Releases (`/releases/latest`, fallback `curl.exe`), auto-chequeo cada 24 h con toggle **`Solo offline`**, `skipped_version`, banner superior solido no intrusivo, streaming `%`, verificacion obligatoria **SHA-256** contra `SHA256SUMS.txt` y descarga/reinicio limpio tras verificar cambios sin guardar.
   - `CrashGuard` + Centro de Soporte sin cuenta GitHub (`deploy/feedback-worker/worker.js` + `wrangler.toml`, respaldo local `.md`/`.json`, portapapeles, `mailto:` y enlace opcional a Issues).
   - Doble estrategia de firma `$0`: `scripts/sign_local.ps1` (certificado Authenticode local + `-InstallTrust`/`-UninstallTrust` solo bajo consentimiento) + `packaging/installer.iss` (`PrivilegesRequired=lowest`) + CI `.github/workflows/build-release.yml` (SLSA `attest-build-provenance` + plantilla SignPath.io Free OSS).
4. **Integraciones:** Windows 11 `HKCU` + CLI completo + extension Manifest V3 (`browser-extension/`) + Native Messaging Host + API REST/OpenAPI perezosa (`127.0.0.1` + Bearer, OFF por defecto) + Panel IA "Preguntar al documento" (BM25 local + BYOK + MCP cliente con consentimiento) + servidor `pdf-md-lector-mcp`.

---

## 4. Referencias de Especificacion y Orquestacion

- **Estado y lista exacta de relevo:** [`docs/HANDOFF_STATUS.md`](docs/HANDOFF_STATUS.md)
- **Guia multi-IA y auditoria:** [`AGENTS.md`](AGENTS.md) y [`docs/AUDIT_HANDOFF.md`](docs/AUDIT_HANDOFF.md)
- **Documentacion tecnica (`docs/`):** [`ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`API_AND_MCP.md`](docs/API_AND_MCP.md), [`SECURITY_AND_PRIVACY.md`](docs/SECURITY_AND_PRIVACY.md), [`CODE_SIGNING_AND_INSTALL.md`](docs/CODE_SIGNING_AND_INSTALL.md)
- **Especificacion completa aprobada por el usuario:** `C:\Users\datg2\.gemini\antigravity\brain\8d390ff7-9f66-48fc-8ad1-209c2224308a\teamwork_prompt_303cf05e-a44a-4016-9147-e5dcb495c9a4.md`
- **Plan maestro y contratos (`PROJECT.md`):** `C:\Users\datg2\.gemini\antigravity\scratch\teamwork\303cf05e-a44a-4016-9147-e5dcb495c9a4\.agents\teamwork\PROJECT.md`
