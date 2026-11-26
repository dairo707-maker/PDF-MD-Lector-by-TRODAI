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
        ("telefono", "sust.", "Dispositivo o numero asignado para comunicacion de voz o mensajeria a distancia.", "numero telefonico, linea, celular"),
        ("direccion", "sust.", "Datos de ubicacion fisica o postal donde reside una persona o funciona una entidad.", "domicilio, ubicacion, residencia"),
        ("propietario", "sust.", "Persona natural o juridica titular de un bien, mascota, inmueble o derecho.", "dueno, titular"),
        ("identificacion", "sust.", "Documento o codigo unico que acredita oficialmente la identidad de alguien.", "cedula, documento, registro"),
        ("correo", "sust.", "Servicio o direccion electronica empleada para enviar y recibir mensajes digitales.", "email, mensajeria"),
        ("historia", "sust.", "Relacion cronologica y documentada de hechos, antecedentes o atenciones clinicas.", "historial, registro, cronica"),
        ("clinica", "adj.", "Relativo al examen, diagnostico, evolucion y tratamiento medico de un paciente.", "asistencial, medica"),
        ("historia clinica", "sust.", "Expediente reservado donde se registran antecedentes, examenes, diagnosticos y formulas.", "expediente medico, historial"),
        ("formula", "sust.", "Prescripcion escrita con medicamentos, dosis, frecuencia y via de administracion.", "receta, prescripcion"),
        ("paciente", "sust.", "Ser vivo que recibe atencion, evaluacion o tratamiento por un profesional de la salud.", "atendido, usuario"),
        ("veterinaria", "sust.", "Rama de la medicina dedicada a la prevencion, diagnostico y curacion de animales.", "medicina animal, zootecnia"),
        ("diagnostico", "sust.", "Determinacion de la naturaleza de una enfermedad o problema mediante signos y pruebas.", "dictamen, valoracion"),
        ("tratamiento", "sust.", "Conjunto de medios higienicos, farmacologicos o quirurgicos para aliviar o curar.", "terapia, manejo"),
        ("medicamento", "sust.", "Sustancia o preparado farmaceutico empleado para prevenir, aliviar o curar dolencias.", "farmaco, medicina"),
        ("fecha", "sust.", "Indicacion del dia, mes y ano en que ocurre o se expide un documento.", "data, jornada"),
        ("numero", "sust.", "Signo o cifra que expresa una cantidad, orden o codigo identificador.", "cifra, codigo, folio"),
        ("ciudad", "sust.", "Nucleo urbano de alta poblacion con organizacion administrativa propia.", "municipio, urbe, localidad"),
        ("empresa", "sust.", "Organizacion dedicada a actividades economicas, industriales, comerciales o de servicios.", "compania, sociedad, negocio"),
        ("cliente", "sust.", "Persona u organizacion que adquiere bienes o contrata servicios.", "usuario, comprador, consumidor"),
        ("servicio", "sust.", "Prestacion intangible realizada para satisfacer una necesidad tecnica, comercial o personal.", "asistencia, prestacion"),
        ("producto", "sust.", "Bien tangible o resultado elaborado para su comercializacion o uso.", "articulo, mercancia"),
        ("pago", "sust.", "Entrega de dinero o transferencia realizada para saldar una obligacion economica.", "abono, cancelacion, desembolso"),
        ("valor", "sust.", "Cuantia economica, utilidad o importancia asignada a un bien o concepto.", "importe, monto, precio"),
        ("cuenta", "sust.", "Registro ordenado de movimientos economicos o credenciales de acceso de un usuario.", "saldo, estado, perfil"),
        ("persona", "sust.", "Individuo de la especie humana sujeto de derechos y obligaciones.", "individuo, ciudadano"),
        ("nombre", "sust.", "Palabra o designacion con la que se identifica a una persona, cosa o entidad.", "denominacion, titulo"),
        ("apellido", "sust.", "Nombre de familia que identifica el linaje de una persona junto al nombre propio.", "patronimico, filiacion"),
        ("trabajo", "sust.", "Actividad fisica o intelectual realizada con un proposito productivo o profesional.", "labor, empleo, ocupacion"),
        ("proyecto", "sust.", "Plan estructurado con objetivos, recursos, plazos y entregables definidos.", "iniciativa, plan, programa"),
        ("informe", "sust.", "Documento expositivo que presenta hechos, resultados o conclusiones de una actividad.", "reporte, memoria, dictamen"),
        ("reporte", "sust.", "Comunicacion estructurada sobre hallazgos, metricas o incidencias observadas.", "informe, parte"),
        ("estudio", "sust.", "Investigacion o analisis detallado sobre una materia o caso especifico.", "analisis, investigacion"),
        ("salud", "sust.", "Estado de completo bienestar fisico, mental y funcional de un organismo.", "bienestar, sanidad"),
        ("sistema", "sust.", "Conjunto ordenado de elementos interrelacionados que operan con un fin comun.", "estructura, plataforma"),
        ("proceso", "sust.", "Secuencia organizada de pasos o transformaciones para lograr un resultado.", "procedimiento, flujo"),
        ("resultado", "sust.", "Efecto, producto o hallazgo obtenido tras ejecutar una accion o calculo.", "desenlace, salida"),
        ("dato", "sust.", "Representacion simbolica minima de un hecho, medida o atributo.", "registro, cifra, elemento"),
        ("informacion", "sust.", "Conjunto organizado de datos procesados que aportan significado y contexto.", "conocimiento, contenido"),
        ("ley", "sust.", "Norma juridica obligatoria dictada por autoridad competente.", "legislacion, estatuto, precepto"),
        ("norma", "sust.", "Regla o estandar tecnico o juridico que orienta una conducta o procedimiento.", "regla, estandar, directriz"),
        ("articulo", "sust.", "Cada una de las divisiones numeradas de una ley, reglamento o texto.", "clausula, apartado"),
        ("solicitud", "sust.", "Peticion formal presentada para obtener un tramite, informacion o servicio.", "requerimiento, peticion"),
        ("comunicacion", "sust.", "Transmision e intercambio efectivo de mensajes o notificaciones entre partes.", "aviso, misiva, contacto"),
        ("mensaje", "sust.", "Contenido escrito o hablado dirigido a uno o varios destinatarios.", "recado, comunicado"),
        ("certificado", "sust.", "Documento oficial o digital que da fe de un hecho, identidad o cualificacion.", "constancia, diploma, acreditacion"),
        ("registro", "sust.", "Inscripcion oficial o asiento ordenado de datos y eventos en una base o libro.", "asiento, matricula, inscripcion"),
        ("control", "sust.", "Comprobacion, seguimiento o regulacion de un proceso para asegurar cumplimiento.", "revision, supervision, chequeo"),
        ("calidad", "sust.", "Grado en que un conjunto de caracteristicas cumple requisitos tecnicos o de excelencia.", "nivel, excelencia"),
        ("version", "sust.", "Edicion especifica de un documento o software identificada con numero o fecha.", "revision, edicion"),
        ("usuario", "sust.", "Persona que opera una aplicacion, servicio o sistema informatico.", "operador, lector"),
        ("clave", "sust.", "Codigo secreto que autoriza el acceso o descifrado de informacion.", "contrasena, llave"),
        ("acceso", "sust.", "Posibilidad o autorizacion para entrar, consultar o usar un recurso.", "ingreso, entrada"),
        ("tiempo", "sust.", "Magnitud fisica que mide la duracion o separacion de acontecimientos.", "plazo, lapso, duracion"),
        ("costo", "sust.", "Valor monetario de los recursos consumidos para producir un bien o servicio.", "gasto, importe"),
        ("total", "sust.", "Suma completa y definitiva de las cantidades o partidas consideradas.", "sumatoria, monto final"),
        ("descuento", "sust.", "Rebaja o deduccion aplicada sobre el precio o importe original.", "rebaja, bonificacion"),
        ("saldo", "sust.", "Diferencia neta entre ingresos y egresos o monto pendiente en una cuenta.", "remanente, balance"),
        ("banco", "sust.", "Entidad financiera autorizada para captar depositos, otorgar creditos y gestionar pagos.", "entidad financiera, caja"),
        ("transferencia", "sust.", "Envio electronico de fondos o datos de una cuenta u origen hacia un destino.", "giro, remesa, traspaso"),
        ("recibo", "sust.", "Comprobante escrito que acredita la entrega de dinero u objetos.", "constancia, comprobante"),
        ("garantia", "sust.", "Compromiso que respalda el funcionamiento, calidad o cumplimiento de una obligacion.", "respaldo, fianza, aval"),
        ("poliza", "sust.", "Documento formal en que constan las condiciones de un contrato de seguro.", "cobertura, contrato de seguro"),
        ("riesgo", "sust.", "Probabilidad de ocurrencia de un evento adverso y su impacto potencial.", "contingencia, peligro"),
        ("objetivo", "sust.", "Meta o resultado concreto que se pretende alcanzar mediante acciones planificadas.", "proposito, finalidad, meta"),
        ("estrategia", "sust.", "Conjunto coordinado de decisiones de largo alcance para cumplir objetivos.", "plan maestro, tactica"),
        ("indicador", "sust.", "Medida cuantitativa o cualitativa empleada para evaluar desempeno o avance.", "metrica, indice, KPI"),
        ("meta", "sust.", "Nivel cuantificado y verificable que se espera lograr en un periodo.", "hito, objetivo"),
        ("equipo", "sust.", "Grupo de personas o conjunto de herramientas organizado para una tarea.", "grupo, dotacion"),
        ("organizacion", "sust.", "Estructura social o institucional coordinada para fines comunes.", "entidad, institucion"),
        ("derecho", "sust.", "Facultad legitima reconocida por la ley o conjunto de normas juridicas.", "prerrogativa, facultad"),
        ("obligacion", "sust.", "Deber juridico o compromiso exigible de dar, hacer o abstenerse.", "deber, compromiso"),
        ("acuerdo", "sust.", "Resolucion adoptada de comun voluntad entre dos o mas partes.", "convenio, consenso"),
        ("vigencia", "sust.", "Periodo durante el cual una norma, contrato o documento surte efectos.", "validez, duracion"),
        ("plazo", "sust.", "Termino o intervalo temporal fijado para cumplir un acto u obligacion.", "vencimiento, termino"),
        ("sancion", "sust.", "Penalidad o medida correctiva aplicada por incumplir una norma.", "multa, penalidad"),
        ("auditoria", "sust.", "Examen independiente y sistematico de registros, cuentas o procesos.", "inspeccion, fiscalizacion"),
        ("evidencia", "sust.", "Prueba clara y verificable que sustenta un hecho o afirmacion.", "prueba, soporte, constancia"),
        ("prueba", "sust.", "Elemento documental, ensayo o verificacion destinada a demostrar un hecho o fallo.", "ensayo, test, evidencia"),
        ("ejemplo", "sust.", "Caso o modelo ilustrativo que facilita comprender una regla o concepto.", "muestra, ilustracion"),
        ("capitulo", "sust.", "Division principal en que se estructura un libro, informe o norma.", "apartado, tomo"),
        ("seccion", "sust.", "Subdivision tematica dentro de una pagina, informe o ventana.", "bloque, modulo"),
        ("parrafo", "sust.", "Unidad de texto compuesta por una o varias oraciones sobre una idea.", "apartado, bloque de texto"),
        ("tabla", "sust.", "Disposicion ordenada de datos en filas y columnas para consulta rapida.", "cuadro, matriz"),
        ("figura", "sust.", "Ilustracion, esquema o diagrama incorporado dentro de un texto.", "grafico, imagen"),
        ("grafico", "sust.", "Representacion visual de datos numericos o relaciones mediante trazos.", "diagrama, esquema"),
        ("imagen", "sust.", "Representacion visual fotografica, escaneada o vectorial de un objeto o escena.", "figura, lamina"),
        ("enlace", "sust.", "Referencia interactiva que conecta con otra pagina, seccion o recurso.", "vinculo, hipervinculo"),
        ("codigo", "sust.", "Sistema de simbolos, instrucciones de software o identificador estructurado.", "clave, script, identificador"),
        ("formato", "sust.", "Estructura o especificacion tecnica de presentacion y codificacion de un archivo.", "plantilla, diseno, extension"),
        ("memoria", "sust.", "Componente donde se almacenan datos o instrucciones temporal o permanentemente.", "RAM, almacenamiento"),
        ("velocidad", "sust.", "Rapidez con que se ejecuta una operacion, lectura o transmision.", "tasa, ritmo"),
        ("pantalla", "sust.", "Superficie visual del dispositivo donde se despliegan documentos e interfaces.", "monitor, visor"),
        ("teclado", "sust.", "Dispositivo de entrada compuesto por teclas para escribir y activar atajos.", "periferico de entrada"),
        ("impresora", "sust.", "Periferico destinado a reproducir textos e imagenes digitales sobre papel.", "equipo de impresion"),
        ("red", "sust.", "Conjunto de equipos interconectados para compartir informacion o recursos.", "conexion, malla"),
        ("servidor", "sust.", "Equipo o proceso que provee servicios y datos a otros programas clientes.", "host, nodo"),
        ("nube", "sust.", "Infraestructura remota de almacenamiento y computo accesible por internet.", "almacenamiento remoto"),
        ("copia", "sust.", "Duplicado fiel de un archivo o documento para respaldo o distribucion.", "duplicado, respaldo"),
        ("respaldo", "sust.", "Copia de seguridad conservada para recuperar datos ante fallos o borrados.", "backup, salvaguarda"),
        ("nit", "sust.", "Numero de Identificacion Tributaria asignado en Colombia a contribuyentes y empresas.", "identificacion fiscal, RUT"),
        ("folio", "sust.", "Hoja numerada de un expediente, libro o documento oficial.", "pagina, hoja"),
        ("hoja", "sust.", "Cada lamina fisica o digital que contiene contenido impreso o escrito.", "pagina, folio"),
        ("ruta", "sust.", "Cadena jerarquica de carpetas que indica la ubicacion exacta de un archivo.", "directorio, path"),
        ("carpeta", "sust.", "Contenedor logico del sistema de archivos para organizar documentos.", "directorio, folder"),
        ("texto", "sust.", "Secuencia coherente de caracteres y palabras que transmite un mensaje.", "escrito, redaccion"),
        ("titulo", "sust.", "Encabezado principal que identifica la materia de una obra o documento.", "rotulo, epigrafe"),
        ("contenido", "sust.", "Materia o conjunto de datos e ideas incluidos dentro de un documento.", "materia, texto"),
        ("observacion", "sust.", "Anotacion descriptiva o comentario realizado tras examinar un hecho o caso.", "nota, reparo, hallazgo"),
        ("recomendacion", "sust.", "Sugerencia fundamentada de accion a seguir tras un analisis.", "consejo, pauta"),
        ("conclusion", "sust.", "Resolucion o sintesis final derivada del analisis previo.", "cierre, dictamen"),
        ("acta", "sust.", "Relacion escrita oficial de lo tratado y acordado en una reunion o diligencia.", "minuta, registro"),
        ("comprobante", "sust.", "Documento que sirve de prueba de una operacion, pago o entrega.", "recibo, soporte"),
        ("salario", "sust.", "Remuneracion periodica recibida por el trabajador a cambio de sus servicios.", "sueldo, jornal"),
        ("ingreso", "sust.", "Entrada economica percibida o admision formal en un establecimiento.", "entrada, renta, admision"),
        ("egreso", "sust.", "Salida de dinero por pago o gasto, o salida hospitalaria de un paciente.", "gasto, salida"),
        ("presupuesto", "sust.", "Calculo anticipado de ingresos y gastos para un proyecto o periodo.", "estimacion, cotizacion"),
        ("capital", "sust.", "Patrimonio financiero invertido o suma principal sobre la cual corren intereses.", "fondo, patrimonio"),
        ("interes", "sust.", "Rendimiento o costo del capital pactado como porcentaje en el tiempo.", "redito, tasa"),
        ("iva", "sust.", "Impuesto sobre el Valor Agregado aplicado al consumo de bienes y servicios.", "tributo al consumo"),
        ("vencimiento", "sust.", "Fecha limite en que expira un plazo, factura o producto.", "caducidad, termino"),
        ("entrega", "sust.", "Acto de poner un bien, documento o resultado a disposicion del destinatario.", "remision, suministro"),
        ("inventario", "sust.", "Lista ordenada y valorada de bienes o existencias disponibles.", "catalogo, existencias"),
        ("precio", "sust.", "Cantidad de dinero fijada para adquirir un bien o servicio.", "valor, tarifa"),
        ("cotizacion", "sust.", "Oferta formal con precios y condiciones antes de confirmar una compra.", "presupuesto, oferta"),
        ("proveedor", "sust.", "Persona o empresa que suministra bienes o servicios a otra entidad.", "suministrador, abastecedor"),
        ("representante", "sust.", "Persona facultada para actuar en nombre y por cuenta de otra o de una entidad.", "apoderado, mandatario"),
        ("entidad", "sust.", "Organismo, institucion, compania o persona juridica reconocida.", "institucion, organismo"),
        ("universidad", "sust.", "Institucion de educacion superior dedicada a la docencia e investigacion.", "academia, alma mater"),
        ("edad", "sust.", "Tiempo transcurrido desde el nacimiento de un individuo o mascota.", "anos de vida"),
        ("peso", "sust.", "Medida de la masa corporal de un individuo u objeto, habitualmente en gramos o kilogramos.", "masa"),
        ("especie", "sust.", "Categoria biologica basica de clasificacion de los seres vivos (p. ej. canina o felina).", "clase, taxonomia"),
        ("raza", "sust.", "Variedad diferenciada dentro de una especie animal por rasgos geneticos.", "linaje, variedad"),
        ("sintoma", "sust.", "Manifestacion percibida o referida que sugiere una alteracion de la salud.", "molestia, indicio"),
        ("dosis", "sust.", "Cantidad exacta de medicamento administrada en cada toma.", "posologia, porcion"),
        ("receta", "sust.", "Indicacion medica escrita de medicamentos, dosis y duracion del tratamiento.", "formula, prescripcion"),
        ("consulta", "sust.", "Atencion profesional, busqueda de informacion o interrogacion sobre un asunto.", "visita, busqueda"),
        ("vacuna", "sust.", "Preparado biologico que estimula la inmunidad frente a una enfermedad.", "inmunizacion"),
        ("cirugia", "sust.", "Procedimiento medico manual e instrumental para corregir o tratar afecciones.", "intervencion, operacion"),
        ("laboratorio", "sust.", "Recinto equipado para realizar analisis clinicos, ensayos o experimentos.", "centro de analisis"),
        ("muestra", "sust.", "Porcion representativa tomada de un material u organismo para su analisis.", "especimen, ejemplo"),
        ("sangre", "sust.", "Tejido liquido vital que circula por vasos sanguineos transportando oxigeno y nutrientes.", "fluido sanguineo"),
        ("evolucion", "sust.", "Desarrollo gradual de un cuadro clinico, proceso o sistema a lo largo del tiempo.", "progreso, curso"),
        ("folio", "sust.", "Hoja numerada de un expediente o libro oficial.", "pagina, lamina"),
        ("ruta", "sust.", "Ubicacion jerarquica de carpetas y nombre de archivo en disco.", "directorio, camino"),
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
        ("phone", "noun", "Communication device or assigned dialing number for voice and messaging.", "telephone, line"),
        ("address", "noun", "Physical location or electronic destination assigned to a person or organization.", "location, residence"),
        ("owner", "noun", "Legal holder or person possessing a property, record, or pet.", "proprietor, holder"),
        ("identification", "noun", "Official credential or unique code verifying who a person or entity is.", "ID, credential"),
        ("email", "noun", "Digital messaging address and protocol for sending electronic mail.", "mail, inbox"),
        ("clinical", "adj.", "Relating to direct medical observation, diagnosis, and care of a patient.", "medical, therapeutic"),
        ("diagnosis", "noun", "Identification of the nature and cause of a condition from signs and tests.", "assessment, finding"),
        ("treatment", "noun", "Medical care or therapy applied to relieve or cure a condition.", "therapy, regimen"),
        ("prescription", "noun", "Written clinical order specifying medication, dosage, and administration.", "formula, Rx"),
        ("patient", "noun", "Individual or animal receiving clinical evaluation or medical care.", "subject, case"),
        ("date", "noun", "Specified calendar day, month, and year of an event or issuance.", "day, timestamp"),
        ("number", "noun", "Numerical value or alphanumeric code identifying an item or quantity.", "figure, count, code"),
        ("city", "noun", "Large municipality or urban settlement with local administration.", "town, municipality"),
        ("company", "noun", "Commercial organization engaged in business or professional services.", "firm, enterprise"),
        ("client", "noun", "Person or organization receiving professional services or goods.", "customer, patron"),
        ("payment", "noun", "Transfer of funds to settle an invoice, balance, or obligation.", "remittance, settlement"),
        ("total", "noun", "Final sum obtained by adding all listed items, fees, and taxes.", "sum, aggregate"),
        ("report", "noun", "Formal account presenting findings, status, or metrics from analysis.", "brief, dossier"),
        ("project", "noun", "Planned undertaking with defined scope, milestones, and deliverables.", "initiative, program"),
        ("evidence", "noun", "Verified material or record establishing a factual conclusion.", "proof, exhibit"),
        ("backup", "noun", "Duplicate copy retained safely to restore lost or corrupted data.", "archive, copy"),
        ("export", "verb", "Save data or conversations into an external portable file format.", "output, extract"),
        ("history", "noun", "Chronological log of past events, clinical notes, or messages.", "log, timeline"),
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

    def _seed_entries(self, lang: str, rows: list[tuple[str, str, str, str]], *, replace_existing: bool = True) -> int:
        db_path = self._db_path(lang)
        conn = self._init_db(db_path)
        sql = (
            "INSERT OR REPLACE INTO entries (norm, word, pos, definition, synonyms) VALUES (?, ?, ?, ?, ?)"
            if replace_existing
            else "INSERT OR IGNORE INTO entries (norm, word, pos, definition, synonyms) VALUES (?, ?, ?, ?, ?)"
        )
        try:
            with conn:
                conn.executemany(
                    sql,
                    [(normalize_term(w), w.strip(), pos.strip(), dfn.strip(), syn.strip()) for w, pos, dfn, syn in rows],
                )
            return len(rows)
        finally:
            conn.close()

    def _ensure_bundled_seeded(self) -> None:
        for code, rows in _BUNDLED_LEXICONS.items():
            db = self._db_path(code)
            if not db.exists() or db.stat().st_size == 0:
                self._seed_entries(code, rows, replace_existing=True)
            elif self.entry_count(code) < len(rows):
                self._seed_entries(code, rows, replace_existing=False)

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
        cleaned = (word or "").strip(" \t\r\n.,;:!?¡¿()[]{}\"'«»“”—–-•*/")
        norm = normalize_term(cleaned)
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
            # Try simple Spanish/English plural or gender inflections if exact hit is absent
            if not rows and len(norm) > 4 and norm.endswith("s"):
                candidates = []
                if norm.endswith("es") and len(norm) > 5:
                    candidates.append(norm[:-2])
                candidates.append(norm[:-1])
                for cand in candidates:
                    rows = conn.execute(
                        "SELECT word, pos, definition, synonyms FROM entries WHERE norm = ? ORDER BY pos",
                        (cand,),
                    ).fetchall()
                    if rows:
                        break
            if not rows and len(norm) > 4 and norm.endswith(("o", "a")):
                alt = norm[:-1] + ("a" if norm.endswith("o") else "o")
                rows = conn.execute(
                    "SELECT word, pos, definition, synonyms FROM entries WHERE norm = ? ORDER BY pos",
                    (alt,),
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
