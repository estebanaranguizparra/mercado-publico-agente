"""Prefiltro heurístico, previo al análisis con Claude.

Su única función es no gastar tokens en licitaciones que el perfil descarta de
plano. Es deliberadamente barato y explicable: cada decisión deja registrado su
motivo, para poder auditar por qué algo quedó fuera.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from ..models import Licitacion, Perfil

# Tipos de licitación de ChileCompra, de menor a mayor tamaño.
# L1/LE son compras menores; LP/LR son las de volumen relevante.
_BONO_POR_TIPO = {
    "L1": -10,
    "LE": 0,
    "LP": 10,
    "LR": 12,
    "LQ": 8,
    "LS": 8,
}


def _normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para que 'adquisición' calce con 'adquisicion'."""
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


@dataclass
class ResultadoPrefiltro:
    codigo: str
    aprobado: bool
    puntaje: float
    motivos: list[str] = field(default_factory=list)


def prefiltrar(lic: Licitacion, perfil: Perfil) -> ResultadoPrefiltro:
    """Aprueba o descarta una licitación según reglas duras del perfil."""
    restricciones = perfil.restricciones
    motivos: list[str] = []
    puntaje = 50.0

    texto = _normalizar(lic.texto_busqueda())

    # --- Reglas de descarte -------------------------------------------- #

    dias = lic.dias_al_cierre
    if dias is None:
        motivos.append("Sin fecha de cierre conocida; no se puede planificar la oferta.")
        return ResultadoPrefiltro(lic.codigo_externo, False, 0.0, motivos)

    if dias < restricciones.dias_minimos_al_cierre:
        motivos.append(
            f"Cierra en {dias} día(s), bajo el mínimo de "
            f"{restricciones.dias_minimos_al_cierre} para alcanzar a ofertar."
        )
        return ResultadoPrefiltro(lic.codigo_externo, False, 0.0, motivos)

    excluidas = [p for p in perfil.palabras_clave_excluir if _normalizar(p) in texto]
    if excluidas:
        motivos.append(f"Contiene términos excluidos del perfil: {', '.join(excluidas[:3])}.")
        return ResultadoPrefiltro(lic.codigo_externo, False, 0.0, motivos)

    monto = lic.monto_estimado
    if monto is not None and monto > 0:
        if monto < restricciones.monto_minimo_clp:
            motivos.append(
                f"Monto estimado ${monto:,.0f} bajo el mínimo de "
                f"${restricciones.monto_minimo_clp:,.0f}."
            )
            return ResultadoPrefiltro(lic.codigo_externo, False, 0.0, motivos)
        if monto > restricciones.monto_maximo_clp:
            motivos.append(
                f"Monto estimado ${monto:,.0f} sobre el máximo de "
                f"${restricciones.monto_maximo_clp:,.0f}."
            )
            return ResultadoPrefiltro(lic.codigo_externo, False, 0.0, motivos)

    incluidas = [p for p in perfil.palabras_clave_incluir if _normalizar(p) in texto]
    if not incluidas and restricciones.exigir_coincidencia_palabras_clave:
        motivos.append("No coincide con ninguna palabra clave del perfil.")
        return ResultadoPrefiltro(lic.codigo_externo, False, 0.0, motivos)

    # --- Puntaje de prioridad ------------------------------------------ #
    # A partir de aquí la licitación pasa; el puntaje solo define el orden
    # en que se envían a Claude cuando hay más candidatas que presupuesto.

    if incluidas:
        aporte = min(len(incluidas) * 6, 24)
        puntaje += aporte
        motivos.append(f"Coincide con {len(incluidas)} palabra(s) clave: {', '.join(incluidas[:4])}.")

    if monto is None or monto <= 0:
        puntaje -= 8
        motivos.append("Sin monto estimado publicado; se evalúa con incertidumbre de tamaño.")
    else:
        # El punto dulce está en la mitad alta del rango tolerado.
        tope = restricciones.monto_maximo_clp
        if tope != float("inf") and tope > restricciones.monto_minimo_clp:
            posicion = (monto - restricciones.monto_minimo_clp) / (
                tope - restricciones.monto_minimo_clp
            )
            puntaje += 12 * min(posicion * 1.5, 1.0)
        if monto > restricciones.capital_trabajo_clp > 0:
            puntaje -= 10
            motivos.append(
                f"El monto supera el capital de trabajo declarado "
                f"(${restricciones.capital_trabajo_clp:,.0f}); exige financiamiento."
            )

    bono_tipo = _BONO_POR_TIPO.get((lic.tipo or "").upper(), 0)
    puntaje += bono_tipo

    if perfil.regiones_operacion:
        region = _normalizar(lic.region or "")
        if any(_normalizar(r) in region or region in _normalizar(r) for r in perfil.regiones_operacion):
            puntaje += 8
            motivos.append(f"Región de operación habitual: {lic.region}.")
        else:
            puntaje -= 6
            motivos.append(f"Fuera de las regiones habituales: {lic.region or 'sin región'}.")

    if dias >= 20:
        puntaje += 6
        motivos.append(f"Cierra en {dias} días: hay margen para cotizar e incluso importar.")
    elif dias < 10:
        puntaje -= 6
        motivos.append(f"Cierra en {dias} días: plazo ajustado para armar la oferta.")

    if not lic.es_detalle:
        motivos.append("Solo hay datos resumidos; conviene traer el detalle antes de evaluar.")

    return ResultadoPrefiltro(
        codigo=lic.codigo_externo,
        aprobado=True,
        puntaje=max(0.0, min(100.0, puntaje)),
        motivos=motivos,
    )
