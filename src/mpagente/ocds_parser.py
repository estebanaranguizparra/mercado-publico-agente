"""Traduce una ficha OCDS de adjudicación a las estructuras del proyecto.

Función pura y sin red: recibe el JSON tal como lo entrega la API y devuelve
las adjudicaciones que contiene. Un mismo proceso puede adjudicarse a varios
proveedores, así que la unidad no es el proceso sino cada par proceso-premio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

_PREFIJO_OCID = "ocds-70d2nz-"


@dataclass
class ItemAdjudicado:
    item_id: str
    unspsc: str | None
    descripcion: str
    cantidad: float | None
    unidad: str | None
    precio_unitario: float | None
    moneda: str | None

    @property
    def familia_unspsc(self) -> str | None:
        """Primeros 4 dígitos del UNSPSC: el nivel de 'familia' de producto."""
        return self.unspsc[:4] if self.unspsc and len(self.unspsc) >= 4 else None


@dataclass
class Adjudicacion:
    award_uid: str
    ocid: str
    codigo: str
    modalidad: str
    titulo: str
    estado: str | None
    fecha: datetime | None
    comprador: str | None
    comprador_id: str | None
    region: str | None
    proveedor: str | None
    proveedor_id: str | None
    monto: float | None
    moneda: str | None
    n_oferentes: int
    items: list[ItemAdjudicado] = field(default_factory=list)

    @property
    def anio(self) -> int | None:
        return self.fecha.year if self.fecha else None

    @property
    def mes(self) -> int | None:
        return self.fecha.month if self.fecha else None


def _fecha(valor: object) -> datetime | None:
    if not isinstance(valor, str) or not valor:
        return None
    texto = valor.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(texto).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(texto[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def _numero(valor: object) -> float | None:
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, str):
        try:
            return float(valor.replace(",", "."))
        except ValueError:
            return None
    return None


def _limpiar(texto: object) -> str | None:
    if not isinstance(texto, str):
        return None
    limpio = texto.strip()
    return limpio or None


def _nombre_corto(nombre: object) -> str | None:
    """Los nombres vienen como 'RAZÓN SOCIAL | Nombre de fantasía'."""
    limpio = _limpiar(nombre)
    return limpio.split("|")[0].strip() if limpio else None


# Estados en que un proceso ya no cambiará: no vale la pena volver a consultarlo.
ADJUDICADO = "adjudicado"
CERRADO_SIN_ADJUDICAR = "cerrado_sin_adjudicar"
PENDIENTE = "pendiente"

ESTADOS_TERMINALES = frozenset({ADJUDICADO, CERRADO_SIN_ADJUDICAR})

# `statusDetails` viene como "8-Adjudicada", "7-Desierta…", "15-Revocada".
_MARCAS_CIERRE_SIN_ADJUDICAR = ("desierta", "revocada", "cancelada", "anulada")


def clasificar_proceso(adjudicaciones: list[Adjudicacion], payload: dict) -> str:
    """Decide si un proceso ya terminó o si hay que volver a consultarlo.

    La API responde a `award/{codigo}` aunque el proceso siga abierto: devuelve
    un premio sin proveedor ni monto. Distinguir esos casos evita guardar ruido
    y, sobre todo, evita darlos por vistos y no volver nunca a mirarlos.
    """
    if any(a.monto and a.proveedor for a in adjudicaciones):
        return ADJUDICADO

    estados = " ".join((a.estado or "").lower() for a in adjudicaciones)
    if any(marca in estados for marca in _MARCAS_CIERRE_SIN_ADJUDICAR):
        return CERRADO_SIN_ADJUDICAR

    del payload  # reservado por si más adelante hace falta mirar el tender
    return PENDIENTE


@dataclass
class ItemSolicitado:
    item_id: str
    unspsc: str | None
    descripcion: str
    cantidad: float | None
    unidad: str | None

    @property
    def familia_unspsc(self) -> str | None:
        return self.unspsc[:4] if self.unspsc and len(self.unspsc) >= 4 else None


@dataclass
class Solicitud:
    """Un llamado publicado: lo que el Estado pide, sin saber aún quién gana."""

    codigo: str
    ocid: str
    modalidad: str
    titulo: str
    descripcion: str | None
    estado: str | None
    tipo_proceso: str | None
    comprador: str | None
    comprador_id: str | None
    region: str | None
    publicada: datetime | None
    cierre: datetime | None
    items: list[ItemSolicitado] = field(default_factory=list)

    @property
    def anio(self) -> int | None:
        return self.publicada.year if self.publicada else None

    @property
    def mes(self) -> int | None:
        return self.publicada.month if self.publicada else None

    def vigente(self, ahora: datetime | None = None) -> bool:
        """Si todavía se puede postular.

        Se decide por la fecha de cierre y no por `statusDetails`: la ficha de
        un proceso ya vencido sigue diciendo "5-Publicada", así que el estado
        declarado no distingue lo abierto de lo cerrado.
        """
        if self.cierre is None:
            return False
        return self.cierre > (ahora or datetime.now())


def parsear_solicitud(payload: dict, modalidad: str) -> Solicitud | None:
    """Extrae el llamado desde una respuesta `tender`. None si viene vacía."""
    releases = payload.get("releases") or []
    for release in releases:
        if not isinstance(release, dict):
            continue
        tender = release.get("tender")
        if not isinstance(tender, dict):
            continue

        ocid = str(release.get("ocid") or "")
        codigo = ocid.removeprefix(_PREFIJO_OCID)

        comprador = comprador_id = region = None
        for parte in release.get("parties") or []:
            if not isinstance(parte, dict):
                continue
            roles = {str(r).lower() for r in (parte.get("roles") or [])}
            if {"buyer", "procuringentity"} & roles:
                comprador = _nombre_corto(parte.get("name"))
                comprador_id = _limpiar(parte.get("id"))
                direccion = parte.get("address")
                if isinstance(direccion, dict):
                    region = _limpiar(direccion.get("region"))
                break

        if comprador is None:
            entidad = tender.get("procuringEntity")
            if isinstance(entidad, dict):
                comprador = _nombre_corto(entidad.get("name"))
                comprador_id = _limpiar(entidad.get("id"))

        periodo = tender.get("tenderPeriod") if isinstance(tender.get("tenderPeriod"), dict) else {}

        items: list[ItemSolicitado] = []
        for item in tender.get("items") or []:
            if not isinstance(item, dict):
                continue
            clasificacion = item.get("classification")
            unspsc = None
            if isinstance(clasificacion, dict):
                if str(clasificacion.get("scheme", "")).upper() == "UNSPSC":
                    unspsc = _limpiar(clasificacion.get("id"))

            unidad = item.get("unit") if isinstance(item.get("unit"), dict) else {}
            items.append(
                ItemSolicitado(
                    item_id=str(item.get("id") or len(items)),
                    unspsc=unspsc,
                    descripcion=_limpiar(item.get("description")) or "",
                    cantidad=_numero(item.get("quantity")),
                    unidad=_limpiar(unidad.get("name")),
                )
            )

        return Solicitud(
            codigo=codigo,
            ocid=ocid,
            modalidad=modalidad,
            titulo=_limpiar(tender.get("title")) or "",
            descripcion=_limpiar(tender.get("description")),
            estado=_limpiar(tender.get("statusDetails")) or _limpiar(tender.get("status")),
            tipo_proceso=_limpiar(tender.get("procurementMethodDetails")),
            comprador=comprador,
            comprador_id=comprador_id,
            region=region,
            publicada=_fecha(periodo.get("startDate")) or _fecha(release.get("date")),
            cierre=_fecha(periodo.get("endDate")),
            items=items,
        )

    return None


def parsear_adjudicaciones(payload: dict, modalidad: str) -> list[Adjudicacion]:
    """Extrae todas las adjudicaciones presentes en una respuesta `award`."""
    resultado: list[Adjudicacion] = []

    for release in payload.get("releases") or []:
        if not isinstance(release, dict):
            continue

        ocid = str(release.get("ocid") or "")
        codigo = ocid.removeprefix(_PREFIJO_OCID)

        comprador = comprador_id = region = None
        n_oferentes = 0

        for parte in release.get("parties") or []:
            if not isinstance(parte, dict):
                continue
            roles = {str(r).lower() for r in (parte.get("roles") or [])}
            if "tenderer" in roles:
                n_oferentes += 1
            if comprador is None and ({"buyer", "procuringentity"} & roles):
                comprador = _nombre_corto(parte.get("name"))
                comprador_id = _limpiar(parte.get("id"))
                direccion = parte.get("address")
                if isinstance(direccion, dict):
                    region = _limpiar(direccion.get("region"))

        for premio in release.get("awards") or []:
            if not isinstance(premio, dict):
                continue

            award_id = str(premio.get("id") or "sin-id")
            valor = premio.get("value") if isinstance(premio.get("value"), dict) else {}

            proveedores = [
                p for p in (premio.get("suppliers") or []) if isinstance(p, dict)
            ]
            proveedor = _nombre_corto(proveedores[0].get("name")) if proveedores else None
            proveedor_id = _limpiar(proveedores[0].get("id")) if proveedores else None

            items: list[ItemAdjudicado] = []
            for item in premio.get("items") or []:
                if not isinstance(item, dict):
                    continue
                clasificacion = item.get("classification")
                unspsc = None
                if isinstance(clasificacion, dict):
                    # Solo sirve si el esquema es UNSPSC; otros no son comparables.
                    if str(clasificacion.get("scheme", "")).upper() == "UNSPSC":
                        unspsc = _limpiar(clasificacion.get("id"))

                unidad = item.get("unit") if isinstance(item.get("unit"), dict) else {}
                valor_unidad = unidad.get("value") if isinstance(unidad.get("value"), dict) else {}

                items.append(
                    ItemAdjudicado(
                        item_id=str(item.get("id") or len(items)),
                        unspsc=unspsc,
                        descripcion=_limpiar(item.get("description")) or "",
                        cantidad=_numero(item.get("quantity")),
                        unidad=_limpiar(unidad.get("name")),
                        precio_unitario=_numero(valor_unidad.get("amount")),
                        moneda=_limpiar(valor_unidad.get("currency")),
                    )
                )

            resultado.append(
                Adjudicacion(
                    award_uid=f"{codigo}:{award_id}",
                    ocid=ocid,
                    codigo=codigo,
                    modalidad=modalidad,
                    titulo=_limpiar(premio.get("title")) or "",
                    estado=_limpiar(premio.get("statusDetails"))
                    or _limpiar(premio.get("status")),
                    fecha=_fecha(premio.get("date")) or _fecha(release.get("date")),
                    comprador=comprador,
                    comprador_id=comprador_id,
                    region=region,
                    proveedor=proveedor,
                    proveedor_id=proveedor_id,
                    monto=_numero(valor.get("amount")),
                    moneda=_limpiar(valor.get("currency")),
                    n_oferentes=n_oferentes,
                    items=items,
                )
            )

    return resultado
