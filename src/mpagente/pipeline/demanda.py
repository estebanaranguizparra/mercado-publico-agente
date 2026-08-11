"""Qué está pidiendo el Estado, a partir de la ficha `tender` de OCDS.

Es la contraparte de `recoleccion.py`, que mira lo ya adjudicado. La diferencia
importa más de lo que parece: en un mes recién cerrado apenas ~12% de los
procesos está adjudicado, pero el 100% tiene su llamado publicado con items y
código UNSPSC. Mirar el llamado en vez del premio multiplica la cobertura de lo
reciente y es la única forma, sin ticket, de ver demanda todavía abierta.

Lo que el llamado no trae es precio: nadie ha ofertado aún. Por eso la
agregación cruza cada familia con lo que el histórico adjudicado pagó por ella,
cuando ese dato existe.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median
from typing import Callable, Sequence

from ..clients.ocds import ClienteOCDS, ErrorOCDS, Modalidad
from ..ocds_parser import Solicitud, parsear_solicitud
from ..storage import Almacen
from .nichos import es_bien, nombre_segmento

log = logging.getLogger(__name__)

_TAMANO_LOTE = 400


@dataclass
class ResumenDemanda:
    meses_procesados: int = 0
    procesos_vistos: int = 0
    solicitudes: int = 0
    vigentes: int = 0
    sin_items: int = 0
    errores: list[str] = field(default_factory=list)


def meses_recientes(cantidad: int, *, hasta: date | None = None) -> list[tuple[int, int]]:
    """Los últimos `cantidad` meses **incluyendo el mes en curso**.

    A diferencia de la recolección de adjudicaciones, acá el mes corriente sí
    interesa: es donde estarían los llamados recién publicados. La API suele no
    tenerlo todavía, y ese caso se maneja como un mes más que falla sin cortar.
    """
    referencia = hasta or date.today()
    anio, mes = referencia.year, referencia.month

    periodos: list[tuple[int, int]] = []
    for _ in range(cantidad):
        periodos.append((anio, mes))
        mes -= 1
        if mes == 0:
            anio, mes = anio - 1, 12
    return periodos


def recolectar_demanda(
    cliente: ClienteOCDS,
    almacen: Almacen,
    *,
    meses: int = 3,
    modalidades: Sequence[Modalidad] = ("licitacion",),
    hilos: int = 8,
    hasta: date | None = None,
    refrescar: bool = False,
    limite_por_mes: int | None = None,
    al_avanzar: Callable[[str, int, int, int, int], None] | None = None,
) -> ResumenDemanda:
    """Descarga los llamados publicados y los persiste."""
    resumen = ResumenDemanda()
    periodos = meses_recientes(meses, hasta=hasta)

    for modalidad in modalidades:
        cerradas = set() if refrescar else almacen.solicitudes_cerradas(modalidad)

        for anio, mes in periodos:
            try:
                codigos = list(cliente.codigos_del_mes(anio, mes, modalidad))
            except ErrorOCDS as exc:
                # El mes en curso no está publicado hasta que termina; no es un
                # fallo real, solo el límite de lo que la API expone.
                log.info("Sin listado para %s %d-%02d: %s", modalidad, anio, mes, exc)
                resumen.errores.append(f"{modalidad} {anio}-{mes:02d}: sin listado disponible")
                continue

            por_consultar = [c for c in codigos if c not in cerradas]
            if limite_por_mes:
                por_consultar = por_consultar[:limite_por_mes]

            total = len(por_consultar)
            if al_avanzar:
                al_avanzar(modalidad, anio, mes, 0, total)

            _procesar_mes(
                cliente,
                almacen,
                por_consultar,
                modalidad=modalidad,
                hilos=hilos,
                resumen=resumen,
                al_avanzar=(
                    (lambda hechos, tot: al_avanzar(modalidad, anio, mes, hechos, tot))
                    if al_avanzar
                    else None
                ),
            )

            resumen.procesos_vistos += total
            resumen.meses_procesados += 1

    return resumen


def _procesar_mes(
    cliente: ClienteOCDS,
    almacen: Almacen,
    codigos: Sequence[str],
    *,
    modalidad: str,
    hilos: int,
    resumen: ResumenDemanda,
    al_avanzar: Callable[[int, int], None] | None,
) -> None:
    """Descarga en paralelo; escribe desde el hilo principal.

    SQLite no acepta escrituras concurrentes: la red se paraleliza, la
    persistencia no.
    """
    if not codigos:
        return

    buffer: list[Solicitud] = []
    hechos = 0
    total = len(codigos)
    ahora = datetime.now()

    def volcar() -> None:
        nonlocal buffer
        if buffer:
            almacen.guardar_solicitudes(buffer)
            buffer = []

    with ThreadPoolExecutor(max_workers=hilos) as pool:
        futuros = {pool.submit(cliente.solicitud, c): c for c in codigos}

        for futuro in as_completed(futuros):
            codigo = futuros[futuro]
            hechos += 1

            try:
                payload = futuro.result()
            except Exception as exc:  # noqa: BLE001 — una ficha rota no corta el mes
                log.debug("Falló el llamado %s: %s", codigo, exc)
                resumen.errores.append(f"{codigo}: {exc}")
                payload = None

            if payload:
                solicitud = parsear_solicitud(payload, modalidad)
                if solicitud is not None:
                    # Un llamado sin items no dice qué se pide; se guarda igual
                    # porque el título todavía sirve, pero se cuenta aparte.
                    if not solicitud.items:
                        resumen.sin_items += 1
                    if solicitud.vigente(ahora):
                        resumen.vigentes += 1
                    buffer.append(solicitud)
                    resumen.solicitudes += 1

            if len(buffer) >= _TAMANO_LOTE:
                volcar()
            if al_avanzar and hechos % 25 == 0:
                al_avanzar(hechos, total)

    volcar()
    if al_avanzar:
        al_avanzar(total, total)


# --------------------------------------------------------------------------- #
# Agregación: qué se pide, por categoría
# --------------------------------------------------------------------------- #


@dataclass
class Demanda:
    familia: str
    segmento: str
    etiqueta: str
    es_servicio: bool
    n_solicitudes: int
    n_compradores: int
    n_items: int
    meses_activos: int
    vigentes: int              # llamados de esta familia todavía abiertos
    regiones: int
    # Del cruce con el histórico adjudicado; 0 si esa familia nunca se adjudicó.
    precio_mediano: float
    oferentes_promedio: float
    valor_estimado: float      # cantidad pedida × precio mediano histórico
    puntaje: float = 0.0
    motivos: list[str] = field(default_factory=list)


_SOLICITUDES = """
    SELECT i.familia       AS familia,
           i.descripcion   AS descripcion,
           i.cantidad      AS cantidad,
           s.codigo        AS codigo,
           s.comprador_id  AS comprador_id,
           s.region        AS region,
           s.anio          AS anio,
           s.mes           AS mes,
           s.cierre        AS cierre
    FROM solicitud_items i
    JOIN solicitudes s ON s.codigo = i.codigo
    WHERE i.familia IS NOT NULL
"""

# Referencia de precio y competencia por familia, tomada de lo ya adjudicado.
_REFERENCIA = """
    SELECT i.familia AS familia,
           i.precio_unitario AS precio_unitario,
           a.n_oferentes AS n_oferentes
    FROM adjudicacion_items i
    JOIN adjudicaciones a ON a.award_uid = i.award_uid
    WHERE i.familia IS NOT NULL
      AND i.precio_unitario > 0
      AND a.monto > 1000
"""


def _etiqueta_item(descripcion: str) -> str:
    """Nombre legible del producto o servicio a partir de la descripción OCDS.

    En `tender` la descripción viene jerárquica ("Familia / Clase / Producto") y
    la última rama es la más específica. Pero algunas terminan en un fragmento
    inútil —un número suelto, una sigla de dos letras—, así que se retrocede a
    la rama anterior en vez de quedarse con eso.
    """
    ramas = [r.strip() for r in descripcion.split("/") if r.strip()]
    for rama in reversed(ramas):
        if len(rama) >= 4 and not rama.isdigit():
            return rama[:70]
    return (ramas[-1] if ramas else descripcion.strip())[:70]


def agregar_demanda(
    con: sqlite3.Connection,
    *,
    minimo_solicitudes: int = 3,
    solo_vigentes: bool = False,
    incluir: str = "todo",
) -> list[Demanda]:
    """Agrupa los llamados por familia UNSPSC y ordena por atractivo.

    `incluir` acepta "todo", "bienes" o "servicios". El default deliberado es
    "todo": el Estado gasta más en servicios que en bienes, y descartarlos de
    entrada esconde la mayor parte de lo que efectivamente pide.
    """
    con.row_factory = sqlite3.Row
    ahora = datetime.now().isoformat()

    referencia: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"precios": [], "oferentes": []}
    )
    for fila in con.execute(_REFERENCIA):
        referencia[fila["familia"]]["precios"].append(float(fila["precio_unitario"]))
        if fila["n_oferentes"]:
            referencia[fila["familia"]]["oferentes"].append(float(fila["n_oferentes"]))

    por_familia: dict[str, dict] = defaultdict(
        lambda: {
            "codigos": set(),
            "compradores": set(),
            "regiones": set(),
            "periodos": set(),
            "vigentes": set(),
            "descripciones": defaultdict(int),
            "cantidades": [],
            "n_items": 0,
        }
    )

    for fila in con.execute(_SOLICITUDES):
        familia = fila["familia"]
        bien = es_bien(familia)
        if incluir == "bienes" and not bien:
            continue
        if incluir == "servicios" and bien:
            continue

        abierta = bool(fila["cierre"] and fila["cierre"] > ahora)
        if solo_vigentes and not abierta:
            continue

        datos = por_familia[familia]
        datos["codigos"].add(fila["codigo"])
        datos["n_items"] += 1
        if abierta:
            datos["vigentes"].add(fila["codigo"])
        if fila["comprador_id"]:
            datos["compradores"].add(fila["comprador_id"])
        if fila["region"]:
            datos["regiones"].add(fila["region"])
        if fila["anio"]:
            datos["periodos"].add((fila["anio"], fila["mes"]))
        if fila["descripcion"]:
            datos["descripciones"][_etiqueta_item(fila["descripcion"])] += 1
        if fila["cantidad"]:
            datos["cantidades"].append(float(fila["cantidad"]))

    demandas: list[Demanda] = []
    for familia, datos in por_familia.items():
        n_solicitudes = len(datos["codigos"])
        if n_solicitudes < minimo_solicitudes:
            continue

        precios = referencia.get(familia, {}).get("precios") or []
        oferentes = referencia.get(familia, {}).get("oferentes") or []
        precio_mediano = median(precios) if precios else 0.0
        cantidad_total = sum(datos["cantidades"])

        etiqueta = (
            max(datos["descripciones"].items(), key=lambda kv: kv[1])[0]
            if datos["descripciones"]
            else familia
        )

        demandas.append(
            Demanda(
                familia=familia,
                segmento=nombre_segmento(familia),
                etiqueta=etiqueta,
                es_servicio=not es_bien(familia),
                n_solicitudes=n_solicitudes,
                n_compradores=len(datos["compradores"]),
                n_items=datos["n_items"],
                meses_activos=len(datos["periodos"]),
                vigentes=len(datos["vigentes"]),
                regiones=len(datos["regiones"]),
                precio_mediano=precio_mediano,
                oferentes_promedio=sum(oferentes) / len(oferentes) if oferentes else 0.0,
                valor_estimado=cantidad_total * precio_mediano,
            )
        )

    if not demandas:
        return []

    max_solicitudes = max(d.n_solicitudes for d in demandas)
    meses_cubiertos = len(
        {(f["anio"], f["mes"]) for f in con.execute("SELECT anio, mes FROM solicitudes")}
    ) or 1

    for demanda in demandas:
        _puntuar(demanda, max_solicitudes, meses_cubiertos)

    demandas.sort(key=lambda d: d.puntaje, reverse=True)
    return demandas


def _puntuar(demanda: Demanda, max_solicitudes: int, meses_cubiertos: int) -> None:
    """Puntaje 0-100 orientado a "¿vale la pena preparar oferta para esto?".

    A diferencia del puntaje de nichos, acá pesa la frecuencia con que se pide y
    cuántos organismos distintos lo piden, no el monto histórico: un llamado
    recurrente y repartido entre muchos compradores es un flujo, no un evento.
    """
    motivos: list[str] = []
    puntaje = 0.0

    # Frecuencia, relativa a la familia más pedida del período.
    frecuencia = demanda.n_solicitudes / max_solicitudes
    puntaje += 30 * frecuencia
    motivos.append(f"{demanda.n_solicitudes} llamados en el período.")

    # Amplitud: muchos compradores distintos significa que no depende de un
    # solo organismo ni de una relación puntual.
    amplitud = min(demanda.n_compradores / 15, 1.0)
    puntaje += 20 * amplitud
    if demanda.n_compradores >= 10:
        motivos.append(f"Lo piden {demanda.n_compradores} organismos distintos.")
    elif demanda.n_compradores <= 2:
        motivos.append(f"Concentrado en {demanda.n_compradores} comprador(es).")

    # Recurrencia mes a mes.
    recurrencia = demanda.meses_activos / meses_cubiertos
    puntaje += 15 * recurrencia
    if recurrencia >= 0.75:
        motivos.append(f"Presente en {demanda.meses_activos} de {meses_cubiertos} meses.")

    # Competencia observada en lo ya adjudicado de esa familia.
    if demanda.oferentes_promedio > 0:
        holgura = max(0.0, 1 - (demanda.oferentes_promedio - 1) / 8)
        puntaje += 20 * holgura
        if demanda.oferentes_promedio <= 3:
            motivos.append(
                f"Históricamente solo {demanda.oferentes_promedio:.1f} oferentes por proceso."
            )
        elif demanda.oferentes_promedio >= 8:
            motivos.append(f"Muy disputado: {demanda.oferentes_promedio:.1f} oferentes.")
    else:
        motivos.append("Sin historial adjudicado: no hay referencia de precio ni competencia.")

    # Lo que se puede postular ahora mismo inclina la balanza.
    if demanda.vigentes:
        puntaje += min(15, 5 * demanda.vigentes)
        motivos.append(f"{demanda.vigentes} llamado(s) todavía abiertos.")

    demanda.puntaje = round(min(puntaje, 100.0), 1)
    demanda.motivos = motivos
