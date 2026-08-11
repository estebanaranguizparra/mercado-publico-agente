"""Modelos de dominio.

Se separan en tres grupos:

1. Los que mapean la respuesta cruda de la API de Mercado Público (alias PascalCase).
2. El perfil de la empresa, que define qué es "interesante".
3. La evaluación que produce Claude para cada licitación.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# 1. Respuesta de la API de Mercado Público
# --------------------------------------------------------------------------- #

_CONFIG_API = ConfigDict(populate_by_name=True, extra="ignore")


def _parsear_fecha(valor: Any) -> datetime | None:
    """La API entrega fechas ISO sin zona horaria, y a veces cadenas vacías."""
    if not valor:
        return None
    if isinstance(valor, datetime):
        return valor
    texto = str(valor).strip().replace("Z", "")
    for formato in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto[: len(formato) + 2], formato)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(texto)
    except ValueError:
        return None


class Comprador(BaseModel):
    model_config = _CONFIG_API

    codigo_organismo: str | None = Field(None, alias="CodigoOrganismo")
    nombre_organismo: str | None = Field(None, alias="NombreOrganismo")
    rut_unidad: str | None = Field(None, alias="RutUnidad")
    nombre_unidad: str | None = Field(None, alias="NombreUnidad")
    region_unidad: str | None = Field(None, alias="RegionUnidad")
    comuna_unidad: str | None = Field(None, alias="ComunaUnidad")


class ItemLicitacion(BaseModel):
    model_config = _CONFIG_API

    correlativo: int | None = Field(None, alias="Correlativo")
    codigo_producto: int | None = Field(None, alias="CodigoProducto")
    categoria: str | None = Field(None, alias="Categoria")
    nombre_producto: str | None = Field(None, alias="NombreProducto")
    descripcion: str | None = Field(None, alias="Descripcion")
    unidad_medida: str | None = Field(None, alias="UnidadMedida")
    cantidad: float | None = Field(None, alias="Cantidad")


class ItemsLicitacion(BaseModel):
    model_config = _CONFIG_API

    cantidad: int | None = Field(None, alias="Cantidad")
    listado: list[ItemLicitacion] = Field(default_factory=list, alias="Listado")


class FechasLicitacion(BaseModel):
    model_config = _CONFIG_API

    fecha_creacion: datetime | None = Field(None, alias="FechaCreacion")
    fecha_publicacion: datetime | None = Field(None, alias="FechaPublicacion")
    fecha_cierre: datetime | None = Field(None, alias="FechaCierre")
    fecha_inicio: datetime | None = Field(None, alias="FechaInicio")
    fecha_final: datetime | None = Field(None, alias="FechaFinal")
    fecha_adjudicacion: datetime | None = Field(None, alias="FechaAdjudicacion")
    fecha_entrega_fisica: datetime | None = Field(None, alias="FechaEntregaFisica")

    @field_validator("*", mode="before")
    @classmethod
    def _fechas(cls, valor: Any) -> Any:
        return _parsear_fecha(valor)


class Licitacion(BaseModel):
    """Una licitación tal como la devuelve `licitaciones.json?codigo=...`."""

    model_config = _CONFIG_API

    codigo_externo: str = Field(alias="CodigoExterno")
    nombre: str | None = Field(None, alias="Nombre")
    descripcion: str | None = Field(None, alias="Descripcion")
    estado: str | None = Field(None, alias="Estado")
    codigo_estado: int | None = Field(None, alias="CodigoEstado")
    tipo: str | None = Field(None, alias="Tipo")
    moneda: str | None = Field(None, alias="Moneda")
    monto_estimado: float | None = Field(None, alias="MontoEstimado")
    tiempo_duracion_contrato: int | None = Field(None, alias="TiempoDuracionContrato")
    unidad_tiempo_contrato: str | None = Field(None, alias="UnidadTiempoContrato")
    dias_cierre_licitacion: int | None = Field(None, alias="DiasCierreLicitacion")

    comprador: Comprador | None = Field(None, alias="Comprador")
    items: ItemsLicitacion | None = Field(None, alias="Items")
    fechas: FechasLicitacion | None = Field(None, alias="Fechas")

    # En el listado resumido `FechaCierre` viene en la raíz; en el detalle,
    # dentro de `Fechas`. Se aceptan ambos y se normalizan en `fecha_cierre`.
    fecha_cierre_raiz: datetime | None = Field(None, alias="FechaCierre")

    @field_validator("fecha_cierre_raiz", mode="before")
    @classmethod
    def _fecha_raiz(cls, valor: Any) -> Any:
        return _parsear_fecha(valor)

    @property
    def fecha_cierre(self) -> datetime | None:
        if self.fechas and self.fechas.fecha_cierre:
            return self.fechas.fecha_cierre
        return self.fecha_cierre_raiz

    @property
    def fecha_publicacion(self) -> datetime | None:
        if not self.fechas:
            return None
        return self.fechas.fecha_publicacion or self.fechas.fecha_creacion

    @property
    def dias_al_cierre(self) -> int | None:
        cierre = self.fecha_cierre
        if cierre is None:
            return None
        return (cierre.date() - date.today()).days

    @property
    def region(self) -> str | None:
        return self.comprador.region_unidad if self.comprador else None

    @property
    def organismo(self) -> str | None:
        return self.comprador.nombre_organismo if self.comprador else None

    @property
    def es_detalle(self) -> bool:
        """El listado resumido no trae items; el detalle sí."""
        return self.items is not None and bool(self.items.listado)

    def texto_busqueda(self) -> str:
        """Todo el texto relevante concatenado y en minúsculas, para filtros."""
        partes: list[str] = [self.nombre or "", self.descripcion or ""]
        if self.items:
            for item in self.items.listado:
                partes.extend(
                    [item.nombre_producto or "", item.descripcion or "", item.categoria or ""]
                )
        return " ".join(partes).lower()


# --------------------------------------------------------------------------- #
# 2. Perfil de la empresa
# --------------------------------------------------------------------------- #


class Importacion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    activa: bool = False
    paises: list[str] = Field(default_factory=list)
    lead_time_dias: int = 60
    incoterm_habitual: str = "FOB"
    monto_minimo_orden_usd: float = 0


class Capacidades(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fabricacion_propia: list[str] = Field(default_factory=list)
    armado_kitting: bool = False
    stock_actual: list[str] = Field(default_factory=list)
    proveedores_locales: bool = True
    importacion: Importacion = Field(default_factory=Importacion)


class Restricciones(BaseModel):
    model_config = ConfigDict(extra="ignore")

    monto_minimo_clp: float = 0
    monto_maximo_clp: float = float("inf")
    dias_minimos_al_cierre: int = 3
    margen_minimo_pct: float = 15
    capital_trabajo_clp: float = 0
    acepta_boleta_garantia: bool = True
    tiene_experiencia_previa_estado: bool = False
    exigir_coincidencia_palabras_clave: bool = True


class Perfil(BaseModel):
    """Define qué considera "interesante" esta empresa en particular."""

    model_config = ConfigDict(extra="ignore")

    nombre_empresa: str
    descripcion: str = ""
    rubros_objetivo: list[str] = Field(default_factory=list)
    palabras_clave_incluir: list[str] = Field(default_factory=list)
    palabras_clave_excluir: list[str] = Field(default_factory=list)
    capacidades: Capacidades = Field(default_factory=Capacidades)
    restricciones: Restricciones = Field(default_factory=Restricciones)
    certificaciones: list[str] = Field(default_factory=list)
    regiones_operacion: list[str] = Field(default_factory=list)
    notas_estrategicas: str = ""


# --------------------------------------------------------------------------- #
# 3. Evaluación producida por Claude
# --------------------------------------------------------------------------- #

Estrategia = Literal[
    "FABRICAR",
    "COMPRAR_LOCAL",
    "IMPORTAR",
    "STOCK_PROPIO",
    "MIXTA",
    "DESCARTAR",
]

Confianza = Literal["alta", "media", "baja"]

# `extra="forbid"` es obligatorio: la API de structured outputs exige
# `additionalProperties: false` en todos los objetos del esquema.
_CONFIG_LLM = ConfigDict(extra="forbid")


class Puntajes(BaseModel):
    model_config = _CONFIG_LLM

    encaje_rubro: int = Field(description="0-100. Qué tan bien calza con los rubros de la empresa.")
    viabilidad_suministro: int = Field(
        description="0-100. Facilidad real de conseguir o producir lo pedido a tiempo."
    )
    margen: int = Field(description="0-100. Atractivo del margen esperado frente al mínimo exigido.")
    competencia: int = Field(
        description="0-100. Más alto significa menos competencia esperada o mejor posición relativa."
    )
    barreras_entrada: int = Field(
        description="0-100. Más alto significa menos barreras (certificaciones, experiencia, garantías)."
    )
    calce_plazos: int = Field(
        description="0-100. Compatibilidad entre el plazo de entrega exigido y los lead times de la empresa."
    )


class Evaluacion(BaseModel):
    """Salida estructurada del análisis de una licitación."""

    model_config = _CONFIG_LLM

    codigo_externo: str = Field(description="Código de la licitación evaluada.")
    resumen: str = Field(description="Dos o tres frases sobre qué se pide y por qué importa.")
    categoria_producto: str = Field(
        description="Categoría comercial del bien o servicio principal, en palabras del rubro."
    )
    estrategia: Estrategia = Field(
        description="Vía recomendada para atender la licitación, o DESCARTAR si no conviene."
    )
    justificacion_estrategia: str = Field(
        description="Por qué esa vía y no las otras, en concreto."
    )
    puntaje_total: int = Field(description="0-100. Atractivo global de la oportunidad.")
    puntajes: Puntajes
    margen_estimado_pct: float = Field(
        description="Margen bruto estimado en porcentaje. Usa -1 si no hay base para estimarlo."
    )
    confianza: Confianza = Field(description="Confianza en la evaluación dada la información disponible.")
    riesgos: list[str] = Field(description="Riesgos concretos, no genéricos. Máximo cinco.")
    requisitos_criticos: list[str] = Field(
        description="Certificaciones, garantías, experiencia o documentos que podrían dejar fuera a la empresa."
    )
    acciones_siguientes: list[str] = Field(
        description="Pasos accionables y ordenados por prioridad. Máximo cinco."
    )
    supuestos: list[str] = Field(
        description="Supuestos que hiciste por falta de información en las bases."
    )


class EvaluacionGuardada(BaseModel):
    """Evaluación más los metadatos de la corrida que la produjo."""

    model_config = ConfigDict(extra="ignore")

    evaluacion: Evaluacion
    modelo: str
    evaluada_en: datetime
    tokens_entrada: int = 0
    tokens_salida: int = 0
