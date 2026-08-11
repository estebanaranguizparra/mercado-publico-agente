"""Ingesta: trae licitaciones, resuelve el detalle y las guarda."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from ..clients.base import FuenteLicitaciones
from ..models import Licitacion
from ..storage import Almacen

log = logging.getLogger(__name__)


@dataclass
class ResumenIngesta:
    listadas: int = 0
    detalles_pedidos: int = 0
    guardadas: int = 0
    omitidas_por_cache: int = 0
    errores: list[str] = field(default_factory=list)


def ingestar(
    fuente: FuenteLicitaciones,
    almacen: Almacen,
    *,
    fecha: date | None = None,
    estado: str | None = "activas",
    limite: int | None = None,
    traer_detalle: bool = True,
    refrescar: bool = False,
    al_avanzar: Callable[[int, int, str], None] | None = None,
) -> ResumenIngesta:
    """Descarga licitaciones y las persiste.

    El listado de la API viene resumido (sin items ni monto), así que el detalle
    se pide licitación por licitación. Ese paso es el que consume el rate limit,
    por lo que se saltan las que ya están en la base salvo que se pida refrescar.
    """
    resumen = ResumenIngesta()

    listado = fuente.listar(fecha=fecha, estado=estado)
    resumen.listadas = len(listado)
    if limite is not None:
        listado = listado[:limite]

    ya_guardadas = almacen.codigos_existentes()
    total = len(listado)

    for indice, lic in enumerate(listado, start=1):
        codigo = lic.codigo_externo
        if al_avanzar:
            al_avanzar(indice, total, codigo)

        if not refrescar and codigo in ya_guardadas:
            resumen.omitidas_por_cache += 1
            continue

        completa: Licitacion = lic
        if traer_detalle and not lic.es_detalle:
            try:
                detalle = fuente.detalle(codigo)
                resumen.detalles_pedidos += 1
                if detalle is not None:
                    completa = detalle
                else:
                    resumen.errores.append(f"{codigo}: la API no devolvió detalle.")
            except Exception as exc:  # noqa: BLE001 — un fallo puntual no debe cortar el lote
                log.warning("No se pudo traer el detalle de %s: %s", codigo, exc)
                resumen.errores.append(f"{codigo}: {exc}")

        almacen.guardar_licitacion(completa)
        resumen.guardadas += 1

    return resumen
