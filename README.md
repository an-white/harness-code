# Equipo de agentes multi-rol (LangChain + LangGraph + OmniRoute)

Plantilla genérica y reutilizable para **definir y ejecutar un equipo de agentes** orquestados con **LangChain** y **LangGraph**, consultando la API compatible con OpenAI expuesta por **OmniRoute** (configurada en `.env`) y tomando como **fuente de verdad** las definiciones de agentes en `agents/*.md`.

El objetivo es disponer de un flujo reproducible de planificación, investigación, implementación, revisión, QA y seguridad, con políticas de permisos y herramientas acotadas al *workspace*, que pueda adaptarse a futuros proyectos sin acoplarse a rutas ni repositorios concretos.

## Estado actual

Implementado (R0–R5 / T1–T4):

- **Base**: paquete `app/` (`config`, `state`, `cli`, `agents/`, `graph/`, `research/`, `tools/`, `i18n`).
- **Loader de agentes** (`app/agents/loader.py`): parsea frontmatter YAML y prompt de `agents/*.md`, normaliza y aplica políticas **deny-por-defecto** (`deny > allow`, con *fallback* seguro ante permisos ausentes o inválidos).
- **Runtime de agentes** (`app/agents/runtime.py`): carga de *specs* y toolsets por rol, *probe* de capacidades (tool-calling) con *fallback* a modo JSON estructurado, ejecución de turnos y manejo de errores (equivalente a T3).
- **Grafo LangGraph y routing** (`app/graph/`): orquestación con estado compartido, interrupciones (HITL), condicionales y límites de ciclos (equivalente a T4).
- **R0 (entrada/traducción e i18n)**: `translator_entry` como nodo inicial, resolución/persistencia de idioma de interacción, traducción de requerimientos/respuestas a inglés para el flujo interno y localización de salidas de usuario cuando aplica.
- **R1 (PM y esquema de tareas)**: planificación con tareas normalizadas/validadas (`id`, `kind`, `executor`, `objective`, `acceptance_criteria`, `target_repo_files_hint`, `risks`, `needs_security`) y ejecutores `builder`/`research`.
- **R2 (research + web)**: agente `research` read-only con herramientas web (`webfetch`/`websearch`) sujetas a consentimiento explícito por ejecución, guardrails SSRF/alcance, sanitización y auditoría local.
- **R3 (detección/onboarding/specs)**: detección/fingerprint, scanner, generación/carga de contexto y artefactos de onboarding (`docs/project_specs.md` canónico en inglés + copia localizada, `docs/research/*` según corresponda).
- **R4/R5 (integración y ciclos)**: onboarding/inyección de contexto acotado (digest), research bajo demanda con retorno a planificación/decisión y límites de ciclos (`MAX_FIX_CYCLES`, `RESEARCH_MAX_CYCLES`).

### Brechas / pendientes conocidos

- [ ] **T5 (SkillResolver / find-skills)**: búsqueda, instalación con aprobación humana explícita e inyección de `SKILL.md` no implementada como flujo dedicado.
- [ ] **CLI avanzada**: no incluye flags previstas originalmente como `--workspace`, `--refresh-skills`, `--reuse-skills`, `--max-cycles`, `--dry-run`.
- [ ] **Commit automático**: el nodo de commit registra aprobación/estado; no realiza commit git automático.
- [ ] El flujo depende de un endpoint/modelos LLM funcionales vía OmniRoute para ejecución completa (sin red/modelo no hay resultados útiles de agentes).

## Requisitos

- **Python** 3.12
- **Entorno virtual** (`venv`)
- Dependencias definidas en `requirements.txt`
- **OmniRoute** accesible (endpoint compatible con OpenAI, normalmente en `OMNIROUTE_BASE_URL`) para ejecución del grafo con LLM
- **Git**
- **Node.js ≥ 22.20** y **`npx`** (opcional/futuro: gestión de *skills* con find-skills, T5)

## Configuración

1. Copiar el archivo de ejemplo y completar los valores:

   ```bash
   cp .env.example .env
   ```

2. Variables principales (ver `.env.example`):

   | Variable | Descripción |
   | --- | --- |
   | `OMNIROUTE_BASE_URL` | URL base de la API de OmniRoute (p. ej. `http://localhost:20128/v1`). |
   | `OMNIROUTE_API_KEY` | Clave de API para OmniRoute. |
   | `OMNIROUTE_MODEL` | Modelo a usar (`auto` o un nombre de modelo concreto). |
   | `TEMPERATURE` | Temperatura de muestreo (por defecto `0.3`, rango `0.0`–`2.0`). |
   | `PM_MODEL`, `BUILDER_MODEL`, `REVIEWER_MODEL`, `QA_MODEL`, `SECURITY_MODEL`, `TRANSLATOR_MODEL`, `RESEARCH_MODEL` | (Opcionales) Modelos por rol; si están vacíos se usa `OMNIROUTE_MODEL`. |
   | `WORKSPACE_ROOT` | (Opcional) Raíz del *workspace* donde se buscan `agents` y se opera el flujo. Por defecto: directorio de trabajo actual. |
   | `MAX_FIX_CYCLES` | (Opcional) Máximo de ciclos de revisión/corrección por tarea. |
   | `MAX_AGENT_STEPS` | (Opcional) Máximo de pasos que un agente puede ejecutar por turno. |
   | `SHELL_TIMEOUT` | (Opcional) Tiempo límite (segundos) para comandos de *shell*. |
   | `SKILLS_AUTO_INSTALL` | (Opcional) Flag de configuración para instalación automática de *skills*; **la instalación por agente siempre requiere aprobación humana explícita** (flujo T5 pendiente). |
   | `INTERACTION_LANGUAGE` | (Opcional) Idioma de interacción por defecto (p. ej. `en`, `es`). La precedencia efectiva es: `--language` (CLI) > `INTERACTION_LANGUAGE` > preferencia persistida > selección interactiva/fallback. |
   | `RESEARCH_ONBOARDING` | (Opcional) Modo de onboarding/research: `auto` \| `always` \| `never` (por defecto `auto`). |
   | `RESEARCH_MAX_FILES` / `RESEARCH_MAX_BYTES` / `RESEARCH_CONTEXT_CHARS` / `RESEARCH_MAX_CYCLES` | (Opcionales) Límites de alcance, digest y ciclos de research (por defecto `40`, `200000`, `8000`, `2`). |
   | `WEB_ACCESS` | (Opcional) Política base de acceso web para research: `ask` \| `allow` \| `deny` (por defecto `ask`). El consentimiento efectivo es explícito por ejecución y se mantiene en estado (no persistido). |
   | `WEBFETCH_TIMEOUT` / `WEBFETCH_MAX_BYTES` | (Opcionales) Límite de tiempo y bytes para `webfetch`/web (por defecto `20`, `512000`). |
   | `WEBFETCH_ALLOWED_DOMAINS` | (Opcional) CSV de dominios permitidos; vacío = dominios públicos permitidos, pero IPs privadas/loopback/link-local/reservadas/metadata siempre bloqueadas (incluye revalidación en redirecciones). |
   | `WEBSEARCH_PROVIDER` | (Opcional) Proveedor de búsqueda web: `none` \| `searxng` \| `tavily` \| `brave` (por defecto `none`). |
   | `WEBSEARCH_ENDPOINT` / `WEBSEARCH_API_KEY` | (Opcionales) Endpoint y API key del proveedor de búsqueda (la key nunca se registra). |

> **Advertencia:** `.env` contiene secretos y **nunca debe versionarse** (está ignorado por `.gitignore` junto con `.env.*`, excepto `.env.example`).

## Ejecución

CLI implementada (`main.py` invoca `app.cli.main`):

```bash
.venv/bin/python main.py [requirement] [flags]
# o
python main.py [requirement] [flags]
```

- Sin `requirement` posicional: en modo interactivo solicita el requerimiento; en `--non-interactive` requiere un requerimiento (salvo que `--research` lo provea como semilla).
- Interrupciones del grafo (idioma, aclaraciones, consentimiento web, aprobación de commit) se atienden por entrada estándar en modo interactivo; con `--non-interactive` no se solicita entrada adicional.

### Flags disponibles

- `requirement` (posicional, opcional): requerimiento inicial.
- `--language <code>`: código de idioma de interacción (p. ej. `en`, `es`).
- `--non-interactive`: no pedir entrada en interrupciones.
- `--allow-web` / `--no-web`: fija consentimiento de acceso web para research en esta ejecución (no persistido).
- `--refresh-research`: fuerza regeneración de onboarding/research del proyecto en esta ejecución.
- `--skip-onboarding`: omite onboarding/research automático en esta ejecución.
- `--research "<question>"`: dispara research bajo demanda sembrando `needs_research=True` y `research_question` (si no hay requerimiento, se usa como semilla de contexto/requerimiento).
- `--check`: valida entorno/configuración y carga de agentes (y *toolsets* básicos) sin ejecutar el grafo ni hacer red por defecto; devuelve `0` si es correcto o `1` si hay errores.

### Comprobación rápida (sin red)

```bash
.venv/bin/python main.py --check
```

### Tests

La suite usa la librería estándar (`unittest`), **sin red**:

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

## Arquitectura y flujo

- **Agentes (7)**: `project-manager`, `translator`, `builder`, `reviewer`, `qa`, `security`, `research` (definidos en `agents/*.md`).
- **Flujo principal**: `START -> translator_entry -> pm_intake -> translator -> pm_plan -> skills_setup -> (builder|research) -> reviewer -> (security si aplica) -> qa -> pm_decide -> (fix/review/more_tasks/research/plan/commit|END)`.
- **Idioma de interacción**: resuelto/persistido con precedencia `--language` > `INTERACTION_LANGUAGE` > preferencia guardada (`.agents/harness/preferences.json`) > selección/fallback. El contenido interno del flujo se normaliza a inglés cuando es necesario; las salidas al usuario se localizan según el idioma de interacción (con fallback seguro).
- **Tareas y ejecutores**: el PM produce un plan con tareas normalizadas; `executor=builder` sigue la cadena de verificación `builder -> reviewer -> (security) -> qa -> pm_decide`, mientras que `executor=research` ejecuta investigación/onboarding y vuelve a planificación/decisión según el caso.
- **Onboarding y contexto**: detección/fingerprint y cache de estado deciden si ejecutar onboarding (según `RESEARCH_ONBOARDING`, `--refresh-research`, `--skip-onboarding`). Se genera/actualiza `docs/project_specs.md` (canónico en inglés) y copia localizada cuando aplica, además de contexto/digest acotado en `.agents/research/` (p. ej. `context.md`, `state.json`) inyectado de forma limitada para ahorrar cómputo.
- **Research bajo demanda**: puede dispararse por señal del plan/estado o por `--research "<question>"` (`needs_research` + `research_question`), con retorno controlado y límites de ciclos.
- **Consentimiento web (research)**: opt-in por ejecución (`WEB_ACCESS` / `--allow-web` / `--no-web`), con modos permitir búsqueda, solo URLs provistas por usuario o sin internet; guardrails SSRF/alcance y auditoría local sanitizada en `.agents/research/audit.jsonl` (gitignored). No se persiste consentimiento fuera del estado de ejecución.

## Estructura del proyecto

```text
.
├── agents/      # Definiciones de agentes (fuente de verdad)
├── app/
│   ├── cli.py             # CLI y flags
│   ├── config.py          # Settings, env y fábrica de LLM
│   ├── state.py           # TeamState compartido
│   ├── i18n.py            # Idioma, preferencias y textos fijos
│   ├── agents/            # Loader y runtime de agentes
│   ├── graph/             # Grafo LangGraph, nodos y routing
│   ├── research/          # Detección, fingerprint, scanner, onboarding/writer
│   └── tools/             # fs/shell/git/web con sandbox y políticas
├── docs/                  # Artefactos generados (p. ej. project_specs.md, research/*)
├── tests/                 # Tests (unittest, stdlib, sin red)
├── main.py                # Entry point
├── .env.example
└── requirements.txt
```

## Agentes y permisos

- **Fuente de verdad:** `agents/*.md` define descripción, metadatos, *system prompt* y `permissions`.
- **Loader y políticas:** `app/agents/loader.py` valida definiciones; `app/tools/policy.py` aplica **deny-por-defecto** con precedencia de `deny` sobre `allow`.
- **Capacidades (alto nivel):** `project-manager` (orquesta/planifica), `translator` (idioma/normalización), `builder` (implementa), `reviewer` (revisa), `qa` (verifica), `security` (seguridad), `research` (contexto read-only local y web acotado con consentimiento). Las capacidades efectivas dependen de los permisos del agente y del runtime.
- **Research:** sin `edit`/`shell`/git/subagentes; lecturas deniegan secretos/ruido (`.env`, llaves, `.ssh`/`.aws`, `.venv`, `node_modules`, `dist`, `build`, etc.). Web solo `http/https` con controles SSRF (bloqueo de rangos privados/reservados, revalidación en redirecciones), timeouts/límites y sanitización. Auditoría sanitizada local.

## Skills (find-skills)

La gestión dedicada de *skills* (T5) está implementada. El flujo permite:

- **Directorio de skills:** `.agents/skills/` (gitignorado, bajo `.agents/`). Cada skill es un subdirectorio con un archivo `SKILL.md` que contiene metadatos en YAML frontmatter.

- **Formato de cada skill:** `SKILL.md` con YAML frontmatter obligatorio y cuerpo de *system prompt*:

  ```yaml
  ---
  name: <skill name>
  description: <free text>
  mode: <optional, e.g. "tools"|"json"|"auto">
  color: <optional hex>
  permissions:
    - action: read
      resource: "*"
      effect: allow
    - action: shell
      resource: "python*"
      effect: allow
  system_prompt: |
    <skill-specific instructions>
  ```

  - `name`: identificador único
  - `description`: texto libre
  - `mode`: tools/json/auto (predeterminado "auto")
  - `color`: color hex para display
  - `permissions`: lista de reglas de permisos (mismo formato que `agents/*.md`)
  - `system_prompt`: instrucciones/conducta de la skill

- **Carga de skills:** `load_skills()` en `app/agents/loader.py` carga skills desde `.agents/skills/*.md`, produciendo `AgentSpec` con namespace `skill-<name>`. El *project-manager* puede proponer skills durante la planificación de tareas y estos se inyectan en los *system prompts* del *builder* o *research* para ampliar capacidades sin modificar el código base.

- **Aprobación humana:** La inclusión de una skill propuesta por el *project-manager* requiere aprobación explícita antes de ser instalada y activa en el flujo de trabajo. Se registran las aprobaciones/rechazos en el estado.

- **Auditoría:** `.agents/skills/` permanece ignorado por git y cada skill es auditable a través de su `SKILL.md`.

### Flujo de trabajo T5

1. El *project-manager* identifica una skill necesaria para la tarea y la propone durante la planificación (`skills_setup` node).
2. Se solicita aprobación humana explícita para instalar la skill (interrupción `skill_approval`).
3. Si se aprueba, la skill se copia a `.agents/skills/<nombre>/` y se carga vía `load_skills()`.
4. La skill se inyecta en el *system prompt* del *builder* o *research* con el namespace `skill-<name>`.
5. Tras completar la tarea, la skill puede desinstalarse o mantenerse para reutilización.

### Ejemplo de skill

Véase `.agents/skills/example-skill/SKILL.md` para un ejemplo completo.
3. Ajustar `agents/*.md` si el proyecto requiere prompts/permisos/roles distintos (fuente de verdad).
4. Ejecutar desde la raíz deseada o definir `WORKSPACE_ROOT`.
5. Validar base sin red: `.venv/bin/python main.py --check` y/o tests.
6. Ejecutar el flujo: `.venv/bin/python main.py "tu requerimiento"` (o flags según necesidad).

## Notas de seguridad

- **Secretos:** `.env`, `.env.*` (salvo `.env.example`), llaves y artefactos sensibles excluidos de git.
- **Sandbox:** herramientas restringidas al `workspace_root` con validación de rutas.
- **Shell:** mediado por políticas (`ShellPolicy`/`PermissionPolicy`) con evaluación defensiva (mitiga, no elimina, riesgos residuales como TOCTOU).
- **Web/research:** consentimiento explícito por ejecución, guardrails SSRF/alcance, sanitización y auditoría local; contenido web tratado como dato no confiable.
- **Skills de terceros:** requieren aprobación humana explícita y revisión antes de uso.
- Revisar configuración y permisos antes de usar en contextos sensibles.

## Roadmap

- [x] T1: Base `app/` (config, state, CLI)
- [x] T2: Loader de agentes y políticas deny-por-defecto
- [x] T3: Runtime de agentes + probe/fallback
- [x] T4: Grafo LangGraph y routing
- [x] R0–R5: entrada/traducción-i18n, esquema de tareas PM, research/web con consentimiento, onboarding/specs/fingerprint, integración de flujo y límites de ciclos
- [x] T5: SkillResolver / find-skills (búsqueda, instalación con aprobación humana, inyección `SKILL.md`)
- [ ] Extensiones CLI avanzadas (`--workspace`, `--max-cycles`, `--dry-run`, gestión de skills) y commit git automático integrado

