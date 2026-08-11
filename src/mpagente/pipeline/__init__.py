"""Etapas del pipeline: ingesta, prefiltro heurístico y scoring con Claude."""

from .demanda import Demanda, ResumenDemanda, agregar_demanda, meses_recientes, recolectar_demanda
from .filtros import ResultadoPrefiltro, prefiltrar
from .ingesta import ingestar
from .nichos import Nicho, detectar_nichos
from .recoleccion import ResumenRecoleccion, meses_hacia_atras, recolectar
from .scoring import AnalizadorClaude, ErrorAnalisis

__all__ = [
    "AnalizadorClaude",
    "Demanda",
    "ErrorAnalisis",
    "Nicho",
    "ResultadoPrefiltro",
    "ResumenDemanda",
    "ResumenRecoleccion",
    "agregar_demanda",
    "detectar_nichos",
    "ingestar",
    "meses_hacia_atras",
    "meses_recientes",
    "prefiltrar",
    "recolectar",
    "recolectar_demanda",
]
