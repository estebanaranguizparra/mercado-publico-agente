from __future__ import annotations

from pathlib import Path

from mpagente.clients import ClienteMock
from mpagente.config import cargar_perfil
from mpagente.models import Evaluacion
from mpagente.pipeline import ingestar, prefiltrar
from mpagente.pipeline.scoring import esquema_evaluacion
from mpagente.prompts import render_licitacion, render_perfil
from mpagente.reporte import informe_markdown
from mpagente.storage import Almacen

RAIZ = Path(__file__).resolve().parents[1]


def test_mock_entrega_licitaciones_con_detalle() -> None:
    cliente = ClienteMock()
    licitaciones = cliente.listar()
    assert len(licitaciones) >= 5
    assert all(lic.es_detalle for lic in licitaciones)
    assert all(lic.dias_al_cierre is not None for lic in licitaciones)


def test_ingesta_guarda_y_no_duplica(tmp_path: Path) -> None:
    cliente = ClienteMock()
    with Almacen(tmp_path / "test.db") as almacen:
        primera = ingestar(cliente, almacen)
        assert primera.guardadas == primera.listadas

        segunda = ingestar(cliente, almacen)
        assert segunda.guardadas == 0
        assert segunda.omitidas_por_cache == primera.guardadas


def test_prefiltro_separa_el_ruido_del_perfil_de_ejemplo() -> None:
    perfil = cargar_perfil(RAIZ / "perfil.ejemplo.json")
    licitaciones = ClienteMock().listar()
    resultados = {lic.codigo_externo: prefiltrar(lic, perfil) for lic in licitaciones}

    # Obra civil y consultoría están en las palabras excluidas del perfil.
    assert not resultados["4501-77-LP26"].aprobado
    assert not resultados["7890-21-LE26"].aprobado
    # El tóner queda bajo el monto mínimo declarado.
    assert not resultados["6788-3-L126"].aprobado
    # EPP e insumos de aseo son el rubro central.
    assert resultados["1509-45-LP26"].aprobado
    assert resultados["2345-88-LR26"].aprobado


def test_esquema_es_compatible_con_salidas_estructuradas() -> None:
    esquema = esquema_evaluacion()

    def revisar(nodo: object) -> None:
        if isinstance(nodo, dict):
            if nodo.get("type") == "object":
                assert nodo.get("additionalProperties") is False
            for prohibida in ("minimum", "maximum", "default", "pattern"):
                assert prohibida not in nodo
            for valor in nodo.values():
                revisar(valor)
        elif isinstance(nodo, list):
            for valor in nodo:
                revisar(valor)

    revisar(esquema)
    assert "codigo_externo" in esquema["properties"]
    assert set(esquema["required"]) >= {"estrategia", "puntaje_total", "puntajes"}


def test_prompts_incluyen_lo_esencial() -> None:
    perfil = cargar_perfil(RAIZ / "perfil.ejemplo.json")
    lic = ClienteMock().detalle("1509-45-LP26")
    assert lic is not None

    texto_perfil = render_perfil(perfil)
    assert perfil.nombre_empresa in texto_perfil
    assert "Margen bruto mínimo" in texto_perfil

    texto_lic = render_licitacion(lic)
    assert "1509-45-LP26" in texto_lic
    assert "Zapato de seguridad" in texto_lic


def test_informe_markdown_se_genera(tmp_path: Path) -> None:
    lic = ClienteMock().detalle("1509-45-LP26")
    assert lic is not None

    evaluacion = Evaluacion(
        codigo_externo=lic.codigo_externo,
        resumen="Compra de EPP para 1.200 funcionarios.",
        categoria_producto="Elementos de protección personal",
        estrategia="IMPORTAR",
        justificacion_estrategia="El plazo de 90 días permite traer volumen desde China.",
        puntaje_total=82,
        puntajes={
            "encaje_rubro": 90,
            "viabilidad_suministro": 80,
            "margen": 78,
            "competencia": 60,
            "barreras_entrada": 70,
            "calce_plazos": 85,
        },
        margen_estimado_pct=26.0,
        confianza="media",
        riesgos=["La certificación EN 374 exige ensayo de laboratorio acreditado."],
        requisitos_criticos=["Boleta de garantía de seriedad de la oferta."],
        acciones_siguientes=["Cotizar con dos proveedores de Guangzhou."],
        supuestos=["Se asume tipo de cambio de 950 CLP por dólar."],
    )

    texto = informe_markdown(
        [(evaluacion, lic)], nombre_empresa="Comercializadora Andes SpA", modelo="claude-opus-5"
    )
    assert "# Oportunidades en Mercado Público" in texto
    assert "IMPORTAR" in texto
    assert "26%" in texto

    destino = tmp_path / "informe.md"
    destino.write_text(texto, encoding="utf-8")
    assert destino.read_text(encoding="utf-8").startswith("# Oportunidades")
