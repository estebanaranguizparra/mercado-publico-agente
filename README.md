# Agente de oportunidades — Mercado Público

Analiza licitaciones publicadas en Mercado Público (ChileCompra), las cruza con
el perfil comercial de tu empresa y entrega un ranking de oportunidades con la
vía recomendada para atender cada una: **fabricar**, **comprar local**,
**importar**, **usar stock propio**, una **mixta**, o **descartar**.

## Cómo funciona

```
ingestar  →  filtrar  →  analizar  →  reporte
   API      heurística    Claude      ranking
```

1. **Ingestar** — descarga el listado de licitaciones y pide la ficha detallada
   de cada una (el listado de la API viene resumido, sin items ni monto). Todo
   se guarda en SQLite, incluido el JSON crudo, para poder reprocesar sin volver
   a consultar la API.
2. **Filtrar** — prefiltro heurístico y barato que descarta lo que tu perfil no
   toca: obra civil, consultorías, montos fuera de rango, cierres inminentes.
   Cada decisión queda con su motivo registrado. Este paso existe para no gastar
   tokens en licitaciones evidentemente fuera de foco.
3. **Analizar** — solo lo que pasó el filtro llega a Claude, que evalúa seis
   dimensiones (encaje de rubro, viabilidad de suministro, margen, competencia,
   barreras de entrada y calce de plazos), elige una estrategia y la justifica.
   La salida es JSON validado contra un esquema, no texto libre.
4. **Reporte** — ranking en consola y, si lo pides, un informe Markdown con una
   ficha por oportunidad: riesgos, requisitos críticos y próximos pasos.

## Instalación

Requiere **Python 3.11 o superior**.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuración

```bash
cp .env.example .env
cp perfil.ejemplo.json perfil.json
```

Edita `.env`:

| Variable | Para qué sirve |
| --- | --- |
| `ANTHROPIC_API_KEY` | Necesaria para el paso `analizar`. Alternativa: `ant auth login`. |
| `MP_FUENTE` | `mock` (datos de ejemplo) o `api` (Mercado Público real). |
| `MP_TICKET` | Ticket de la API de ChileCompra. Obligatorio si `MP_FUENTE=api`. |
| `MP_MODELO` | Modelo de Claude. Por defecto `claude-opus-5`. |
| `MP_EFFORT` | Profundidad de razonamiento: `low`, `medium`, `high`, `xhigh`, `max`. |
| `MP_REQ_POR_MINUTO` | Límite de llamadas a la API de Mercado Público. Sé conservador. |

**El archivo que de verdad importa es `perfil.json`.** Ahí defines qué es
"interesante" para tu empresa, y de él dependen tanto el prefiltro como el
criterio con que Claude evalúa:

- `rubros_objetivo` y `palabras_clave_incluir` / `excluir` — el foco comercial.
- `capacidades` — qué puedes fabricar, qué tienes en bodega, si importas y con
  qué lead time. El agente no recomendará importar cuando el plazo de entrega
  exigido sea menor que tu lead time declarado.
- `restricciones` — rango de monto, margen mínimo, capital de trabajo, días
  mínimos para alcanzar a ofertar.
- `notas_estrategicas` — texto libre que se le pasa a Claude tal cual. Es el
  lugar para reglas propias del tipo "evitamos licitaciones con marca única".

### Sobre el ticket de la API

Se solicita en [desarrolladores.mercadopublico.cl](https://desarrolladores.mercadopublico.cl).
Mientras lo tramitas, deja `MP_FUENTE=mock`: el proyecto trae ocho licitaciones
de ejemplo con la misma estructura que devuelve la API real, y sus fechas de
cierre se calculan relativas a hoy para que no caduquen.

## Uso

```bash
# Todo el flujo de una vez
mpagente pipeline --limite 100 --top 15 --salida informes/oportunidades.md

# O paso a paso
mpagente ingestar --fecha 2026-08-10 --estado activas --limite 100
mpagente filtrar
mpagente analizar --top 15 --paralelo 3
mpagente reporte --salida informes/oportunidades.md

# Diagnóstico
mpagente estado
```

Opciones útiles:

- `--refrescar` vuelve a bajar licitaciones ya guardadas (por defecto se saltan,
  que es lo que cuida el rate limit).
- `--sin-detalle` omite pedir la ficha completa: mucho más rápido, pero sin
  items ni monto la evaluación pierde bastante.
- `--reevaluar` rehace evaluaciones ya hechas, por ejemplo tras cambiar el perfil.
- `--paralelo N` evaluaciones simultáneas contra la API de Claude.

## Costo del análisis

El prompt de sistema (rol + perfil) es idéntico en todas las licitaciones de una
corrida, así que va marcado con `cache_control`: se paga completo en la primera
llamada y a ~10% en las siguientes. El costo real termina dominado por el largo
de las bases de cada licitación.

Si necesitas bajar el gasto, en este orden: sube el mínimo de monto en el perfil
(menos candidatas), baja `MP_EFFORT` a `medium`, y recién después considera un
modelo más chico.

## Estructura

```
src/mpagente/
├── cli.py              # subcomandos
├── config.py           # .env y carga del perfil
├── models.py           # API de Mercado Público, perfil y evaluación
├── prompts.py          # armado del prompt de sistema y del turno de usuario
├── reporte.py          # tabla de consola e informe Markdown
├── storage.py          # SQLite
├── clients/            # fuente real (HTTP) y simulada
└── pipeline/           # ingesta, prefiltro y scoring
```

## Tests

```bash
pytest
```

Los 21 tests corren sin red ni credenciales: el paso de scoring se ejercita con
un cliente simulado que verifica el armado de la petición y el manejo de
respuestas truncadas, rechazadas o con JSON inválido.

## Modo demanda — qué está pidiendo el Estado

Responde *"¿qué está solicitando el Estado, y qué de eso sigue abierto?"*, para
bienes y servicios por igual. **No requiere ticket ni perfil de empresa.**

```bash
mpagente demanda --meses 3               # descarga los llamados publicados
mpagente pidiendo --top 25 --abiertos    # qué se pide + lo que aún se puede postular
mpagente pidiendo --solo-servicios --salida informes/demanda.md
mpagente ficha 2439-22-LP26              # un llamado: qué pide y contra quién competirías
```

Para consumo por otro programa o por un agente, `--json` en `pidiendo` y `ficha`
entrega los datos estructurados; `--dias-min N` descarta lo que cierra antes de
N días, porque preparar una oferta toma tiempo.

### El pipeline: qué se decidió y por qué

Los datos dicen qué se está pidiendo. El pipeline guarda **qué decidiste al
respecto**, para que el sistema no vuelva a analizar lo que ya descartaste.

```bash
mpagente pipeline-estado                 # el embudo y todo lo registrado
mpagente pipeline-estado --estado descartada
mpagente historial 2439-22-LP26          # el recorrido de una oportunidad
mpagente registrar 2439-22-LP26 --estado descartada \
  --motivo "Exige tres obras similares acreditadas." --autor analista-leads
```

Los estados van `nueva → analizada → descartada`, o bien
`→ en_preparacion → ofertada → ganada`/`perdida`. Cuando cierres el ciclo con el
resultado real, `pipeline-estado` calcula tasa de conversión y margen medio.

El **motivo del descarte es lo más valioso** que se guarda: `mpagente ficha` lo
saca a la superficie cuando aparece otra licitación de la misma categoría, así
que la barrera se aplica en vez de redescubrirse.

La fuente es la ficha `tender/{código}` de OCDS: lo que se pide, publicado desde
que el llamado sale, sin esperar a la adjudicación. Trae los items con su código
UNSPSC, la cantidad, el organismo comprador y el plazo de postulación.

### Por qué mirar el llamado y no solo la adjudicación

Un proceso se adjudica semanas o meses después de publicarse. En julio de 2026,
de una muestra de 25 procesos, solo **3 estaban adjudicados (12%)** — pero
**los 25 tenían su llamado publicado con items**. Analizar solo lo adjudicado
descarta la enorme mayoría de lo reciente, que es justamente donde está la
demanda viva.

Lo que el llamado no tiene es precio: nadie ha ofertado todavía. Por eso
`pidiendo` cruza cada categoría con el histórico ya adjudicado de esa misma
familia y le pega el precio unitario mediano y el promedio de oferentes. Para
que esa columna se llene hace falta haber corrido también `recolectar`.

### Dos límites reales de la API

**El mes en curso no existe.** El listado OCDS no publica el mes corriente:
estando a 10 de agosto, agosto todavía no responde. Lo más reciente listable es
el mes anterior. En la práctica se ven los llamados de plazo largo publicados el
mes pasado que siguen abiertos (~21% de una muestra de julio seguía con cierre
futuro al 10 de agosto), no los publicados esta semana. Para el flujo del día se
necesita la API v1, que exige ticket.

**El estado declarado miente.** La ficha dice `5-Publicada` incluso en procesos
cuyo plazo venció hace semanas. La vigencia se determina comparando la fecha de
cierre contra el reloj, nunca por ese campo.

## Modo exploración de nichos — histórico adjudicado

Responde *"¿qué se compró, a qué precio y con cuánta competencia?"*. Es el
complemento del modo demanda: aporta el precio y la barrera de entrada que el
llamado no puede tener.

```bash
mpagente recolectar --meses 12          # descarga el histórico de adjudicaciones
mpagente nichos --top 25 --salida informes/nichos.md
```

### Cómo consigue los datos

La API OCDS (`api.mercadopublico.cl/APISOCDS/OCDS/`) es pública y sin
autenticación. Funciona en dos niveles: un listado paginado por mes que entrega
los identificadores de proceso, y una ficha `award/{código}` por proceso con la
adjudicación completa — quién ganó, por cuánto, qué items con su código UNSPSC y
precio unitario, y **quiénes más ofertaron**.

Ese último dato es el más valioso y no existe en los CSV de órdenes de compra:
el número de oferentes por licitación es la medida directa de cuánta competencia
tiene una categoría.

### Dos cosas que la API impone

**Límite de tasa.** Medido: sobre 5 peticiones por segundo empieza a responder
429. El cliente respeta ese techo y hace backoff. En la práctica se sostienen
~2,5 req/s, así que un año de licitaciones (~116.000 procesos) toma del orden de
**12 horas** desatendidas. Para explorar no hace falta el censo completo: con
`--limite-mes 1500` se obtiene una muestra representativa en un par de horas.

**Las adjudicaciones llegan tarde.** Un proceso se adjudica semanas o meses
después de publicarse. En el último mes cerrado solo ~20% está adjudicado; en un
mes de hace un año, ~70%. Por eso el recolector clasifica cada proceso en
`adjudicado`, `cerrado_sin_adjudicar` (desierto o revocado) o `pendiente`, y
**vuelve a consultar los pendientes** en la siguiente corrida. Es idempotente y
reanudable: si se interrumpe, retoma donde quedó.

### Qué busca

La unidad es la **familia UNSPSC** (4 dígitos): "guantes de nitrilo" y "guantes
de látex" son códigos distintos pero el mismo negocio.

**Entran bienes y servicios.** En UNSPSC los segmentos bajo 70 son bienes
físicos y de 70 en adelante servicios; el Estado gasta más en los segundos, así
que descartarlos escondería la mayor parte del mercado. Si la pregunta es
específicamente qué importar, `--solo-bienes` restringe a mercadería física
(y en `pidiendo` también existe `--solo-servicios`).

| Señal | Qué indica |
|---|---|
| Monto y recurrencia | Que es un negocio, no un peak aislado |
| Proveedores distintos y share del líder | Si está fragmentado (entrable) o capturado |
| Oferentes por licitación | Competencia real; pocos puede ser oportunidad o barrera |
| Dispersión p75/p25 del precio unitario | Cuánto margen hay para quien cotiza bien |
| Ticket mediano | Cuánto capital de trabajo exige |

El puntaje de 0 a 100 combina esas señales con pesos explícitos y deja
registrado por qué sumó cada una. No reemplaza el juicio: ordena qué mirar.

## El equipo de agentes

Sobre la CLI hay cinco subagentes de Claude Code que convierten los datos en
decisiones comerciales, más un orquestador:

```
buscador-leads → analista-leads → [alto: tú eliges] → equipo-construccion
                                                        ↓            ↓
                                            propuesta-tecnica  propuesta-economica
```

Se corre con `/ciclo-comercial`, opcionalmente acotado:

```
/ciclo-comercial servicios profesionales
/ciclo-comercial --solo-bienes
```

También se pueden invocar sueltos en lenguaje natural: *"usa analista-leads para
evaluar la 3172-54-LP26"*.

**Si `/ciclo-comercial` responde "Unknown command", reinicia Claude Code.** Los
archivos de `.claude/` se leen al arrancar la sesión.

El orquestador **se detiene después de analizar** y te muestra qué encontró para
que elijas qué avanza. Es a propósito: construir un plan y dos documentos por
oportunidad cuesta tiempo, y la mayoría se descarta al leer las bases.

Los agentes están escritos para licitación pública chilena y para quien parte sin
empresa constituida. Revisan barreras que no aparecen en los datos —inscripción
en ChileProveedores, garantías, experiencia acreditada, plazos de pago del
Estado— y tienen prohibido inventar experiencia o certificaciones.

Las plantillas genéricas de las que salieron quedaron en `Area Autonoma/`, por si
alguna vez sirven para clientes privados.

## Inspeccionar descargas masivas (alternativa manual)

ChileCompra también publica descargas masivas en CSV por semestre en
[datos-abiertos.chilecompra.cl/descargas](https://datos-abiertos.chilecompra.cl/descargas).
No hacen falta para el modo autónomo, pero sirven si quieres cruzar órdenes de
compra (que la API OCDS no cubre) o cargar varios años de golpe sin esperar la
recolección.

Como esos datasets cambian de codificación, separador decimal y nombres de
columna entre períodos, hay un inspector que describe la estructura real antes
de escribir cualquier cargador.

```bash
mpagente inspeccionar data/oc_2025_s1.zip --listar   # qué hay dentro del ZIP
mpagente inspeccionar data/oc_2025_s1.zip            # estructura real del CSV
```

Reporta codificación, separador, convención decimal, tipo y cardinalidad de cada
columna, y adivina qué columna cumple cada rol (categoría, precio unitario,
cantidad, proveedor, organismo, fecha). Si falta alguno de los roles esenciales,
lo dice.

## Alcance actual y qué falta

Esta versión llega hasta el **análisis y priorización**. Deliberadamente **no**
incluye:

- Búsqueda de proveedores ni precios de referencia reales. Los márgenes que
  estima Claude son juicio experto sobre las bases, no cotizaciones. Trátalos
  como una señal para ordenar prioridades, no como un costeo.
- Generación de la documentación de la oferta ni el checklist administrativo
  para postular.

Ambos son la extensión natural: el primero requiere conectar búsqueda web o
catálogos de proveedores; el segundo, parsear las bases administrativas
completas, que la API no entrega en el mismo endpoint.
