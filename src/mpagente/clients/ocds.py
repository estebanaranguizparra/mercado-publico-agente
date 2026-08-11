"""Cliente de la API OCDS de ChileCompra.

A diferencia de `api.mercadopublico.cl/servicios/v1/publico`, esta API **no
requiere ticket**, lo que permite que el agente se abastezca solo.

Funciona en dos niveles:

1. `listaOCDSAgnoMes*` devuelve, paginado por mes, los identificadores (OCID) de
   los procesos de compra junto a las URL de su ficha y su adjudicación.
2. `award/{codigo}` entrega la adjudicación: quién ganó, por cuánto, qué items
   con su clasificación UNSPSC y precio unitario, y — lo más valioso — quiénes
   más ofertaron (`parties` con rol `tenderer`), que es el indicador directo de
   cuánta competencia tiene esa categoría.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterator, Literal

import httpx

log = logging.getLogger(__name__)

Modalidad = Literal["licitacion", "trato_directo", "convenio_marco"]

_ENDPOINT_LISTA: dict[str, str] = {
    "licitacion": "listaOCDSAgnoMes",
    "trato_directo": "listaOCDSAgnoMesTratoDirecto",
    "convenio_marco": "listaOCDSAgnoMesConvenio",
}

_PREFIJO_OCID = "ocds-70d2nz-"


class ErrorOCDS(RuntimeError):
    """La API OCDS respondió algo inutilizable."""


class _Regulador:
    """Limita la tasa global de peticiones para no golpear una API pública."""

    def __init__(self, req_por_segundo: float) -> None:
        self._intervalo = 1.0 / max(req_por_segundo, 0.1)
        self._proxima = 0.0
        self._lock = threading.Lock()

    def esperar(self) -> None:
        with self._lock:
            ahora = time.monotonic()
            if self._proxima > ahora:
                time.sleep(self._proxima - ahora)
                ahora = time.monotonic()
            self._proxima = ahora + self._intervalo


class ClienteOCDS:
    BASE = "https://api.mercadopublico.cl/APISOCDS/OCDS"

    def __init__(
        self,
        *,
        req_por_segundo: float = 15.0,
        timeout: float = 60.0,
        reintentos: int = 3,
        max_conexiones: int = 16,
    ) -> None:
        self._reintentos = max(reintentos, 1)
        self._regulador = _Regulador(req_por_segundo)
        self._http = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "mercado-publico-agente/0.1", "Accept": "application/json"},
            limits=httpx.Limits(max_connections=max_conexiones),
            follow_redirects=True,
        )

    def cerrar(self) -> None:
        self._http.close()

    def __enter__(self) -> ClienteOCDS:
        return self

    def __exit__(self, *_: object) -> None:
        self.cerrar()

    # -- transporte -------------------------------------------------------- #

    def _get(self, url: str) -> dict | None:
        """Devuelve el JSON, o None si el recurso no existe o quedó ilegible."""
        for intento in range(1, self._reintentos + 1):
            self._regulador.esperar()
            try:
                respuesta = self._http.get(url)
            except httpx.HTTPError as exc:
                log.debug("Red falló en %s (intento %d): %s", url, intento, exc)
                time.sleep(min(2**intento, 10))
                continue

            if respuesta.status_code == 404:
                return None
            if respuesta.status_code == 429 or respuesta.status_code >= 500:
                espera = float(respuesta.headers.get("Retry-After", min(2**intento, 10)))
                log.debug("HTTP %d en %s; espero %.0fs", respuesta.status_code, url, espera)
                time.sleep(espera)
                continue
            if respuesta.status_code >= 400:
                log.warning("HTTP %d en %s", respuesta.status_code, url)
                return None

            try:
                return respuesta.json()
            except ValueError:
                # La API devuelve HTML de error con status 200 en algunos casos.
                log.debug("Respuesta no-JSON en %s", url)
                return None

        log.warning("Se agotaron los reintentos en %s", url)
        return None

    # -- operaciones ------------------------------------------------------- #

    def total_del_mes(self, anio: int, mes: int, modalidad: Modalidad = "licitacion") -> int:
        endpoint = _ENDPOINT_LISTA[modalidad]
        cuerpo = self._get(f"{self.BASE}/{endpoint}/{anio}/{mes:02d}/0/1")
        if not cuerpo:
            raise ErrorOCDS(f"No se pudo consultar {modalidad} {anio}-{mes:02d}")
        return int(cuerpo.get("pagination", {}).get("total", 0))

    def codigos_del_mes(
        self,
        anio: int,
        mes: int,
        modalidad: Modalidad = "licitacion",
        *,
        pagina: int = 500,
    ) -> Iterator[str]:
        """Recorre la lista paginada del mes y va entregando códigos de proceso."""
        endpoint = _ENDPOINT_LISTA[modalidad]
        offset = 0
        total: int | None = None

        while total is None or offset < total:
            url = f"{self.BASE}/{endpoint}/{anio}/{mes:02d}/{offset}/{pagina}"
            cuerpo = self._get(url)
            if not cuerpo:
                log.warning("Página vacía en %s %d-%02d offset %d", modalidad, anio, mes, offset)
                return

            if total is None:
                total = int(cuerpo.get("pagination", {}).get("total", 0))

            registros = cuerpo.get("data") or []
            if not registros:
                return

            for registro in registros:
                ocid = registro.get("ocid") or ""
                if ocid:
                    yield ocid.removeprefix(_PREFIJO_OCID)

            offset += len(registros)

    def adjudicacion(self, codigo: str) -> dict | None:
        """Ficha OCDS de adjudicación. None si el proceso no tiene adjudicación."""
        return self._get(f"{self.BASE}/award/{codigo}")

    def solicitud(self, codigo: str) -> dict | None:
        """Ficha OCDS del llamado: lo que el Estado pide, antes de adjudicar.

        A diferencia de `award`, esta responde con contenido útil desde que el
        proceso se publica: trae los items con su clasificación UNSPSC y el
        plazo de postulación. Es la única vía sin ticket para saber qué se está
        solicitando hoy.
        """
        return self._get(f"{self.BASE}/tender/{codigo}")
