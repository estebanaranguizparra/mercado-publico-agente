"""Contrato común entre la fuente real y la simulada."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ..models import Licitacion


class FuenteLicitaciones(Protocol):
    """Todo lo que el pipeline necesita saber sobre de dónde vienen los datos."""

    def listar(
        self,
        *,
        fecha: date | None = None,
        estado: str | None = None,
    ) -> list[Licitacion]:
        """Listado resumido: trae código, nombre y fecha de cierre, sin items."""
        ...

    def detalle(self, codigo: str) -> Licitacion | None:
        """Ficha completa de una licitación, con items y datos del comprador."""
        ...
