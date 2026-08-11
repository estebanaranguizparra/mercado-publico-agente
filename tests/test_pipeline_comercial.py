"""Pipeline comercial: estados, historial y memoria de lo decidido."""

from __future__ import annotations

from pathlib import Path

import pytest

from mpagente.storage import Almacen


@pytest.fixture
def almacen(tmp_path: Path) -> Almacen:
    with Almacen(tmp_path / "pipeline.db") as a:
        yield a


def test_registra_y_actualiza_sin_borrar_lo_previo(almacen: Almacen) -> None:
    almacen.guardar_oportunidad(
        "123-4-LE26",
        estado="analizada",
        modalidad="importar",
        motivo="Margen suficiente contra el p25.",
        autor="analista-leads",
    )
    # Otro agente actualiza solo el precio: el motivo debe sobrevivir.
    almacen.guardar_oportunidad(
        "123-4-LE26", estado="ofertada", precio_ofertado=4_500_000, autor="propuesta-economica"
    )

    o = almacen.oportunidad("123-4-LE26")
    assert o is not None
    assert o["estado"] == "ofertada"
    assert o["precio_ofertado"] == 4_500_000
    assert o["motivo"] == "Margen suficiente contra el p25."
    assert o["modalidad"] == "importar"


def test_cada_cambio_deja_evento(almacen: Almacen) -> None:
    almacen.guardar_oportunidad("1-1-LE26", estado="nueva", autor="buscador-leads")
    almacen.guardar_oportunidad("1-1-LE26", estado="analizada", nota="Viable", autor="analista-leads")
    almacen.guardar_oportunidad("1-1-LE26", estado="descartada", nota="Sin garantía")

    eventos = almacen.eventos_oportunidad("1-1-LE26")
    assert [e["estado"] for e in eventos] == ["nueva", "analizada", "descartada"]
    assert eventos[1]["autor"] == "analista-leads"
    assert eventos[2]["nota"] == "Sin garantía"


def test_rechaza_estado_desconocido(almacen: Almacen) -> None:
    with pytest.raises(ValueError, match="desconocido"):
        almacen.guardar_oportunidad("x", estado="inventado")


def test_rechaza_campos_desconocidos(almacen: Almacen) -> None:
    with pytest.raises(ValueError, match="Campos desconocidos"):
        almacen.guardar_oportunidad("x", estado="nueva", presupuesto=100)


def test_el_embudo_cuenta_conversion_sobre_lo_resuelto(almacen: Almacen) -> None:
    almacen.guardar_oportunidad("g1", estado="ganada", precio_ofertado=1_000_000, margen_pct=15)
    almacen.guardar_oportunidad("g2", estado="ganada", precio_ofertado=3_000_000, margen_pct=25)
    almacen.guardar_oportunidad("p1", estado="perdida")
    # Una oferta todavía en curso no cuenta como derrota.
    almacen.guardar_oportunidad("o1", estado="ofertada")

    r = almacen.resumen_pipeline()
    assert r["ganadas"] == 2
    assert r["monto_ganado"] == 4_000_000
    assert r["tasa_conversion"] == pytest.approx(2 / 3)
    assert r["margen_medio"] == pytest.approx(20)


def test_sin_nada_resuelto_la_conversion_es_indefinida(almacen: Almacen) -> None:
    almacen.guardar_oportunidad("o1", estado="ofertada")
    assert almacen.resumen_pipeline()["tasa_conversion"] is None


def test_busca_antecedentes_por_familia(almacen: Almacen) -> None:
    almacen.guardar_oportunidad(
        "familia:7213",
        estado="descartada",
        tipo="categoria",
        familia="7213",
        motivo="Exige experiencia acreditada.",
    )
    almacen.guardar_oportunidad("otra", estado="nueva", familia="4213")

    de_obras = almacen.listar_oportunidades(familia="7213")
    assert len(de_obras) == 1
    assert de_obras[0]["motivo"] == "Exige experiencia acreditada."
    assert almacen.listar_oportunidades(estado="descartada")[0]["clave"] == "familia:7213"


def test_contexto_viaja_sin_la_base_completa(tmp_path: Path) -> None:
    """El caso de la nube: una base vacía debe quedar utilizable con solo el JSON."""
    from mpagente.ocds_parser import Adjudicacion, ItemAdjudicado
    from datetime import datetime

    origen = Almacen(tmp_path / "origen.db")
    origen.guardar_adjudicaciones([
        Adjudicacion(
            award_uid=f"A{i}:1", ocid=f"o{i}", codigo=f"A{i}", modalidad="licitacion",
            titulo="x", estado="8-Adjudicada", fecha=datetime(2026, 5, 10),
            comprador="Org", comprador_id="C1", region="RM",
            proveedor=f"P{i}", proveedor_id=f"P{i}", monto=5_000_000, moneda="CLP",
            n_oferentes=4,
            items=[ItemAdjudicado("1", "43211500", "PC", 10, "Unidad", 500_000 + i * 1000, "CLP")],
        )
        for i in range(4)
    ])
    origen.guardar_oportunidad(
        "familia:4321", estado="analizada", tipo="categoria", familia="4321",
        motivo="Competido pero sin barrera.", autor="analista-leads",
    )

    contexto = origen.exportar_contexto()
    assert len(contexto["familias"]) == 1
    assert contexto["familias"][0]["familia"] == "4321"

    # Base nueva y vacía, como el sandbox de una corrida en la nube.
    destino = Almacen(tmp_path / "nube.db")
    resultado = destino.importar_contexto(contexto)
    assert resultado["familias"] == 1
    assert resultado["oportunidades"] == 1

    ctx = destino.contexto_familia("4321")
    assert ctx["oferentes_promedio"] == 4
    assert ctx["precio_mediano"] > 0
    assert ctx["origen"] == "importado"
    # Y conserva la memoria de lo ya decidido.
    assert destino.oportunidad("familia:4321")["motivo"] == "Competido pero sin barrera."


def test_importar_contexto_dos_veces_no_duplica(tmp_path: Path) -> None:
    origen = Almacen(tmp_path / "o.db")
    origen.guardar_oportunidad("x-1", estado="analizada", motivo="m", autor="a")
    contexto = origen.exportar_contexto()

    destino = Almacen(tmp_path / "d.db")
    destino.importar_contexto(contexto)
    destino.importar_contexto(contexto)

    assert len(destino.listar_oportunidades()) == 1
    assert len(destino.eventos_oportunidad("x-1")) == 1


def test_reexportar_sin_base_cruda_no_borra_el_historico(tmp_path: Path) -> None:
    """El ciclo de la nube: importar y volver a exportar debe conservar precios.

    Sin esto, la segunda corrida nocturna commitearía un contexto vacío y el
    sistema perdería para siempre la referencia de precio y competencia.
    """
    contexto = {
        "familias": [
            {
                "familia": "4321", "n_adjudicaciones": 12, "n_proveedores": 5,
                "oferentes_promedio": 8.0, "precio_p25": 100.0,
                "precio_mediano": 500.0, "precio_p75": 900.0, "monto_total": 9e6,
            }
        ],
        "oportunidades": [],
        "eventos": [],
    }

    for _ in range(3):
        almacen = Almacen(tmp_path / "nube.db")
        almacen.importar_contexto(contexto)
        contexto = almacen.exportar_contexto()
        almacen.cerrar()
        (tmp_path / "nube.db").unlink()

        assert len(contexto["familias"]) == 1
        assert contexto["familias"][0]["precio_mediano"] == 500.0
        assert contexto["familias"][0]["oferentes_promedio"] == 8.0
