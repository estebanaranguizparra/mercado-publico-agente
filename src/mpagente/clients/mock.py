"""Fuente simulada, para desarrollar mientras se tramita el ticket de la API.

El archivo de datos usa la misma forma que la API real, con una extensión: la
clave `_dias_al_cierre`. El cliente la convierte en `FechaCierre` relativa a
hoy, para que los datos de ejemplo no caduquen.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import RAIZ_PROYECTO
from ..models import Licitacion

RUTA_MOCK_POR_DEFECTO = RAIZ_PROYECTO / "data" / "mock" / "licitaciones.json"


class ClienteMock:
    def __init__(self, ruta: Path | None = None) -> None:
        self._ruta = ruta or RUTA_MOCK_POR_DEFECTO
        if not self._ruta.exists():
            raise FileNotFoundError(f"No encontré datos simulados en {self._ruta}")
        self._registros: list[dict] = json.loads(self._ruta.read_text(encoding="utf-8"))

    # -- helpers ----------------------------------------------------------- #

    def _materializar(self, registro: dict) -> dict:
        """Traduce `_dias_al_cierre` a fechas absolutas alrededor de hoy."""
        datos = copy.deepcopy(registro)
        dias = datos.pop("_dias_al_cierre", None)
        if dias is None:
            return datos

        cierre = datetime.combine(date.today() + timedelta(days=int(dias)), datetime.min.time())
        cierre = cierre.replace(hour=15, minute=0)
        publicacion = cierre - timedelta(days=14)

        datos["FechaCierre"] = cierre.isoformat(timespec="seconds")
        fechas = datos.setdefault("Fechas", {})
        fechas.setdefault("FechaCierre", cierre.isoformat(timespec="seconds"))
        fechas.setdefault("FechaPublicacion", publicacion.isoformat(timespec="seconds"))
        fechas.setdefault("FechaCreacion", publicacion.isoformat(timespec="seconds"))
        return datos

    # -- interfaz de fuente ------------------------------------------------ #

    def listar(
        self,
        *,
        fecha: date | None = None,
        estado: str | None = None,
    ) -> list[Licitacion]:
        del fecha  # los datos simulados no se segmentan por fecha de publicación

        licitaciones = [Licitacion.model_validate(self._materializar(r)) for r in self._registros]
        if estado and estado.lower() != "activas":
            licitaciones = [
                lic for lic in licitaciones if (lic.estado or "").lower() == estado.lower()
            ]
        return licitaciones

    def detalle(self, codigo: str) -> Licitacion | None:
        for registro in self._registros:
            if registro.get("CodigoExterno") == codigo:
                return Licitacion.model_validate(self._materializar(registro))
        return None

    def cerrar(self) -> None:  # simetría con el cliente HTTP
        return None
