<!-- Versión en español — generada con v0.79, puede quedar desactualizada tras actualizaciones -->

<h1 align="center">codeloom</h1>

<p align="center">
  <b>Dale a tu agente de codificación con IA un mapa del repositorio en un segundo — y una memoria que sobrevive a la compactación.</b><br/>
  Un archivo · cero dependencias · sin daemon · 100% local · MIT
</p>

<p align="center">
  <a href="https://github.com/sloemo01/codeloom/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.8%2B-blue"/></a>
  <a href="https://github.com/sloemo01/codeloom/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/CI-passing-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom#readme"><img src="https://img.shields.io/badge/deps-zero-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom/stargazers"><img src="https://img.shields.io/github/stars/sloemo01/codeloom"/></a>
</p>

<p align="center">
  <a href="#quickstart">Inicio rápido</a> ·
  <a href="#what-it-gives-your-agent">Funcionalidades</a> ·
  <a href="#mcp-server-82-tools--1-router">MCP</a> ·
  <a href="#pr-review-bot">Bot de PR</a> ·
  <a href="#why-its-different">vs. competidores</a> ·
  <a href="#documentation">Documentación</a>
</p>

---

## El problema: los agentes no solo queman tokens — olvidan

Todo agente de codificación con IA tiene el mismo problema: antes de poder
*hacer* nada, debe averiguar qué *es* tu codebase. Así que hace grep, lee
archivos completos y quema 40,000+ tokens construyendo contexto. Después, una
compactación de contexto lo borra todo — y lo vuelve a derivar todo desde
cero. Una y otra vez.

**codeloom resuelve las dos mitades.**

1. **El mapa** — un solo comando produce un modelo estructural compacto de tu
   repositorio (árbol de carpetas + resúmenes de una línea por módulo + puntos
   de entrada + grafo de imports + grafo de llamadas) que el agente lee en un
   segundo.
2. **La memoria** — `--decide`, `--checkpoint`, `--resume` registran el flujo
   de decisiones del agente, de modo que `--resume` restaura *tanto* el
   contexto estructural *como* lo que el agente ya intentó, decidió y
   rechazó — después de cualquier compactación.

Sin instalación. Sin daemon. Sin GPU. Sin telemetría. Se ejecuta 100% en tu
máquina.

## Inicio rápido

```bash
# Option A: copy the one file (no pip, no deps)
curl -O https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py

# Option B: pip
pip install codeloom

# Map any repo (<1s to first result)
python3 codeloom.py /path/to/repo > AGENTS.md

# Tell your agent: "read AGENTS.md first"
```

Conéctalo de forma nativa a tu agente (17 compatibles):

```bash
python3 codeloom.py --install-agent claude-code   # or cursor, codex, gemini-cli,
                                                  # opencode, cline, aider, ...
```

## Lo que le ofrece a tu agente

### Herramientas con forma de tarea (el foso competitivo)

| Comando | Lo que recibe el agente |
|---|---|
| `--pack "TASK"` | **Informe de una sola pasada**: orden de lectura + impacto + símbolos relevantes, precalculado |
| `--answer "Q"` | Respuesta citada con confianza calibrada |
| `--context-card S1 S2` | Tarjeta de triaje por lotes para N símbolos en una sola llamada |
| `--why QUERY` | Consulta de decisiones sellada `[exact]`/`[fuzzy]`/`[unverified]` |
| `--plan TASK` | Plan de lectura priorizado, nativo para agentes |

### Memoria de trabajo entre compactaciones (nadie más ofrece esto)

```bash
codeloom --decide "use retry(3) not retry(∞) — unbounded hangs agents"
codeloom --checkpoint --task "fix login bug"     # save working state
codeloom --resume                                 # restore after compaction
```

También: `--remember`, `--seen`, `--working-state`, `--lessons`, `--supersede`,
`--adr`, `--query-memory`.

### Inteligencia estructural

| Comando | Resultado |
|---|---|
| `--graph` | Grafo de imports completo (385 módulos, 1126 aristas en <1s) |
| `--cross` | Grafo de llamadas entre archivos, resuelto vía AST |
| `--search` / `--usages` / `--grep` / `--read` | Índice de símbolos, sitios de llamada, fragmentos, código eficiente en tokens |
| `--get-symbol X` | Recuperación resumen-primero (~95–99% de ahorro de tokens) |
| `--impact M` / `--refactor` / `--rename` | Predicción del radio de impacto |
| `--similar` / `--deadcode` / `--explain` | Inteligencia de refactorización, cero LLM |
| `--trace` | Aristas de llamadas en runtime que el análisis estático no puede ver |
| `--routes` / `--channels` | Rutas HTTP, canales de eventos pub-sub |
| `--pattern '$F($$$ARGS)'` | **Búsqueda estructural AST** con captura de metavariables |

### Velocidad y calidad

| Comando | Resultado |
|---|---|
| `--health` | Pantalla de salud del código: 0–10 por archivo, **0.2s**, detectores deterministas |
| `--risk HEAD~1..HEAD` | Puntuación de riesgo del cambio 0–100 + impulsores con nombre para cualquier rango de commits |
| `--embed-search Q` | Búsqueda semántica sin conexión — subword-hash, cero dependencias (ggml opt-in) |
| `--watch` → `--watch-merge` | Frescura en vivo: el watcher nativo canaliza al índice persistente |
| `--engine c` | Núcleo C de auto-compilación: grafo completo del kernel de Linux en ~89–113s (motor C) |
| `--verify FILE` | Verificación de suma de verificación SHA-256 |

**50 lenguajes tree-sitter despachados · 46 probados con fixtures** (pruebas
de paridad de archivos golden validan la CI en cada gramática) ·
**130+ extensiones mediante respaldo regex**.

## Servidor MCP (82 herramientas + 1 router)

```json
{"command": "python3", "args": ["-m", "codeloom_mcp"]}
```

O conecta automáticamente cualquiera de los 17 agentes:
`codeloom --install-agent <name>`.

82 herramientas en total, pero la superficie efectiva del agente es
**una herramienta**: `codeloom_ask` toma lenguaje natural y enruta de forma
determinista — sin errores de selección de herramienta. Listado completo:
[`docs/mcp-listing.md`](docs/mcp-listing.md).

## Bot de revisión de PR

`.github/workflows/pr-bot.yml` convierte cada pull request en:

1. **Comentarios en línea anclados a líneas** en ubicaciones exactas del diff:
   - **P1** seguridad (`eval`/`exec`, secretos hardcodeados), **P2** (http
     inseguro, `shell=True`), **P3** (símbolos nuevos huérfanos, marcadores
     TODO/FIXME)
2. **Comentario resumen fijo** (actualizado por push): veredicto de riesgo
   0–100 con impulsores, resumen del diff, salud de los archivos tocados,
   checklist de revisión adaptativa, contexto inicial del revisor
3. **Etiquetas de riesgo**: `risk:low/medium/high/critical`, rotadas
   automáticamente
4. **Traspaso**: `@codex` ejecuta su pasada de LLM después de la nuestra,
   centrada en semántica/lógica/diseño (nuestras categorías deterministas ya
   están cubiertas)

Coste de LLM cero para la etapa 1. Funciona en cualquier repositorio de
GitHub — copia el archivo del workflow.

## Por qué es diferente

Matriz completa con fuentes citadas:
[`docs/COMPETITION.md`](docs/COMPETITION.md).
Resumen frente al panorama (verificado desde sus repositorios, crg medido en
vivo el 2026-08-22 — consulta [`benchmarks/README.md`](benchmarks/README.md)
para los números):

| | **codeloom** | code-review-graph (30.6k★) | code-context-engine | claude-context |
|---|---|---|---|---|
| Instalación | **un archivo stdlib** | pip: **78 paquetes** + daemon + configuración TOML | pip + ONNX + server | npm |
| Proceso en segundo plano | **ninguno** | `crg-daemon` (16MB RSS, comprobaciones de salud) | `cce serve` + gobernador de recursos | — |
| Memoria de compactación | ✅ **registro de decisiones, medido: 2 llamadas / ~985 tok para recuperar** (95.4% menos) | ⚠️ diario Q&A en markdown, cero menciones de compactación | ⚠️ MCP `record_decision` invocado por el agente | plugin memsearch |
| Superficie MCP | **82 + 1 router NL** | 30, sin router | 22 | muchas |
| Búsqueda semántica | ✅ cero dependencias, sin conexión | ❌ extra `[embeddings]` (~2GB) o clave en la nube | ❌ requiere ONNX | ✅ (Zilliz) |
| Prueba de lenguajes | **46 probados por fixtures en CI** | no publicado | — | — |
| Configuración→respuesta | **0.13s en caliente** | 41s pip + 4s build + daemon | tras la indexación | tras la indexación |

Números medidos: recuperación de símbolos con 24–36× menos tokens que crg;
recuperación tras compactación con **95.4% menos tokens**; grafo completo del
kernel de Linux en ~89–113s (motor C). Detalles y comandos de reproducción en
[`benchmarks/README.md`](benchmarks/README.md).

Dónde los competidores van por delante, dicho sin rodeos: jcodemunch tiene una
verificación previa de seguridad más amplia (segura para editar/borrar,
verificación del compilador SCIP); codegraph tiene escala de comunidad de
67k★; codebase-memory incluye 158 gramáticas y una evaluación publicada en
arXiv; repowise (AGPL) tiene puntuación de riesgo validada contra defectos.
Nosotros reclamamos velocidad + forma + prueba por gramática + profundidad de
memoria — no sus fosos.

## Limitaciones conocidas (honestas)

- Python recibe el análisis más profundo (`ast` de la stdlib); otros lenguajes
  reciben esquemas de tree-sitter y respaldos regex.
- Salud/riesgo son heurísticas estructurales — **no** validadas contra
  defectos en un corpus etiquetado (el foso de repowise; lo decimos en vez de
  exagerar).
- Los benchmarks de ahorro de tokens con agentes en vivo están diseñados pero
  no comprobados — nuestros números publicados son reproducción estática con
  filas de pérdida incluidas ([`bench/RESULTS.md`](bench/RESULTS.md)).
- Los embeddings neuronales requieren una instalación opcional de
  ggml/modelo; sin ella obtienes el hash de subpalabras cero dependencias
  (sigue sin conexión y sigue detectando erratas).

## Documentación

| Doc | Contenido |
|---|---|
| [`CAPABILITIES.md`](CAPABILITIES.md) | Todo lo que codeloom puede hacer |
| [`USER_GUIDE.md`](USER_GUIDE.md) | Guías prácticas |
| [`CLI.md`](CLI.md) | Todos los flags explicados |
| [`FEATURES.md`](FEATURES.md) | Mapa estratégico de funcionalidades |
| [`SECURITY.md`](SECURITY.md) | Modelo de confianza y verificación |
| [`docs/COMPETITION.md`](docs/COMPETITION.md) | Matriz de competidores con fuentes citadas |
| [`docs/FAQ.md`](docs/FAQ.md) | «vs LSP/repomix/code-review-graph» — compensaciones honestas |
| [`docs/mcp-listing.md`](docs/mcp-listing.md) | Texto del listado del marketplace MCP |
| [`bench/RESULTS.md`](bench/RESULTS.md) | Resultados del bench de reproducción (filas de pérdida publicadas) |
| [`BENCHMARKS.md`](BENCHMARKS.md) | Números de rendimiento medidos |
| [`TECHNICAL_REPORT.md`](TECHNICAL_REPORT.md) | Documento de decisiones de arquitectura |
| [`AGENT_TRACE.md`](AGENT_TRACE.md) | Traza del agente antes/después de la tarea |

## Confianza y verificación

- **CI**: Linux/macOS/Windows × Python 3.8–3.12, 77 pruebas, ≥46 fixtures de
  gramática controlados por archivos golden
- **Sumas de verificación**: cada versión publica el SHA-256 de `codeloom.py`;
  verifica con `codeloom --verify codeloom.py`
- **Auditable**: un solo archivo de la stdlib — lee el archivo completo antes
  de ejecutarlo

## Contribuciones

Los PR son bienvenidos. Ejecuta las pruebas con `python3 tests.py`. Principio
rector: cero dependencias, rápido, un solo archivo, afirmaciones honestas.

## Skill del agente

Una skill lista para cargar, para usar y mantener codeloom, se incluye en
[`skills/codeloom/SKILL.md`](skills/codeloom/SKILL.md) — todos los flags, la
configuración MCP, la suite de pruebas, volver a grabar el GIF de
demostración y cómo extender la herramienta. Instala en el directorio de
skills de tu agente (p. ej., `~/.hermes/skills/software-development/codeloom/`).

## Licencia

MIT — do whatever you want with it.

---

*Hecho para personas que prefieren que su agente de codificación publique
código antes que pasar 15 minutos releyendo un repositorio de 40k LOC después
de cada compactación.*
