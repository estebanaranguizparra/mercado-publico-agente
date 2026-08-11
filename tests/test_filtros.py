from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from mpagente.models import Licitacion, Perfil
from mpagente.pipeline.filtros import prefiltrar


@pytest.fixture
def perfil() -> Perfil:
    return Perfil.model_validate(
        {
            "nombre_empresa": "Test SpA",
            "palabras_clave_incluir": ["insumos", "epp", "adquisición"],
            "palabras_clave_excluir": ["obra civil", "consultoría"],
            "restricciones": {
                "monto_minimo_clp": 5_000_000,
                "monto_maximo_clp": 500_000_000,
                "dias_minimos_al_cierre": 5,
                "capital_trabajo_clp": 100_000_000,
                "exigir_coincidencia_palabras_clave": True,
            },
            "regiones_operacion": ["Región Metropolitana de Santiago"],
        }
    )


def _licitacion(**cambios) -> Licitacion:
    base = {
        "CodigoExterno": "1000-1-LE26",
        "Nombre": "Adquisición de insumos de aseo",
        "Descripcion": "Suministro de insumos para establecimientos",
        "Tipo": "LE",
        "MontoEstimado": 50_000_000,
        "FechaCierre": (datetime.now() + timedelta(days=20)).isoformat(timespec="seconds"),
        "Comprador": {"RegionUnidad": "Región Metropolitana de Santiago"},
    }
    base.update(cambios)
    return Licitacion.model_validate(base)


def test_aprueba_licitacion_que_calza(perfil: Perfil) -> None:
    resultado = prefiltrar(_licitacion(), perfil)
    assert resultado.aprobado
    assert resultado.puntaje > 50


def test_descarta_por_palabra_excluida(perfil: Perfil) -> None:
    lic = _licitacion(Nombre="Ejecución de obra civil en sector norte")
    resultado = prefiltrar(lic, perfil)
    assert not resultado.aprobado
    assert "excluidos" in resultado.motivos[0]


def test_descarta_por_cierre_inminente(perfil: Perfil) -> None:
    cierre = (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds")
    resultado = prefiltrar(_licitacion(FechaCierre=cierre), perfil)
    assert not resultado.aprobado


def test_descarta_por_monto_bajo(perfil: Perfil) -> None:
    resultado = prefiltrar(_licitacion(MontoEstimado=1_000_000), perfil)
    assert not resultado.aprobado


def test_descarta_si_no_hay_palabras_clave(perfil: Perfil) -> None:
    lic = _licitacion(
        Nombre="Arriendo de maquinaria pesada",
        Descripcion="Arriendo mensual de retroexcavadoras",
    )
    resultado = prefiltrar(lic, perfil)
    assert not resultado.aprobado


def test_ignora_tildes_al_comparar(perfil: Perfil) -> None:
    # "adquisición" en el perfil debe calzar con "ADQUISICION" sin tilde.
    lic = _licitacion(Nombre="ADQUISICION DE EPP", Descripcion="Compra de elementos")
    resultado = prefiltrar(lic, perfil)
    assert resultado.aprobado


def test_penaliza_monto_sobre_capital_de_trabajo(perfil: Perfil) -> None:
    holgado = prefiltrar(_licitacion(MontoEstimado=50_000_000), perfil)
    apretado = prefiltrar(_licitacion(MontoEstimado=400_000_000), perfil)
    assert apretado.aprobado
    assert any("capital de trabajo" in m for m in apretado.motivos)
    assert apretado.puntaje < holgado.puntaje + 20


def test_sin_fecha_de_cierre_se_descarta(perfil: Perfil) -> None:
    resultado = prefiltrar(_licitacion(FechaCierre=None), perfil)
    assert not resultado.aprobado
