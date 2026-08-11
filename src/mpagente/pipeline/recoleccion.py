"""Recolección autónoma del histórico de adjudicaciones vía la API OCDS.

No requiere ticket ni descarga manual: el agente recorre mes a mes, baja las
fichas de adjudicación en paralelo y las guarda. Es idempotente y reanudable —
si se interrumpe, la siguiente corrida retoma donde quedó.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Sequence

from ..clients.ocds import ClienteOCDS, ErrorOCDS, Modalidad
from ..ocds_parser import (
    ADJUDICADO,
    CERRADO_SIN_ADJUDICAR,
    PENDIENTE,
    clasificar_proceso,
    parsear_adjudicaciones,
)
from ..storage import Almacen

log = logging.getLogger(__name__)

# Cada cuántas fichas se vuelca el buffer a SQLite. Acota la memoria sin
# convertir la escritura en el cuello de botella.
_TAMANO_LOTE = 400


@dataclass
class ResumenRecoleccion:
    meses_procesados: int = 0
    meses_omitidos: int = 0
    procesos_vistos: int = 0
    adjudicaciones: int = 0
    adjudicados: int = 0
    cerrados_sin_adjudicar: int = 0
    pendientes: int = 0
    errores: list[str] = field(default_factory=list)


def meses_hacia_atras(cantidad: int, *, hasta: date | None = None) -> list[tuple[int, int]]:
    """Los últimos `cantidad` meses, del más reciente al más antiguo.

    Por defecto termina en el último mes cerrado: el mes en curso todavía se
    está publicando y daría cifras incompletas.
    """
    referencia = hasta or date.today().replace(day=1)
    anio, mes = referencia.year, referencia.month

    periodos: list[tuple[int, int]] = []
    for _ in range(cantidad):
        mes -= 1
        if mes == 0:
            anio, mes = anio - 1, 12
        periodos.append((anio, mes))
    return periodos


def recolectar(
    cliente: ClienteOCDS,
    almacen: Almacen,
    *,
    meses: int = 12,
    modalidades: Sequence[Modalidad] = ("licitacion",),
    hilos: int = 8,
    hasta: date | None = None,
    refrescar: bool = False,
    limite_por_mes: int | None = None,
    al_avanzar: Callable[[str, int, int, int, int], None] | None = None,
) -> ResumenRecoleccion:
    """Descarga el histórico de adjudicaciones y lo persiste.

    `al_avanzar` recibe (modalidad, año, mes, procesados, total) para reportar
    progreso sin que este módulo dependa de la capa de presentación.
    """
    resumen = ResumenRecoleccion()
    periodos = meses_hacia_atras(meses, hasta=hasta)

    for modalidad in modalidades:
        resueltos = almacen.procesos_resueltos(modalidad)

        for anio, mes in periodos:
            # Un mes solo se omite cuando ya no queda nada por adjudicar; los
            # procesos recientes se adjudican semanas después de publicarse.
            if not refrescar and almacen.mes_sin_pendientes(modalidad, anio, mes):
                resumen.meses_omitidos += 1
                continue

            try:
                codigos = list(cliente.codigos_del_mes(anio, mes, modalidad))
            except ErrorOCDS as exc:
                log.warning("No se pudo listar %s %d-%02d: %s", modalidad, anio, mes, exc)
                resumen.errores.append(f"{modalidad} {anio}-{mes:02d}: {exc}")
                continue

            por_consultar = [c for c in codigos if refrescar or c not in resueltos]
            if limite_por_mes:
                por_consultar = por_consultar[:limite_por_mes]

            total = len(por_consultar)
            if al_avanzar:
                al_avanzar(modalidad, anio, mes, 0, total)

            adjudicaciones_mes, estados = _procesar_mes(
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

            resueltos.update(
                codigo for codigo, estado in estados.items() if estado != PENDIENTE
            )
            pendientes_mes = sum(1 for e in estados.values() if e == PENDIENTE)

            resumen.procesos_vistos += total
            resumen.adjudicaciones += adjudicaciones_mes
            resumen.meses_procesados += 1
            almacen.marcar_mes_completado(
                modalidad,
                anio,
                mes,
                procesos=len(codigos),
                adjudicaciones=adjudicaciones_mes,
                pendientes=pendientes_mes,
            )

    return resumen


def _procesar_mes(
    cliente: ClienteOCDS,
    almacen: Almacen,
    codigos: Sequence[str],
    *,
    modalidad: str,
    hilos: int,
    resumen: ResumenRecoleccion,
    al_avanzar: Callable[[int, int], None] | None,
) -> tuple[int, dict[str, str]]:
    """Descarga en paralelo y escribe desde el hilo principal.

    SQLite no acepta escrituras concurrentes desde varios hilos, así que la red
    se paraleliza pero la persistencia queda serializada acá.

    Devuelve cuántas adjudicaciones se guardaron y el estado de cada proceso.
    """
    if not codigos:
        return 0, {}

    guardadas = 0
    estados: dict[str, str] = {}
    buffer_adj: list = []
    buffer_estados: list[tuple[str, str]] = []
    hechos = 0
    total = len(codigos)

    def volcar() -> None:
        nonlocal guardadas, buffer_adj, buffer_estados
        if buffer_adj:
            guardadas += almacen.guardar_adjudicaciones(buffer_adj)
        if buffer_estados:
            almacen.marcar_procesos_vistos(buffer_estados, modalidad)
        buffer_adj, buffer_estados = [], []

    with ThreadPoolExecutor(max_workers=hilos) as pool:
        futuros = {pool.submit(cliente.adjudicacion, c): c for c in codigos}

        for futuro in as_completed(futuros):
            codigo = futuros[futuro]
            hechos += 1

            try:
                payload = futuro.result()
            except Exception as exc:  # noqa: BLE001 — una ficha rota no corta el mes
                log.debug("Falló la ficha %s: %s", codigo, exc)
                resumen.errores.append(f"{codigo}: {exc}")
                payload = None

            if payload:
                adjudicaciones = parsear_adjudicaciones(payload, modalidad)
                estado = clasificar_proceso(adjudicaciones, payload)
                # Solo se persiste lo adjudicado: un proceso abierto devuelve un
                # premio sin proveedor ni monto, que ensuciaría las estadísticas.
                if estado == ADJUDICADO:
                    buffer_adj.extend(a for a in adjudicaciones if a.monto and a.proveedor)
            else:
                # Sin ficha no se puede concluir nada; se reintentará más adelante.
                estado = PENDIENTE

            estados[codigo] = estado
            buffer_estados.append((codigo, estado))

            if estado == ADJUDICADO:
                resumen.adjudicados += 1
            elif estado == CERRADO_SIN_ADJUDICAR:
                resumen.cerrados_sin_adjudicar += 1
            else:
                resumen.pendientes += 1

            if len(buffer_adj) >= _TAMANO_LOTE or len(buffer_estados) >= _TAMANO_LOTE:
                volcar()
            if al_avanzar and hechos % 25 == 0:
                al_avanzar(hechos, total)

    volcar()
    if al_avanzar:
        al_avanzar(total, total)
    return guardadas, estados
