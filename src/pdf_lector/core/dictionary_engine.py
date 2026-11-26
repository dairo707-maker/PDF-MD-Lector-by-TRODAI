"""Offline SQLite dictionary engine with bundled ES/EN lexicons and on-demand language packs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import ssl
import subprocess
import unicodedata
from typing import Callable
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .path_guard import validate_local_glossary_path


def _default_dict_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".config"
    return base / "TRODAI" / "PDFLector" / "dictionaries"


def normalize_term(word: str) -> str:
    """Lowercase and strip diacritics for fast accent-insensitive SQLite lookups."""
    text = (word or "").strip().lower()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@dataclass(frozen=True)
class DictionaryEntry:
    word: str
    lang: str
    pos: str
    definition: str
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True)
class LanguagePackInfo:
    code: str
    name: str
    source_label: str
    url: str
    bundled: bool = False


AVAILABLE_LANGUAGE_PACKS: tuple[LanguagePackInfo, ...] = (
    LanguagePackInfo("es", "Espanol", "Nucleo Offline TRODAI", "", bundled=True),
    LanguagePackInfo("en", "English", "TRODAI Offline Core", "", bundled=True),
    LanguagePackInfo(
        "pt",
        "Portugues",
        "OpenLexicon PT (Wiktionary/Apache-2.0)",
        "https://raw.githubusercontent.com/dairo707-maker/trodai-dictionaries/main/pt.json",
    ),
    LanguagePackInfo(
        "fr",
        "Francais",
        "OpenLexicon FR (Wiktionary/Apache-2.0)",
        "https://raw.githubusercontent.com/dairo707-maker/trodai-dictionaries/main/fr.json",
    ),
    LanguagePackInfo(
        "de",
        "Deutsch",
        "OpenLexicon DE (Wiktionary/Apache-2.0)",
        "https://raw.githubusercontent.com/dairo707-maker/trodai-dictionaries/main/de.json",
    ),
    LanguagePackInfo(
        "it",
        "Italiano",
        "OpenLexicon IT (Wiktionary/Apache-2.0)",
        "https://raw.githubusercontent.com/dairo707-maker/trodai-dictionaries/main/it.json",
    ),
)

_PACK_BY_CODE = {p.code: p for p in AVAILABLE_LANGUAGE_PACKS}

_BUNDLED_LEXICONS: dict[str, list[tuple[str, str, str, str]]] = {
    "es": [
        ("documento", "sust.", "Escrito o registro digital que contiene datos, acuerdos o evidencia formal.", "archivo, texto, folio"),
        ("lectura", "sust.", "Accion de interpretar y comprender signos escritos o digitales.", "revision, consulta, estudio"),
        ("privacidad", "sust.", "Derecho y garantia de mantener la informacion personal bajo control exclusivo.", "confidencialidad, reserva"),
        ("seguridad", "sust.", "Conjunto de medidas tecnicas para proteger datos frente a accesos no autorizados.", "proteccion, resguardo"),
        ("cifrado", "sust.", "Transformacion criptografica de datos para volverlos ilegibles sin clave autorizada.", "encriptacion, proteccion"),
        ("firma", "sust.", "Trazo o sello criptografico que acredita autoria e integridad de un documento.", "rubrica, certificado"),
        ("criptografica", "adj.", "Relativo al uso de algoritmos matematicos de clave publica o simetrica para proteger datos.", "cifrada, segura"),
        ("factura", "sust.", "Comprobante tributario y comercial que detalla bienes o servicios prestados.", "recibo, comprobante"),
        ("electronica", "adj.", "Emitida, firmada y validada en formato digital estructurado conforme a norma.", "digital, telematica"),
        ("contrato", "sust.", "Acuerdo formal entre dos o mas partes con obligaciones juridicas exigibles.", "convenio, pacto"),
        ("clausula", "sust.", "Disposicion especifica dentro de un contrato, estatuto o documento legal.", "articulo, estipulacion"),
        ("anexo", "sust.", "Documento adjunto que complementa o detalla el contenido de un escrito principal.", "adjunto, apendice"),
        ("adjunto", "sust.", "Archivo incrustado o vinculado dentro de un PDF o comunicacion electronica.", "anexo, suplemento"),
        ("marcador", "sust.", "Senal de acceso rapido a una pagina o seccion especifica de un documento.", "indice, senalador"),
        ("indice", "sust.", "Lista ordenada de capitulos y secciones que permite navegar un documento.", "tabla de contenido, sumario"),
        ("metadatos", "sust.", "Propiedades tecnicas incrustadas en un archivo (autor, titulo, fecha, software).", "atributos, propiedades"),
        ("tachado", "sust.", "Eliminacion definitiva de texto o imagenes sensibles para impedir su recuperacion.", "redaccion, censura"),
        ("compresion", "sust.", "Reduccion del tamano de un archivo optimizando flujos de datos e imagenes.", "compactacion, optimizacion"),
        ("pagina", "sust.", "Cada una de las caras o folios numerados que componen un documento PDF.", "hoja, folio"),
        ("rotacion", "sust.", "Giro geometrico de una pagina en angulos de 90, 180 o 270 grados.", "giro, orientacion"),
        ("escaner", "sust.", "Dispositivo optico que digitaliza documentos fisicos en imagenes o PDF.", "digitalizador"),
        ("reconocimiento", "sust.", "Proceso automatizado de deteccion de texto (OCR) sobre imagenes escaneadas.", "lectura optica, OCR"),
        ("validacion", "sust.", "Verificacion tecnica de estructura, esquemas XML y reglas de negocio.", "auditoria, comprobacion"),
        ("integridad", "sust.", "Garantia de que un archivo no ha sufrido alteraciones desde su emision.", "autenticidad, inalterabilidad"),
        ("resumen", "sust.", "Exposicion breve que condensa las ideas principales de un documento extenso.", "sintesis, compendio"),
        ("local", "adj.", "Que se ejecuta y almacena exclusivamente en el equipo del usuario sin red externa.", "interno, sin conexion"),
        ("rendimiento", "sust.", "Capacidad de un software para responder con fluidez y bajo consumo de recursos.", "eficiencia, velocidad"),
        ("fluido", "adj.", "Que opera de manera continua, rapida y sin bloqueos perceptibles.", "agil, continuo"),
        ("bateria", "sust.", "Fuente de energia portatil cuya autonomia se protege reduciendo carga grafica.", "acumulador, energia"),
        ("impuesto", "sust.", "Tributo obligatorio aplicado sobre transacciones comerciales o ingresos.", "gravamen, IVA"),
        ("subtotal", "sust.", "Importe parcial resultante antes de sumar impuestos o descuentos.", "base gravable"),
        ("identidad", "sust.", "Conjunto de atributos visuales y tecnicos que distinguen a una organizacion.", "marca, sello"),
        ("gobernanza", "sust.", "Marco de politicas y controles para administrar datos y procesos con rigor.", "gestion, control"),
        ("analitica", "sust.", "Disciplina de examen sistematico de datos para extraer conocimiento util.", "analisis, estadistica"),
        ("almacenamiento", "sust.", "Espacio y organizacion destinados a conservar documentos y recursos digitales.", "archivo, repositorio"),
        ("licencia", "sust.", "Autorizacion legal que define los derechos de uso y distribucion del software.", "permiso, concesion"),
        ("permiso", "sust.", "Autorizacion granular para imprimir, copiar o modificar un documento.", "facultad, privilegio"),
        ("esquema", "sust.", "Definicion formal de la estructura valida de etiquetas XML o datos JSON.", "estructura, XSD"),
        ("prototipo", "sust.", "Version funcional temprana construida para validar diseno, flujos y usabilidad.", "maqueta funcional"),
        ("contraste", "sust.", "Diferencia de luminancia entre texto y fondo que garantiza legibilidad visual.", "nitidez, claridad"),
        ("diccionario", "sust.", "Repertorio ordenado de palabras con sus definiciones, categoria y sinonimos.", "lexico, glosario"),
        ("idioma", "sust.", "Sistema linguistico utilizado en la interfaz o en el contenido de un documento.", "lengua, lenguaje"),
        ("archivo", "sust.", "Conjunto estructurado de bytes almacenado bajo un nombre en disco local.", "fichero, documento"),
        ("nota", "sust.", "Comentario o observacion adherida a una seccion especifica durante la lectura.", "anotacion, apunte"),
        ("precision", "sust.", "Exactitud y rigor en el renderizado, calculo o ejecucion tecnica.", "exactitud, rigor"),
    ],
    "en": [
        ("document", "noun", "A structured written or digital record conveying information or evidence.", "file, record, paper"),
        ("privacy", "noun", "The right and technical guarantee to keep personal data under local control.", "confidentiality, seclusion"),
        ("security", "noun", "Measures protecting documents and systems from unauthorized access or tampering.", "protection, safeguard"),
        ("encryption", "noun", "Mathematical encoding of data so only authorized keyholders can read it.", "cipher, protection"),
        ("signature", "noun", "Visual mark or cryptographic seal proving authorship and document integrity.", "endorsement, seal"),
        ("invoice", "noun", "Commercial and tax bill itemizing goods, services, taxes, and totals.", "bill, statement"),
        ("electronic", "adj.", "Generated, validated, and exchanged in structured digital machine-readable form.", "digital, paperless"),
        ("contract", "noun", "Legally binding agreement specifying obligations between parties.", "agreement, pact"),
        ("clause", "noun", "A distinct provision or article inside a legal document or agreement.", "term, stipulation"),
        ("attachment", "noun", "An auxiliary file embedded inside a PDF container.", "enclosure, annex"),
        ("bookmark", "noun", "A saved navigation pointer linking directly to a specific page or heading.", "marker, pin"),
        ("outline", "noun", "Hierarchical table of contents embedded in a PDF for rapid navigation.", "toc, index"),
        ("metadata", "noun", "Embedded properties describing author, title, creation date, and tooling.", "attributes, properties"),
        ("redaction", "noun", "Irreversible removal of sensitive text or graphics from a document.", "sanitization, scrubbing"),
        ("compression", "noun", "Reduction of file size by optimizing object streams and embedded images.", "shrinking, compaction"),
        ("page", "noun", "A single numbered sheet within a PDF document.", "sheet, leaf"),
        ("rotation", "noun", "Geometric page orientation turn in increments of 90 degrees.", "orientation, turn"),
        ("scanner", "noun", "Hardware device that digitizes physical sheets into PDF or image files.", "digitizer"),
        ("validation", "noun", "Automated verification of XML schemas, checksums, and business rules.", "audit, check"),
        ("integrity", "noun", "Assurance that a document has not been altered since signing or creation.", "authenticity"),
        ("summary", "noun", "Concise synthesis capturing the essential points of a longer text.", "abstract, digest"),
        ("local", "adj.", "Executed and stored exclusively on the user's device without external servers.", "offline, on-device"),
        ("performance", "noun", "Speed, responsiveness, and resource efficiency during active reading.", "throughput, fluidity"),
        ("battery", "noun", "Portable power source preserved by throttling background motion effects.", "power cell"),
        ("contrast", "noun", "Luminance ratio between foreground text and backdrop ensuring legibility.", "clarity, readability"),
        ("dictionary", "noun", "Structured lexicon providing definitions, parts of speech, and synonyms.", "glossary, lexicon"),
        ("language", "noun", "Linguistic system supported by UI labels and offline lookup packs.", "tongue, locale"),
        ("permission", "noun", "Access rule governing printing, copying, or editing inside a PDF.", "privilege, grant"),
        ("prototype", "noun", "Early working build used to test visual architecture and real workflows.", "pilot, preview"),
        ("precision", "noun", "Exactness in geometric rendering, layout alignment, and numerical output.", "accuracy, rigor"),
        ("governance", "noun", "Framework of policies and controls ensuring trustworthy data stewardship.", "oversight, stewardship"),
        ("analytics", "noun", "Systematic inspection of structured metrics to support informed decisions.", "analysis, insights"),
        ("storage", "noun", "Persistent local space dedicated to keeping documents and configurations.", "repository, disk"),
        ("license", "noun", "Legal grant specifying permitted use, modification, and redistribution terms.", "permit, grant"),
        ("schema", "noun", "Formal specification defining valid XML or JSON data structure.", "specification, XSD"),
        ("reading", "noun", "Process of inspecting and comprehending written or typographic content.", "review, study"),
        ("annotation", "noun", "Highlight, sticky note, or markup layered onto a document page.", "comment, note"),
        ("watermark", "noun", "Translucent text or seal stamped across pages to indicate status.", "stamp, imprint"),
        ("merge", "verb", "Combine multiple PDF documents sequentially into a single file.", "combine, join"),
        ("split", "verb", "Divide a multi-page PDF into separate ranges or individual files.", "separate, extract"),
    ],
}


class DictionaryEngine:
    """Offline-first SQLite dictionary engine with ES/EN core and downloadable language packs."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._dir = Path(base_dir) if base_dir else _default_dict_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ensure_bundled_seeded()

    def _db_path(self, lang: str) -> Path:
        clean = "".join(ch for ch in lang.lower() if ch.isalpha())[:8] or "es"
        return self._dir / f"dict_{clean}.sqlite"

    def _init_db(self, db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                norm TEXT NOT NULL,
                word TEXT NOT NULL,
                pos TEXT NOT NULL,
                definition TEXT NOT NULL,
                synonyms TEXT NOT NULL,
                PRIMARY KEY (norm, pos)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_norm ON entries (norm)")
        return conn

    def _seed_entries(self, lang: str, rows: list[tuple[str, str, str, str]]) -> int:
        db_path = self._db_path(lang)
        conn = self._init_db(db_path)
        try:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO entries (norm, word, pos, definition, synonyms) VALUES (?, ?, ?, ?, ?)",
                    [(normalize_term(w), w.strip(), pos.strip(), dfn.strip(), syn.strip()) for w, pos, dfn, syn in rows],
                )
            return len(rows)
        finally:
            conn.close()

    def _ensure_bundled_seeded(self) -> None:
        for code, rows in _BUNDLED_LEXICONS.items():
            db = self._db_path(code)
            if not db.exists() or db.stat().st_size == 0:
                self._seed_entries(code, rows)

    def installed_languages(self) -> list[str]:
        codes: list[str] = []
        for path in sorted(self._dir.glob("dict_*.sqlite")):
            code = path.stem.removeprefix("dict_")
            if code:
                codes.append(code)
        return codes

    def entry_count(self, lang: str) -> int:
        db = self._db_path(lang)
        if not db.exists():
            return 0
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute("SELECT COUNT(*) FROM entries").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def lookup(self, word: str, lang: str = "es") -> list[DictionaryEntry]:
        norm = normalize_term(word)
        if not norm:
            return []
        db = self._db_path(lang)
        if not db.exists():
            return []
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT word, pos, definition, synonyms FROM entries WHERE norm = ? ORDER BY pos",
                (norm,),
            ).fetchall()
            # Try simple Spanish/English plural fallback if exact hit is absent
            if not rows and len(norm) > 4 and norm.endswith("s"):
                singular = norm[:-2] if norm.endswith("es") and len(norm) > 5 else norm[:-1]
                rows = conn.execute(
                    "SELECT word, pos, definition, synonyms FROM entries WHERE norm = ? ORDER BY pos",
                    (singular,),
                ).fetchall()
            out: list[DictionaryEntry] = []
            for w, pos, dfn, syn in rows:
                syns = tuple(s.strip() for s in syn.split(",") if s.strip())
                out.append(DictionaryEntry(word=w, lang=lang, pos=pos, definition=dfn, synonyms=syns))
            return out
        finally:
            conn.close()

    def suggest(self, prefix: str, lang: str = "es", *, limit: int = 8) -> list[str]:
        norm = normalize_term(prefix)
        db = self._db_path(lang)
        if not db.exists():
            return []
        conn = sqlite3.connect(str(db))
        try:
            if norm:
                rows = conn.execute(
                    "SELECT DISTINCT word FROM entries WHERE norm LIKE ? ORDER BY norm LIMIT ?",
                    (f"{norm}%", int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT word FROM entries ORDER BY norm LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [str(r[0]) for r in rows]
        finally:
            conn.close()

    def import_local_glossary(self, lang: str, json_path: Path | str) -> int:
        """Import a local JSON glossary list/dict (enforces strict path_guard validation)."""
        ok, reason = validate_local_glossary_path(json_path, check_exists=True)
        if not ok:
            raise ValueError(reason)
        path = Path(json_path).resolve()
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = self._parse_pack_payload(raw)
        if not rows:
            raise ValueError("empty_glossary")
        return self._seed_entries(lang, rows)

    def _parse_pack_payload(self, raw: object) -> list[tuple[str, str, str, str]]:
        rows: list[tuple[str, str, str, str]] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("word") and item.get("definition"):
                    syns = item.get("synonyms", [])
                    syn_str = ", ".join(str(s) for s in syns) if isinstance(syns, list) else str(syns)
                    rows.append(
                        (
                            str(item["word"]),
                            str(item.get("pos", "term")),
                            str(item["definition"]),
                            syn_str,
                        )
                    )
        elif isinstance(raw, dict):
            for word, val in raw.items():
                if isinstance(val, str):
                    rows.append((str(word), "term", val, ""))
                elif isinstance(val, dict) and val.get("definition"):
                    syns = val.get("synonyms", [])
                    syn_str = ", ".join(str(s) for s in syns) if isinstance(syns, list) else str(syns)
                    rows.append((str(word), str(val.get("pos", "term")), str(val["definition"]), syn_str))
        return rows

    def download_language_pack(
        self,
        lang: str,
        *,
        fetcher: Callable[[str], bytes] | None = None,
        consent_granted: bool | None = None,
    ) -> int:
        """Download an additional open-source language pack ONLY under explicit user consent."""
        info = _PACK_BY_CODE.get(lang)
        if info is None:
            raise ValueError(f"Unsupported language pack: {lang}")
        if info.bundled:
            self._ensure_bundled_seeded()
            return self.entry_count(lang)
        if consent_granted is False or (consent_granted is None and fetcher is None):
            raise PermissionError("user_consent_required")
        if not info.url:
            raise ValueError("Missing pack URL")

        def _default_fetch(url: str) -> bytes:
            parsed_url = urlparse(url)
            scheme = (parsed_url.scheme or "").lower()
            host = (parsed_url.hostname or "").lower()
            is_loopback = scheme == "http" and host in ("127.0.0.1", "localhost")
            if scheme != "https" and not is_loopback:
                raise ValueError("network_or_url_blocked")
            req = Request(url, headers={"User-Agent": "PDF-MD-Lector-by-TRODAI/0.1"})
            try:
                with urlopen(req, timeout=15) as resp:
                    return resp.read(15 * 1024 * 1024)
            except (URLError, ssl.SSLError, OSError) as exc:
                clean_env = {
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith("_MEIPASS") and k != "PYTHONHOME"
                }
                try:
                    proc = subprocess.run(
                        [
                            "curl.exe",
                            "-fsSL",
                            "--max-time",
                            "15",
                            "--max-filesize",
                            str(15 * 1024 * 1024),
                            url,
                        ],
                        capture_output=True,
                        check=True,
                        env=clean_env,
                        timeout=20,
                    )
                    return proc.stdout
                except Exception as curl_exc:
                    raise RuntimeError(f"Language pack download failed: {exc}") from curl_exc

        payload_bytes = (fetcher or _default_fetch)(info.url)
        parsed = json.loads(payload_bytes.decode("utf-8"))
        rows = self._parse_pack_payload(parsed)
        if not rows:
            raise ValueError("Downloaded pack contained no entries")
        return self._seed_entries(lang, rows)

    def remove_language_pack(self, lang: str) -> bool:
        info = _PACK_BY_CODE.get(lang)
        if info is not None and info.bundled:
            return False
        db = self._db_path(lang)
        if db.exists():
            db.unlink()
            return True
        return False
