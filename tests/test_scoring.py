"""Verifica el armado de la petición y el parseo de la respuesta sin tocar la red."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mpagente.clients import ClienteMock
from mpagente.config import cargar_perfil
from mpagente.pipeline.scoring import AnalizadorClaude

RAIZ = Path(__file__).resolve().parents[1]

RESPUESTA_VALIDA = {
    "codigo_externo": "IGNORADO-POR-EL-MODELO",
    "resumen": "Compra de EPP para personal operativo de un servicio de salud.",
    "categoria_producto": "Elementos de protección personal",
    "estrategia": "IMPORTAR",
    "justificacion_estrategia": "El plazo de 90 días cubre el lead time de 55 días.",
    "puntaje_total": 180,  # fuera de rango a propósito: debe quedar acotado a 100
    "puntajes": {
        "encaje_rubro": 90,
        "viabilidad_suministro": 85,
        "margen": 80,
        "competencia": 55,
        "barreras_entrada": -20,  # fuera de rango a propósito
        "calce_plazos": 88,
    },
    "margen_estimado_pct": 27.5,
    "confianza": "media",
    "riesgos": ["La certificación EN 374 exige ensayo acreditado."],
    "requisitos_criticos": ["Boleta de garantía de seriedad."],
    "acciones_siguientes": ["Cotizar con dos proveedores de Guangzhou."],
    "supuestos": ["Tipo de cambio de 950 CLP por dólar."],
}


# --- Dobles de prueba del SDK ---------------------------------------------- #


@dataclass
class _Bloque:
    text: str
    type: str = "text"


@dataclass
class _Uso:
    input_tokens: int = 1200
    output_tokens: int = 450
    cache_read_input_tokens: int = 0


@dataclass
class _Respuesta:
    content: list[_Bloque]
    usage: _Uso = field(default_factory=_Uso)
    stop_reason: str = "end_turn"
    stop_details: Any = None


class _Mensajes:
    def __init__(self, respuesta: _Respuesta) -> None:
        self._respuesta = respuesta
        self.llamadas: list[dict] = []

    def create(self, **kwargs: Any) -> _Respuesta:
        self.llamadas.append(kwargs)
        return self._respuesta


class _ClienteFalso:
    def __init__(self, respuesta: _Respuesta) -> None:
        self.messages = _Mensajes(respuesta)


def _analizador(respuesta: _Respuesta) -> tuple[AnalizadorClaude, _ClienteFalso]:
    perfil = cargar_perfil(RAIZ / "perfil.ejemplo.json")
    cliente = _ClienteFalso(respuesta)
    return (
        AnalizadorClaude(perfil, modelo="claude-opus-5", effort="high", cliente=cliente),
        cliente,
    )


# --- Tests ------------------------------------------------------------------ #


@pytest.fixture
def licitacion():
    lic = ClienteMock().detalle("1509-45-LP26")
    assert lic is not None
    return lic


def test_peticion_lleva_esquema_effort_y_cache(licitacion) -> None:
    respuesta = _Respuesta(content=[_Bloque(json.dumps(RESPUESTA_VALIDA))])
    analizador, cliente = _analizador(respuesta)
    analizador.analizar(licitacion)

    (kwargs,) = cliente.messages.llamadas
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["output_config"]["effort"] == "high"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    # El bloque de sistema es estable y se cachea entre licitaciones.
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Comercializadora Andes SpA" in kwargs["system"][0]["text"]
    # La ficha de la licitación viaja en el turno del usuario.
    assert "1509-45-LP26" in kwargs["messages"][0]["content"]


def test_no_manda_effort_a_modelos_que_no_lo_aceptan(licitacion) -> None:
    respuesta = _Respuesta(content=[_Bloque(json.dumps(RESPUESTA_VALIDA))])
    perfil = cargar_perfil(RAIZ / "perfil.ejemplo.json")
    cliente = _ClienteFalso(respuesta)
    analizador = AnalizadorClaude(perfil, modelo="claude-haiku-4-5", cliente=cliente)
    analizador.analizar(licitacion)

    (kwargs,) = cliente.messages.llamadas
    assert "effort" not in kwargs["output_config"]


def test_normaliza_codigo_y_acota_puntajes(licitacion) -> None:
    respuesta = _Respuesta(content=[_Bloque(json.dumps(RESPUESTA_VALIDA))])
    analizador, _ = _analizador(respuesta)
    resultado = analizador.analizar(licitacion)

    assert resultado.ok
    evaluacion = resultado.evaluacion
    assert evaluacion is not None
    # El código lo fija el pipeline, no el modelo.
    assert evaluacion.codigo_externo == "1509-45-LP26"
    assert evaluacion.puntaje_total == 100
    assert evaluacion.puntajes.barreras_entrada == 0
    assert resultado.tokens_entrada == 1200
    assert resultado.tokens_salida == 450


def test_respuesta_truncada_se_reporta_como_error(licitacion) -> None:
    respuesta = _Respuesta(content=[_Bloque("{\"codigo")], stop_reason="max_tokens")
    analizador, _ = _analizador(respuesta)
    resultado = analizador.analizar(licitacion)

    assert not resultado.ok
    assert "max_tokens" in (resultado.error or "")


def test_rechazo_del_modelo_no_rompe_el_lote(licitacion) -> None:
    respuesta = _Respuesta(content=[], stop_reason="refusal")
    analizador, _ = _analizador(respuesta)
    resultado = analizador.analizar(licitacion)

    assert not resultado.ok
    assert "declinó" in (resultado.error or "")


def test_json_que_no_calza_con_el_esquema_se_reporta(licitacion) -> None:
    invalido = dict(RESPUESTA_VALIDA, estrategia="REGALAR")
    respuesta = _Respuesta(content=[_Bloque(json.dumps(invalido))])
    analizador, _ = _analizador(respuesta)
    resultado = analizador.analizar(licitacion)

    assert not resultado.ok
    assert "esquema" in (resultado.error or "")


def test_lote_conserva_el_orden_de_entrada() -> None:
    respuesta = _Respuesta(content=[_Bloque(json.dumps(RESPUESTA_VALIDA))])
    analizador, _ = _analizador(respuesta)
    licitaciones = ClienteMock().listar()[:4]

    resultados = analizador.analizar_lote(licitaciones)
    assert [r.codigo for r in resultados] == [lic.codigo_externo for lic in licitaciones]
