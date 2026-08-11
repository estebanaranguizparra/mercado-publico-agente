"""Evaluación de licitaciones con Claude, usando salidas estructuradas."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import anthropic
from pydantic import ValidationError

from ..models import Evaluacion, Licitacion, Perfil, Puntajes
from ..prompts import INSTRUCCIONES, render_licitacion, render_perfil

log = logging.getLogger(__name__)

# Palabras clave del esquema JSON que la API de salidas estructuradas no acepta.
_KEYWORDS_NO_SOPORTADAS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "default",
}

# Modelos que aceptan `output_config.effort`. Los anteriores devuelven 400.
_PREFIJOS_CON_EFFORT = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-fable-5",
    "claude-mythos-5",
)


class ErrorAnalisis(RuntimeError):
    """El modelo no entregó una evaluación utilizable."""


class ErrorCredenciales(ErrorAnalisis):
    """No hay forma de autenticarse contra la API de Claude."""


def _cliente_autenticado() -> anthropic.Anthropic:
    """Construye el cliente y verifica las credenciales de inmediato.

    El SDK resuelve la autenticación recién al enviar la petición (y ahí lanza un
    `TypeError` genérico), así que sin esta comprobación el fallo aparecería una
    vez por licitación en medio del lote en vez de al principio.
    """
    cliente = anthropic.Anthropic(max_retries=3)

    # Mismo orden de resolución que usa el SDK: api_key, auth_token y, por
    # último, el perfil OAuth de `ant auth login`.
    tiene_credencial = any(
        getattr(cliente, atributo, None)
        for atributo in ("api_key", "auth_token", "_token_cache")
    )
    if not tiene_credencial:
        raise ErrorCredenciales(
            "No hay credenciales de Claude disponibles. Define ANTHROPIC_API_KEY "
            "en tu .env o inicia sesión con `ant auth login`."
        )
    return cliente


@dataclass
class ResultadoAnalisis:
    codigo: str
    evaluacion: Evaluacion | None
    tokens_entrada: int = 0
    tokens_salida: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.evaluacion is not None


def _sanear_esquema(nodo: Any) -> Any:
    """Adapta el esquema de Pydantic a lo que acepta la API.

    Quita las palabras clave no soportadas y fuerza `additionalProperties: false`
    en todo objeto, que es un requisito del modo estricto.
    """
    if isinstance(nodo, list):
        return [_sanear_esquema(x) for x in nodo]
    if not isinstance(nodo, dict):
        return nodo

    limpio = {k: _sanear_esquema(v) for k, v in nodo.items() if k not in _KEYWORDS_NO_SOPORTADAS}
    if limpio.get("type") == "object":
        limpio["additionalProperties"] = False
    return limpio


def esquema_evaluacion() -> dict:
    return _sanear_esquema(Evaluacion.model_json_schema())


class AnalizadorClaude:
    """Envuelve una llamada por licitación al Messages API."""

    def __init__(
        self,
        perfil: Perfil,
        *,
        modelo: str = "claude-opus-5",
        effort: str = "high",
        max_tokens: int = 16000,
        max_paralelo: int = 3,
        cliente: anthropic.Anthropic | None = None,
    ) -> None:
        self._perfil = perfil
        self._modelo = modelo
        self._effort = effort
        self._max_tokens = max_tokens
        self._max_paralelo = max(1, max_paralelo)
        # El SDK reintenta 429 y 5xx con backoff exponencial por su cuenta.
        self._cliente = cliente or _cliente_autenticado()
        self._esquema = esquema_evaluacion()

        # Bloque estable de la petición: rol + perfil. Se cachea para no pagarlo
        # de nuevo en cada licitación de la corrida.
        self._system = [
            {
                "type": "text",
                "text": f"{INSTRUCCIONES}\n\n{render_perfil(perfil)}",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # -- una licitación ---------------------------------------------------- #

    def analizar(self, lic: Licitacion) -> ResultadoAnalisis:
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": self._esquema}
        }
        if self._modelo.startswith(_PREFIJOS_CON_EFFORT):
            output_config["effort"] = self._effort

        try:
            respuesta = self._cliente.messages.create(
                model=self._modelo,
                max_tokens=self._max_tokens,
                system=self._system,
                output_config=output_config,
                messages=[{"role": "user", "content": render_licitacion(lic)}],
            )
        except anthropic.AnthropicError as exc:
            # Cubre errores de API y también los del propio SDK (autenticación,
            # parámetros inválidos); un fallo puntual no debe cortar el lote.
            log.error("Error de API evaluando %s: %s", lic.codigo_externo, exc)
            return ResultadoAnalisis(lic.codigo_externo, None, error=str(exc))

        uso = respuesta.usage
        entrada = (uso.input_tokens or 0) + (getattr(uso, "cache_read_input_tokens", 0) or 0)
        salida = uso.output_tokens or 0

        if respuesta.stop_reason == "refusal":
            detalle = getattr(respuesta, "stop_details", None)
            categoria = getattr(detalle, "category", None) if detalle else None
            return ResultadoAnalisis(
                lic.codigo_externo,
                None,
                entrada,
                salida,
                error=f"El modelo declinó la solicitud (categoría: {categoria or 'no informada'}).",
            )

        if respuesta.stop_reason == "max_tokens":
            return ResultadoAnalisis(
                lic.codigo_externo,
                None,
                entrada,
                salida,
                error="Respuesta truncada por max_tokens; sube el límite y reintenta.",
            )

        texto = next((b.text for b in respuesta.content if b.type == "text"), None)
        if not texto:
            return ResultadoAnalisis(
                lic.codigo_externo, None, entrada, salida, error="La respuesta no trajo texto."
            )

        try:
            evaluacion = Evaluacion.model_validate_json(texto)
        except ValidationError as exc:
            log.error("JSON inválido para %s: %s", lic.codigo_externo, exc.errors()[:2])
            return ResultadoAnalisis(
                lic.codigo_externo,
                None,
                entrada,
                salida,
                error=f"El JSON no calzó con el esquema: {exc.errors()[:1]}",
            )
        except json.JSONDecodeError as exc:
            return ResultadoAnalisis(
                lic.codigo_externo, None, entrada, salida, error=f"JSON malformado: {exc}"
            )

        # El código lo pone el modelo; lo forzamos para que nunca se desalinee
        # con la licitación que realmente se evaluó.
        evaluacion.codigo_externo = lic.codigo_externo
        evaluacion.puntaje_total = _acotar(evaluacion.puntaje_total)
        for campo in Puntajes.model_fields:
            setattr(evaluacion.puntajes, campo, _acotar(getattr(evaluacion.puntajes, campo)))

        return ResultadoAnalisis(lic.codigo_externo, evaluacion, entrada, salida)

    # -- lote -------------------------------------------------------------- #

    def analizar_lote(
        self,
        licitaciones: Iterable[Licitacion],
        *,
        al_terminar: Callable[[ResultadoAnalisis], None] | None = None,
    ) -> list[ResultadoAnalisis]:
        """Evalúa varias licitaciones en paralelo, conservando el orden de entrada.

        `al_terminar` se invoca a medida que cada evaluación cierra, para poder
        mostrar avance sin esperar al lote completo.
        """
        pendientes = list(licitaciones)
        if not pendientes:
            return []

        resultados: list[ResultadoAnalisis | None] = [None] * len(pendientes)
        with ThreadPoolExecutor(max_workers=self._max_paralelo) as pool:
            futuros = {
                pool.submit(self.analizar, lic): indice
                for indice, lic in enumerate(pendientes)
            }
            for futuro in as_completed(futuros):
                indice = futuros[futuro]
                resultado = futuro.result()
                resultados[indice] = resultado
                if al_terminar:
                    al_terminar(resultado)

        return [r for r in resultados if r is not None]


def _acotar(valor: int) -> int:
    return max(0, min(100, int(valor)))
