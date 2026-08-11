# CLAUDE.md

Instrucciones para trabajar sobre este código. El `README.md` explica cómo se
usa el agente; este archivo explica cómo se le hace mantención.

## Qué es

Agente que analiza compras públicas de Chile (Mercado Público / ChileCompra).
Tiene **dos modos independientes** que comparten almacenamiento y CLI:

| Modo | Pregunta que responde | Estado |
|---|---|---|
| **Demanda** (`demanda` → `pidiendo`) | ¿Qué está pidiendo el Estado ahora? | Activo. Es el foco actual. |
| **Nichos** (`recolectar` → `nichos`) | ¿Qué se compró históricamente y a qué precio? | Activo. Da el contexto de precio y competencia al modo demanda. |
| **Licitaciones abiertas** (`ingestar` → `filtrar` → `analizar` → `reporte`) | ¿Postulo a esta licitación? | En pausa: depende de un `perfil.json` real que todavía no existe. |

Los dos primeros no necesitan perfil de empresa ni ticket. El tercero sí
necesita ambos.

**Demanda y nichos son complementarios, no alternativos.** El llamado (`tender`)
dice qué se pide y cuándo cierra, pero no tiene precio porque nadie ha ofertado.
La adjudicación (`award`) tiene precio y número de oferentes, pero solo existe
semanas después. `agregar_demanda()` cruza ambos: agrupa por familia lo
solicitado y le pega el precio mediano y la competencia del histórico.

**Ni bienes ni servicios se descartan por defecto.** El Estado gasta más en
servicios que en bienes; filtrar a mercadería física es una restricción opcional
(`--solo-bienes`), no el comportamiento base.

## Estado de los datos (11-08-2026)

| | |
|---|---|
| Llamados (`solicitudes`) | 42.347 · seis meses completos, marzo a agosto 2026 |
| Líneas con categoría | 203.213 |
| Vigentes | ~1.167 |
| Adjudicaciones | ~1.000 y creciendo — **recolección de 12 meses en curso** |
| Base | `data/mercadopublico.db`, ~96 MB |

Consecuencia práctica: las cifras de **demanda son firmes**, pero el **precio de
referencia y los oferentes** salen de un histórico todavía parcial y se moverán.
Cualquier informe que use esas dos columnas debe decirlo.

## Entorno

Requiere **Python 3.11+**. El Mac de este proyecto traía 3.9.6, así que se
instaló `python@3.12` con Homebrew y el entorno vive en `.venv/`.

```bash
.venv/bin/python -m pytest -q      # 44 tests, sin red ni credenciales
.venv/bin/mpagente <subcomando>
```

Si `.venv` no existe: `/opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`

## Mapa del código

```
src/mpagente/
├── cli.py            # subcomandos; frontera donde se capturan los errores
├── config.py         # .env + carga de perfil.json
├── models.py         # API v1 de Mercado Público, Perfil, Evaluacion (pydantic)
├── ocds_parser.py    # OCDS → Adjudicacion (award) y Solicitud (tender); puro, sin red
├── inspector.py      # describe CSV/ZIP de Datos Abiertos antes de cargarlos
├── prompts.py        # prompt de sistema (cacheado) + ficha por licitación
├── reporte.py        # tablas rich + informes Markdown
├── storage.py        # SQLite: esquema, migraciones y consultas
├── clients/
│   ├── ocds.py           # API OCDS — sin ticket. Alimenta el modo nichos.
│   ├── mercadopublico.py # API v1 — con ticket. Alimenta el modo licitaciones.
│   └── mock.py           # datos simulados para desarrollo sin red
└── pipeline/
    ├── demanda.py        # descarga de llamados (tender) + agregación de lo pedido
    ├── recoleccion.py    # descarga masiva reanudable de adjudicaciones (award)
    ├── nichos.py         # agregación del histórico por familia UNSPSC + puntaje
    ├── ingesta.py, filtros.py, scoring.py
```

`recoleccion.py` reanuda por *mes* (`recoleccion_meses`) porque un mes viejo se
da por cerrado. `demanda.py` reanuda por *llamado*: guarda todos y solo omite
los que ya tienen el plazo vencido, porque uno abierto todavía puede cambiar de
items o correr su fecha de cierre.

## Hechos de las APIs que costó descubrir

No los vuelvas a averiguar por las malas.

### API OCDS (`api.mercadopublico.cl/APISOCDS/OCDS/`)

- **No requiere ticket.** Es lo que hace posible la autonomía.
- **Límite de tasa medido: sobre 5 req/s responde 429 al 100%.** A 5 req/s
  acepta todo. Con backoff se sostienen ~2,5 req/s reales, así que un año de
  licitaciones (~116.000 procesos) toma ~12 h. El default de `--rps` es 5;
  no lo subas sin volver a medir.
- Dos niveles: `listaOCDSAgnoMes/{año}/{mes}/{offset}/{limit}` da los OCID
  paginados; `award/{código}` da la ficha completa. Variantes del listado:
  `listaOCDSAgnoMesTratoDirecto` y `listaOCDSAgnoMesConvenio`.
- **`award/{código}` responde 200 aunque el proceso siga abierto**, con un
  premio sin proveedor ni monto. Hay que distinguirlo o se guarda basura: en el
  último mes cerrado solo ~20% está adjudicado, contra ~69% en un mes de hace un
  año. Eso lo resuelve `clasificar_proceso()`.
- **`tender/{código}` es la ficha del llamado y también va sin ticket.** Es lo
  que permite ver qué se pide sin esperar la adjudicación. Trae
  `tender.items[]` con `classification` UNSPSC y cantidad, `tenderPeriod`
  (inicio y cierre), `procurementMethodDetails` y el comprador. **No trae
  precio ni monto estimado** (`value` viene nulo): nadie ha ofertado todavía.
- **La cobertura de `tender` es muchísimo mejor que la de `award` en lo
  reciente.** Medido en julio 2026: de 25 procesos, 3 adjudicados (12%) pero
  100% con su llamado publicado y con items. Si la pregunta es qué se pide,
  usar `award` descarta el 88% de lo reciente.
- **`tender.statusDetails` no sirve para saber si sigue abierto.** Dice
  "5-Publicada" incluso en procesos cuyo plazo venció hace semanas (verificado:
  48 de 48 en una muestra de julio). Lo vigente se decide comparando
  `tenderPeriod.endDate` contra la fecha actual; eso hace `Solicitud.vigente()`.
- **El listado no incluye el mes en curso.** `listaOCDSAgnoMes/2026/08/...`
  devuelve el cuerpo sin `pagination` estando a 10 de agosto. El mes más
  reciente listable es el anterior. Consecuencia práctica: los llamados
  publicados en los últimos días son invisibles, y lo abierto que se alcanza a
  ver son procesos de plazo largo publicados el mes pasado (~21% de una muestra
  de julio seguía con cierre futuro al 10 de agosto).
- `parties[]` con rol `tenderer` da **cuántos ofertaron**. Es el mejor indicador
  de barrera de entrada y no existe en los CSV de órdenes de compra.
- El precio unitario está en `items[].unit.value.amount`; la clasificación en
  `items[].classification.id` (verificar que `scheme == "UNSPSC"`).
- Algunas adjudicaciones traen `monto = 1` como marcador de "según convenio".
  Se filtran en la consulta de `nichos.py`.

### API v1 (`api.mercadopublico.cl/servicios/v1/publico/`)

- **Requiere ticket** (se solicita en desarrolladores.mercadopublico.cl).
- Devuelve HTTP 200 con el error en el cuerpo cuando el ticket es inválido.
- El listado viene resumido, sin items ni monto: hay que pedir el detalle
  proceso por proceso.

### UNSPSC

Segmentos (2 primeros dígitos) **bajo 70 son bienes físicos; de 70 en adelante,
servicios**. `SEGMENTOS_UNSPSC` en `nichos.py` nombra ambos rangos; sin las
entradas de servicios, más de la mitad del gasto caía en un cajón "Otros".
El filtro por tipo existe (`es_bien()`) pero está apagado por defecto.

La agregación usa la **familia** (4 dígitos), no el código completo: "guantes de
nitrilo" y "guantes de látex" son el mismo negocio.

En `tender`, la descripción del item viene jerárquica —"Familia / Clase /
Producto"— así que la etiqueta se toma de la última rama. En `award` viene
plana.

## Agentes (`.claude/`)

Sobre la CLI hay un equipo de subagentes que convierte los datos en decisiones
comerciales. Viven en `.claude/agents/` y el orquestador en `.claude/commands/`.

```
buscador-leads → analista-leads (triage) → lector-bases → analista-leads (veredicto)
                                                    → [alto: el usuario elige] →
                     equipo-construccion → propuesta-tecnica + propuesta-economica
```

Son **diez agentes**. Tres reglas que gobiernan el conjunto:

- **`solicitador-cotizaciones` es el único que se comunica con terceros**, y
  tiene prohibido enviar: deja borradores en Gmail o en `cotizaciones/<codigo>/`
  y ahí se detiene. Un correo enviado no se deshace, y van a empresas reales a
  nombre de alguien sin empresa constituida. No le des capacidad de envío
  aunque el conector lo permita.
- **`buscador-financiamiento` puede revertir un descarte.** Las garantías son la
  causa más común de descarte —tumbaron 5 de 7 licitaciones de TI— y una póliza
  de caución o una SGR las emite sin inmovilizar capital. Por eso corre **antes**
  del veredicto, no después.
- **Todo lo que se investiga se registra** (`proveedores`, `cotizaciones`,
  `instrumentos_garantia`) y viaja en el contexto portable. Sin eso, cada corrida
  nocturna vuelve a buscar los mismos mayoristas y las mismas aseguradoras.

**`lector-bases` va entre las dos pasadas de `analista-leads`, y esa posición es
deliberada.** Leer bases cuesta (descarga, extracción, revisión de cláusulas) y
la mayoría de las oportunidades se descarta antes con datos que la API ya da: en
la corrida real de TI, de 7 licitaciones solo 1 pasó el triage. Pero tampoco
puede ir después del punto de control, porque el usuario decide justo ahí y sin
la garantía ni los requisitos de experiencia esa decisión no vale.

Su reporte queda en `bases/<codigo>/informe.md` y lo consumen después
`equipo-construccion` (plazos, multas, pago) y `propuesta-tecnica` (los criterios
de evaluación mandan la estructura del documento). Nadie vuelve a descargar nada.

Los originales genéricos quedaron en `Area Autonoma/` como referencia; los de
`.claude/` están reescritos para licitación pública chilena y para alguien que
parte **sin empresa constituida**.

- **El orquestador es un slash command, no un agente.** Un subagente no puede
  invocar a otro subagente en Claude Code, así que `/ciclo-comercial` corre en el
  agente principal, que sí tiene el tool `Agent`. Si alguna vez se mueve a
  `agents/`, el flujo se cuelga al intentar delegar.
- **Los agentes consumen `--json`, nunca las tablas `rich`.** Las tablas truncan
  textos y llevan caracteres de caja.
- **Ningún agente puede correr `demanda` ni `recolectar`**: son descargas de
  horas contra una API pública. Está dicho explícitamente en sus prompts.

### El pipeline es la memoria del sistema

Las decisiones viven en `oportunidades` + `oportunidad_eventos`, no en los
markdown que escriben los agentes. Sin esa tabla, cada análisis parte de cero y
se vuelve a evaluar lo ya descartado.

- `guardar_oportunidad()` **solo pisa los campos que se le pasan**. Así
  `propuesta-economica` puede escribir el precio sin borrar el motivo que dejó
  `analista-leads`. No lo cambies a un REPLACE completo.
- `ficha` inyecta `ya_decidido` y `antecedentes_misma_familia` en su salida:
  es el mecanismo por el que un agente se entera de que esa categoría ya se
  descartó, sin tener que consultarlo aparte.
- La tasa de conversión se calcula **sobre lo resuelto** (ganadas + perdidas).
  Una oferta en curso no es una derrota; incluirla daría una tasa falsamente baja.
- El motivo de un descarte es el dato más valioso de la tabla. Cualquier cambio
  que lo haga opcional o lo sobrescriba en silencio rompe el propósito.

## Convenciones

- **Todo en español**: nombres de funciones, variables, comentarios, docstrings
  y salida de la CLI. El código existente es consistente en esto.
- **Sin pandas.** El inspector y el agregador leen en streaming o con SQL. No
  agregues la dependencia sin una razón fuerte.
- **Los comentarios explican el porqué**, no el qué. Si un comentario describe
  lo que hace la línea siguiente, sobra.
- Los errores de red o de una ficha puntual **no cortan el lote**: se registran
  y se sigue. La única frontera que atrapa todo es `cli.py`.

## Trampas conocidas

- **SQLite no acepta escrituras concurrentes.** En `recoleccion.py` la red se
  paraleliza pero la persistencia se hace desde el hilo principal. No muevas las
  escrituras adentro del `ThreadPoolExecutor`.
- **Salidas estructuradas de Claude**: el esquema debe llevar
  `additionalProperties: false` en todo objeto y no admite `minimum`, `maximum`,
  `pattern` ni `default`. Por eso los modelos de salida usan
  `ConfigDict(extra="forbid")`, no tienen constraints numéricas y pasan por
  `_sanear_esquema()`. Los puntajes se acotan en Python.
- **El SDK de Anthropic resuelve credenciales al enviar la petición**, no al
  construir el cliente, y lanza un `TypeError` genérico. Por eso
  `_cliente_autenticado()` verifica antes y falla temprano con un mensaje útil.
- **Migraciones**: `CREATE TABLE IF NOT EXISTS` no altera tablas ya creadas. Al
  agregar una columna, agrégala también en `Almacen._migrar()`.
- `rich` no muestra la barra de progreso cuando la salida está redirigida. Si un
  comando "no imprime nada" en segundo plano, consulta la base para ver avance.
- **`.claude/` se carga al arrancar la sesión.** Un agente o comando creado a
  mitad de conversación no existe hasta reiniciar Claude Code: `/ciclo-comercial`
  responde "Unknown command" y los subagentes no aparecen entre los invocables.
  No es un error del archivo; antes de depurar el frontmatter, reinicia.
- **Pocos oferentes puede ser una barrera, no una oportunidad.** El puntaje
  premia la baja competencia, así que tiende a destacar justo lo inalcanzable.
  Caso real: seguros (familia 8413) tenía 1,6 oferentes —el mínimo de todos los
  servicios— porque intermediar seguros exige inscripción como corredor en la
  CMF. Ante menos de ~3 oferentes, busca el requisito regulatorio antes de
  recomendar. Lo mismo con SENCE en capacitación, registro ISP en insumos
  médicos y experiencia acreditada en obras.
- **Las bases no están en la API.** Garantías, criterios de evaluación y
  requisitos de experiencia solo están en mercadopublico.cl, como PDF o DOCX
  adjuntos. Los lee `lector-bases` con `scripts/extraer_texto.py`; todo análisis
  hecho solo con datos de la API es preliminar y debe declararse como tal.
- **No hay OCR en este entorno.** `extraer_texto.py` sale con **código 2** si el
  documento no tiene capa de texto (un escaneo). Eso es un riesgo documental, no
  una ausencia de requisitos: confundirlos hace que una garantía imposible pase
  por inexistente. La extracción usa `pypdf` y `python-docx` (extra `bases` de
  `pyproject.toml`), no `pdftotext` ni `pandoc`, para no depender de binarios del
  sistema — el Mac no los trae y el `.md` original asumía `apt-get`.
- **El sufijo del código dice el tamaño del contrato**, y con eso se estima la
  garantía sin abrir nada: L1 < 100 UTM · LE 100–1.000 · LP 1.000–5.000 · LR
  > 5.000. Con la UTM en ~$71.649, un LR parte en $358M y su garantía de fiel
  cumplimiento (5–10%) son decenas de millones inmovilizados. Es lo que descartó
  cinco de siete licitaciones de TI, sin importar lo simple que fuera el producto.

## Al cambiar cosas

- Los tests corren sin red ni credenciales; mantenlo así. El scoring se prueba
  con un cliente falso (`tests/test_scoring.py`), no con la API.
- Si tocas el prompt de sistema en `prompts.py`, recuerda que va marcado con
  `cache_control`: cualquier cambio invalida la caché de la corrida.
- Antes de una recolección larga, prueba con `--limite-mes` para muestrear.
