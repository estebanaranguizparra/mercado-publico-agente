"""Cliente HTTP de la API pública de ChileCompra.

Documentación oficial: https://desarrolladores.mercadopublico.cl

Notas de comportamiento aprendidas de la API:

- Devuelve HTTP 200 incluso cuando el ticket es inválido o no hay resultados;
  el error viaja en el cuerpo, así que hay que inspeccionarlo.
- El listado resumido no incluye items ni monto: hay que pedir el detalle
  licitación por licitación, lo que hace del rate limit el cuello de botella.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date

import httpx
from pydantic import ValidationError

from ..models import Licitacion

log = logging.getLogger(__name__)


class ErrorMercadoPublico(RuntimeError):
    """La API respondió algo que no podemos usar."""


class _Limitador:
    """Espaciador de llamadas simple y seguro entre hilos."""

    def __init__(self, req_por_minuto: int) -> None:
        self._intervalo = 60.0 / max(req_por_minuto, 1)
        self._ultima = 0.0
        self._lock = threading.Lock()

    def esperar(self) -> None:
        with self._lock:
            transcurrido = time.monotonic() - self._ultima
            faltante = self._intervalo - transcurrido
            if faltante > 0:
                time.sleep(faltante)
            self._ultima = time.monotonic()


class ClienteMercadoPublico:
    BASE = "https://api.mercadopublico.cl/servicios/v1/publico"

    def __init__(
        self,
        ticket: str,
        *,
        req_por_minuto: int = 20,
        timeout: float = 30.0,
        reintentos: int = 3,
    ) -> None:
        if not ticket:
            raise ErrorMercadoPublico("Se requiere un ticket de Mercado Público.")
        self._ticket = ticket
        self._reintentos = max(reintentos, 1)
        self._limitador = _Limitador(req_por_minuto)
        self._http = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "mercado-publico-agente/0.1", "Accept": "application/json"},
        )

    # -- ciclo de vida ----------------------------------------------------- #

    def cerrar(self) -> None:
        self._http.close()

    def __enter__(self) -> ClienteMercadoPublico:
        return self

    def __exit__(self, *_: object) -> None:
        self.cerrar()

    # -- transporte -------------------------------------------------------- #

    def _get(self, endpoint: str, **params: str) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        params["ticket"] = self._ticket
        url = f"{self.BASE}/{endpoint}"

        ultimo_error: Exception | None = None
        for intento in range(1, self._reintentos + 1):
            self._limitador.esperar()
            try:
                respuesta = self._http.get(url, params=params)
            except httpx.HTTPError as exc:
                ultimo_error = exc
                log.warning("Fallo de red en %s (intento %d): %s", endpoint, intento, exc)
                time.sleep(2**intento)
                continue

            if respuesta.status_code == 429 or respuesta.status_code >= 500:
                ultimo_error = ErrorMercadoPublico(
                    f"HTTP {respuesta.status_code} en {endpoint}"
                )
                espera = float(respuesta.headers.get("Retry-After", 2**intento))
                log.warning(
                    "HTTP %d en %s; reintentando en %.0fs", respuesta.status_code, endpoint, espera
                )
                time.sleep(espera)
                continue

            if respuesta.status_code >= 400:
                raise ErrorMercadoPublico(
                    f"HTTP {respuesta.status_code} en {endpoint}: {respuesta.text[:300]}"
                )

            try:
                cuerpo = respuesta.json()
            except ValueError as exc:
                raise ErrorMercadoPublico(
                    f"Respuesta no-JSON en {endpoint}: {respuesta.text[:300]}"
                ) from exc

            # La API señala errores de ticket o de parámetros dentro del cuerpo.
            if isinstance(cuerpo, dict) and "Codigo" in cuerpo and "Mensaje" in cuerpo:
                raise ErrorMercadoPublico(
                    f"La API rechazó la consulta: {cuerpo.get('Mensaje')} "
                    f"(código {cuerpo.get('Codigo')})"
                )
            if not isinstance(cuerpo, dict):
                raise ErrorMercadoPublico(f"Respuesta inesperada en {endpoint}: {cuerpo!r}")
            return cuerpo

        raise ErrorMercadoPublico(
            f"No se pudo consultar {endpoint} tras {self._reintentos} intentos"
        ) from ultimo_error

    # -- operaciones ------------------------------------------------------- #

    def listar(
        self,
        *,
        fecha: date | None = None,
        estado: str | None = None,
    ) -> list[Licitacion]:
        params: dict[str, str] = {}
        if fecha is not None:
            params["fecha"] = fecha.strftime("%d%m%Y")
        if estado is not None:
            params["estado"] = estado

        cuerpo = self._get("licitaciones.json", **params)
        return _parsear_listado(cuerpo.get("Listado") or [])

    def detalle(self, codigo: str) -> Licitacion | None:
        cuerpo = self._get("licitaciones.json", codigo=codigo)
        listado = _parsear_listado(cuerpo.get("Listado") or [])
        return listado[0] if listado else None


def _parsear_listado(bruto: list[dict]) -> list[Licitacion]:
    """Descarta registros malformados en vez de tumbar la corrida completa."""
    licitaciones: list[Licitacion] = []
    for registro in bruto:
        try:
            licitaciones.append(Licitacion.model_validate(registro))
        except ValidationError as exc:
            log.warning(
                "Se omitió una licitación malformada (%s): %s",
                registro.get("CodigoExterno", "sin código"),
                exc.errors()[:2],
            )
    return licitaciones
