"""Salida legible: tabla en consola e informe en Markdown."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.table import Table

from .models import Evaluacion, Licitacion

_COLOR_POR_ESTRATEGIA = {
    "IMPORTAR": "cyan",
    "COMPRAR_LOCAL": "green",
    "FABRICAR": "magenta",
    "STOCK_PROPIO": "bright_green",
    "MIXTA": "yellow",
    "DESCARTAR": "red",
}


def _color_puntaje(puntaje: int) -> str:
    if puntaje >= 75:
        return "bold green"
    if puntaje >= 55:
        return "yellow"
    return "red"


def _margen(evaluacion: Evaluacion) -> str:
    if evaluacion.margen_estimado_pct is None or evaluacion.margen_estimado_pct < 0:
        return "s/i"
    return f"{evaluacion.margen_estimado_pct:.0f}%"


def tabla_ranking(
    filas: list[tuple[Evaluacion, Licitacion]], console: Console | None = None
) -> None:
    """Imprime el ranking de oportunidades en consola."""
    console = console or Console()

    if not filas:
        console.print("[yellow]No hay evaluaciones todavía. Corre `mpagente analizar`.[/]")
        return

    tabla = Table(title="Oportunidades priorizadas", header_style="bold")
    tabla.add_column("Puntaje", justify="right", width=7)
    tabla.add_column("Código", width=14)
    tabla.add_column("Licitación", overflow="fold", max_width=44)
    tabla.add_column("Estrategia", width=14)
    tabla.add_column("Margen", justify="right", width=7)
    tabla.add_column("Monto CLP", justify="right", width=14)
    tabla.add_column("Cierra", justify="right", width=8)

    for evaluacion, lic in filas:
        dias = lic.dias_al_cierre
        cierre = f"{dias}d" if dias is not None else "s/f"
        monto = f"{lic.monto_estimado:,.0f}" if lic.monto_estimado else "s/i"
        color_estrategia = _COLOR_POR_ESTRATEGIA.get(evaluacion.estrategia, "white")

        tabla.add_row(
            f"[{_color_puntaje(evaluacion.puntaje_total)}]{evaluacion.puntaje_total}[/]",
            lic.codigo_externo,
            lic.nombre or "sin nombre",
            f"[{color_estrategia}]{evaluacion.estrategia}[/]",
            _margen(evaluacion),
            monto,
            cierre,
        )

    console.print(tabla)


def informe_markdown(
    filas: list[tuple[Evaluacion, Licitacion]],
    *,
    nombre_empresa: str,
    modelo: str,
) -> str:
    """Genera el informe completo, con una ficha por oportunidad."""
    generado = datetime.now().strftime("%d-%m-%Y %H:%M")
    lineas = [
        f"# Oportunidades en Mercado Público — {nombre_empresa}",
        "",
        f"Generado el {generado} · modelo `{modelo}` · {len(filas)} licitación(es) evaluada(s).",
        "",
    ]

    if not filas:
        lineas.append("_No hay evaluaciones registradas._")
        return "\n".join(lineas)

    # --- Resumen ---------------------------------------------------------- #
    lineas += [
        "## Resumen",
        "",
        "| Puntaje | Código | Licitación | Estrategia | Margen | Monto CLP | Cierra en |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for evaluacion, lic in filas:
        dias = lic.dias_al_cierre
        lineas.append(
            "| {p} | {c} | {n} | {e} | {m} | {mo} | {d} |".format(
                p=evaluacion.puntaje_total,
                c=lic.codigo_externo,
                n=(lic.nombre or "sin nombre").replace("|", "/"),
                e=evaluacion.estrategia,
                m=_margen(evaluacion),
                mo=f"{lic.monto_estimado:,.0f}" if lic.monto_estimado else "s/i",
                d=f"{dias} días" if dias is not None else "s/f",
            )
        )

    # --- Fichas ----------------------------------------------------------- #
    lineas += ["", "## Detalle por oportunidad", ""]
    for evaluacion, lic in filas:
        cierre = lic.fecha_cierre.strftime("%d-%m-%Y %H:%M") if lic.fecha_cierre else "sin fecha"
        p = evaluacion.puntajes

        lineas += [
            f"### {lic.codigo_externo} — {lic.nombre or 'sin nombre'}",
            "",
            f"**Puntaje {evaluacion.puntaje_total}/100 · {evaluacion.estrategia} · "
            f"confianza {evaluacion.confianza}**",
            "",
            evaluacion.resumen,
            "",
            f"- **Categoría:** {evaluacion.categoria_producto}",
            f"- **Organismo:** {lic.organismo or 'no informado'} "
            f"({lic.region or 'región no informada'})",
            f"- **Monto estimado:** "
            + (f"${lic.monto_estimado:,.0f} {lic.moneda or 'CLP'}" if lic.monto_estimado else "no publicado"),
            f"- **Cierre:** {cierre}"
            + (f" (faltan {lic.dias_al_cierre} días)" if lic.dias_al_cierre is not None else ""),
            f"- **Margen estimado:** {_margen(evaluacion)}",
            "",
            f"**Por qué {evaluacion.estrategia}:** {evaluacion.justificacion_estrategia}",
            "",
            "| Dimensión | Puntaje |",
            "| --- | ---: |",
            f"| Encaje con el rubro | {p.encaje_rubro} |",
            f"| Viabilidad de suministro | {p.viabilidad_suministro} |",
            f"| Margen | {p.margen} |",
            f"| Posición ante competencia | {p.competencia} |",
            f"| Ausencia de barreras | {p.barreras_entrada} |",
            f"| Calce de plazos | {p.calce_plazos} |",
            "",
        ]

        lineas += _seccion("Riesgos", evaluacion.riesgos)
        lineas += _seccion("Requisitos críticos", evaluacion.requisitos_criticos)
        lineas += _seccion("Próximos pasos", evaluacion.acciones_siguientes, numerada=True)
        lineas += _seccion("Supuestos", evaluacion.supuestos)
        lineas += ["---", ""]

    return "\n".join(lineas)


# --------------------------------------------------------------------------- #
# Nichos (búsqueda autónoma sobre el histórico)
# --------------------------------------------------------------------------- #


def _millones(monto: float) -> str:
    if monto >= 1_000_000_000:
        return f"{monto / 1_000_000_000:,.1f}MM"
    return f"{monto / 1_000_000:,.0f}M"


def tabla_nichos(nichos: list, console: Console | None = None) -> None:
    """Ranking de familias de producto en consola."""
    console = console or Console()

    tabla = Table(
        title="Nichos detectados en compras públicas",
        header_style="bold",
        expand=False,
        pad_edge=False,
        padding=(0, 1),
    )
    tabla.add_column("Pts", justify="right", width=3)
    tabla.add_column("Cód", width=4)
    tabla.add_column("Qué compra el Estado", overflow="ellipsis", max_width=30, no_wrap=True)
    tabla.add_column("Monto", justify="right", width=6)
    tabla.add_column("Adj", justify="right", width=4)
    tabla.add_column("Prv", justify="right", width=3)
    tabla.add_column("Líder", justify="right", width=5)
    tabla.add_column("Ofrt", justify="right", width=4)
    tabla.add_column("Disp", justify="right", width=5)

    for nicho in nichos:
        color = "bold green" if nicho.puntaje >= 65 else "yellow" if nicho.puntaje >= 50 else "white"
        tabla.add_row(
            f"[{color}]{nicho.puntaje:.0f}[/]",
            nicho.familia,
            nicho.etiqueta,
            _millones(nicho.monto_total),
            f"{nicho.n_adjudicaciones:,}",
            f"{nicho.n_proveedores:,}",
            f"{nicho.share_lider:.0%}",
            f"{nicho.oferentes_promedio:.1f}",
            f"{nicho.dispersion:.1f}×",
        )

    console.print(tabla)
    console.print(
        "[dim]Líder = share del proveedor mayor · Ofrt = oferentes promedio por licitación "
        "· Disp = cuánto paga el percentil 75 respecto del 25.[/]"
    )


def informe_nichos(nichos: list, *, periodos: list[tuple[str, int, int, int]]) -> str:
    """Informe Markdown con una ficha por nicho."""
    generado = datetime.now().strftime("%d-%m-%Y %H:%M")
    lineas = [
        "# Nichos detectados en compras públicas",
        "",
        f"Generado el {generado} · {len(nichos)} categorías sobre "
        f"{len(periodos)} mes(es) de historia.",
        "",
        "La unidad es la **familia UNSPSC** (4 dígitos). Todas las cifras salen de "
        "adjudicaciones reales publicadas vía OCDS.",
        "",
        "| Pts | UNSPSC | Categoría | Monto CLP | Adj. | Prov. | Líder | Oferentes | Dispersión |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for n in nichos:
        lineas.append(
            f"| {n.puntaje:.0f} | {n.familia} | {n.etiqueta.replace('|', '/')} | "
            f"{_millones(n.monto_total)} | {n.n_adjudicaciones:,} | {n.n_proveedores:,} | "
            f"{n.share_lider:.0%} | {n.oferentes_promedio:.1f} | {n.dispersion:.1f}× |"
        )

    lineas += ["", "## Detalle por nicho", ""]
    for n in nichos:
        lineas += [
            f"### {n.familia} — {n.etiqueta}",
            "",
            f"**Puntaje {n.puntaje:.0f}/100 · {n.segmento}**",
            "",
            f"- **Monto histórico:** ${n.monto_total:,.0f} CLP en {n.n_adjudicaciones:,} adjudicaciones",
            f"- **Ticket mediano:** ${n.ticket_mediano:,.0f} CLP",
            f"- **Proveedores distintos:** {n.n_proveedores} · líder con {n.share_lider:.0%} · HHI {n.hhi:.2f}",
            f"- **Compradores distintos:** {n.n_compradores}",
            f"- **Meses con actividad:** {n.meses_activos}",
            f"- **Oferentes por licitación:** {n.oferentes_promedio:.1f} en promedio",
            f"- **Precio unitario:** p25 ${n.precio_p25:,.0f} · mediana ${n.precio_mediano:,.0f} "
            f"· p75 ${n.precio_p75:,.0f} (dispersión {n.dispersion:.1f}×)",
            "",
        ]
        lineas += _seccion("Señales", n.motivos)
        lineas += ["---", ""]

    return "\n".join(lineas)


# --------------------------------------------------------------------------- #
# Demanda (lo que el Estado está pidiendo)
# --------------------------------------------------------------------------- #


def tabla_demanda(demandas: list, console: Console | None = None) -> None:
    """Ranking de categorías por lo que se está solicitando."""
    console = console or Console()

    tabla = Table(
        title="Lo que el Estado está pidiendo",
        header_style="bold",
        expand=False,
        pad_edge=False,
        padding=(0, 1),
    )
    tabla.add_column("Pts", justify="right", width=3)
    tabla.add_column("Cód", width=4)
    tabla.add_column("Qué se pide", overflow="ellipsis", max_width=34, no_wrap=True)
    tabla.add_column("Tipo", width=8)
    tabla.add_column("Llam", justify="right", width=4)
    tabla.add_column("Abrt", justify="right", width=4)
    tabla.add_column("Org", justify="right", width=4)
    tabla.add_column("Reg", justify="right", width=3)
    tabla.add_column("Ofrt", justify="right", width=4)

    for d in demandas:
        color = "bold green" if d.puntaje >= 65 else "yellow" if d.puntaje >= 50 else "white"
        tabla.add_row(
            f"[{color}]{d.puntaje:.0f}[/]",
            d.familia,
            d.etiqueta,
            "servicio" if d.es_servicio else "bien",
            f"{d.n_solicitudes:,}",
            f"[bold green]{d.vigentes}[/]" if d.vigentes else "[dim]0[/]",
            f"{d.n_compradores:,}",
            f"{d.regiones}",
            f"{d.oferentes_promedio:.1f}" if d.oferentes_promedio else "[dim]—[/]",
        )

    console.print(tabla)
    console.print(
        "[dim]Llam = llamados publicados · Abrt = todavía abiertos · Org = organismos "
        "distintos · Ofrt = oferentes promedio según el histórico adjudicado.[/]"
    )


def tabla_vigentes(filas: list, console: Console | None = None) -> None:
    """Llamados cuyo plazo de postulación sigue corriendo."""
    console = console or Console()

    tabla = Table(
        title="Llamados abiertos ahora",
        header_style="bold",
        expand=False,
        pad_edge=False,
        padding=(0, 1),
    )
    tabla.add_column("Cierra", width=13, no_wrap=True)
    tabla.add_column("Código", width=16, no_wrap=True)
    tabla.add_column("Qué piden", overflow="ellipsis", max_width=44, no_wrap=True)
    tabla.add_column("Organismo", overflow="ellipsis", max_width=28, no_wrap=True)
    tabla.add_column("Items", justify="right", width=5)

    ahora = datetime.now()
    for fila in filas:
        cierre = datetime.fromisoformat(fila["cierre"])
        dias = (cierre - ahora).days
        # Menos de tres días es prácticamente inalcanzable si hay que preparar
        # documentación; se marca para que no compita visualmente con el resto.
        color = "red" if dias <= 2 else "yellow" if dias <= 7 else "green"
        tabla.add_row(
            f"[{color}]{cierre.strftime('%d-%m')} ({dias}d)[/]",
            fila["codigo"],
            fila["titulo"] or "[dim]sin título[/]",
            fila["comprador"] or "[dim]—[/]",
            str(fila["n_items"]),
        )

    console.print(tabla)


def informe_demanda(demandas: list, *, meses: int) -> str:
    """Informe Markdown con una ficha por categoría demandada."""
    generado = datetime.now().strftime("%d-%m-%Y %H:%M")
    lineas = [
        "# Lo que el Estado está pidiendo",
        "",
        f"Generado el {generado} · {len(demandas)} categorías sobre "
        f"{meses} mes(es) de llamados publicados.",
        "",
        "La unidad es la **familia UNSPSC** (4 dígitos) y la fuente son las fichas "
        "`tender` de OCDS: lo solicitado, no lo adjudicado. El precio de referencia "
        "y los oferentes provienen del histórico ya adjudicado de esa misma familia.",
        "",
        "| Pts | UNSPSC | Categoría | Tipo | Llamados | Abiertos | Organismos | Oferentes |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for d in demandas:
        oferentes = f"{d.oferentes_promedio:.1f}" if d.oferentes_promedio else "—"
        lineas.append(
            f"| {d.puntaje:.0f} | {d.familia} | {d.etiqueta.replace('|', '/')} | "
            f"{'servicio' if d.es_servicio else 'bien'} | {d.n_solicitudes:,} | "
            f"{d.vigentes} | {d.n_compradores:,} | {oferentes} |"
        )

    lineas += ["", "## Detalle por categoría", ""]
    for d in demandas:
        lineas += [
            f"### {d.familia} — {d.etiqueta}",
            "",
            f"**Puntaje {d.puntaje:.0f}/100 · {d.segmento} · "
            f"{'servicio' if d.es_servicio else 'bien'}**",
            "",
            f"- **Llamados publicados:** {d.n_solicitudes:,} ({d.n_items:,} líneas)",
            f"- **Todavía abiertos:** {d.vigentes}",
            f"- **Organismos distintos:** {d.n_compradores} · en {d.regiones} región(es)",
            f"- **Meses con actividad:** {d.meses_activos}",
        ]
        if d.precio_mediano:
            lineas.append(
                f"- **Precio unitario de referencia:** ${d.precio_mediano:,.0f} CLP "
                f"(mediana histórica adjudicada)"
            )
        if d.valor_estimado:
            lineas.append(
                f"- **Valor estimado de lo pedido:** ${d.valor_estimado:,.0f} CLP "
                "(cantidad solicitada × precio de referencia)"
            )
        if d.oferentes_promedio:
            lineas.append(f"- **Competencia histórica:** {d.oferentes_promedio:.1f} oferentes")
        lineas.append("")
        lineas += _seccion("Señales", d.motivos)
        lineas += ["---", ""]

    return "\n".join(lineas)


# --------------------------------------------------------------------------- #
# Pipeline comercial
# --------------------------------------------------------------------------- #

# El color codifica en qué punto del embudo está, no una preferencia.
_COLOR_ESTADO = {
    "nueva": "white",
    "analizada": "cyan",
    "descartada": "bright_black",
    "en_preparacion": "yellow",
    "ofertada": "blue",
    "ganada": "bold green",
    "perdida": "red",
}


def tabla_pipeline(oportunidades: list, console: Console | None = None) -> None:
    """Las oportunidades registradas, con su estado y el porqué."""
    console = console or Console()

    if not oportunidades:
        console.print("[dim]No hay oportunidades con ese filtro.[/]")
        return

    tabla = Table(header_style="bold", expand=False, pad_edge=False, padding=(0, 1))
    tabla.add_column("Estado", width=15)
    tabla.add_column("Clave", width=16, no_wrap=True)
    tabla.add_column("Título", overflow="ellipsis", max_width=34, no_wrap=True)
    tabla.add_column("Modalidad", width=15)
    tabla.add_column("Motivo", overflow="ellipsis", max_width=40, no_wrap=True)

    for o in oportunidades:
        color = _COLOR_ESTADO.get(o["estado"], "white")
        tabla.add_row(
            f"[{color}]{o['estado']}[/]",
            o["clave"],
            o["titulo"] or "[dim]—[/]",
            o["modalidad"] or "[dim]—[/]",
            o["motivo"] or "[dim]—[/]",
        )

    console.print(tabla)


def _seccion(titulo: str, elementos: list[str], *, numerada: bool = False) -> list[str]:
    if not elementos:
        return []
    lineas = [f"**{titulo}**", ""]
    for indice, elemento in enumerate(elementos, start=1):
        prefijo = f"{indice}." if numerada else "-"
        lineas.append(f"{prefijo} {elemento}")
    lineas.append("")
    return lineas
