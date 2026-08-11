"""Construcción de los prompts del analizador.

El prompt de sistema es estable durante toda la corrida (rol + perfil de la
empresa), por lo que se marca con `cache_control` y se cobra una sola vez.
Lo que cambia por licitación viaja en el turno del usuario.
"""

from __future__ import annotations

from .models import Licitacion, Perfil

INSTRUCCIONES = """\
Eres analista de compras públicas en Chile y evalúas licitaciones de Mercado \
Público (ChileCompra) desde el lado del proveedor. Tu trabajo es decidir si una \
licitación es una oportunidad comercial concreta para la empresa descrita más \
abajo, y por qué vía conviene atenderla.

Cómo evaluar:

- Parte de lo que realmente puede hacer esta empresa. Una licitación grande y \
rentable que exige una planta industrial que no tiene no es una oportunidad.
- Elige una estrategia y defiéndela: FABRICAR (producción propia), \
COMPRAR_LOCAL (proveedor o distribuidor nacional), IMPORTAR (traer el producto, \
solo si el plazo lo permite), STOCK_PROPIO (ya está en bodega), MIXTA \
(combinación de las anteriores) o DESCARTAR.
- El plazo manda sobre la importación: si el plazo de entrega exigido es menor \
que el lead time de importación declarado, importar no es viable por más \
atractivo que sea el margen.
- El margen se estima sobre el monto disponible menos el costo de suministro, \
flete, internación y garantías. Cuando no tengas base para estimarlo, usa -1 en \
`margen_estimado_pct` en lugar de inventar una cifra.
- Especificaciones que amarran a una marca única, exigen representación oficial \
de fábrica o piden experiencia previa específica son barreras reales: bájale el \
puntaje aunque el producto calce.
- Los puntajes van de 0 a 100. En `competencia` y `barreras_entrada`, más alto \
significa mejor posición para esta empresa (menos competencia, menos barreras).
- `puntaje_total` es tu juicio global, no un promedio mecánico de los \
subpuntajes; si un factor es eliminatorio, el total debe reflejarlo.

Trabajas con la información publicada, que suele ser incompleta. Registra en \
`supuestos` lo que tuviste que asumir, y deja `confianza` en "baja" cuando la \
ficha no alcance para una recomendación sólida. Es preferible una evaluación \
honesta y acotada que una segura pero inventada. Escribe en español neutro, \
concreto y sin relleno: nada de riesgos genéricos del tipo "puede haber \
competencia".
"""


def render_perfil(perfil: Perfil) -> str:
    """El perfil como texto legible; entra en el bloque cacheado del sistema."""
    cap = perfil.capacidades
    res = perfil.restricciones
    imp = cap.importacion

    lineas = [
        "# Perfil de la empresa",
        "",
        f"Empresa: {perfil.nombre_empresa}",
        f"Descripción: {perfil.descripcion or 'sin descripción'}",
        "",
        "## Rubros objetivo",
        _lista(perfil.rubros_objetivo),
        "",
        "## Capacidades",
        f"- Fabricación propia: {_lista_inline(cap.fabricacion_propia) or 'no tiene'}",
        f"- Armado / kitting propio: {'sí' if cap.armado_kitting else 'no'}",
        f"- Stock disponible en bodega: {_lista_inline(cap.stock_actual) or 'sin stock relevante'}",
        f"- Red de proveedores locales: {'sí' if cap.proveedores_locales else 'no'}",
    ]

    if imp.activa:
        lineas.append(
            f"- Importación activa desde {_lista_inline(imp.paises) or 'origen no especificado'}; "
            f"lead time {imp.lead_time_dias} días, incoterm {imp.incoterm_habitual}, "
            f"orden mínima USD {imp.monto_minimo_orden_usd:,.0f}"
        )
    else:
        lineas.append("- Importación: no opera importaciones")

    tope = (
        "sin tope" if res.monto_maximo_clp == float("inf") else f"${res.monto_maximo_clp:,.0f} CLP"
    )
    lineas += [
        "",
        "## Restricciones comerciales",
        f"- Rango de monto: ${res.monto_minimo_clp:,.0f} CLP a {tope}",
        f"- Margen bruto mínimo exigido: {res.margen_minimo_pct}%",
        f"- Capital de trabajo disponible: ${res.capital_trabajo_clp:,.0f} CLP",
        f"- Días mínimos al cierre para ofertar: {res.dias_minimos_al_cierre}",
        f"- Puede tomar boletas de garantía: {'sí' if res.acepta_boleta_garantia else 'no'}",
        f"- Experiencia previa con el Estado: "
        f"{'sí' if res.tiene_experiencia_previa_estado else 'no'}",
        "",
        "## Certificaciones vigentes",
        _lista(perfil.certificaciones) or "- ninguna declarada",
        "",
        "## Regiones de operación",
        _lista(perfil.regiones_operacion) or "- sin restricción declarada",
    ]

    if perfil.notas_estrategicas:
        lineas += ["", "## Notas estratégicas", perfil.notas_estrategicas]

    return "\n".join(lineas)


def render_licitacion(lic: Licitacion) -> str:
    """La ficha de la licitación como texto para el turno del usuario."""
    monto = (
        f"${lic.monto_estimado:,.0f} {lic.moneda or 'CLP'}"
        if lic.monto_estimado
        else "no publicado"
    )
    cierre = lic.fecha_cierre.strftime("%d-%m-%Y %H:%M") if lic.fecha_cierre else "sin fecha"
    dias = lic.dias_al_cierre
    plazo = (
        f"{lic.tiempo_duracion_contrato} {lic.unidad_tiempo_contrato or ''}".strip()
        if lic.tiempo_duracion_contrato
        else "no especificado"
    )

    lineas = [
        "# Licitación a evaluar",
        "",
        f"Código: {lic.codigo_externo}",
        f"Nombre: {lic.nombre or 'sin nombre'}",
        f"Estado: {lic.estado or 'desconocido'}",
        f"Tipo de licitación: {lic.tipo or 'no informado'}",
        f"Monto estimado: {monto}",
        f"Cierre de ofertas: {cierre}"
        + (f" (faltan {dias} días)" if dias is not None else ""),
        f"Duración del contrato: {plazo}",
    ]

    if lic.comprador:
        lineas += [
            "",
            "## Comprador",
            f"- Organismo: {lic.comprador.nombre_organismo or 'no informado'}",
            f"- Unidad: {lic.comprador.nombre_unidad or 'no informada'}",
            f"- Región: {lic.comprador.region_unidad or 'no informada'}"
            + (f", comuna {lic.comprador.comuna_unidad}" if lic.comprador.comuna_unidad else ""),
        ]

    if lic.descripcion:
        lineas += ["", "## Descripción de las bases", lic.descripcion]

    if lic.items and lic.items.listado:
        lineas += ["", "## Items solicitados"]
        for item in lic.items.listado:
            cantidad = (
                f"{item.cantidad:,.0f} {item.unidad_medida or 'unidades'}"
                if item.cantidad
                else "cantidad no informada"
            )
            lineas.append(f"- {item.nombre_producto or 'item sin nombre'} — {cantidad}")
            if item.descripcion:
                lineas.append(f"  Especificación: {item.descripcion}")
            if item.categoria:
                lineas.append(f"  Categoría: {item.categoria}")
    else:
        lineas += [
            "",
            "## Items solicitados",
            "La ficha no incluye el detalle de items. Evalúa con esa limitación y "
            "déjalo anotado en los supuestos.",
        ]

    lineas += [
        "",
        f"Evalúa esta licitación para {lic.codigo_externo} y devuelve el JSON del esquema.",
    ]
    return "\n".join(lineas)


def _lista(valores: list[str]) -> str:
    return "\n".join(f"- {v}" for v in valores)


def _lista_inline(valores: list[str]) -> str:
    return ", ".join(valores)
