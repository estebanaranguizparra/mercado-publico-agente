"""Carga de configuración desde `.env` y del perfil de empresa."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .models import Perfil

RAIZ_PROYECTO = Path(__file__).resolve().parents[2]


class ErrorConfig(RuntimeError):
    """Configuración ausente o inconsistente."""


def _ruta(valor: str) -> Path:
    ruta = Path(valor).expanduser()
    return ruta if ruta.is_absolute() else RAIZ_PROYECTO / ruta


@dataclass(frozen=True)
class Config:
    fuente: str  # "mock" | "api"
    ticket: str | None
    modelo: str
    effort: str
    db_path: Path
    perfil_path: Path
    req_por_minuto: int
    timeout: float

    @classmethod
    def cargar(cls) -> Config:
        load_dotenv(RAIZ_PROYECTO / ".env")

        fuente = os.getenv("MP_FUENTE", "mock").strip().lower()
        if fuente not in {"mock", "api"}:
            raise ErrorConfig(f"MP_FUENTE debe ser 'mock' o 'api', recibí '{fuente}'.")

        ticket = (os.getenv("MP_TICKET") or "").strip() or None
        if fuente == "api" and not ticket:
            raise ErrorConfig(
                "MP_FUENTE=api requiere MP_TICKET. Solicítalo en "
                "https://desarrolladores.mercadopublico.cl o usa MP_FUENTE=mock."
            )

        effort = os.getenv("MP_EFFORT", "high").strip().lower()
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ErrorConfig(
                f"MP_EFFORT debe ser low|medium|high|xhigh|max, recibí '{effort}'."
            )

        return cls(
            fuente=fuente,
            ticket=ticket,
            modelo=os.getenv("MP_MODELO", "claude-opus-5").strip(),
            effort=effort,
            db_path=_ruta(os.getenv("MP_DB", "data/mercadopublico.db")),
            perfil_path=_ruta(os.getenv("MP_PERFIL", "perfil.json")),
            req_por_minuto=int(os.getenv("MP_REQ_POR_MINUTO", "20")),
            timeout=float(os.getenv("MP_TIMEOUT", "30")),
        )


def cargar_perfil(ruta: Path) -> Perfil:
    """Lee el perfil de empresa; cae al de ejemplo si no existe uno propio."""
    if not ruta.exists():
        ejemplo = RAIZ_PROYECTO / "perfil.ejemplo.json"
        if not ejemplo.exists():
            raise ErrorConfig(f"No existe {ruta} ni un perfil.ejemplo.json de respaldo.")
        ruta = ejemplo

    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ErrorConfig(f"{ruta} no es JSON válido: {exc}") from exc

    return Perfil.model_validate(datos)
