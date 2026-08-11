"""Inspección de los CSV masivos de Datos Abiertos de ChileCompra.

Antes de escribir un cargador conviene saber qué trae el archivo: los datasets
públicos chilenos varían en codificación (UTF-8 o CP1252), separador decimal
(coma o punto) y nombres de columna entre períodos. Este módulo lee una muestra
y describe la estructura real, sin suponer nada.

No usa pandas a propósito: para inspeccionar un archivo de cientos de MB basta
con leer las primeras N filas en streaming.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# Los CSV de ChileCompra suelen venir en UTF-8, pero los períodos antiguos y
# algunos exports vienen en la codificación de Windows en español.
_CODIFICACIONES = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

_SEPARADORES = (";", ",", "\t", "|")

# Formato numérico chileno: 1.234.567,89
_DECIMAL_ES = re.compile(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$|^-?\d+,\d+$")
# Formato anglosajón: 1234567.89
_DECIMAL_EN = re.compile(r"^-?\d+\.\d+$")
_ENTERO = re.compile(r"^-?\d+$")
_FECHA = re.compile(
    r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$|^\d{2}[-/]\d{2}[-/]\d{4}"
)

# Pistas para adivinar el rol de cada columna. Se comparan contra el nombre
# normalizado (minúsculas, sin tildes, sin separadores).
_ROLES = {
    "categoria": ("rubro", "categoria", "onu", "unspsc", "codigoproducto", "especie"),
    "producto": ("nombreproducto", "producto", "descripcionproducto", "glosa"),
    "precio_unitario": ("preciounitario", "precioneto", "valorunitario"),
    "cantidad": ("cantidad",),
    "monto_total": ("montototal", "totallinea", "total", "montoneto"),
    "proveedor": ("proveedor", "razonsocialproveedor", "rutproveedor", "rutsucursal"),
    "organismo": ("organismo", "comprador", "institucion", "razonsocialcomprador"),
    "fecha": ("fecha", "fechaenvio", "fechacreacion", "fechaaceptacion"),
    "identificador": ("codigoorden", "codigooc", "ordendecompra", "codigolicitacion"),
    "moneda": ("moneda",),
    "region": ("region",),
}


def _normalizar(nombre: str) -> str:
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", nombre.lower()) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]", "", sin_tildes)


# --------------------------------------------------------------------------- #
# Estructuras del informe
# --------------------------------------------------------------------------- #


@dataclass
class ColumnaInfo:
    nombre: str
    tipo: str
    vacios_pct: float
    valores_distintos: int
    ejemplos: list[str] = field(default_factory=list)
    top_valores: list[tuple[str, int]] = field(default_factory=list)
    rol_probable: str | None = None


@dataclass
class InformeArchivo:
    ruta: Path
    archivo_interno: str | None
    codificacion: str
    separador: str
    convencion_decimal: str
    filas_leidas: int
    filas_totales_estimadas: int | None
    columnas: list[ColumnaInfo]

    @property
    def roles_detectados(self) -> dict[str, str]:
        return {c.rol_probable: c.nombre for c in self.columnas if c.rol_probable}

    def roles_faltantes(self) -> list[str]:
        """Roles necesarios para el análisis por categoría que no aparecieron."""
        esenciales = {"categoria", "precio_unitario", "cantidad", "proveedor", "fecha"}
        return sorted(esenciales - set(self.roles_detectados))


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #


def _abrir(ruta: Path) -> tuple[bytes, str | None]:
    """Devuelve los bytes del CSV y, si venía en un ZIP, el nombre interno."""
    if ruta.suffix.lower() != ".zip":
        return ruta.read_bytes(), None

    with zipfile.ZipFile(ruta) as zf:
        candidatos = [
            i for i in zf.infolist() if i.filename.lower().endswith((".csv", ".txt"))
        ]
        if not candidatos:
            raise ValueError(f"El ZIP {ruta.name} no contiene ningún .csv")
        # El dataset principal es el archivo más pesado del paquete.
        elegido = max(candidatos, key=lambda i: i.file_size)
        return zf.read(elegido), elegido.filename


def _decodificar(bruto: bytes) -> tuple[str, str]:
    for codificacion in _CODIFICACIONES:
        try:
            return bruto.decode(codificacion), codificacion
        except UnicodeDecodeError:
            continue
    # Último recurso: latin-1 nunca falla, aunque puede producir mojibake.
    return bruto.decode("latin-1", errors="replace"), "latin-1 (con reemplazos)"


def _detectar_separador(muestra: str) -> str:
    primera_linea = muestra.splitlines()[0] if muestra else ""
    conteos = {sep: primera_linea.count(sep) for sep in _SEPARADORES}
    mejor = max(conteos, key=lambda s: conteos[s])
    return mejor if conteos[mejor] > 0 else ";"


def _clasificar(valor: str) -> str:
    v = valor.strip()
    if not v:
        return "vacio"
    if _ENTERO.match(v):
        return "entero"
    if _DECIMAL_ES.match(v):
        return "decimal_es"
    if _DECIMAL_EN.match(v):
        return "decimal_en"
    if _FECHA.match(v):
        return "fecha"
    return "texto"


def inspeccionar(ruta: Path, *, limite_filas: int = 20_000) -> InformeArchivo:
    """Lee una muestra del archivo y describe su estructura real."""
    if not ruta.exists():
        raise FileNotFoundError(f"No existe {ruta}")

    bruto, archivo_interno = _abrir(ruta)
    texto, codificacion = _decodificar(bruto)
    separador = _detectar_separador(texto[:8192])

    lector = csv.reader(io.StringIO(texto), delimiter=separador)
    try:
        encabezados = next(lector)
    except StopIteration as exc:
        raise ValueError(f"{ruta.name} está vacío") from exc

    encabezados = [h.strip().lstrip("﻿") for h in encabezados]
    n = len(encabezados)

    vacios = [0] * n
    distintos: list[set[str]] = [set() for _ in range(n)]
    tipos: list[dict[str, int]] = [{} for _ in range(n)]
    conteos: list[dict[str, int]] = [{} for _ in range(n)]

    filas_leidas = 0
    for fila in lector:
        if filas_leidas >= limite_filas:
            break
        filas_leidas += 1
        for i in range(n):
            valor = fila[i].strip() if i < len(fila) else ""
            if not valor:
                vacios[i] += 1
                continue
            if len(distintos[i]) < 5000:
                distintos[i].add(valor)
            tipo = _clasificar(valor)
            tipos[i][tipo] = tipos[i].get(tipo, 0) + 1
            if len(conteos[i]) < 200:
                conteos[i][valor] = conteos[i].get(valor, 0) + 1
            elif valor in conteos[i]:
                conteos[i][valor] += 1

    columnas: list[ColumnaInfo] = []
    decimal_es = decimal_en = 0

    for i, nombre in enumerate(encabezados):
        conteo_tipos = tipos[i]
        decimal_es += conteo_tipos.get("decimal_es", 0)
        decimal_en += conteo_tipos.get("decimal_en", 0)

        tipo = max(conteo_tipos, key=lambda t: conteo_tipos[t]) if conteo_tipos else "vacio"
        ejemplos = sorted(distintos[i])[:3]
        top = sorted(conteos[i].items(), key=lambda kv: -kv[1])[:5]

        columnas.append(
            ColumnaInfo(
                nombre=nombre,
                tipo=tipo,
                vacios_pct=100 * vacios[i] / filas_leidas if filas_leidas else 100.0,
                valores_distintos=len(distintos[i]),
                ejemplos=ejemplos,
                top_valores=top if len(distintos[i]) <= 30 else [],
                rol_probable=_adivinar_rol(nombre),
            )
        )

    convencion = (
        "chilena (1.234,56)"
        if decimal_es > decimal_en
        else "anglosajona (1234.56)"
        if decimal_en > 0
        else "sin decimales detectados"
    )

    # Estimación del total de filas a partir del peso promedio de las leídas.
    totales = texto.count("\n")

    return InformeArchivo(
        ruta=ruta,
        archivo_interno=archivo_interno,
        codificacion=codificacion,
        separador=separador,
        convencion_decimal=convencion,
        filas_leidas=filas_leidas,
        filas_totales_estimadas=max(totales - 1, filas_leidas),
        columnas=columnas,
    )


def _adivinar_rol(nombre: str) -> str | None:
    """Asigna el rol más específico cuyo indicio aparezca en el nombre."""
    normalizado = _normalizar(nombre)
    mejor_rol: str | None = None
    mejor_largo = 0
    for rol, indicios in _ROLES.items():
        for indicio in indicios:
            if indicio in normalizado and len(indicio) > mejor_largo:
                mejor_rol, mejor_largo = rol, len(indicio)
    return mejor_rol


def listar_contenido_zip(ruta: Path) -> Iterator[tuple[str, int]]:
    """Nombre y tamaño de cada archivo dentro de un ZIP."""
    with zipfile.ZipFile(ruta) as zf:
        for info in sorted(zf.infolist(), key=lambda i: -i.file_size):
            yield info.filename, info.file_size
