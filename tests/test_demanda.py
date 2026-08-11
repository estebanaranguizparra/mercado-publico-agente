"""Parseo de llamados y agregación de la demanda. Sin red ni credenciales."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mpagente.ocds_parser import parsear_solicitud
from mpagente.pipeline.demanda import agregar_demanda, meses_recientes
from mpagente.storage import Almacen


def _payload_tender(
    codigo: str,
    *,
    unspsc: str,
    comprador: str = "CL-MP-1",
    cierre: datetime | None = None,
    publicada: datetime | None = None,
) -> dict:
    cierre = cierre or datetime(2026, 7, 20, 15, 0)
    publicada = publicada or datetime(2026, 7, 3, 14, 0)
    return {
        "releases": [
            {
                "ocid": f"ocds-70d2nz-{codigo}",
                "date": publicada.isoformat() + "Z",
                "parties": [
                    {
                        "name": "SERVICIO DE SALUD | Abastecimiento",
                        "id": comprador,
                        "address": {"region": "Región de Valparaíso"},
                        "roles": ["procuringEntity", "buyer"],
                    }
                ],
                "tender": {
                    "id": codigo,
                    "title": "Suministro de prueba",
                    "description": "Detalle del llamado",
                    "status": "active",
                    "statusDetails": "5-Publicada",
                    "procurementMethodDetails": "Licitación Pública (LE)",
                    "tenderPeriod": {
                        "startDate": publicada.isoformat() + "Z",
                        "endDate": cierre.isoformat() + "Z",
                    },
                    "items": [
                        {
                            "id": "1",
                            "description": "Equipos médicos / Insumos / Guantes de nitrilo",
                            "quantity": 100.0,
                            "unit": {"name": "Caja"},
                            "classification": {"id": unspsc, "scheme": "UNSPSC"},
                        }
                    ],
                },
            }
        ]
    }


def test_parsea_un_llamado_abierto() -> None:
    solicitud = parsear_solicitud(_payload_tender("123-45-LE26", unspsc="42132203"), "licitacion")

    assert solicitud is not None
    assert solicitud.codigo == "123-45-LE26"
    assert solicitud.comprador == "SERVICIO DE SALUD"
    assert solicitud.region == "Región de Valparaíso"
    assert solicitud.tipo_proceso == "Licitación Pública (LE)"
    assert solicitud.anio == 2026 and solicitud.mes == 7
    assert len(solicitud.items) == 1
    assert solicitud.items[0].familia_unspsc == "4213"
    assert solicitud.items[0].cantidad == 100.0


def test_la_vigencia_se_decide_por_la_fecha_no_por_el_estado() -> None:
    """La ficha dice "5-Publicada" incluso cuando el plazo ya venció."""
    vencido = parsear_solicitud(
        _payload_tender("1-1-LE26", unspsc="42132203", cierre=datetime(2020, 1, 1)), "licitacion"
    )
    abierto = parsear_solicitud(
        _payload_tender(
            "2-2-LE26", unspsc="42132203", cierre=datetime.now() + timedelta(days=5)
        ),
        "licitacion",
    )

    assert vencido is not None and abierto is not None
    assert vencido.estado == abierto.estado == "5-Publicada"
    assert not vencido.vigente()
    assert abierto.vigente()


def test_ignora_clasificaciones_que_no_son_unspsc() -> None:
    payload = _payload_tender("3-3-LE26", unspsc="42132203")
    payload["releases"][0]["tender"]["items"][0]["classification"]["scheme"] = "CPV"

    solicitud = parsear_solicitud(payload, "licitacion")
    assert solicitud is not None
    assert solicitud.items[0].unspsc is None
    assert solicitud.items[0].familia_unspsc is None


def test_payload_sin_tender_devuelve_none() -> None:
    assert parsear_solicitud({"releases": [{"ocid": "x", "awards": []}]}, "licitacion") is None
    assert parsear_solicitud({}, "licitacion") is None


def test_meses_recientes_incluye_el_mes_en_curso() -> None:
    from datetime import date

    periodos = meses_recientes(3, hasta=date(2026, 8, 10))
    assert periodos == [(2026, 8), (2026, 7), (2026, 6)]


@pytest.fixture
def almacen(tmp_path: Path) -> Almacen:
    with Almacen(tmp_path / "demanda.db") as a:
        futuro = datetime.now() + timedelta(days=10)
        solicitudes = []
        # Familia 4213 (bien): 5 llamados, compradores distintos, uno abierto.
        for i in range(5):
            solicitudes.append(
                parsear_solicitud(
                    _payload_tender(
                        f"B{i}-1-LE26",
                        unspsc="42132203",
                        comprador=f"CL-MP-{i}",
                        cierre=futuro if i == 0 else datetime(2026, 7, 20),
                        publicada=datetime(2026, 6 + (i % 2), 3),
                    ),
                    "licitacion",
                )
            )
        # Familia 8010 (servicio): 4 llamados, un solo comprador.
        for i in range(4):
            solicitudes.append(
                parsear_solicitud(
                    _payload_tender(
                        f"S{i}-1-LE26",
                        unspsc="80101504",
                        comprador="CL-MP-99",
                        cierre=datetime(2026, 7, 15),
                    ),
                    "licitacion",
                )
            )
        # Familia 5313: bajo el umbral, no debe aparecer.
        solicitudes.append(
            parsear_solicitud(_payload_tender("U1-1-LE26", unspsc="53131608"), "licitacion")
        )
        a.guardar_solicitudes([s for s in solicitudes if s])
        yield a


def test_agrega_bienes_y_servicios_por_defecto(almacen: Almacen) -> None:
    familias = {d.familia for d in agregar_demanda(almacen.conexion(), minimo_solicitudes=3)}
    assert familias == {"4213", "8010"}


def test_puede_filtrar_por_tipo(almacen: Almacen) -> None:
    solo_bienes = agregar_demanda(almacen.conexion(), minimo_solicitudes=3, incluir="bienes")
    solo_servicios = agregar_demanda(almacen.conexion(), minimo_solicitudes=3, incluir="servicios")

    assert {d.familia for d in solo_bienes} == {"4213"}
    assert {d.familia for d in solo_servicios} == {"8010"}
    assert solo_servicios[0].es_servicio


def test_cuenta_compradores_vigentes_y_etiqueta(almacen: Almacen) -> None:
    demandas = {d.familia: d for d in agregar_demanda(almacen.conexion(), minimo_solicitudes=3)}
    bien = demandas["4213"]

    assert bien.n_solicitudes == 5
    assert bien.n_compradores == 5
    assert bien.vigentes == 1
    # La descripción viene jerárquica; se queda con la última rama.
    assert bien.etiqueta == "Guantes de nitrilo"
    # La amplitud de compradores debe rankear el bien sobre el servicio cautivo.
    assert bien.puntaje > demandas["8010"].puntaje


def test_solo_vigentes_deja_fuera_lo_cerrado(almacen: Almacen) -> None:
    demandas = agregar_demanda(
        almacen.conexion(), minimo_solicitudes=1, solo_vigentes=True
    )
    assert {d.familia for d in demandas} == {"4213"}
    assert demandas[0].n_solicitudes == 1


def test_sin_historico_no_hay_precio_de_referencia(almacen: Almacen) -> None:
    demandas = agregar_demanda(almacen.conexion(), minimo_solicitudes=3)
    assert all(d.precio_mediano == 0.0 for d in demandas)
    assert all(any("Sin historial" in m for m in d.motivos) for d in demandas)


def test_la_etiqueta_ignora_ramas_inutiles() -> None:
    from mpagente.pipeline.demanda import _etiqueta_item

    assert _etiqueta_item("Equipos / Insumos / Guantes de nitrilo") == "Guantes de nitrilo"
    # Una última rama numérica o demasiado corta no nombra nada: se retrocede.
    assert _etiqueta_item("Ganado vivo / Bovinos de engorda / 3") == "Bovinos de engorda"
    assert _etiqueta_item("Papelería / Tóner / AB") == "Tóner"
    assert _etiqueta_item("Servicio único") == "Servicio único"


def test_base_vacia_devuelve_lista_vacia(tmp_path: Path) -> None:
    with Almacen(tmp_path / "vacia.db") as vacia:
        assert agregar_demanda(vacia.conexion()) == []


def test_ficha_de_solicitud_trae_sus_items(almacen: Almacen) -> None:
    ficha = almacen.ficha_solicitud("B0-1-LE26")

    assert ficha is not None
    assert ficha["comprador"] == "SERVICIO DE SALUD"
    assert len(ficha["items"]) == 1
    assert ficha["items"][0]["familia"] == "4213"


def test_ficha_de_codigo_inexistente_es_none(almacen: Almacen) -> None:
    assert almacen.ficha_solicitud("NO-EXISTE") is None


def test_contexto_de_familia_sin_historial_no_inventa_precio(almacen: Almacen) -> None:
    ctx = almacen.contexto_familia("4213")

    # Hay llamados pero ninguna adjudicación: la demanda se ve, el precio no.
    assert ctx["n_llamados"] == 5
    assert ctx["vigentes"] == 1
    assert ctx["n_adjudicaciones"] == 0
    assert ctx["precio_mediano"] == 0.0
    assert ctx["proveedores_top"] == []


def test_dias_minimos_descarta_los_que_cierran_pronto(almacen: Almacen) -> None:
    # El único vigente del fixture cierra en 10 días.
    assert len(almacen.solicitudes_vigentes(dias_minimos=5)) == 1
    assert len(almacen.solicitudes_vigentes(dias_minimos=20)) == 0
