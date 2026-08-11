"""Parseo de fichas OCDS y clasificación del estado de un proceso."""

from __future__ import annotations

from mpagente.ocds_parser import (
    ADJUDICADO,
    CERRADO_SIN_ADJUDICAR,
    PENDIENTE,
    clasificar_proceso,
    parsear_adjudicaciones,
)
from mpagente.pipeline.recoleccion import meses_hacia_atras


def _ficha(**cambios) -> dict:
    premio = {
        "id": "9543850",
        "title": "Adquisición de guantes",
        "status": "active",
        "statusDetails": "8-Adjudicada",
        "date": "2025-07-23T10:38:20Z",
        "value": {"amount": 4550000.0, "currency": "CLP"},
        "suppliers": [{"name": "FESCOP SpA | FESCOP", "id": "CL-MP-970130"}],
        "items": [
            {
                "id": "42877643",
                "description": "Guantes de nitrilo caja 100 unidades",
                "quantity": 200.0,
                "unit": {"name": "Caja", "value": {"amount": 22750.0, "currency": "CLP"}},
                "classification": {"id": "46181504", "scheme": "UNSPSC"},
            }
        ],
    }
    premio.update(cambios)
    return {
        "releases": [
            {
                "ocid": "ocds-70d2nz-5061-55-L125",
                "date": "2025-07-23T10:38:20Z",
                "parties": [
                    {
                        "name": "I MUNICIPALIDAD DE TEMUCO | Municipalidad",
                        "id": "CL-MP-5974",
                        "roles": ["procuringEntity", "buyer"],
                        "address": {"region": "Región de la Araucanía "},
                    },
                    {"name": "Competidor 1", "id": "CL-MP-1", "roles": ["tenderer"]},
                    {"name": "Competidor 2", "id": "CL-MP-2", "roles": ["tenderer"]},
                    {"name": "FESCOP SpA", "id": "CL-MP-970130", "roles": ["supplier", "tenderer"]},
                ],
                "awards": [premio],
            }
        ]
    }


def test_extrae_los_campos_que_importan() -> None:
    (adj,) = parsear_adjudicaciones(_ficha(), "licitacion")

    assert adj.codigo == "5061-55-L125"
    assert adj.award_uid == "5061-55-L125:9543850"
    assert adj.proveedor == "FESCOP SpA"
    assert adj.monto == 4550000.0
    assert adj.comprador == "I MUNICIPALIDAD DE TEMUCO"
    assert adj.region == "Región de la Araucanía"  # sin el espacio final del origen
    assert adj.anio == 2025 and adj.mes == 7


def test_cuenta_los_oferentes_incluido_el_ganador() -> None:
    (adj,) = parsear_adjudicaciones(_ficha(), "licitacion")
    # Tres partes tienen rol tenderer; el ganador también compitió.
    assert adj.n_oferentes == 3


def test_extrae_items_con_unspsc_y_precio_unitario() -> None:
    (adj,) = parsear_adjudicaciones(_ficha(), "licitacion")
    (item,) = adj.items

    assert item.unspsc == "46181504"
    assert item.familia_unspsc == "4618"
    assert item.cantidad == 200.0
    assert item.precio_unitario == 22750.0


def test_ignora_clasificaciones_que_no_son_unspsc() -> None:
    ficha = _ficha()
    ficha["releases"][0]["awards"][0]["items"][0]["classification"] = {
        "id": "ABC",
        "scheme": "OTRO",
    }
    (adj,) = parsear_adjudicaciones(ficha, "licitacion")
    assert adj.items[0].unspsc is None


def test_un_proceso_con_varios_ganadores_produce_varias_filas() -> None:
    ficha = _ficha()
    segundo = dict(ficha["releases"][0]["awards"][0])
    segundo["id"] = "9543851"
    segundo["suppliers"] = [{"name": "Otra SpA", "id": "CL-MP-2"}]
    ficha["releases"][0]["awards"].append(segundo)

    adjudicaciones = parsear_adjudicaciones(ficha, "licitacion")
    assert len(adjudicaciones) == 2
    assert len({a.award_uid for a in adjudicaciones}) == 2


# --- Clasificación del estado del proceso ---------------------------------- #


def test_clasifica_como_adjudicado() -> None:
    ficha = _ficha()
    assert clasificar_proceso(parsear_adjudicaciones(ficha, "licitacion"), ficha) == ADJUDICADO


def test_clasifica_desierta_como_terminal() -> None:
    ficha = _ficha(
        statusDetails="7-Desierta (o art. 3 ó 9 Ley 19.886)", suppliers=[], value={}
    )
    estado = clasificar_proceso(parsear_adjudicaciones(ficha, "licitacion"), ficha)
    assert estado == CERRADO_SIN_ADJUDICAR


def test_proceso_abierto_queda_pendiente() -> None:
    # La API responde igual aunque el proceso siga abierto: premio sin
    # proveedor ni monto. Debe quedar pendiente para volver a consultarlo.
    ficha = _ficha(statusDetails=None, status=None, suppliers=[], value={})
    estado = clasificar_proceso(parsear_adjudicaciones(ficha, "licitacion"), ficha)
    assert estado == PENDIENTE


def test_ficha_sin_releases_no_revienta() -> None:
    assert parsear_adjudicaciones({}, "licitacion") == []
    assert clasificar_proceso([], {}) == PENDIENTE


# --- Ventana de meses ------------------------------------------------------ #


def test_meses_hacia_atras_excluye_el_mes_en_curso() -> None:
    from datetime import date

    periodos = meses_hacia_atras(3, hasta=date(2026, 3, 1))
    assert periodos == [(2026, 2), (2026, 1), (2025, 12)]
