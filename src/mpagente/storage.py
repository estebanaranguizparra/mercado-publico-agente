"""Persistencia en SQLite.

Se guarda el JSON crudo de cada licitación además de las columnas indexadas,
para poder reprocesar con criterios nuevos sin volver a consultar la API.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from .models import Evaluacion, Licitacion
from .ocds_parser import Adjudicacion, Solicitud

ESQUEMA = """
CREATE TABLE IF NOT EXISTS licitaciones (
    codigo             TEXT PRIMARY KEY,
    nombre             TEXT,
    estado             TEXT,
    tipo               TEXT,
    organismo          TEXT,
    region             TEXT,
    monto_estimado     REAL,
    moneda             TEXT,
    fecha_cierre       TEXT,
    fecha_publicacion  TEXT,
    payload            TEXT NOT NULL,
    ingresada_en       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prefiltro (
    codigo       TEXT PRIMARY KEY,
    aprobado     INTEGER NOT NULL,
    puntaje      REAL NOT NULL,
    motivos      TEXT NOT NULL,
    calculado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluaciones (
    codigo          TEXT PRIMARY KEY,
    puntaje_total   INTEGER NOT NULL,
    estrategia      TEXT NOT NULL,
    margen_pct      REAL,
    confianza       TEXT,
    payload         TEXT NOT NULL,
    modelo          TEXT NOT NULL,
    tokens_entrada  INTEGER DEFAULT 0,
    tokens_salida   INTEGER DEFAULT 0,
    evaluada_en     TEXT NOT NULL
);

-- Datos históricos que alimentan la búsqueda autónoma de nichos.
CREATE TABLE IF NOT EXISTS adjudicaciones (
    award_uid      TEXT PRIMARY KEY,
    ocid           TEXT,
    codigo         TEXT,
    modalidad      TEXT,
    titulo         TEXT,
    estado         TEXT,
    fecha          TEXT,
    anio           INTEGER,
    mes            INTEGER,
    comprador      TEXT,
    comprador_id   TEXT,
    region         TEXT,
    proveedor      TEXT,
    proveedor_id   TEXT,
    monto          REAL,
    moneda         TEXT,
    n_oferentes    INTEGER,
    recolectada_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adjudicacion_items (
    award_uid       TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    unspsc          TEXT,
    familia         TEXT,
    descripcion     TEXT,
    cantidad        REAL,
    unidad          TEXT,
    precio_unitario REAL,
    moneda          TEXT,
    PRIMARY KEY (award_uid, item_id)
);

-- Lo que el Estado pide, tomado de la ficha `tender`. Se puebla desde que el
-- llamado se publica, sin esperar la adjudicación: es la base para responder
-- qué se está solicitando y no solo qué terminó comprándose.
CREATE TABLE IF NOT EXISTS solicitudes (
    codigo         TEXT PRIMARY KEY,
    ocid           TEXT,
    modalidad      TEXT,
    titulo         TEXT,
    descripcion    TEXT,
    estado         TEXT,
    tipo_proceso   TEXT,
    comprador      TEXT,
    comprador_id   TEXT,
    region         TEXT,
    publicada      TEXT,
    cierre         TEXT,
    anio           INTEGER,
    mes            INTEGER,
    recolectada_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS solicitud_items (
    codigo      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    unspsc      TEXT,
    familia     TEXT,
    descripcion TEXT,
    cantidad    REAL,
    unidad      TEXT,
    PRIMARY KEY (codigo, item_id)
);

-- El pipeline comercial: qué se decidió sobre cada oportunidad y por qué.
-- Sin esto, cada análisis parte de cero y se vuelve a evaluar lo ya descartado.
-- `clave` es el código del proceso, o "familia:NNNN" cuando la oportunidad es
-- una categoría completa en vez de una licitación puntual.
CREATE TABLE IF NOT EXISTS oportunidades (
    clave           TEXT PRIMARY KEY,
    tipo            TEXT NOT NULL DEFAULT 'licitacion',
    codigo          TEXT,
    familia         TEXT,
    titulo          TEXT,
    organismo       TEXT,
    estado          TEXT NOT NULL DEFAULT 'nueva',
    veredicto       TEXT,
    modalidad       TEXT,
    prioridad       TEXT,
    motivo          TEXT,
    costo_estimado  REAL,
    precio_ofertado REAL,
    margen_pct      REAL,
    cierre          TEXT,
    documentos      TEXT,
    creada_en       TEXT NOT NULL,
    actualizada_en  TEXT NOT NULL
);

-- Cada cambio de estado queda registrado: sirve para saber qué se decidió,
-- cuándo y con qué argumento, sin depender de la memoria de nadie.
CREATE TABLE IF NOT EXISTS oportunidad_eventos (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    clave    TEXT NOT NULL,
    estado   TEXT NOT NULL,
    nota     TEXT,
    autor    TEXT,
    ocurrio_en TEXT NOT NULL
);

-- Quién puede proveer o ejecutar: empresas y profesionales ya encontrados.
-- Existe para no volver a buscar en la web lo que ya se buscó, y para acumular
-- desempeño real: un proveedor que cotizó rápido y cumplió vale más que uno
-- nuevo con mejor precio en el papel.
CREATE TABLE IF NOT EXISTS proveedores (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre       TEXT NOT NULL,
    tipo         TEXT NOT NULL DEFAULT 'empresa',   -- empresa | profesional
    rubro        TEXT,
    familia      TEXT,
    contacto     TEXT,
    sitio        TEXT,
    fuente       TEXT,
    confianza    TEXT,
    certificaciones TEXT,
    notas        TEXT,
    desempeno    TEXT,
    creado_en    TEXT NOT NULL,
    actualizado_en TEXT NOT NULL,
    UNIQUE (nombre, tipo)
);

-- Cotizaciones pedidas y recibidas. El estado "no_respondio" se guarda a
-- propósito: es información sobre la confiabilidad del proveedor.
CREATE TABLE IF NOT EXISTS cotizaciones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    clave         TEXT NOT NULL,
    proveedor     TEXT NOT NULL,
    estado        TEXT NOT NULL DEFAULT 'solicitada',
    precio        REAL,
    plazo_dias    INTEGER,
    forma_pago    TEXT,
    vigencia      TEXT,
    notas         TEXT,
    solicitada_en TEXT NOT NULL,
    actualizada_en TEXT NOT NULL,
    UNIQUE (clave, proveedor)
);

-- Instrumentos para cubrir garantías. Es referencia reutilizable entre
-- licitaciones: las condiciones de una SGR no cambian de una semana a otra.
CREATE TABLE IF NOT EXISTS instrumentos_garantia (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    proveedor       TEXT NOT NULL,
    instrumento     TEXT NOT NULL,
    costo_pct       REAL,
    plazo_dias      INTEGER,
    requisitos      TEXT,
    inmoviliza_capital INTEGER DEFAULT 1,
    fuente          TEXT,
    notas           TEXT,
    verificado_en   TEXT NOT NULL,
    UNIQUE (proveedor, instrumento)
);

-- Precio y competencia por familia, ya agregados. Existe para que una corrida
-- en la nube tenga la referencia histórica sin arrastrar la base entera: el
-- agregado son ~170 KB contra los 112 MB de las adjudicaciones crudas.
-- `contexto_familia()` la usa como respaldo cuando no hay adjudicaciones.
CREATE TABLE IF NOT EXISTS referencia_familias (
    familia            TEXT PRIMARY KEY,
    n_adjudicaciones   INTEGER,
    n_proveedores      INTEGER,
    oferentes_promedio REAL,
    precio_p25         REAL,
    precio_mediano     REAL,
    precio_p75         REAL,
    monto_total        REAL,
    origen             TEXT,
    actualizada_en     TEXT NOT NULL
);

-- Permite reanudar una recolección interrumpida sin repetir descargas.
-- `estado` distingue lo terminal (adjudicado, desierto) de lo que todavía
-- puede cambiar: un proceso publicado se adjudica semanas después, así que
-- los pendientes hay que volver a consultarlos en la siguiente corrida.
CREATE TABLE IF NOT EXISTS procesos_vistos (
    codigo    TEXT NOT NULL,
    modalidad TEXT NOT NULL,
    estado    TEXT NOT NULL DEFAULT 'pendiente',
    visto_en  TEXT NOT NULL,
    PRIMARY KEY (codigo, modalidad)
);

CREATE TABLE IF NOT EXISTS recoleccion_meses (
    modalidad      TEXT NOT NULL,
    anio           INTEGER NOT NULL,
    mes            INTEGER NOT NULL,
    procesos       INTEGER,
    adjudicaciones INTEGER,
    pendientes     INTEGER DEFAULT 0,
    completado_en  TEXT NOT NULL,
    PRIMARY KEY (modalidad, anio, mes)
);

CREATE INDEX IF NOT EXISTS idx_lic_cierre    ON licitaciones (fecha_cierre);
CREATE INDEX IF NOT EXISTS idx_eval_puntaje  ON evaluaciones (puntaje_total DESC);
CREATE INDEX IF NOT EXISTS idx_adj_periodo   ON adjudicaciones (anio, mes);
CREATE INDEX IF NOT EXISTS idx_adj_proveedor ON adjudicaciones (proveedor_id);
CREATE INDEX IF NOT EXISTS idx_item_familia  ON adjudicacion_items (familia);
CREATE INDEX IF NOT EXISTS idx_item_unspsc   ON adjudicacion_items (unspsc);
CREATE INDEX IF NOT EXISTS idx_sol_cierre    ON solicitudes (cierre);
CREATE INDEX IF NOT EXISTS idx_sol_periodo   ON solicitudes (anio, mes);
CREATE INDEX IF NOT EXISTS idx_sol_familia   ON solicitud_items (familia);
CREATE INDEX IF NOT EXISTS idx_oport_estado  ON oportunidades (estado);
CREATE INDEX IF NOT EXISTS idx_oport_familia ON oportunidades (familia);
CREATE INDEX IF NOT EXISTS idx_evento_clave  ON oportunidad_eventos (clave);
"""

# Estados del pipeline, en el orden en que avanzan. `descartada` y `perdida`
# son finales; se guardan igual porque saber qué NO hacer vale tanto como el resto.
ESTADOS_PIPELINE: tuple[str, ...] = (
    "nueva",
    "analizada",
    "descartada",
    "en_preparacion",
    "ofertada",
    "ganada",
    "perdida",
)


class Almacen:
    def __init__(self, ruta: Path) -> None:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(ruta)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(ESQUEMA)
        self._migrar()
        self._con.commit()

    def _migrar(self) -> None:
        """Añade columnas que no existían en versiones anteriores del esquema.

        `CREATE TABLE IF NOT EXISTS` no altera tablas ya creadas, así que una
        base hecha con una versión previa se quedaría sin las columnas nuevas.
        """
        columnas = {
            f["name"] for f in self._con.execute("PRAGMA table_info(procesos_vistos)")
        }
        if columnas and "estado" not in columnas:
            self._con.execute(
                "ALTER TABLE procesos_vistos "
                "ADD COLUMN estado TEXT NOT NULL DEFAULT 'pendiente'"
            )

        columnas_meses = {
            f["name"] for f in self._con.execute("PRAGMA table_info(recoleccion_meses)")
        }
        if columnas_meses and "pendientes" not in columnas_meses:
            self._con.execute("ALTER TABLE recoleccion_meses ADD COLUMN pendientes INTEGER DEFAULT 0")

    # -- ciclo de vida ----------------------------------------------------- #

    def cerrar(self) -> None:
        self._con.close()

    def __enter__(self) -> Almacen:
        return self

    def __exit__(self, *_: object) -> None:
        self.cerrar()

    # -- licitaciones ------------------------------------------------------ #

    def guardar_licitacion(self, lic: Licitacion) -> None:
        self._con.execute(
            """
            INSERT INTO licitaciones (
                codigo, nombre, estado, tipo, organismo, region,
                monto_estimado, moneda, fecha_cierre, fecha_publicacion,
                payload, ingresada_en
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(codigo) DO UPDATE SET
                nombre=excluded.nombre,
                estado=excluded.estado,
                tipo=excluded.tipo,
                organismo=excluded.organismo,
                region=excluded.region,
                monto_estimado=excluded.monto_estimado,
                moneda=excluded.moneda,
                fecha_cierre=excluded.fecha_cierre,
                fecha_publicacion=excluded.fecha_publicacion,
                payload=excluded.payload,
                ingresada_en=excluded.ingresada_en
            """,
            (
                lic.codigo_externo,
                lic.nombre,
                lic.estado,
                lic.tipo,
                lic.organismo,
                lic.region,
                lic.monto_estimado,
                lic.moneda,
                lic.fecha_cierre.isoformat() if lic.fecha_cierre else None,
                lic.fecha_publicacion.isoformat() if lic.fecha_publicacion else None,
                lic.model_dump_json(by_alias=True),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._con.commit()

    def guardar_licitaciones(self, licitaciones: Iterable[Licitacion]) -> int:
        total = 0
        for lic in licitaciones:
            self.guardar_licitacion(lic)
            total += 1
        return total

    def obtener_licitacion(self, codigo: str) -> Licitacion | None:
        fila = self._con.execute(
            "SELECT payload FROM licitaciones WHERE codigo = ?", (codigo,)
        ).fetchone()
        return Licitacion.model_validate_json(fila["payload"]) if fila else None

    def listar_licitaciones(self) -> list[Licitacion]:
        filas = self._con.execute(
            "SELECT payload FROM licitaciones ORDER BY fecha_cierre"
        ).fetchall()
        return [Licitacion.model_validate_json(f["payload"]) for f in filas]

    def codigos_existentes(self) -> set[str]:
        return {f["codigo"] for f in self._con.execute("SELECT codigo FROM licitaciones")}

    # -- prefiltro --------------------------------------------------------- #

    def guardar_prefiltro(
        self, codigo: str, *, aprobado: bool, puntaje: float, motivos: list[str]
    ) -> None:
        self._con.execute(
            """
            INSERT INTO prefiltro (codigo, aprobado, puntaje, motivos, calculado_en)
            VALUES (?,?,?,?,?)
            ON CONFLICT(codigo) DO UPDATE SET
                aprobado=excluded.aprobado,
                puntaje=excluded.puntaje,
                motivos=excluded.motivos,
                calculado_en=excluded.calculado_en
            """,
            (
                codigo,
                int(aprobado),
                puntaje,
                json.dumps(motivos, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._con.commit()

    def codigos_aprobados_prefiltro(self) -> list[str]:
        filas = self._con.execute(
            "SELECT codigo FROM prefiltro WHERE aprobado = 1 ORDER BY puntaje DESC"
        ).fetchall()
        return [f["codigo"] for f in filas]

    # -- evaluaciones ------------------------------------------------------ #

    def guardar_evaluacion(
        self,
        evaluacion: Evaluacion,
        *,
        modelo: str,
        tokens_entrada: int = 0,
        tokens_salida: int = 0,
    ) -> None:
        self._con.execute(
            """
            INSERT INTO evaluaciones (
                codigo, puntaje_total, estrategia, margen_pct, confianza,
                payload, modelo, tokens_entrada, tokens_salida, evaluada_en
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(codigo) DO UPDATE SET
                puntaje_total=excluded.puntaje_total,
                estrategia=excluded.estrategia,
                margen_pct=excluded.margen_pct,
                confianza=excluded.confianza,
                payload=excluded.payload,
                modelo=excluded.modelo,
                tokens_entrada=excluded.tokens_entrada,
                tokens_salida=excluded.tokens_salida,
                evaluada_en=excluded.evaluada_en
            """,
            (
                evaluacion.codigo_externo,
                evaluacion.puntaje_total,
                evaluacion.estrategia,
                evaluacion.margen_estimado_pct,
                evaluacion.confianza,
                evaluacion.model_dump_json(),
                modelo,
                tokens_entrada,
                tokens_salida,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._con.commit()

    def codigos_evaluados(self) -> set[str]:
        return {f["codigo"] for f in self._con.execute("SELECT codigo FROM evaluaciones")}

    def ranking(self, limite: int | None = None) -> list[tuple[Evaluacion, Licitacion]]:
        """Evaluaciones ordenadas por puntaje, junto con su licitación."""
        sql = """
            SELECT e.payload AS eval_payload, l.payload AS lic_payload
            FROM evaluaciones e
            JOIN licitaciones l ON l.codigo = e.codigo
            ORDER BY e.puntaje_total DESC, e.margen_pct DESC
        """
        if limite is not None:
            sql += f" LIMIT {int(limite)}"
        return [
            (
                Evaluacion.model_validate_json(f["eval_payload"]),
                Licitacion.model_validate_json(f["lic_payload"]),
            )
            for f in self._con.execute(sql).fetchall()
        ]

    # -- adjudicaciones históricas (OCDS) ---------------------------------- #

    def guardar_adjudicaciones(self, adjudicaciones: Sequence[Adjudicacion]) -> int:
        """Inserta un lote completo en una sola transacción."""
        if not adjudicaciones:
            return 0

        ahora = datetime.now().isoformat(timespec="seconds")
        filas = [
            (
                a.award_uid,
                a.ocid,
                a.codigo,
                a.modalidad,
                a.titulo,
                a.estado,
                a.fecha.isoformat() if a.fecha else None,
                a.anio,
                a.mes,
                a.comprador,
                a.comprador_id,
                a.region,
                a.proveedor,
                a.proveedor_id,
                a.monto,
                a.moneda,
                a.n_oferentes,
                ahora,
            )
            for a in adjudicaciones
        ]
        items = [
            (
                a.award_uid,
                i.item_id,
                i.unspsc,
                i.familia_unspsc,
                i.descripcion,
                i.cantidad,
                i.unidad,
                i.precio_unitario,
                i.moneda,
            )
            for a in adjudicaciones
            for i in a.items
        ]

        self._con.executemany(
            "INSERT OR REPLACE INTO adjudicaciones VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            filas,
        )
        if items:
            self._con.executemany(
                "INSERT OR REPLACE INTO adjudicacion_items VALUES (?,?,?,?,?,?,?,?,?)", items
            )
        self._con.commit()
        return len(filas)

    # -- solicitudes: lo que el Estado pide (OCDS tender) ------------------- #

    def guardar_solicitudes(self, solicitudes: Sequence[Solicitud]) -> int:
        """Inserta un lote de llamados con sus items en una sola transacción."""
        if not solicitudes:
            return 0

        ahora = datetime.now().isoformat(timespec="seconds")
        filas = [
            (
                s.codigo,
                s.ocid,
                s.modalidad,
                s.titulo,
                s.descripcion,
                s.estado,
                s.tipo_proceso,
                s.comprador,
                s.comprador_id,
                s.region,
                s.publicada.isoformat() if s.publicada else None,
                s.cierre.isoformat() if s.cierre else None,
                s.anio,
                s.mes,
                ahora,
            )
            for s in solicitudes
        ]
        items = [
            (s.codigo, i.item_id, i.unspsc, i.familia_unspsc, i.descripcion, i.cantidad, i.unidad)
            for s in solicitudes
            for i in s.items
        ]

        self._con.executemany(
            "INSERT OR REPLACE INTO solicitudes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", filas
        )
        if items:
            self._con.executemany(
                "INSERT OR REPLACE INTO solicitud_items VALUES (?,?,?,?,?,?,?)", items
            )
        self._con.commit()
        return len(filas)

    def solicitudes_cerradas(self, modalidad: str) -> set[str]:
        """Códigos ya guardados cuyo plazo venció: no van a cambiar más.

        Los que siguen abiertos se vuelven a bajar, porque un llamado vigente
        todavía puede sumar items o correr su fecha de cierre.
        """
        return {
            f["codigo"]
            for f in self._con.execute(
                "SELECT codigo FROM solicitudes "
                "WHERE modalidad = ? AND cierre IS NOT NULL AND cierre < ?",
                (modalidad, datetime.now().isoformat()),
            )
        }

    def solicitudes_vigentes(
        self, limite: int | None = None, *, dias_minimos: int = 0
    ) -> list[sqlite3.Row]:
        """Llamados cuyo plazo de postulación sigue abierto, del que cierra antes.

        `dias_minimos` descarta los que cierran demasiado pronto: preparar una
        oferta toma días, así que un llamado que vence mañana no es accionable.
        """
        desde = datetime.now() + timedelta(days=dias_minimos)
        sql = """
            SELECT s.codigo, s.titulo, s.descripcion, s.tipo_proceso, s.comprador,
                   s.comprador_id, s.region, s.publicada, s.cierre,
                   COUNT(i.item_id) AS n_items,
                   GROUP_CONCAT(DISTINCT i.familia) AS familias
            FROM solicitudes s
            LEFT JOIN solicitud_items i ON i.codigo = s.codigo
            WHERE s.cierre > ?
            GROUP BY s.codigo
            ORDER BY s.cierre ASC
        """
        if limite is not None:
            sql += f" LIMIT {int(limite)}"
        return self._con.execute(sql, (desde.isoformat(),)).fetchall()

    def ficha_solicitud(self, codigo: str) -> dict | None:
        """Un llamado con sus items. None si no está en la base."""
        fila = self._con.execute(
            "SELECT * FROM solicitudes WHERE codigo = ?", (codigo,)
        ).fetchone()
        if fila is None:
            return None

        items = self._con.execute(
            "SELECT item_id, unspsc, familia, descripcion, cantidad, unidad "
            "FROM solicitud_items WHERE codigo = ? ORDER BY item_id",
            (codigo,),
        ).fetchall()

        ficha = dict(fila)
        ficha["items"] = [dict(i) for i in items]
        return ficha

    def contexto_familia(self, familia: str, *, top_proveedores: int = 5) -> dict:
        """Qué se sabe de una familia por lo ya adjudicado: precio, competencia y quién gana.

        Es lo que convierte un llamado suelto en una decisión informada: contra
        quién se compite y a qué precio se ha cerrado antes.
        """
        agregado = self._con.execute(
            """
            SELECT COUNT(DISTINCT a.award_uid) AS n_adjudicaciones,
                   COUNT(DISTINCT a.proveedor_id) AS n_proveedores,
                   COUNT(DISTINCT a.comprador_id) AS n_compradores,
                   AVG(a.n_oferentes) AS oferentes_promedio,
                   SUM(a.monto) AS monto_total
            FROM adjudicacion_items i
            JOIN adjudicaciones a ON a.award_uid = i.award_uid
            WHERE i.familia = ? AND a.monto > 1000
            """,
            (familia,),
        ).fetchone()

        precios = [
            float(f["precio_unitario"])
            for f in self._con.execute(
                "SELECT i.precio_unitario FROM adjudicacion_items i "
                "JOIN adjudicaciones a ON a.award_uid = i.award_uid "
                "WHERE i.familia = ? AND i.precio_unitario > 0 AND a.monto > 1000",
                (familia,),
            )
        ]

        proveedores = self._con.execute(
            """
            SELECT a.proveedor AS proveedor,
                   COUNT(DISTINCT a.award_uid) AS ganadas,
                   SUM(a.monto) AS monto
            FROM adjudicacion_items i
            JOIN adjudicaciones a ON a.award_uid = i.award_uid
            WHERE i.familia = ? AND a.monto > 1000 AND a.proveedor IS NOT NULL
            GROUP BY a.proveedor_id
            ORDER BY monto DESC
            LIMIT ?
            """,
            (familia, top_proveedores),
        ).fetchall()

        demanda = self._con.execute(
            """
            SELECT COUNT(DISTINCT s.codigo) AS n_llamados,
                   SUM(CASE WHEN s.cierre > ? THEN 1 ELSE 0 END) AS vigentes
            FROM solicitud_items i
            JOIN solicitudes s ON s.codigo = i.codigo
            WHERE i.familia = ?
            """,
            (datetime.now().isoformat(), familia),
        ).fetchone()

        ordenados = sorted(precios)
        def _pct(p: float) -> float:
            if not ordenados:
                return 0.0
            k = min(int(p * (len(ordenados) - 1)), len(ordenados) - 1)
            return ordenados[k]

        # Sin adjudicaciones crudas —el caso de una corrida en la nube— se usa
        # el agregado portable, que trae los mismos números ya calculados.
        if not agregado["n_adjudicaciones"]:
            ref = self._con.execute(
                "SELECT * FROM referencia_familias WHERE familia = ?", (familia,)
            ).fetchone()
            if ref:
                return {
                    "familia": familia,
                    "n_adjudicaciones": int(ref["n_adjudicaciones"] or 0),
                    "n_proveedores": int(ref["n_proveedores"] or 0),
                    "n_compradores": 0,
                    "oferentes_promedio": float(ref["oferentes_promedio"] or 0),
                    "monto_total": float(ref["monto_total"] or 0),
                    "precio_p25": float(ref["precio_p25"] or 0),
                    "precio_mediano": float(ref["precio_mediano"] or 0),
                    "precio_p75": float(ref["precio_p75"] or 0),
                    "n_llamados": int(demanda["n_llamados"] or 0),
                    "vigentes": int(demanda["vigentes"] or 0),
                    "proveedores_top": [],
                    "origen": ref["origen"] or "referencia importada",
                }

        return {
            "familia": familia,
            "n_adjudicaciones": int(agregado["n_adjudicaciones"] or 0),
            "n_proveedores": int(agregado["n_proveedores"] or 0),
            "n_compradores": int(agregado["n_compradores"] or 0),
            "oferentes_promedio": float(agregado["oferentes_promedio"] or 0),
            "monto_total": float(agregado["monto_total"] or 0),
            "precio_p25": _pct(0.25),
            "precio_mediano": _pct(0.50),
            "precio_p75": _pct(0.75),
            "n_llamados": int(demanda["n_llamados"] or 0),
            "vigentes": int(demanda["vigentes"] or 0),
            "proveedores_top": [dict(p) for p in proveedores],
        }

    # -- pipeline comercial ------------------------------------------------ #

    def guardar_oportunidad(
        self,
        clave: str,
        *,
        estado: str,
        nota: str | None = None,
        autor: str | None = None,
        **campos: object,
    ) -> dict:
        """Crea o actualiza una oportunidad y deja registrado el cambio.

        Solo se sobrescriben los campos que se pasan explícitamente: así un
        agente puede actualizar el precio sin borrar el motivo que escribió otro.
        """
        if estado not in ESTADOS_PIPELINE:
            raise ValueError(
                f"Estado '{estado}' desconocido. Válidos: {', '.join(ESTADOS_PIPELINE)}"
            )

        ahora = datetime.now().isoformat(timespec="seconds")
        existente = self._con.execute(
            "SELECT * FROM oportunidades WHERE clave = ?", (clave,)
        ).fetchone()

        permitidos = {
            "tipo", "codigo", "familia", "titulo", "organismo", "veredicto",
            "modalidad", "prioridad", "motivo", "costo_estimado",
            "precio_ofertado", "margen_pct", "cierre", "documentos",
        }
        desconocidos = set(campos) - permitidos
        if desconocidos:
            raise ValueError(f"Campos desconocidos: {', '.join(sorted(desconocidos))}")

        datos = dict(existente) if existente else {"clave": clave, "creada_en": ahora}
        datos.update({k: v for k, v in campos.items() if v is not None})
        datos["estado"] = estado
        datos["actualizada_en"] = ahora
        datos.setdefault("tipo", "licitacion")
        datos.setdefault("creada_en", ahora)

        columnas = [
            "clave", "tipo", "codigo", "familia", "titulo", "organismo", "estado",
            "veredicto", "modalidad", "prioridad", "motivo", "costo_estimado",
            "precio_ofertado", "margen_pct", "cierre", "documentos",
            "creada_en", "actualizada_en",
        ]
        self._con.execute(
            f"INSERT OR REPLACE INTO oportunidades ({','.join(columnas)}) "
            f"VALUES ({','.join('?' * len(columnas))})",
            [datos.get(c) for c in columnas],
        )
        self._con.execute(
            "INSERT INTO oportunidad_eventos (clave, estado, nota, autor, ocurrio_en) "
            "VALUES (?,?,?,?,?)",
            (clave, estado, nota, autor, ahora),
        )
        self._con.commit()
        return {c: datos.get(c) for c in columnas}

    def oportunidad(self, clave: str) -> dict | None:
        fila = self._con.execute(
            "SELECT * FROM oportunidades WHERE clave = ?", (clave,)
        ).fetchone()
        return dict(fila) if fila else None

    def listar_oportunidades(
        self, *, estado: str | None = None, familia: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM oportunidades"
        condiciones, parametros = [], []
        if estado:
            condiciones.append("estado = ?")
            parametros.append(estado)
        if familia:
            condiciones.append("familia = ?")
            parametros.append(familia)
        if condiciones:
            sql += " WHERE " + " AND ".join(condiciones)
        sql += " ORDER BY actualizada_en DESC"
        return [dict(f) for f in self._con.execute(sql, parametros)]

    def eventos_oportunidad(self, clave: str) -> list[dict]:
        return [
            dict(f)
            for f in self._con.execute(
                "SELECT estado, nota, autor, ocurrio_en FROM oportunidad_eventos "
                "WHERE clave = ? ORDER BY id",
                (clave,),
            )
        ]

    def resumen_pipeline(self) -> dict:
        """Conteo por estado más los números que interesan del embudo."""
        por_estado = {
            f["estado"]: int(f["n"])
            for f in self._con.execute(
                "SELECT estado, COUNT(*) AS n FROM oportunidades GROUP BY estado"
            )
        }
        totales = self._con.execute(
            """
            SELECT SUM(CASE WHEN estado='ofertada' THEN 1 ELSE 0 END) AS ofertadas,
                   SUM(CASE WHEN estado='ganada'   THEN 1 ELSE 0 END) AS ganadas,
                   SUM(CASE WHEN estado='ganada'   THEN precio_ofertado ELSE 0 END) AS monto_ganado,
                   AVG(CASE WHEN margen_pct IS NOT NULL THEN margen_pct END) AS margen_medio
            FROM oportunidades
            """
        ).fetchone()

        cerradas = (totales["ganadas"] or 0) + (por_estado.get("perdida", 0))
        return {
            "por_estado": {e: por_estado.get(e, 0) for e in ESTADOS_PIPELINE},
            "total": sum(por_estado.values()),
            "ofertadas": int(totales["ofertadas"] or 0),
            "ganadas": int(totales["ganadas"] or 0),
            "monto_ganado": float(totales["monto_ganado"] or 0),
            "margen_medio": float(totales["margen_medio"] or 0),
            # Sobre procesos ya resueltos: ofertar y esperar no cuenta como derrota.
            "tasa_conversion": (totales["ganadas"] or 0) / cerradas if cerradas else None,
        }

    # -- proveedores, cotizaciones y garantías ------------------------------ #

    def guardar_proveedor(self, nombre: str, **campos: object) -> None:
        """Registra o actualiza un proveedor. Solo pisa lo que se le pasa."""
        ahora = datetime.now().isoformat(timespec="seconds")
        tipo = str(campos.get("tipo") or "empresa")
        existente = self._con.execute(
            "SELECT * FROM proveedores WHERE nombre = ? AND tipo = ?", (nombre, tipo)
        ).fetchone()

        datos = dict(existente) if existente else {"creado_en": ahora}
        datos.update({k: v for k, v in campos.items() if v is not None})
        datos.update({"nombre": nombre, "tipo": tipo, "actualizado_en": ahora})
        datos.setdefault("creado_en", ahora)

        columnas = [
            "nombre", "tipo", "rubro", "familia", "contacto", "sitio", "fuente",
            "confianza", "certificaciones", "notas", "desempeno",
            "creado_en", "actualizado_en",
        ]
        self._con.execute(
            f"INSERT OR REPLACE INTO proveedores ({','.join(columnas)}) "
            f"VALUES ({','.join('?' * len(columnas))})",
            [datos.get(c) for c in columnas],
        )
        self._con.commit()

    def listar_proveedores(
        self, *, tipo: str | None = None, familia: str | None = None
    ) -> list[dict]:
        sql, parametros = "SELECT * FROM proveedores", []
        condiciones = []
        if tipo:
            condiciones.append("tipo = ?")
            parametros.append(tipo)
        if familia:
            condiciones.append("familia = ?")
            parametros.append(familia)
        if condiciones:
            sql += " WHERE " + " AND ".join(condiciones)
        return [dict(f) for f in self._con.execute(sql + " ORDER BY nombre", parametros)]

    def guardar_cotizacion(self, clave: str, proveedor: str, **campos: object) -> None:
        ahora = datetime.now().isoformat(timespec="seconds")
        existente = self._con.execute(
            "SELECT * FROM cotizaciones WHERE clave = ? AND proveedor = ?", (clave, proveedor)
        ).fetchone()

        datos = dict(existente) if existente else {"solicitada_en": ahora}
        datos.update({k: v for k, v in campos.items() if v is not None})
        datos.update({"clave": clave, "proveedor": proveedor, "actualizada_en": ahora})
        datos.setdefault("estado", "solicitada")
        datos.setdefault("solicitada_en", ahora)

        columnas = [
            "clave", "proveedor", "estado", "precio", "plazo_dias", "forma_pago",
            "vigencia", "notas", "solicitada_en", "actualizada_en",
        ]
        self._con.execute(
            f"INSERT OR REPLACE INTO cotizaciones ({','.join(columnas)}) "
            f"VALUES ({','.join('?' * len(columnas))})",
            [datos.get(c) for c in columnas],
        )
        self._con.commit()

    def listar_cotizaciones(self, clave: str | None = None) -> list[dict]:
        sql = "SELECT * FROM cotizaciones"
        parametros: tuple = ()
        if clave:
            sql += " WHERE clave = ?"
            parametros = (clave,)
        # Las que cotizaron primero y más barato encabezan el comparativo.
        return [
            dict(f)
            for f in self._con.execute(
                sql + " ORDER BY CASE estado WHEN 'cotizo' THEN 0 ELSE 1 END, precio", parametros
            )
        ]

    def guardar_instrumento_garantia(
        self, proveedor: str, instrumento: str, **campos: object
    ) -> None:
        ahora = datetime.now().isoformat(timespec="seconds")
        existente = self._con.execute(
            "SELECT * FROM instrumentos_garantia WHERE proveedor = ? AND instrumento = ?",
            (proveedor, instrumento),
        ).fetchone()

        datos = dict(existente) if existente else {}
        datos.update({k: v for k, v in campos.items() if v is not None})
        datos.update(
            {"proveedor": proveedor, "instrumento": instrumento, "verificado_en": ahora}
        )

        columnas = [
            "proveedor", "instrumento", "costo_pct", "plazo_dias", "requisitos",
            "inmoviliza_capital", "fuente", "notas", "verificado_en",
        ]
        self._con.execute(
            f"INSERT OR REPLACE INTO instrumentos_garantia ({','.join(columnas)}) "
            f"VALUES ({','.join('?' * len(columnas))})",
            [datos.get(c) for c in columnas],
        )
        self._con.commit()

    def listar_instrumentos_garantia(self) -> list[dict]:
        # Primero los que no inmovilizan capital: son los únicos alcanzables
        # para quien no tiene caja ni línea de crédito.
        return [
            dict(f)
            for f in self._con.execute(
                "SELECT * FROM instrumentos_garantia "
                "ORDER BY inmoviliza_capital ASC, costo_pct ASC"
            )
        ]

    # -- contexto portable (para correr en la nube) ------------------------- #

    def exportar_contexto(self) -> dict:
        """El mínimo que necesita una corrida sin la base completa.

        Lleva el agregado de precio y competencia por familia y el pipeline de
        decisiones. Deja fuera las 42.000 solicitudes y las adjudicaciones
        crudas, que se pueden volver a bajar de la API pero no caben en git.
        """
        familias = []
        for fila in self._con.execute(
            """
            SELECT i.familia AS familia,
                   COUNT(DISTINCT a.award_uid) AS n_adjudicaciones,
                   COUNT(DISTINCT a.proveedor_id) AS n_proveedores,
                   AVG(a.n_oferentes) AS oferentes_promedio,
                   SUM(a.monto) AS monto_total
            FROM adjudicacion_items i
            JOIN adjudicaciones a ON a.award_uid = i.award_uid
            WHERE i.familia IS NOT NULL AND a.monto > 1000
            GROUP BY i.familia
            """
        ):
            precios = sorted(
                float(f["precio_unitario"])
                for f in self._con.execute(
                    "SELECT i.precio_unitario FROM adjudicacion_items i "
                    "JOIN adjudicaciones a ON a.award_uid = i.award_uid "
                    "WHERE i.familia = ? AND i.precio_unitario > 0 AND a.monto > 1000",
                    (fila["familia"],),
                )
            )

            def _pct(p: float) -> float:
                if not precios:
                    return 0.0
                return precios[min(int(p * (len(precios) - 1)), len(precios) - 1)]

            familias.append(
                {
                    "familia": fila["familia"],
                    "n_adjudicaciones": int(fila["n_adjudicaciones"] or 0),
                    "n_proveedores": int(fila["n_proveedores"] or 0),
                    "oferentes_promedio": round(float(fila["oferentes_promedio"] or 0), 3),
                    "precio_p25": _pct(0.25),
                    "precio_mediano": _pct(0.50),
                    "precio_p75": _pct(0.75),
                    "monto_total": float(fila["monto_total"] or 0),
                }
            )

        # Una corrida en la nube no tiene adjudicaciones crudas: solo la
        # referencia que importó. Sin esto, al re-exportar devolvería una lista
        # vacía y el commit destruiría el histórico de precios para siempre.
        calculadas = {f["familia"] for f in familias}
        # Ojo: `NOT IN ()` vacío no se puede expresar en SQL —`NOT IN (NULL)`
        # nunca es verdadero— así que sin familias calculadas se traen todas.
        if calculadas:
            sql = (
                "SELECT * FROM referencia_familias WHERE familia NOT IN "
                f"({','.join('?' * len(calculadas))})"
            )
            parametros = tuple(calculadas)
        else:
            sql, parametros = "SELECT * FROM referencia_familias", ()

        for fila in self._con.execute(sql, parametros):
            familias.append(
                {
                    "familia": fila["familia"],
                    "n_adjudicaciones": int(fila["n_adjudicaciones"] or 0),
                    "n_proveedores": int(fila["n_proveedores"] or 0),
                    "oferentes_promedio": float(fila["oferentes_promedio"] or 0),
                    "precio_p25": float(fila["precio_p25"] or 0),
                    "precio_mediano": float(fila["precio_mediano"] or 0),
                    "precio_p75": float(fila["precio_p75"] or 0),
                    "monto_total": float(fila["monto_total"] or 0),
                    "heredada": True,
                }
            )

        familias.sort(key=lambda f: f["familia"])

        return {
            "generado_en": datetime.now().isoformat(timespec="seconds"),
            "familias": familias,
            "oportunidades": self.listar_oportunidades(),
            "eventos": [
                dict(f)
                for f in self._con.execute(
                    "SELECT clave, estado, nota, autor, ocurrio_en FROM oportunidad_eventos "
                    "ORDER BY id"
                )
            ],
            # Lo que ya se investigó sobre quién provee y cómo cubrir garantías:
            # sin esto, cada corrida nocturna repetiría las mismas búsquedas.
            "proveedores": self.listar_proveedores(),
            "cotizaciones": self.listar_cotizaciones(),
            "instrumentos_garantia": self.listar_instrumentos_garantia(),
        }

    def importar_contexto(self, contexto: dict, *, origen: str = "importado") -> dict:
        """Carga un contexto exportado. Idempotente: se puede repetir sin duplicar."""
        ahora = datetime.now().isoformat(timespec="seconds")

        familias = contexto.get("familias") or []
        self._con.executemany(
            "INSERT OR REPLACE INTO referencia_familias "
            "(familia, n_adjudicaciones, n_proveedores, oferentes_promedio, "
            " precio_p25, precio_mediano, precio_p75, monto_total, origen, actualizada_en) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f["familia"],
                    f.get("n_adjudicaciones"),
                    f.get("n_proveedores"),
                    f.get("oferentes_promedio"),
                    f.get("precio_p25"),
                    f.get("precio_mediano"),
                    f.get("precio_p75"),
                    f.get("monto_total"),
                    origen,
                    ahora,
                )
                for f in familias
            ],
        )

        columnas = [
            "clave", "tipo", "codigo", "familia", "titulo", "organismo", "estado",
            "veredicto", "modalidad", "prioridad", "motivo", "costo_estimado",
            "precio_ofertado", "margen_pct", "cierre", "documentos",
            "creada_en", "actualizada_en",
        ]
        oportunidades = contexto.get("oportunidades") or []
        self._con.executemany(
            f"INSERT OR REPLACE INTO oportunidades ({','.join(columnas)}) "
            f"VALUES ({','.join('?' * len(columnas))})",
            [[o.get(c) for c in columnas] for o in oportunidades],
        )

        # Los eventos se reinsertan desde cero para no duplicar el historial.
        eventos = contexto.get("eventos") or []
        if eventos:
            self._con.execute("DELETE FROM oportunidad_eventos")
            self._con.executemany(
                "INSERT INTO oportunidad_eventos (clave, estado, nota, autor, ocurrio_en) "
                "VALUES (?,?,?,?,?)",
                [
                    (e.get("clave"), e.get("estado"), e.get("nota"), e.get("autor"),
                     e.get("ocurrio_en"))
                    for e in eventos
                ],
            )

        for prov in contexto.get("proveedores") or []:
            nombre = prov.get("nombre")
            if nombre:
                self.guardar_proveedor(
                    nombre,
                    **{k: v for k, v in prov.items()
                       if k not in {"id", "nombre", "creado_en", "actualizado_en"}},
                )

        for cot in contexto.get("cotizaciones") or []:
            if cot.get("clave") and cot.get("proveedor"):
                self.guardar_cotizacion(
                    cot["clave"], cot["proveedor"],
                    **{k: v for k, v in cot.items()
                       if k not in {"id", "clave", "proveedor", "solicitada_en", "actualizada_en"}},
                )

        for ins in contexto.get("instrumentos_garantia") or []:
            if ins.get("proveedor") and ins.get("instrumento"):
                self.guardar_instrumento_garantia(
                    ins["proveedor"], ins["instrumento"],
                    **{k: v for k, v in ins.items()
                       if k not in {"id", "proveedor", "instrumento", "verificado_en"}},
                )

        self._con.commit()
        return {
            "familias": len(familias),
            "oportunidades": len(oportunidades),
            "eventos": len(eventos),
            "proveedores": len(contexto.get("proveedores") or []),
            "cotizaciones": len(contexto.get("cotizaciones") or []),
            "instrumentos": len(contexto.get("instrumentos_garantia") or []),
        }

    def marcar_procesos_vistos(
        self, estados_por_codigo: Iterable[tuple[str, str]], modalidad: str
    ) -> None:
        """Registra el estado de cada proceso consultado (código, estado)."""
        ahora = datetime.now().isoformat(timespec="seconds")
        self._con.executemany(
            """
            INSERT INTO procesos_vistos (codigo, modalidad, estado, visto_en)
            VALUES (?,?,?,?)
            ON CONFLICT(codigo, modalidad) DO UPDATE SET
                estado=excluded.estado, visto_en=excluded.visto_en
            """,
            [(codigo, modalidad, estado, ahora) for codigo, estado in estados_por_codigo],
        )
        self._con.commit()

    def procesos_resueltos(self, modalidad: str) -> set[str]:
        """Códigos que ya no cambiarán; los pendientes quedan fuera a propósito."""
        return {
            f["codigo"]
            for f in self._con.execute(
                "SELECT codigo FROM procesos_vistos "
                "WHERE modalidad = ? AND estado IN ('adjudicado','cerrado_sin_adjudicar')",
                (modalidad,),
            )
        }

    def conteo_por_estado(self, modalidad: str | None = None) -> dict[str, int]:
        sql = "SELECT estado, COUNT(*) AS n FROM procesos_vistos"
        parametros: tuple = ()
        if modalidad:
            sql += " WHERE modalidad = ?"
            parametros = (modalidad,)
        sql += " GROUP BY estado"
        return {f["estado"]: int(f["n"]) for f in self._con.execute(sql, parametros)}

    def mes_completado(self, modalidad: str, anio: int, mes: int) -> bool:
        fila = self._con.execute(
            "SELECT 1 FROM recoleccion_meses WHERE modalidad=? AND anio=? AND mes=?",
            (modalidad, anio, mes),
        ).fetchone()
        return fila is not None

    def marcar_mes_completado(
        self,
        modalidad: str,
        anio: int,
        mes: int,
        *,
        procesos: int,
        adjudicaciones: int,
        pendientes: int = 0,
    ) -> None:
        self._con.execute(
            """
            INSERT OR REPLACE INTO recoleccion_meses
                (modalidad, anio, mes, procesos, adjudicaciones, pendientes, completado_en)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                modalidad,
                anio,
                mes,
                procesos,
                adjudicaciones,
                pendientes,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._con.commit()

    def mes_sin_pendientes(self, modalidad: str, anio: int, mes: int) -> bool:
        """Un mes solo se da por cerrado cuando ya no queda nada por adjudicar."""
        fila = self._con.execute(
            "SELECT pendientes FROM recoleccion_meses WHERE modalidad=? AND anio=? AND mes=?",
            (modalidad, anio, mes),
        ).fetchone()
        return fila is not None and not fila["pendientes"]

    def periodos_recolectados(self) -> list[tuple[str, int, int, int]]:
        return [
            (f["modalidad"], f["anio"], f["mes"], f["adjudicaciones"])
            for f in self._con.execute(
                "SELECT modalidad, anio, mes, adjudicaciones FROM recoleccion_meses "
                "ORDER BY anio DESC, mes DESC"
            )
        ]

    def conexion(self) -> sqlite3.Connection:
        """Acceso directo para las consultas de agregación de nichos."""
        return self._con

    # -- métricas ---------------------------------------------------------- #

    def resumen(self) -> dict[str, int]:
        def contar(tabla: str, donde: str = "") -> int:
            fila = self._con.execute(f"SELECT COUNT(*) AS n FROM {tabla} {donde}").fetchone()
            return int(fila["n"])

        return {
            "licitaciones": contar("licitaciones"),
            "prefiltro_aprobadas": contar("prefiltro", "WHERE aprobado = 1"),
            "prefiltro_rechazadas": contar("prefiltro", "WHERE aprobado = 0"),
            "evaluadas": contar("evaluaciones"),
            "adjudicaciones": contar("adjudicaciones"),
            "items_adjudicados": contar("adjudicacion_items"),
            "meses_recolectados": contar("recoleccion_meses"),
            "solicitudes": contar("solicitudes"),
            "items_solicitados": contar("solicitud_items"),
            "solicitudes_vigentes": contar(
                "solicitudes", f"WHERE cierre > '{datetime.now().isoformat()}'"
            ),
        }
