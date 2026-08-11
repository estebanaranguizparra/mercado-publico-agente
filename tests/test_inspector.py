"""El inspector debe sobrevivir a las rarezas típicas de los CSV públicos."""

from __future__ import annotations

import zipfile
from pathlib import Path

from mpagente.inspector import inspeccionar, listar_contenido_zip

ENCABEZADO = (
    "Codigo OC;Fecha Envio OC;Nombre Organismo;RUT Proveedor;Razon Social Proveedor;"
    "Codigo Producto ONU;Nombre Producto;Cantidad;Precio Unitario;Moneda;Region"
)
FILAS = [
    "1509-45-SE26;2026-03-14;Servicio de Salud Sur;76.543.210-1;Insumos Andes SpA;"
    "46181504;Guantes de nitrilo;9.600;1.234,50;CLP;Región Metropolitana",
    "2345-88-SE26;2026-03-15;Corporación Valparaíso;77.111.222-3;Aseo Total Ltda;"
    "47131802;Jabón líquido;7.200;3.890,00;CLP;Región de Valparaíso",
    "3120-12-SE26;2026-03-16;Gobierno Regional;78.999.888-7;TecnoSur SpA;"
    "43211503;Notebook 14 pulgadas;180;789.900,00;CLP;Región de O'Higgins",
]


def _escribir_csv(ruta: Path, *, codificacion: str = "utf-8") -> Path:
    contenido = "\n".join([ENCABEZADO, *FILAS]) + "\n"
    ruta.write_text(contenido, encoding=codificacion)
    return ruta


def test_detecta_estructura_basica(tmp_path: Path) -> None:
    informe = inspeccionar(_escribir_csv(tmp_path / "oc.csv"))

    assert informe.separador == ";"
    assert informe.codificacion.startswith("utf-8")
    assert informe.convencion_decimal.startswith("chilena")
    assert informe.filas_leidas == 3
    assert len(informe.columnas) == 11


def test_asigna_los_roles_esenciales(tmp_path: Path) -> None:
    informe = inspeccionar(_escribir_csv(tmp_path / "oc.csv"))
    roles = informe.roles_detectados

    assert roles["categoria"] == "Codigo Producto ONU"
    assert roles["precio_unitario"] == "Precio Unitario"
    assert roles["cantidad"] == "Cantidad"
    assert roles["proveedor"] in {"RUT Proveedor", "Razon Social Proveedor"}
    assert roles["organismo"] == "Nombre Organismo"
    assert roles["fecha"] == "Fecha Envio OC"
    assert informe.roles_faltantes() == []


def test_soporta_codificacion_windows(tmp_path: Path) -> None:
    # Los períodos antiguos vienen en CP1252; no debe reventar ni perder tildes.
    informe = inspeccionar(_escribir_csv(tmp_path / "oc_cp1252.csv", codificacion="cp1252"))
    nombres = [c.nombre for c in informe.columnas]

    assert "Razon Social Proveedor" in nombres
    region = next(c for c in informe.columnas if c.nombre == "Region")
    assert any("Región" in e or "Regi" in e for e in region.ejemplos)


def test_lee_el_csv_mas_grande_dentro_del_zip(tmp_path: Path) -> None:
    destino = tmp_path / "paquete.zip"
    with zipfile.ZipFile(destino, "w") as zf:
        zf.writestr("readme.txt", "notas del paquete")
        zf.writestr("diccionario.csv", "campo;descripcion\nCodigo OC;identificador\n")
        zf.writestr("ordenes.csv", "\n".join([ENCABEZADO, *FILAS * 20]))

    informe = inspeccionar(destino)
    assert informe.archivo_interno == "ordenes.csv"
    assert informe.filas_leidas == 60

    contenido = dict(listar_contenido_zip(destino))
    assert "ordenes.csv" in contenido


def test_respeta_el_limite_de_muestreo(tmp_path: Path) -> None:
    ruta = tmp_path / "grande.csv"
    ruta.write_text("\n".join([ENCABEZADO, *FILAS * 500]), encoding="utf-8")

    informe = inspeccionar(ruta, limite_filas=100)
    assert informe.filas_leidas == 100
    assert informe.filas_totales_estimadas > 100


def test_reporta_roles_faltantes(tmp_path: Path) -> None:
    ruta = tmp_path / "parcial.csv"
    ruta.write_text("Columna A;Columna B\n1;2\n", encoding="utf-8")

    informe = inspeccionar(ruta)
    assert "precio_unitario" in informe.roles_faltantes()
    assert "categoria" in informe.roles_faltantes()
