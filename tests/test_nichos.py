"""Agregación por categoría y puntaje de nichos."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from mpagente.ocds_parser import Adjudicacion, ItemAdjudicado
from mpagente.pipeline.nichos import detectar_nichos, es_bien, nombre_segmento
from mpagente.storage import Almacen


def _adjudicacion(
    uid: str,
    *,
    unspsc: str,
    proveedor: str,
    monto: float,
    precio: float,
    mes: int,
    oferentes: int = 4,
    comprador: str = "COMP-1",
) -> Adjudicacion:
    return Adjudicacion(
        award_uid=uid,
        ocid=f"ocds-{uid}",
        codigo=uid.split(":")[0],
        modalidad="licitacion",
        titulo="Compra de prueba",
        estado="8-Adjudicada",
        fecha=datetime(2025, mes, 15),
        comprador="Organismo",
        comprador_id=comprador,
        region="Región Metropolitana de Santiago",
        proveedor=proveedor,
        proveedor_id=proveedor,
        monto=monto,
        moneda="CLP",
        n_oferentes=oferentes,
        items=[
            ItemAdjudicado(
                item_id="1",
                unspsc=unspsc,
                descripcion="Guantes de nitrilo",
                cantidad=monto / precio,
                unidad="Caja",
                precio_unitario=precio,
                moneda="CLP",
            )
        ],
    )


@pytest.fixture
def almacen(tmp_path: Path) -> Almacen:
    with Almacen(tmp_path / "nichos.db") as a:
        # Familia 4618 (bien): fragmentada, 6 proveedores, precios dispersos.
        a.guardar_adjudicaciones(
            [
                _adjudicacion(
                    f"A{i}:1",
                    unspsc="46181504",
                    proveedor=f"PROV-{i % 6}",
                    monto=30_000_000,
                    precio=10_000 + i * 2_500,
                    mes=(i % 6) + 1,
                    comprador=f"COMP-{i % 4}",
                )
                for i in range(12)
            ]
        )
        # Familia 8010 (servicio): debe quedar fuera del modo bienes.
        a.guardar_adjudicaciones(
            [
                _adjudicacion(
                    f"S{i}:1",
                    unspsc="80101504",
                    proveedor=f"CONS-{i}",
                    monto=50_000_000,
                    precio=50_000_000,
                    mes=(i % 6) + 1,
                )
                for i in range(10)
            ]
        )
        # Familia 5313 (bien): monopolizada por un solo proveedor.
        a.guardar_adjudicaciones(
            [
                _adjudicacion(
                    f"M{i}:1",
                    unspsc="53131608",
                    proveedor="MONOPOLIO",
                    monto=40_000_000,
                    precio=5_000,
                    mes=(i % 6) + 1,
                )
                for i in range(10)
            ]
        )
        yield a


def test_distingue_bienes_de_servicios() -> None:
    assert es_bien("4618")
    assert es_bien("5313")
    assert not es_bien("8010")  # consultoría
    assert not es_bien("7213")  # obras
    assert not es_bien("")
    assert nombre_segmento("4618").startswith("Defensa")
    assert nombre_segmento("8010").startswith("Servicios de gestión")
    assert nombre_segmento("7213").startswith("Construcción")


def test_por_defecto_incluye_servicios(almacen: Almacen) -> None:
    nichos = detectar_nichos(almacen.conexion(), minimo_adjudicaciones=2, minimo_monto=1_000_000)
    familias = {n.familia for n in nichos}

    assert "4618" in familias
    assert "5313" in familias
    assert "8010" in familias


def test_puede_restringir_a_bienes_a_pedido(almacen: Almacen) -> None:
    nichos = detectar_nichos(
        almacen.conexion(),
        minimo_adjudicaciones=2,
        minimo_monto=1_000_000,
        solo_bienes=True,
    )
    assert "8010" not in {n.familia for n in nichos}


def test_penaliza_la_concentracion_de_proveedores(almacen: Almacen) -> None:
    nichos = {
        n.familia: n
        for n in detectar_nichos(
            almacen.conexion(), minimo_adjudicaciones=2, minimo_monto=1_000_000
        )
    }
    fragmentada, monopolizada = nichos["4618"], nichos["5313"]

    assert fragmentada.n_proveedores == 6
    assert monopolizada.n_proveedores == 1
    assert monopolizada.share_lider == pytest.approx(1.0)
    assert monopolizada.hhi == pytest.approx(1.0)
    # A igualdad de volumen, la fragmentada debe rankear más alto.
    assert fragmentada.puntaje > monopolizada.puntaje
    assert any("líder" in m for m in monopolizada.motivos)


def test_calcula_dispersion_de_precios(almacen: Almacen) -> None:
    nichos = {
        n.familia: n
        for n in detectar_nichos(
            almacen.conexion(), minimo_adjudicaciones=2, minimo_monto=1_000_000
        )
    }
    fragmentada = nichos["4618"]

    assert fragmentada.precio_p25 < fragmentada.precio_mediano < fragmentada.precio_p75
    assert fragmentada.dispersion > 1
    # Un proveedor único a precio fijo no tiene dispersión.
    assert nichos["5313"].dispersion == pytest.approx(1.0)


def test_respeta_los_umbrales_minimos(almacen: Almacen) -> None:
    assert detectar_nichos(almacen.conexion(), minimo_adjudicaciones=999) == []
    assert detectar_nichos(almacen.conexion(), minimo_monto=1e15) == []


def test_base_vacia_devuelve_lista_vacia(tmp_path: Path) -> None:
    with Almacen(tmp_path / "vacia.db") as vacia:
        assert detectar_nichos(vacia.conexion()) == []
