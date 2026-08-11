> **Estos archivos son la versión genérica original.** Los agentes que el
> proyecto usa de verdad están en `.claude/agents/`, reescritos para licitación
> pública chilena y conectados a `mpagente`. Esta carpeta queda como referencia
> de la plantilla de la que salieron. Si quieres usarlos para clientes privados
> (fuera de Mercado Público), estos son los que sirven.

# Agentes de gestión de proyectos (leads → propuesta)

5 subagentes de Claude Code que implementan el flujo: búsqueda de leads → análisis y decisión de modalidad → construcción/análisis del proyecto → propuesta técnica + propuesta económica.

## Instalación
Copia los 5 archivos `.md` (no este README) a la carpeta de agentes de Claude Code:

- Para un proyecto específico: `.claude/agents/`
- Para que estén disponibles en todos tus proyectos: `~/.claude/agents/`

```bash
mkdir -p .claude/agents
cp buscador-leads.md analista-leads.md equipo-construccion.md propuesta-tecnica.md propuesta-economica.md .claude/agents/
```

Claude Code los detecta automáticamente y los invoca según su campo `description`, o puedes llamarlos explícitamente (ej. "usa el agente analista-leads para evaluar este proyecto").

## Flujo entre agentes

1. **buscador-leads** — encuentra y ficha oportunidades.
2. **analista-leads** — evalúa viabilidad y decide la modalidad: contratar empresa, importar producto, servicio profesional propio, automatizar el servicio, o contratar un profesional independiente.
3. **equipo-construccion** — diseña la solución (alcance, cronograma, recursos, costos internos) según la modalidad decidida.
4. **propuesta-tecnica** y **propuesta-economica** — trabajan en paralelo a partir de la definición del proyecto, y sus documentos se combinan en la propuesta final para el cliente.

## Orquestación sugerida
Puedes pedirle a Claude directamente: "toma este lead y llévalo de punta a punta hasta la propuesta final", y Claude encadenará los agentes en orden usando las salidas de uno como entrada del siguiente.

## Ajustes recomendados antes de usarlos en producción
- Revisa el campo `tools` de cada agente y quita los que no necesites (por ejemplo, `Bash` en `equipo-construccion` si no vas a ejecutar cálculos o scripts).
- Ajusta los criterios de la modalidad en `analista-leads.md` a la realidad de tu empresa (márgenes, capacidad interna, umbrales de repetibilidad, etc.).
- Si tienes plantillas de propuesta ya definidas, pégalas dentro de `propuesta-tecnica.md` y `propuesta-economica.md` para que el formato de salida las respete exactamente.
