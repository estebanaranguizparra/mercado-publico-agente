"""Interfaz de línea de comandos: `mpagente <subcomando>`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from rich.console import Console

from .clients import ClienteMercadoPublico, ClienteMock
from .clients.base import FuenteLicitaciones
from .config import Config, ErrorConfig, cargar_perfil
from .models import Perfil
from .pipeline import AnalizadorClaude, ErrorAnalisis, ingestar, prefiltrar
from .reporte import informe_markdown, tabla_ranking
from .storage import ESTADOS_PIPELINE, Almacen

console = Console()


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def _abrir_fuente(config: Config) -> FuenteLicitaciones:
    if config.fuente == "mock":
        console.print("[dim]Fuente: datos simulados (MP_FUENTE=mock).[/]")
        return ClienteMock()
    console.print("[dim]Fuente: API de Mercado Público.[/]")
    return ClienteMercadoPublico(
        config.ticket or "",
        req_por_minuto=config.req_por_minuto,
        timeout=config.timeout,
    )


def _parsear_fecha(texto: str | None) -> date | None:
    if not texto:
        return None
    try:
        return datetime.strptime(texto, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Fecha inválida '{texto}'. Usa el formato YYYY-MM-DD.") from exc


# --------------------------------------------------------------------------- #
# Subcomandos
# --------------------------------------------------------------------------- #


def cmd_ingestar(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    del perfil
    fuente = _abrir_fuente(config)
    fecha = _parsear_fecha(args.fecha)

    try:
        with Almacen(config.db_path) as almacen:
            with console.status("Descargando licitaciones..."):
                resumen = ingestar(
                    fuente,
                    almacen,
                    fecha=fecha,
                    estado=args.estado,
                    limite=args.limite,
                    traer_detalle=not args.sin_detalle,
                    refrescar=args.refrescar,
                )
    finally:
        cerrar = getattr(fuente, "cerrar", None)
        if callable(cerrar):
            cerrar()

    console.print(
        f"Listadas [bold]{resumen.listadas}[/] · "
        f"guardadas [bold green]{resumen.guardadas}[/] · "
        f"ya conocidas [dim]{resumen.omitidas_por_cache}[/] · "
        f"detalles pedidos [bold]{resumen.detalles_pedidos}[/]"
    )
    for error in resumen.errores[:10]:
        console.print(f"[yellow]· {error}[/]")
    if len(resumen.errores) > 10:
        console.print(f"[yellow]· … y {len(resumen.errores) - 10} error(es) más[/]")
    return 0


def cmd_filtrar(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    del args
    with Almacen(config.db_path) as almacen:
        licitaciones = almacen.listar_licitaciones()
        if not licitaciones:
            console.print("[yellow]No hay licitaciones guardadas. Corre `mpagente ingestar`.[/]")
            return 1

        aprobadas = 0
        for lic in licitaciones:
            resultado = prefiltrar(lic, perfil)
            almacen.guardar_prefiltro(
                resultado.codigo,
                aprobado=resultado.aprobado,
                puntaje=resultado.puntaje,
                motivos=resultado.motivos,
            )
            aprobadas += int(resultado.aprobado)

    console.print(
        f"Prefiltro aplicado a [bold]{len(licitaciones)}[/] licitaciones: "
        f"[bold green]{aprobadas}[/] pasan, "
        f"[dim]{len(licitaciones) - aprobadas}[/] descartadas."
    )
    return 0


def cmd_analizar(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    with Almacen(config.db_path) as almacen:
        codigos = almacen.codigos_aprobados_prefiltro()
        if not codigos:
            console.print(
                "[yellow]No hay licitaciones aprobadas por el prefiltro. "
                "Corre `mpagente filtrar` primero.[/]"
            )
            return 1

        if not args.reevaluar:
            ya = almacen.codigos_evaluados()
            codigos = [c for c in codigos if c not in ya]

        codigos = codigos[: args.top]
        if not codigos:
            console.print("[green]Todo lo aprobado ya está evaluado. Usa --reevaluar para rehacerlo.[/]")
            return 0

        licitaciones = [
            lic for c in codigos if (lic := almacen.obtener_licitacion(c)) is not None
        ]

        try:
            analizador = AnalizadorClaude(
                perfil,
                modelo=config.modelo,
                effort=config.effort,
                max_paralelo=args.paralelo,
            )
        except ErrorAnalisis as exc:
            console.print(f"[bold red]{exc}[/]")
            return 2

        console.print(
            f"Evaluando [bold]{len(licitaciones)}[/] licitación(es) con "
            f"[bold]{config.modelo}[/] (effort={config.effort})…"
        )

        completadas = 0
        fallidas: list[str] = []

        def al_terminar(resultado) -> None:  # noqa: ANN001 — tipo interno del pipeline
            nonlocal completadas
            completadas += 1
            if resultado.ok:
                evaluacion = resultado.evaluacion
                console.print(
                    f"  [{completadas}/{len(licitaciones)}] {resultado.codigo} → "
                    f"[bold]{evaluacion.puntaje_total}[/] · {evaluacion.estrategia}"
                )
            else:
                fallidas.append(f"{resultado.codigo}: {resultado.error}")
                console.print(f"  [{completadas}/{len(licitaciones)}] [red]{resultado.codigo} falló[/]")

        resultados = analizador.analizar_lote(licitaciones, al_terminar=al_terminar)

        tokens_entrada = tokens_salida = 0
        for resultado in resultados:
            tokens_entrada += resultado.tokens_entrada
            tokens_salida += resultado.tokens_salida
            if resultado.ok and resultado.evaluacion is not None:
                almacen.guardar_evaluacion(
                    resultado.evaluacion,
                    modelo=config.modelo,
                    tokens_entrada=resultado.tokens_entrada,
                    tokens_salida=resultado.tokens_salida,
                )

    console.print(
        f"\nListo: [bold green]{len(resultados) - len(fallidas)}[/] evaluadas, "
        f"[red]{len(fallidas)}[/] con error · "
        f"tokens {tokens_entrada:,} entrada / {tokens_salida:,} salida"
    )
    for error in fallidas:
        console.print(f"[yellow]· {error}[/]")
    return 0


def cmd_reporte(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    with Almacen(config.db_path) as almacen:
        filas = almacen.ranking(limite=args.top)

    tabla_ranking(filas, console)

    if args.salida:
        texto = informe_markdown(
            filas, nombre_empresa=perfil.nombre_empresa, modelo=config.modelo
        )
        ruta = Path(args.salida)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(texto, encoding="utf-8")
        console.print(f"\nInforme escrito en [bold]{ruta}[/]")
    return 0


def cmd_pipeline(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    for paso in (cmd_ingestar, cmd_filtrar, cmd_analizar, cmd_reporte):
        console.rule(paso.__name__.removeprefix("cmd_"))
        codigo = paso(args, config, perfil)
        if codigo != 0:
            return codigo
    return 0


def cmd_recolectar(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Descarga autónoma del histórico de adjudicaciones. No requiere ticket."""
    del perfil
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    from .clients.ocds import ClienteOCDS
    from .pipeline import meses_hacia_atras, recolectar

    modalidades = args.modalidad or ["licitacion"]
    periodos = meses_hacia_atras(args.meses)
    console.print(
        f"Recolectando [bold]{args.meses}[/] mes(es) "
        f"({periodos[-1][0]}-{periodos[-1][1]:02d} a {periodos[0][0]}-{periodos[0][1]:02d}) · "
        f"modalidades: [bold]{', '.join(modalidades)}[/]"
    )

    with ClienteOCDS(req_por_segundo=args.rps) as cliente, Almacen(config.db_path) as almacen:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progreso:
            tarea = progreso.add_task("preparando…", total=None)

            def al_avanzar(modalidad: str, anio: int, mes: int, hechos: int, total: int) -> None:
                if hechos == 0:
                    progreso.reset(
                        tarea, total=total, description=f"{modalidad} {anio}-{mes:02d}"
                    )
                else:
                    progreso.update(tarea, completed=hechos)

            resumen = recolectar(
                cliente,
                almacen,
                meses=args.meses,
                modalidades=tuple(modalidades),
                hilos=args.hilos,
                refrescar=args.refrescar,
                limite_por_mes=args.limite_mes,
                al_avanzar=al_avanzar,
            )

    console.print(
        f"\nMeses procesados [bold green]{resumen.meses_procesados}[/] · "
        f"omitidos por estar cerrados [dim]{resumen.meses_omitidos}[/]\n"
        f"Procesos revisados [bold]{resumen.procesos_vistos:,}[/] → "
        f"adjudicados [bold green]{resumen.adjudicados:,}[/] · "
        f"desiertos o revocados [dim]{resumen.cerrados_sin_adjudicar:,}[/] · "
        f"aún sin resolver [yellow]{resumen.pendientes:,}[/]\n"
        f"Adjudicaciones guardadas: [bold green]{resumen.adjudicaciones:,}[/]"
    )
    if resumen.pendientes:
        console.print(
            "[dim]Los pendientes se adjudican semanas después de publicarse; "
            "vuelve a correr `recolectar` más adelante y se recogen solos.[/]"
        )
    if resumen.errores:
        console.print(f"[yellow]{len(resumen.errores)} error(es); los primeros:[/]")
        for error in resumen.errores[:5]:
            console.print(f"[yellow]· {error}[/]")
    return 0


def cmd_demanda(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Descarga los llamados publicados: qué está pidiendo el Estado."""
    del perfil
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    from .clients.ocds import ClienteOCDS
    from .pipeline import meses_recientes, recolectar_demanda

    modalidades = args.modalidad or ["licitacion"]
    periodos = meses_recientes(args.meses)
    console.print(
        f"Recolectando llamados de [bold]{args.meses}[/] mes(es) "
        f"({periodos[-1][0]}-{periodos[-1][1]:02d} a {periodos[0][0]}-{periodos[0][1]:02d}) · "
        f"modalidades: [bold]{', '.join(modalidades)}[/]"
    )

    with ClienteOCDS(req_por_segundo=args.rps) as cliente, Almacen(config.db_path) as almacen:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progreso:
            tarea = progreso.add_task("preparando…", total=None)

            def al_avanzar(modalidad: str, anio: int, mes: int, hechos: int, total: int) -> None:
                if hechos == 0:
                    progreso.reset(tarea, total=total, description=f"{modalidad} {anio}-{mes:02d}")
                else:
                    progreso.update(tarea, completed=hechos)

            resumen = recolectar_demanda(
                cliente,
                almacen,
                meses=args.meses,
                modalidades=tuple(modalidades),
                hilos=args.hilos,
                refrescar=args.refrescar,
                limite_por_mes=args.limite_mes,
                al_avanzar=al_avanzar,
            )

    console.print(
        f"\nMeses procesados [bold green]{resumen.meses_procesados}[/] · "
        f"procesos revisados [bold]{resumen.procesos_vistos:,}[/]\n"
        f"Llamados guardados [bold green]{resumen.solicitudes:,}[/] · "
        f"todavía abiertos [bold green]{resumen.vigentes:,}[/] · "
        f"sin detalle de items [dim]{resumen.sin_items:,}[/]"
    )
    if resumen.errores:
        console.print(f"[yellow]{len(resumen.errores)} aviso(s); los primeros:[/]")
        for error in resumen.errores[:5]:
            console.print(f"[yellow]· {error}[/]")
    return 0


def cmd_pidiendo(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Agrega los llamados por categoría y muestra qué se está solicitando."""
    del perfil
    from .pipeline import agregar_demanda
    from .reporte import informe_demanda, tabla_demanda, tabla_vigentes

    incluir = "bienes" if args.solo_bienes else "servicios" if args.solo_servicios else "todo"

    with Almacen(config.db_path) as almacen:
        resumen = almacen.resumen()
        if not resumen["solicitudes"]:
            console.print(
                "[yellow]No hay llamados recolectados. Corre `mpagente demanda` primero.[/]"
            )
            return 1

        demandas = agregar_demanda(
            almacen.conexion(),
            minimo_solicitudes=args.min_llamados,
            solo_vigentes=args.vigentes,
            incluir=incluir,
        )
        abiertos = (
            almacen.solicitudes_vigentes(limite=args.top, dias_minimos=args.dias_min)
            if args.abiertos or args.json
            else []
        )
        meses_cubiertos = len(
            almacen.conexion()
            .execute("SELECT DISTINCT anio, mes FROM solicitudes WHERE anio IS NOT NULL")
            .fetchall()
        )

    # Salida para consumo por otro programa o por un subagente: las tablas de
    # `rich` no se parsean de forma confiable.
    if args.json:
        import dataclasses
        import json as _json

        print(
            _json.dumps(
                {
                    "base": {
                        "llamados": resumen["solicitudes"],
                        "items": resumen["items_solicitados"],
                        "vigentes": resumen["solicitudes_vigentes"],
                        "meses_cubiertos": meses_cubiertos,
                        "generado": datetime.now().isoformat(timespec="seconds"),
                    },
                    "categorias": [dataclasses.asdict(d) for d in demandas[: args.top]],
                    "abiertos": [dict(f) for f in abiertos],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    console.print(
        f"[dim]Base: {resumen['solicitudes']:,} llamados · "
        f"{resumen['items_solicitados']:,} líneas · "
        f"{resumen['solicitudes_vigentes']:,} todavía abiertos.[/]\n"
    )

    if not demandas:
        console.print(
            "[yellow]Ninguna categoría superó el umbral. "
            "Baja --min-llamados o recolecta más meses.[/]"
        )
        return 1

    seleccion = demandas[: args.top]
    tabla_demanda(seleccion, console)
    console.print(
        f"\n[dim]{len(demandas)} categorías superaron el umbral; se muestran {len(seleccion)}.[/]"
    )

    if abiertos:
        console.print()
        tabla_vigentes(abiertos, console)

    if args.salida:
        ruta = Path(args.salida)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(informe_demanda(seleccion, meses=meses_cubiertos), encoding="utf-8")
        console.print(f"\nInforme escrito en [bold]{ruta}[/]")
    return 0


def cmd_ficha(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Detalle de un llamado y el contexto histórico de lo que pide."""
    del perfil
    import json as _json

    with Almacen(config.db_path) as almacen:
        ficha = almacen.ficha_solicitud(args.codigo)
        if ficha is None:
            console.print(
                f"[yellow]{args.codigo} no está en la base.[/] "
                "Puede ser de un mes no recolectado o el código estar mal escrito."
            )
            return 1

        familias = sorted({i["familia"] for i in ficha["items"] if i["familia"]})
        contexto = {f: almacen.contexto_familia(f) for f in familias}
        # Lo ya decidido sobre este proceso o sobre su categoría: evita volver a
        # analizar desde cero algo que ya se descartó, y por qué.
        decidido = almacen.oportunidad(args.codigo)
        antecedentes = [
            o
            for f in familias
            for o in almacen.listar_oportunidades(familia=f)
            if o["clave"] != args.codigo
        ]

    cierre = datetime.fromisoformat(ficha["cierre"]) if ficha["cierre"] else None
    dias = (cierre - datetime.now()).days if cierre else None
    ficha["dias_para_cierre"] = dias
    ficha["vigente"] = bool(cierre and cierre > datetime.now())

    if args.json:
        print(
            _json.dumps(
                {
                    "llamado": ficha,
                    "contexto_por_familia": contexto,
                    "ya_decidido": decidido,
                    "antecedentes_misma_familia": antecedentes[:10],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    estado = (
        f"[bold green]abierto[/], cierra en {dias} día(s)"
        if ficha["vigente"]
        else "[dim]cerrado[/]"
    )
    console.print(f"[bold]{ficha['codigo']}[/] · {estado}")
    console.print(f"{ficha['titulo']}\n")

    if decidido:
        console.print(
            f"[bold yellow]Ya decidido:[/] {decidido['estado']}"
            + (f" · {decidido['modalidad']}" if decidido["modalidad"] else "")
        )
        if decidido["motivo"]:
            console.print(f"  [dim]{decidido['motivo']}[/]")
        console.print()
    elif antecedentes:
        previo = antecedentes[0]
        console.print(
            f"[yellow]Antecedente en la misma familia:[/] {previo['clave']} "
            f"quedó [bold]{previo['estado']}[/]."
            + (f" {previo['motivo']}" if previo["motivo"] else "")
        )
        console.print()

    console.print(f"  Organismo: {ficha['comprador'] or '—'}")
    console.print(f"  Región:    {ficha['region'] or '—'}")
    console.print(f"  Proceso:   {ficha['tipo_proceso'] or '—'}")
    console.print(f"  Cierre:    {ficha['cierre'] or '—'}")

    console.print(f"\n[bold]Qué piden[/] ({len(ficha['items'])} ítem(s))")
    for item in ficha["items"][:20]:
        cantidad = f"{item['cantidad']:,.0f} {item['unidad'] or ''}".strip()
        console.print(f"  · {item['descripcion'][:70]} [dim]({cantidad})[/]")
    if len(ficha["items"]) > 20:
        console.print(f"  [dim]… y {len(ficha['items']) - 20} ítem(s) más[/]")

    console.print("\n[bold]Contra qué te enfrentas[/]")
    for familia, ctx in contexto.items():
        if not ctx["n_adjudicaciones"]:
            console.print(
                f"  [bold]{familia}[/] · [yellow]sin historial adjudicado; "
                "no hay referencia de precio ni de competencia[/]"
            )
            continue
        console.print(
            f"  [bold]{familia}[/] · {ctx['oferentes_promedio']:.1f} oferentes promedio · "
            f"precio mediano ${ctx['precio_mediano']:,.0f} "
            f"(p25 ${ctx['precio_p25']:,.0f} – p75 ${ctx['precio_p75']:,.0f})"
        )
        for prov in ctx["proveedores_top"][:3]:
            console.print(
                f"      [dim]{prov['proveedor'][:46]} — {prov['ganadas']} adjudicación(es)[/]"
            )
    return 0


def cmd_registrar(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Guarda o actualiza una decisión sobre una oportunidad."""
    del perfil
    campos = {
        "tipo": args.tipo,
        "codigo": args.codigo_proceso,
        "familia": args.familia,
        "titulo": args.titulo,
        "organismo": args.organismo,
        "veredicto": args.veredicto,
        "modalidad": args.modalidad,
        "prioridad": args.prioridad,
        "motivo": args.motivo,
        "costo_estimado": args.costo,
        "precio_ofertado": args.precio,
        "margen_pct": args.margen,
        "cierre": args.cierre,
        "documentos": args.documentos,
    }

    with Almacen(config.db_path) as almacen:
        # Si la clave es un código conocido, se completan solos los datos del
        # llamado: evita que cada agente los tenga que repetir a mano.
        if not campos["titulo"] and args.tipo == "licitacion":
            ficha = almacen.ficha_solicitud(args.clave)
            if ficha:
                campos.setdefault("codigo", args.clave)
                campos["codigo"] = campos["codigo"] or args.clave
                campos["titulo"] = ficha["titulo"]
                campos["organismo"] = campos["organismo"] or ficha["comprador"]
                campos["cierre"] = campos["cierre"] or ficha["cierre"]

        guardada = almacen.guardar_oportunidad(
            args.clave,
            estado=args.estado,
            nota=args.nota,
            autor=args.autor,
            **campos,
        )

    console.print(
        f"[bold green]✓[/] {guardada['clave']} → [bold]{guardada['estado']}[/]"
        + (f" · {guardada['modalidad']}" if guardada.get("modalidad") else "")
    )
    if guardada.get("motivo"):
        console.print(f"  [dim]{guardada['motivo']}[/]")
    return 0


def cmd_pipeline_estado(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Muestra el embudo comercial y lo que hay en cada estado."""
    del perfil
    import json as _json

    from .reporte import tabla_pipeline

    with Almacen(config.db_path) as almacen:
        resumen = almacen.resumen_pipeline()
        oportunidades = almacen.listar_oportunidades(
            estado=args.estado, familia=args.familia
        )

    if args.json:
        print(
            _json.dumps(
                {"resumen": resumen, "oportunidades": oportunidades},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    if not resumen["total"]:
        console.print(
            "[yellow]El pipeline está vacío.[/] Se llena solo cuando los agentes "
            "analizan oportunidades, o a mano con `mpagente registrar`."
        )
        return 0

    embudo = " · ".join(
        f"{estado} [bold]{n}[/]"
        for estado, n in resumen["por_estado"].items()
        if n
    )
    console.print(f"[bold]Embudo:[/] {embudo}")
    if resumen["ofertadas"] or resumen["ganadas"]:
        linea = f"Ofertadas [bold]{resumen['ofertadas']}[/] · ganadas [bold green]{resumen['ganadas']}[/]"
        if resumen["tasa_conversion"] is not None:
            linea += f" · conversión [bold]{resumen['tasa_conversion']:.0%}[/]"
        if resumen["monto_ganado"]:
            linea += f" · ${resumen['monto_ganado']:,.0f} adjudicados"
        console.print(linea)
    console.print()

    tabla_pipeline(oportunidades, console)
    return 0


def cmd_historial(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Muestra el recorrido completo de una oportunidad."""
    del perfil
    with Almacen(config.db_path) as almacen:
        oportunidad = almacen.oportunidad(args.clave)
        if oportunidad is None:
            console.print(f"[yellow]{args.clave} no está en el pipeline.[/]")
            return 1
        eventos = almacen.eventos_oportunidad(args.clave)

    console.print(f"[bold]{oportunidad['clave']}[/] · {oportunidad['titulo'] or '—'}")
    console.print(
        f"Estado actual: [bold]{oportunidad['estado']}[/]"
        + (f" · {oportunidad['modalidad']}" if oportunidad["modalidad"] else "")
    )
    if oportunidad["motivo"]:
        console.print(f"[dim]{oportunidad['motivo']}[/]")

    console.print("\n[bold]Recorrido[/]")
    for evento in eventos:
        fecha = evento["ocurrio_en"][:16].replace("T", " ")
        autor = f" [dim]({evento['autor']})[/]" if evento["autor"] else ""
        console.print(f"  {fecha} → [bold]{evento['estado']}[/]{autor}")
        if evento["nota"]:
            console.print(f"      [dim]{evento['nota']}[/]")
    return 0


def cmd_proveedor(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Registra o lista proveedores: empresas y profesionales ya encontrados."""
    del perfil
    import json as _json

    with Almacen(config.db_path) as almacen:
        if args.nombre:
            almacen.guardar_proveedor(
                args.nombre,
                tipo=args.tipo,
                rubro=args.rubro,
                familia=args.familia,
                contacto=args.contacto,
                sitio=args.sitio,
                fuente=args.fuente,
                confianza=args.confianza,
                certificaciones=args.certificaciones,
                notas=args.notas,
                desempeno=args.desempeno,
            )
            console.print(f"[bold green]✓[/] {args.nombre} ({args.tipo})")
            return 0

        proveedores = almacen.listar_proveedores(tipo=args.filtro_tipo, familia=args.familia)

    if args.json:
        print(_json.dumps(proveedores, ensure_ascii=False, indent=2, default=str))
        return 0

    if not proveedores:
        console.print("[dim]No hay proveedores registrados con ese filtro.[/]")
        return 0

    from rich.table import Table

    tabla = Table(header_style="bold", expand=False, pad_edge=False)
    tabla.add_column("Nombre", overflow="ellipsis", max_width=32, no_wrap=True)
    tabla.add_column("Tipo", width=11)
    tabla.add_column("Rubro", overflow="ellipsis", max_width=26, no_wrap=True)
    tabla.add_column("Fam", width=4)
    tabla.add_column("Contacto", overflow="ellipsis", max_width=28, no_wrap=True)
    tabla.add_column("Conf", width=5)
    for p in proveedores:
        tabla.add_row(
            p["nombre"], p["tipo"], p["rubro"] or "—", p["familia"] or "—",
            p["contacto"] or "[dim]—[/]", p["confianza"] or "—",
        )
    console.print(tabla)
    return 0


def cmd_cotizacion(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Registra o lista cotizaciones pedidas a proveedores."""
    del perfil
    import json as _json

    with Almacen(config.db_path) as almacen:
        if args.proveedor:
            almacen.guardar_cotizacion(
                args.clave,
                args.proveedor,
                estado=args.estado,
                precio=args.precio,
                plazo_dias=args.plazo,
                forma_pago=args.forma_pago,
                vigencia=args.vigencia,
                notas=args.notas,
            )
            console.print(
                f"[bold green]✓[/] {args.proveedor} → {args.estado}"
                + (f" · ${args.precio:,.0f}" if args.precio else "")
            )
            return 0

        cotizaciones = almacen.listar_cotizaciones(args.clave)

    if args.json:
        print(_json.dumps(cotizaciones, ensure_ascii=False, indent=2, default=str))
        return 0

    if not cotizaciones:
        console.print("[dim]No hay cotizaciones registradas.[/]")
        return 0

    from rich.table import Table

    tabla = Table(
        title=f"Cotizaciones · {args.clave}" if args.clave else "Cotizaciones",
        header_style="bold", expand=False, pad_edge=False,
    )
    tabla.add_column("Proveedor", overflow="ellipsis", max_width=30, no_wrap=True)
    tabla.add_column("Estado", width=13)
    tabla.add_column("Precio", justify="right", width=13)
    tabla.add_column("Plazo", justify="right", width=6)
    tabla.add_column("Pago", overflow="ellipsis", max_width=20, no_wrap=True)
    for c in cotizaciones:
        color = "green" if c["estado"] == "cotizo" else "dim"
        tabla.add_row(
            c["proveedor"],
            f"[{color}]{c['estado']}[/]",
            f"${c['precio']:,.0f}" if c["precio"] else "[dim]—[/]",
            f"{c['plazo_dias']}d" if c["plazo_dias"] else "[dim]—[/]",
            c["forma_pago"] or "[dim]—[/]",
        )
    console.print(tabla)
    return 0


def cmd_garantia(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Registra o lista instrumentos para cubrir garantías."""
    del perfil
    import json as _json

    with Almacen(config.db_path) as almacen:
        if args.proveedor:
            almacen.guardar_instrumento_garantia(
                args.proveedor,
                args.instrumento,
                costo_pct=args.costo_pct,
                plazo_dias=args.plazo,
                requisitos=args.requisitos,
                inmoviliza_capital=int(args.inmoviliza) if args.inmoviliza is not None else None,
                fuente=args.fuente,
                notas=args.notas,
            )
            console.print(f"[bold green]✓[/] {args.proveedor} · {args.instrumento}")
            return 0

        instrumentos = almacen.listar_instrumentos_garantia()

    if args.json:
        print(_json.dumps(instrumentos, ensure_ascii=False, indent=2, default=str))
        return 0

    if not instrumentos:
        console.print(
            "[dim]No hay instrumentos registrados. Los busca `buscador-financiamiento`.[/]"
        )
        return 0

    from rich.table import Table

    tabla = Table(
        title="Instrumentos para cubrir garantías", header_style="bold", expand=False
    )
    tabla.add_column("Proveedor", overflow="ellipsis", max_width=28, no_wrap=True)
    tabla.add_column("Instrumento", width=18)
    tabla.add_column("Costo", justify="right", width=7)
    tabla.add_column("Emisión", justify="right", width=8)
    tabla.add_column("Inmoviliza", width=10)
    tabla.add_column("Requisitos", overflow="ellipsis", max_width=34, no_wrap=True)
    for i in instrumentos:
        inmoviliza = i["inmoviliza_capital"]
        tabla.add_row(
            i["proveedor"], i["instrumento"],
            f"{i['costo_pct']:.1f}%" if i["costo_pct"] else "[dim]—[/]",
            f"{i['plazo_dias']}d" if i["plazo_dias"] else "[dim]—[/]",
            "[red]sí[/]" if inmoviliza else "[bold green]no[/]",
            i["requisitos"] or "[dim]—[/]",
        )
    console.print(tabla)
    return 0


def cmd_contexto(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Exporta o importa el contexto portable que hace posible correr en la nube."""
    del perfil
    import json as _json

    ruta = Path(args.archivo)

    with Almacen(config.db_path) as almacen:
        if args.importar:
            if not ruta.exists():
                console.print(f"[bold red]No existe {ruta}[/]")
                return 1
            contexto = _json.loads(ruta.read_text(encoding="utf-8"))
            resultado = almacen.importar_contexto(contexto, origen=str(ruta.name))
            console.print(
                f"[bold green]✓[/] Contexto importado: "
                f"{resultado['familias']} familias · "
                f"{resultado['oportunidades']} oportunidades · "
                f"{resultado['eventos']} eventos"
            )
            if contexto.get("generado_en"):
                console.print(f"  [dim]Generado el {contexto['generado_en']}[/]")
            return 0

        contexto = almacen.exportar_contexto()

    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        _json.dumps(contexto, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    peso = ruta.stat().st_size / 1024
    console.print(
        f"[bold green]✓[/] Contexto exportado a [bold]{ruta}[/] ({peso:,.0f} KB)\n"
        f"  {len(contexto['familias'])} familias con precio y competencia · "
        f"{len(contexto['oportunidades'])} decisiones · "
        f"{len(contexto['eventos'])} eventos"
    )
    return 0


def cmd_nichos(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    """Agrega el histórico por categoría y muestra los nichos más atractivos."""
    del perfil
    from .pipeline import detectar_nichos
    from .reporte import informe_nichos, tabla_nichos

    with Almacen(config.db_path) as almacen:
        resumen = almacen.resumen()
        if not resumen["adjudicaciones"]:
            console.print(
                "[yellow]No hay adjudicaciones recolectadas. "
                "Corre `mpagente recolectar` primero.[/]"
            )
            return 1

        nichos = detectar_nichos(
            almacen.conexion(),
            minimo_adjudicaciones=args.min_adjudicaciones,
            minimo_monto=args.min_monto,
            solo_bienes=args.solo_bienes,
        )
        periodos = almacen.periodos_recolectados()

    console.print(
        f"[dim]Base: {resumen['adjudicaciones']:,} adjudicaciones · "
        f"{resumen['items_adjudicados']:,} líneas · "
        f"{len(periodos)} mes(es) recolectados.[/]\n"
    )

    if not nichos:
        console.print(
            "[yellow]Ninguna familia superó los umbrales. "
            "Baja --min-monto o --min-adjudicaciones, o recolecta más meses.[/]"
        )
        return 1

    seleccion = nichos[: args.top]
    tabla_nichos(seleccion, console)
    console.print(f"\n[dim]{len(nichos)} familias superaron los umbrales; se muestran {len(seleccion)}.[/]")

    if args.salida:
        ruta = Path(args.salida)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(informe_nichos(seleccion, periodos=periodos), encoding="utf-8")
        console.print(f"Informe escrito en [bold]{ruta}[/]")
    return 0


def cmd_inspeccionar(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    del config, perfil
    from rich.table import Table

    from .inspector import inspeccionar, listar_contenido_zip

    ruta = Path(args.archivo)
    if ruta.suffix.lower() == ".zip" and args.listar:
        console.print(f"[bold]Contenido de {ruta.name}[/]")
        for nombre, tamano in listar_contenido_zip(ruta):
            console.print(f"  {nombre}  [dim]{tamano / 1e6:.1f} MB[/]")
        return 0

    informe = inspeccionar(ruta, limite_filas=args.filas)

    console.print(f"[bold]{informe.ruta.name}[/]")
    if informe.archivo_interno:
        console.print(f"Archivo dentro del ZIP: [bold]{informe.archivo_interno}[/]")
    console.print(
        f"Codificación [bold]{informe.codificacion}[/] · "
        f"separador [bold]{informe.separador!r}[/] · "
        f"decimales {informe.convencion_decimal}"
    )
    console.print(
        f"Filas leídas [bold]{informe.filas_leidas:,}[/] de "
        f"~[bold]{informe.filas_totales_estimadas:,}[/] · "
        f"{len(informe.columnas)} columnas\n"
    )

    tabla = Table(header_style="bold", expand=False, pad_edge=False)
    tabla.add_column("Columna", overflow="ellipsis", max_width=26, no_wrap=True)
    tabla.add_column("Tipo", width=10)
    tabla.add_column("Vacíos", justify="right", width=6)
    tabla.add_column("Distintos", justify="right", width=9)
    tabla.add_column("Rol probable", width=15)
    tabla.add_column("Ejemplo", overflow="ellipsis", max_width=32, no_wrap=True)

    for col in informe.columnas:
        tabla.add_row(
            col.nombre,
            col.tipo,
            f"{col.vacios_pct:.0f}%",
            f"{col.valores_distintos:,}",
            f"[green]{col.rol_probable}[/]" if col.rol_probable else "[dim]—[/]",
            col.ejemplos[0] if col.ejemplos else "[dim]—[/]",
        )
    console.print(tabla)

    roles = informe.roles_detectados
    console.print(f"\n[bold]Roles detectados:[/] {len(roles)}")
    for rol, columna in sorted(roles.items()):
        console.print(f"  {rol:16} → {columna}")

    faltantes = informe.roles_faltantes()
    if faltantes:
        console.print(
            f"\n[yellow]Sin columna evidente para: {', '.join(faltantes)}.[/]\n"
            "[dim]Revisa la tabla de arriba; puede estar con otro nombre.[/]"
        )
    else:
        console.print("\n[green]Están todas las columnas que necesita el análisis por categoría.[/]")
    return 0


def cmd_estado(args: argparse.Namespace, config: Config, perfil: Perfil) -> int:
    del args
    with Almacen(config.db_path) as almacen:
        resumen = almacen.resumen()
        estados = almacen.conteo_por_estado()
        periodos = almacen.periodos_recolectados()

    console.print(f"[bold]Perfil:[/] {perfil.nombre_empresa}")
    console.print(f"[bold]Base de datos:[/] {config.db_path}")
    console.print(f"[bold]Fuente:[/] {config.fuente} · [bold]Modelo:[/] {config.modelo}")

    console.print("\n[bold]Demanda: lo que el Estado pide (OCDS tender)[/]")
    console.print(f"  Llamados: [bold]{resumen['solicitudes']:,}[/]")
    console.print(f"  Líneas con categoría: [bold]{resumen['items_solicitados']:,}[/]")
    console.print(
        f"  Todavía abiertos: [bold green]{resumen['solicitudes_vigentes']:,}[/]"
    )

    console.print("\n[bold]Histórico adjudicado (OCDS award)[/]")
    console.print(f"  Adjudicaciones: [bold]{resumen['adjudicaciones']:,}[/]")
    console.print(f"  Líneas con producto: [bold]{resumen['items_adjudicados']:,}[/]")
    console.print(f"  Meses recolectados: [bold]{resumen['meses_recolectados']}[/]")
    if periodos:
        ultimo, primero = periodos[0], periodos[-1]
        console.print(
            f"  Rango: {primero[1]}-{primero[2]:02d} a {ultimo[1]}-{ultimo[2]:02d}"
        )
    if estados:
        detalle = " · ".join(f"{k} {v:,}" for k, v in sorted(estados.items()))
        console.print(f"  Procesos revisados: {detalle}")
        if estados.get("pendiente"):
            console.print(
                f"  [yellow]{estados['pendiente']:,} sin adjudicar todavía; "
                "vuelve a correr `recolectar` para recogerlos.[/]"
            )

    console.print("\n[bold]Licitaciones abiertas (perfil de empresa)[/]")
    console.print(f"  Guardadas: [bold]{resumen['licitaciones']}[/]")
    console.print(f"  Aprobadas por prefiltro: [bold green]{resumen['prefiltro_aprobadas']}[/]")
    console.print(f"  Descartadas: [dim]{resumen['prefiltro_rechazadas']}[/]")
    console.print(f"  Evaluadas con Claude: [bold]{resumen['evaluadas']}[/]")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def _construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpagente",
        description="Analiza qué compra el Estado chileno y prioriza oportunidades de negocio.",
    )
    parser.add_argument("-v", "--verboso", action="store_true", help="Muestra logs de depuración.")
    sub = parser.add_subparsers(dest="comando", required=True)

    def agregar_opciones_ingesta(p: argparse.ArgumentParser) -> None:
        p.add_argument("--fecha", help="Fecha de publicación a consultar (YYYY-MM-DD).")
        p.add_argument("--estado", default="activas", help="Estado a consultar (por defecto: activas).")
        p.add_argument("--limite", type=int, help="Máximo de licitaciones a procesar.")
        p.add_argument("--refrescar", action="store_true", help="Vuelve a bajar las ya guardadas.")
        p.add_argument(
            "--sin-detalle",
            action="store_true",
            help="No pide la ficha completa; más rápido pero sin items ni monto.",
        )

    def agregar_opciones_analisis(p: argparse.ArgumentParser) -> None:
        p.add_argument("--top", type=int, default=15, help="Cuántas licitaciones evaluar.")
        p.add_argument("--paralelo", type=int, default=3, help="Evaluaciones simultáneas.")
        p.add_argument("--reevaluar", action="store_true", help="Rehace evaluaciones existentes.")

    p_ingestar = sub.add_parser("ingestar", help="Descarga licitaciones y las guarda.")
    agregar_opciones_ingesta(p_ingestar)
    p_ingestar.set_defaults(func=cmd_ingestar)

    p_filtrar = sub.add_parser("filtrar", help="Aplica el prefiltro heurístico del perfil.")
    p_filtrar.set_defaults(func=cmd_filtrar)

    p_analizar = sub.add_parser("analizar", help="Evalúa con Claude las licitaciones aprobadas.")
    agregar_opciones_analisis(p_analizar)
    p_analizar.set_defaults(func=cmd_analizar)

    p_reporte = sub.add_parser("reporte", help="Muestra el ranking y opcionalmente lo escribe.")
    p_reporte.add_argument("--top", type=int, help="Cuántas oportunidades mostrar.")
    p_reporte.add_argument("--salida", help="Ruta del informe Markdown a generar.")
    p_reporte.set_defaults(func=cmd_reporte)

    p_pipeline = sub.add_parser("pipeline", help="Ingesta, filtra, analiza y reporta de una vez.")
    agregar_opciones_ingesta(p_pipeline)
    agregar_opciones_analisis(p_pipeline)
    p_pipeline.add_argument("--salida", help="Ruta del informe Markdown a generar.")
    p_pipeline.set_defaults(func=cmd_pipeline)

    p_recolectar = sub.add_parser(
        "recolectar",
        help="Descarga el histórico de adjudicaciones vía OCDS. No requiere ticket.",
    )
    p_recolectar.add_argument(
        "--meses", type=int, default=12, help="Meses hacia atrás a recolectar (por defecto 12)."
    )
    p_recolectar.add_argument(
        "--modalidad",
        action="append",
        choices=["licitacion", "trato_directo", "convenio_marco"],
        help="Modalidad a recolectar; repetible. Por defecto solo licitacion.",
    )
    p_recolectar.add_argument("--hilos", type=int, default=4, help="Descargas simultáneas.")
    p_recolectar.add_argument(
        "--rps",
        type=float,
        default=5.0,
        help="Peticiones por segundo. Medido: sobre 5 la API empieza a responder 429.",
    )
    p_recolectar.add_argument(
        "--limite-mes",
        type=int,
        dest="limite_mes",
        help="Tope de procesos por mes; útil para muestrear antes de una corrida larga.",
    )
    p_recolectar.add_argument(
        "--refrescar", action="store_true", help="Vuelve a bajar meses ya completados."
    )
    p_recolectar.set_defaults(func=cmd_recolectar)

    p_demanda = sub.add_parser(
        "demanda",
        help="Descarga los llamados publicados: qué está pidiendo el Estado. No requiere ticket.",
    )
    p_demanda.add_argument(
        "--meses",
        type=int,
        default=3,
        help="Meses hacia atrás, incluyendo el actual (por defecto 3).",
    )
    p_demanda.add_argument(
        "--modalidad",
        action="append",
        choices=["licitacion", "trato_directo", "convenio_marco"],
        help="Modalidad a recolectar; repetible. Por defecto solo licitacion.",
    )
    p_demanda.add_argument("--hilos", type=int, default=4, help="Descargas simultáneas.")
    p_demanda.add_argument(
        "--rps",
        type=float,
        default=5.0,
        help="Peticiones por segundo. Medido: sobre 5 la API empieza a responder 429.",
    )
    p_demanda.add_argument(
        "--limite-mes",
        type=int,
        dest="limite_mes",
        help="Tope de llamados por mes; útil para muestrear antes de una corrida larga.",
    )
    p_demanda.add_argument(
        "--refrescar", action="store_true", help="Vuelve a bajar los llamados ya guardados."
    )
    p_demanda.set_defaults(func=cmd_demanda)

    p_pidiendo = sub.add_parser(
        "pidiendo", help="Agrega los llamados por categoría: qué se está solicitando y qué sigue abierto."
    )
    p_pidiendo.add_argument("--top", type=int, default=25, help="Cuántas categorías mostrar.")
    p_pidiendo.add_argument(
        "--min-llamados",
        type=int,
        default=3,
        dest="min_llamados",
        help="Llamados mínimos para considerar una categoría.",
    )
    p_pidiendo.add_argument(
        "--vigentes",
        action="store_true",
        help="Agrega solo sobre lo que todavía está abierto.",
    )
    p_pidiendo.add_argument(
        "--abiertos",
        action="store_true",
        help="Lista además los llamados con plazo todavía corriendo.",
    )
    grupo = p_pidiendo.add_mutually_exclusive_group()
    grupo.add_argument(
        "--solo-bienes", action="store_true", dest="solo_bienes", help="Solo mercadería física."
    )
    grupo.add_argument(
        "--solo-servicios", action="store_true", dest="solo_servicios", help="Solo servicios."
    )
    p_pidiendo.add_argument(
        "--dias-min",
        type=int,
        default=0,
        dest="dias_min",
        help="Descarta los llamados que cierran antes de N días; preparar una oferta toma tiempo.",
    )
    p_pidiendo.add_argument(
        "--json",
        action="store_true",
        help="Emite JSON en vez de tablas, para consumo por otro programa o agente.",
    )
    p_pidiendo.add_argument("--salida", help="Ruta del informe Markdown a generar.")
    p_pidiendo.set_defaults(func=cmd_pidiendo)

    p_registrar = sub.add_parser(
        "registrar",
        help="Guarda una decisión sobre una oportunidad (la usan los agentes y tú).",
    )
    p_registrar.add_argument(
        "clave", help="Código del proceso, o 'familia:7213' si es una categoría."
    )
    p_registrar.add_argument(
        "--estado",
        required=True,
        choices=list(ESTADOS_PIPELINE),
        help="Estado al que pasa la oportunidad.",
    )
    p_registrar.add_argument(
        "--tipo", default="licitacion", choices=["licitacion", "categoria"]
    )
    p_registrar.add_argument("--codigo", dest="codigo_proceso", help="Código del proceso.")
    p_registrar.add_argument("--familia", help="Familia UNSPSC de 4 dígitos.")
    p_registrar.add_argument("--titulo")
    p_registrar.add_argument("--organismo")
    p_registrar.add_argument(
        "--veredicto", choices=["viable", "viable_con_condiciones", "descartado"]
    )
    p_registrar.add_argument(
        "--modalidad",
        choices=[
            "importar", "comprar_local", "subcontratar", "servicio_propio", "freelance",
        ],
    )
    p_registrar.add_argument("--prioridad", choices=["alta", "media", "baja"])
    p_registrar.add_argument(
        "--motivo", help="Por qué se decidió esto. Es lo que evita repetir el análisis."
    )
    p_registrar.add_argument("--costo", type=float, help="Costo interno estimado en CLP.")
    p_registrar.add_argument("--precio", type=float, help="Precio ofertado en CLP.")
    p_registrar.add_argument("--margen", type=float, help="Margen sobre costo, en %%.")
    p_registrar.add_argument("--cierre", help="Fecha de cierre (ISO).")
    p_registrar.add_argument("--documentos", help="Carpeta donde quedaron los entregables.")
    p_registrar.add_argument("--nota", help="Nota de este cambio de estado.")
    p_registrar.add_argument(
        "--autor", help="Quién lo registró (por ejemplo, el nombre del agente)."
    )
    p_registrar.set_defaults(func=cmd_registrar)

    p_pipeline_estado = sub.add_parser(
        "pipeline-estado", help="Embudo comercial: qué se analizó, descartó, ofertó y ganó."
    )
    p_pipeline_estado.add_argument("--estado", choices=list(ESTADOS_PIPELINE))
    p_pipeline_estado.add_argument("--familia", help="Filtra por familia UNSPSC.")
    p_pipeline_estado.add_argument("--json", action="store_true")
    p_pipeline_estado.set_defaults(func=cmd_pipeline_estado)

    p_historial = sub.add_parser(
        "historial", help="El recorrido completo de una oportunidad, con sus decisiones."
    )
    p_historial.add_argument("clave")
    p_historial.set_defaults(func=cmd_historial)

    p_proveedor = sub.add_parser(
        "proveedor", help="Registra o lista empresas y profesionales encontrados."
    )
    p_proveedor.add_argument("nombre", nargs="?", help="Nombre a registrar. Sin él, lista.")
    p_proveedor.add_argument("--tipo", default="empresa", choices=["empresa", "profesional"])
    p_proveedor.add_argument("--rubro")
    p_proveedor.add_argument("--familia", help="Familia UNSPSC con la que calza.")
    p_proveedor.add_argument("--contacto", help="Correo, teléfono o formulario.")
    p_proveedor.add_argument("--sitio")
    p_proveedor.add_argument("--fuente", help="De dónde salió el dato.")
    p_proveedor.add_argument("--confianza", choices=["alta", "media", "baja"])
    p_proveedor.add_argument("--certificaciones")
    p_proveedor.add_argument("--notas")
    p_proveedor.add_argument("--desempeno", help="Cómo resultó en trabajos anteriores.")
    p_proveedor.add_argument(
        "--filtro-tipo", dest="filtro_tipo", choices=["empresa", "profesional"],
        help="Al listar, filtra por tipo.",
    )
    p_proveedor.add_argument("--json", action="store_true")
    p_proveedor.set_defaults(func=cmd_proveedor)

    p_cotizacion = sub.add_parser(
        "cotizacion", help="Registra o lista cotizaciones pedidas a proveedores."
    )
    p_cotizacion.add_argument("clave", nargs="?", help="Código de la oportunidad.")
    p_cotizacion.add_argument("--proveedor", help="Nombre del proveedor. Sin él, lista.")
    p_cotizacion.add_argument(
        "--estado", default="solicitada",
        choices=["solicitada", "cotizo", "no_respondio", "declino"],
    )
    p_cotizacion.add_argument("--precio", type=float)
    p_cotizacion.add_argument("--plazo", type=int, help="Plazo de entrega en días.")
    p_cotizacion.add_argument("--forma-pago", dest="forma_pago")
    p_cotizacion.add_argument("--vigencia")
    p_cotizacion.add_argument("--notas")
    p_cotizacion.add_argument("--json", action="store_true")
    p_cotizacion.set_defaults(func=cmd_cotizacion)

    p_garantia = sub.add_parser(
        "garantia", help="Registra o lista instrumentos para cubrir garantías."
    )
    p_garantia.add_argument("proveedor", nargs="?", help="Emisor. Sin él, lista.")
    p_garantia.add_argument(
        "--instrumento",
        default="poliza_caucion",
        choices=["boleta_bancaria", "poliza_caucion", "sgr", "corfo", "otro"],
    )
    p_garantia.add_argument("--costo-pct", dest="costo_pct", type=float, help="Prima o comisión en %%.")
    p_garantia.add_argument("--plazo", type=int, help="Días de emisión.")
    p_garantia.add_argument("--requisitos")
    p_garantia.add_argument(
        "--inmoviliza", type=int, choices=[0, 1],
        help="1 si inmoviliza capital o línea de crédito, 0 si no.",
    )
    p_garantia.add_argument("--fuente")
    p_garantia.add_argument("--notas")
    p_garantia.add_argument("--json", action="store_true")
    p_garantia.set_defaults(func=cmd_garantia)

    p_contexto = sub.add_parser(
        "contexto",
        help="Exporta o importa el contexto portable (precio, competencia y decisiones).",
    )
    p_contexto.add_argument(
        "archivo", help="Ruta del JSON, por ejemplo contexto/contexto.json."
    )
    p_contexto.add_argument(
        "--importar",
        action="store_true",
        help="Carga el archivo en la base en vez de generarlo.",
    )
    p_contexto.set_defaults(func=cmd_contexto)

    p_ficha = sub.add_parser(
        "ficha",
        help="Detalle de un llamado: qué pide, quién lo pide y contra quién competirías.",
    )
    p_ficha.add_argument("codigo", help="Código del proceso, por ejemplo 2439-22-LP26.")
    p_ficha.add_argument("--json", action="store_true", help="Emite JSON en vez de texto.")
    p_ficha.set_defaults(func=cmd_ficha)

    p_nichos = sub.add_parser(
        "nichos", help="Agrega el histórico por categoría y ordena los nichos más atractivos."
    )
    p_nichos.add_argument("--top", type=int, default=25, help="Cuántos nichos mostrar.")
    p_nichos.add_argument(
        "--min-adjudicaciones",
        type=int,
        default=8,
        help="Adjudicaciones mínimas para considerar una familia.",
    )
    p_nichos.add_argument(
        "--min-monto", type=float, default=20_000_000, help="Monto histórico mínimo en CLP."
    )
    p_nichos.add_argument(
        "--solo-bienes",
        action="store_true",
        dest="solo_bienes",
        help="Restringe a mercadería física. Por defecto entran bienes y servicios.",
    )
    p_nichos.add_argument("--salida", help="Ruta del informe Markdown a generar.")
    p_nichos.set_defaults(func=cmd_nichos)

    p_inspeccionar = sub.add_parser(
        "inspeccionar",
        help="Describe la estructura de un CSV o ZIP de Datos Abiertos de ChileCompra.",
    )
    p_inspeccionar.add_argument("archivo", help="Ruta al .csv o .zip descargado.")
    p_inspeccionar.add_argument(
        "--filas", type=int, default=20_000, help="Filas a muestrear (por defecto 20.000)."
    )
    p_inspeccionar.add_argument(
        "--listar", action="store_true", help="Solo lista los archivos dentro del ZIP."
    )
    p_inspeccionar.set_defaults(func=cmd_inspeccionar)

    p_estado = sub.add_parser("estado", help="Resumen de la configuración y de la base.")
    p_estado.set_defaults(func=cmd_estado)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _construir_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verboso else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        config = Config.cargar()
        perfil = cargar_perfil(config.perfil_path)
    except ErrorConfig as exc:
        console.print(f"[bold red]Error de configuración:[/] {exc}")
        return 2

    # `reporte` acepta --top opcional; el resto lo define con default.
    if not hasattr(args, "top"):
        args.top = None

    try:
        return args.func(args, config, perfil)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrumpido por el usuario.[/]")
        return 130
    except Exception as exc:  # noqa: BLE001 — frontera de la CLI
        console.print(f"[bold red]Error:[/] {exc}")
        if args.verboso:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
