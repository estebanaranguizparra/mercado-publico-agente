"""Detección de nichos: agrega las adjudicaciones por categoría de producto.

La unidad de análisis es la **familia UNSPSC** (los primeros 4 dígitos del
código): "guantes de nitrilo" y "guantes de látex" son códigos distintos pero el
mismo negocio. Dentro de una familia interesante después se puede bajar al
código completo.

Todas las señales salen de SQL y aritmética; no se gasta un token en este paso.
Claude entra solo sobre las familias que sobrevivan al ranking.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

# UNSPSC ordena el mundo en segmentos de dos dígitos: del 10 al 60 son bienes
# físicos; del 70 en adelante, servicios. La distinción sirve para filtrar
# cuando interesa una u otra cosa, pero no para descartar: el Estado gasta más
# en servicios que en bienes, y ahí hay negocio igual.
SEGMENTOS_UNSPSC: dict[str, str] = {
    "10": "Material vegetal y animal vivo",
    "11": "Materiales minerales, textiles y no comestibles",
    "12": "Químicos y gases",
    "13": "Resinas, cauchos, films y elastómeros",
    "14": "Papel y productos de papel",
    "15": "Combustibles, lubricantes y anticorrosivos",
    "20": "Maquinaria de minería y perforación",
    "21": "Maquinaria agrícola, pesquera y forestal",
    "22": "Maquinaria de construcción",
    "23": "Maquinaria industrial y de procesos",
    "24": "Manejo de materiales, envases y almacenaje",
    "25": "Vehículos comerciales, militares y particulares",
    "26": "Generación y distribución de energía",
    "27": "Herramientas y maquinaria general",
    "30": "Componentes de estructuras y construcción",
    "31": "Componentes y suministros de manufactura",
    "32": "Componentes electrónicos",
    "39": "Sistemas eléctricos e iluminación",
    "40": "Sistemas de distribución y climatización",
    "41": "Equipamiento de laboratorio y medición",
    "42": "Equipamiento e insumos médicos",
    "43": "Tecnologías de la información y telecomunicaciones",
    "44": "Equipamiento y suministros de oficina",
    "45": "Equipos de impresión, fotografía y audiovisuales",
    "46": "Defensa, seguridad y protección personal",
    "47": "Equipos e insumos de aseo",
    "48": "Maquinaria para la industria de servicios",
    "49": "Equipamiento deportivo y recreativo",
    "50": "Alimentos, bebidas y tabaco",
    "51": "Medicamentos y productos farmacéuticos",
    "52": "Electrodomésticos y electrónica de consumo",
    "53": "Vestuario, equipaje y cuidado personal",
    "54": "Relojería y joyería",
    "55": "Productos editoriales",
    "56": "Mobiliario y equipamiento de espacios",
    "60": "Instrumentos musicales, juegos y material educativo",
    "70": "Servicios agrícolas, pesqueros y forestales",
    "71": "Servicios de minería, petróleo y gas",
    "72": "Construcción, obras y mantenimiento",
    "73": "Servicios de producción industrial",
    "76": "Aseo industrial y tratamiento de residuos",
    "77": "Servicios medioambientales",
    "78": "Transporte, almacenaje y correo",
    "80": "Servicios de gestión y profesionales",
    "81": "Ingeniería, investigación y tecnología",
    "82": "Servicios editoriales, diseño y artes gráficas",
    "83": "Servicios básicos y telecomunicaciones",
    "84": "Servicios financieros y seguros",
    "85": "Servicios de salud",
    "86": "Educación y capacitación",
    "90": "Viajes, alimentación, hospedaje y eventos",
    "91": "Servicios personales y domésticos",
    "92": "Defensa, orden y seguridad pública",
    "93": "Servicios sociales y de asuntos cívicos",
    "94": "Organizaciones y asociaciones",
    "95": "Terrenos, edificios y estructuras",
}


def es_bien(familia: str) -> bool:
    """True si la familia UNSPSC corresponde a un bien físico, no a un servicio."""
    if not familia or len(familia) < 2 or not familia[:2].isdigit():
        return False
    return int(familia[:2]) < 70


def nombre_segmento(familia: str) -> str:
    return SEGMENTOS_UNSPSC.get(familia[:2], "Otros")


# Las líneas con precio 0 o nulo son ruido: adjudicaciones sin desglose.
_LINEAS = """
    SELECT i.familia          AS familia,
           i.unspsc           AS unspsc,
           i.descripcion      AS descripcion,
           a.award_uid        AS award_uid,
           a.proveedor_id     AS proveedor_id,
           a.comprador_id     AS comprador_id,
           a.n_oferentes      AS n_oferentes,
           a.anio             AS anio,
           a.mes              AS mes,
           i.precio_unitario  AS precio_unitario,
           COALESCE(i.cantidad, 1) * i.precio_unitario AS valor
    FROM adjudicacion_items i
    JOIN adjudicaciones a ON a.award_uid = i.award_uid
    WHERE i.familia IS NOT NULL
      AND i.precio_unitario IS NOT NULL
      AND i.precio_unitario > 0
      -- Algunas adjudicaciones traen monto 1 como marcador de "según convenio";
      -- distorsionan cualquier estadística de precio.
      AND a.monto > 1000
"""


@dataclass
class Nicho:
    familia: str
    segmento: str
    etiqueta: str
    n_adjudicaciones: int
    monto_total: float
    ticket_mediano: float
    n_proveedores: int
    n_compradores: int
    meses_activos: int
    share_lider: float          # 0-1: cuánto se lleva el proveedor más grande
    hhi: float                  # 0-1: concentración del mercado
    oferentes_promedio: float   # competencia observada por licitación
    precio_p25: float
    precio_mediano: float
    precio_p75: float
    dispersion: float           # p75/p25: cuánto varía el precio del mismo producto
    puntaje: float = 0.0
    motivos: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.motivos is None:
            self.motivos = []


def _percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    if len(ordenados) == 1:
        return ordenados[0]
    posicion = p * (len(ordenados) - 1)
    bajo = int(math.floor(posicion))
    alto = min(bajo + 1, len(ordenados) - 1)
    peso = posicion - bajo
    return ordenados[bajo] * (1 - peso) + ordenados[alto] * peso


def detectar_nichos(
    con: sqlite3.Connection,
    *,
    minimo_adjudicaciones: int = 8,
    minimo_monto: float = 20_000_000,
    solo_bienes: bool = False,
) -> list[Nicho]:
    """Agrega el histórico por familia UNSPSC y ordena por atractivo.

    Por defecto entran bienes y servicios. `solo_bienes` restringe a mercadería
    física, útil solo cuando la pregunta es específicamente qué importar.
    """
    con.row_factory = sqlite3.Row

    # Un solo recorrido: la tabla de líneas cabe holgadamente en memoria para
    # los volúmenes de un histórico de uno o dos años.
    filas = con.execute(_LINEAS).fetchall()
    if not filas:
        return []

    por_familia: dict[str, dict] = defaultdict(
        lambda: {
            "awards": set(),
            "proveedores": defaultdict(float),
            "compradores": set(),
            "periodos": set(),
            "precios": [],
            "valores_award": defaultdict(float),
            "oferentes": {},
            "descripciones": defaultdict(int),
            "monto": 0.0,
        }
    )

    for fila in filas:
        if solo_bienes and not es_bien(fila["familia"]):
            continue
        datos = por_familia[fila["familia"]]
        award = fila["award_uid"]
        valor = float(fila["valor"] or 0)

        datos["awards"].add(award)
        datos["monto"] += valor
        datos["valores_award"][award] += valor
        datos["precios"].append(float(fila["precio_unitario"]))
        datos["periodos"].add((fila["anio"], fila["mes"]))
        if fila["proveedor_id"]:
            datos["proveedores"][fila["proveedor_id"]] += valor
        if fila["comprador_id"]:
            datos["compradores"].add(fila["comprador_id"])
        if fila["n_oferentes"] is not None:
            datos["oferentes"][award] = int(fila["n_oferentes"])
        if fila["descripcion"]:
            datos["descripciones"][fila["descripcion"][:70]] += 1

    nichos: list[Nicho] = []
    for familia, datos in por_familia.items():
        n_adj = len(datos["awards"])
        monto = datos["monto"]
        if n_adj < minimo_adjudicaciones or monto < minimo_monto:
            continue

        montos_proveedor = sorted(datos["proveedores"].values(), reverse=True)
        total_prov = sum(montos_proveedor) or 1.0
        share_lider = montos_proveedor[0] / total_prov if montos_proveedor else 1.0
        hhi = sum((m / total_prov) ** 2 for m in montos_proveedor)

        oferentes = list(datos["oferentes"].values())
        precios = datos["precios"]
        p25, p50, p75 = (
            _percentil(precios, 0.25),
            _percentil(precios, 0.50),
            _percentil(precios, 0.75),
        )

        etiqueta = max(datos["descripciones"].items(), key=lambda kv: kv[1])[0]

        nichos.append(
            Nicho(
                familia=familia,
                segmento=nombre_segmento(familia),
                etiqueta=etiqueta,
                n_adjudicaciones=n_adj,
                monto_total=monto,
                ticket_mediano=median(datos["valores_award"].values()),
                n_proveedores=len(datos["proveedores"]),
                n_compradores=len(datos["compradores"]),
                meses_activos=len(datos["periodos"]),
                share_lider=share_lider,
                hhi=hhi,
                oferentes_promedio=sum(oferentes) / len(oferentes) if oferentes else 0.0,
                precio_p25=p25,
                precio_mediano=p50,
                precio_p75=p75,
                dispersion=p75 / p25 if p25 > 0 else 0.0,
            )
        )

    if not nichos:
        return []

    meses_cubiertos = len({(f["anio"], f["mes"]) for f in filas}) or 1
    for nicho in nichos:
        _puntuar(nicho, meses_cubiertos)

    nichos.sort(key=lambda n: n.puntaje, reverse=True)
    return nichos


def _puntuar(nicho: Nicho, meses_cubiertos: int) -> None:
    """Puntaje explicable de 0 a 100 para quien quiere entrar a un mercado.

    Cada componente aporta un máximo fijo y deja constancia de por qué sumó.
    Nada aquí reemplaza el juicio: solo ordena qué mirar primero.
    """
    motivos: list[str] = []
    puntaje = 0.0

    # Volumen: escala logarítmica, porque la diferencia entre 50 y 500 millones
    # importa más que entre 5.000 y 5.450 millones.
    if nicho.monto_total > 0:
        volumen = min(math.log10(nicho.monto_total) / 10, 1.0)
        puntaje += 25 * volumen
        motivos.append(f"Volumen histórico de ${nicho.monto_total:,.0f}.")

    # Recurrencia: compras repartidas en el tiempo, no un peak aislado.
    recurrencia = nicho.meses_activos / meses_cubiertos
    puntaje += 20 * recurrencia
    if recurrencia >= 0.75:
        motivos.append(f"Compra sostenida: activa {nicho.meses_activos} de {meses_cubiertos} meses.")
    elif recurrencia < 0.34:
        motivos.append(f"Compra esporádica: solo {nicho.meses_activos} de {meses_cubiertos} meses.")

    # Fragmentación: si un proveedor domina, entrar cuesta mucho más.
    fragmentacion = 1 - nicho.hhi
    puntaje += 20 * fragmentacion
    if nicho.share_lider >= 0.5:
        motivos.append(
            f"Concentrado: el proveedor líder se lleva el {nicho.share_lider:.0%} del monto."
        )
    elif nicho.n_proveedores >= 10 and nicho.share_lider < 0.25:
        motivos.append(
            f"Fragmentado: {nicho.n_proveedores} proveedores y el líder solo toma "
            f"{nicho.share_lider:.0%}."
        )

    # Competencia: pocos oferentes por licitación es la señal más directa de
    # que hay espacio, pero también puede esconder una barrera. Se premia con
    # tope y se marca para que Claude averigüe la causa.
    if nicho.oferentes_promedio > 0:
        holgura = max(0.0, 1 - (nicho.oferentes_promedio - 1) / 8)
        puntaje += 20 * holgura
        if nicho.oferentes_promedio <= 3:
            motivos.append(
                f"Solo {nicho.oferentes_promedio:.1f} oferentes por licitación en promedio; "
                "conviene entender si es oportunidad o barrera."
            )
        elif nicho.oferentes_promedio >= 8:
            motivos.append(f"Muy disputado: {nicho.oferentes_promedio:.1f} oferentes en promedio.")

    # Dispersión de precios: el mismo producto pagado a precios muy distintos
    # indica que hay margen para quien cotiza bien.
    if nicho.dispersion > 1:
        margen = min((nicho.dispersion - 1) / 3, 1.0)
        puntaje += 15 * margen
        if nicho.dispersion >= 2:
            motivos.append(
                f"Precios dispersos: el percentil 75 paga {nicho.dispersion:.1f}× "
                "lo que paga el percentil 25."
            )

    nicho.puntaje = round(min(puntaje, 100.0), 1)
    nicho.motivos = motivos
