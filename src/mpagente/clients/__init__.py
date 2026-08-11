"""Clientes de datos: API real de Mercado Público y fuente simulada."""

from .base import FuenteLicitaciones
from .mercadopublico import ClienteMercadoPublico, ErrorMercadoPublico
from .mock import ClienteMock

__all__ = [
    "ClienteMercadoPublico",
    "ClienteMock",
    "ErrorMercadoPublico",
    "FuenteLicitaciones",
]
