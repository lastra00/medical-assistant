🩺 Asistente Farmacéutico — Guía Completa y Profunda

Esta aplicación entrega información de farmacias (generales y de turno) del MINSAL y fichas factuales de medicamentos (desde un vademécum CSV) con búsqueda semántica. Está pensada para que, con conocimientos básicos de programación, LangGraph y front/back, puedas entender y explicar en detalle su funcionamiento.

Importante: el asistente no entrega recomendaciones médicas ni pautas de dosificación. Sólo brinda información factual. Ante dudas de salud, consulta a un profesional.

### Lista de contenidos
- Arquitectura general (alto nivel) y componentes reales del repo
- Flujo completo: desde que el usuario abre el front hasta 6 mensajes de conversación
- El Grafo LangGraph en detalle: nodos, entradas/salidas y condiciones de transición
- Integración con MINSAL (tools y proxys) y con Qdrant (retrieval semántico)
- Endpoints de la API y ejemplos de llamadas (UI y LangServe)
- Variables de entorno y ejecución local
- Despliegue (Fly.io) y troubleshooting
- Ejemplos de inputs/outputs esperados para casos comunes y de borde

---

## Arquitectura general

```mermaid
graph TD
  UI["UI Web (HTML/JS) /app/"] --> API["FastAPI + LangServe (/chat, /graph, /ui/chat)"]
  CLI["CLI (med_agent/chat_cli.py)"] --> API
  API --> LG["LangGraph (orquestador)"]
  LG --> SC["in_scope (clasificador de tópico)"]
  SC -- "off-topic" --> RS["Mensaje fijo fuera de alcance"]
  SC -- "in-scope" --> GR["guardrails (bloqueo dosis/prescripción)"]
  GR --> RT["router (intención + filtros)"]
  RT --> NF["nodo_farmacias"]
  RT --> NT["nodo_turnos"]
  RT --> NM["nodo_meds"]
  NF --> MINSAL["APIs MINSAL (con proxy/headers)"]
  NT --> MINSAL
  NM --> QDRANT["Qdrant (vector DB)"]
  API -.memoria.-> REDIS["Redis (historial por usuario)"]
```

### Componentes (rutas reales)
- `med_agent/server.py`: FastAPI + LangServe, UI estática `/app/`, proxys `/locales` y `/turnos`, chat UI `/ui/chat`, salud `/healthz`, limpieza de historial `/history/clear` y Playgrounds `/graph` y `/chat`.
- `med_agent/graph.py`: construcción del grafo LangGraph con nodos y reglas de transición.
- `med_agent/tools.py`: llamadas HTTP robustas a MINSAL (headers de navegador, reintentos y proxys públicos si falla).
- `med_agent/retrieval.py`: indexa `drug_dataset/DrugData.csv` en Qdrant y hace retrieval con `OpenAIEmbeddings`.
- `med_agent/static/index.html`: UI mínima (HTML/JS) que conversa con `/ui/chat` y mantiene `usuario_actual`.
- `med_agent/chat_cli.py`: CLI con memoria persistente en Redis.

Estructura del proyecto
```
medical-assistant/
├── med_agent/
│   ├── server.py         # FastAPI + LangServe + UI + proxys MINSAL
│   ├── graph.py          # Grafo LangGraph (nodos y flujo)
│   ├── tools.py          # HTTP robusto a MINSAL
│   ├── retrieval.py      # Qdrant + OpenAIEmbeddings sobre CSV
│   ├── static/
│   │   └── index.html    # UI de chat
│   └── chat_cli.py       # Cliente de consola con Redis
├── drug_dataset/
│   └── DrugData.csv      # Vademécum local
├── fly.toml              # Configuración Fly.io
├── Dockerfile            # Imagen de despliegue
└── requirements.txt
```

---

## Flujo completo: del front a 6 mensajes

1) El usuario abre `/app/` (UI estática). La UI muestra un saludo y pide el nombre para identificar la sesión.
2) El usuario escribe. La UI hace `POST /ui/chat` con `{ message, current_user? }`.
3) El backend intenta identificar usuario con un LLM ligero. Si el mensaje es sólo un nombre (“Ana”), fija `session_id=usuario_ana` y responde sin invocar el grafo.
4) Cuando ya hay usuario, el backend:
   - Lee historial desde Redis (`session_id=usuario_<nombre>`), recorta a `UI_HISTORY_LIMIT` (default 14), compone `messages = historial + mensaje`.
   - Invoca el grafo LangGraph con `invoke({messages})`.
   - Persiste manualmente el turno (usuario y AI) en Redis.
5) El grafo aplica guardrails, enruta a nodos (farmacias/turnos/meds/saludo), obtiene datos (MINSAL/Qdrant), y formatea la respuesta.
6) La UI muestra el texto, mantiene `usuario_actual` y continúa el ciclo.

Secuencia (6 mensajes)
```mermaid
sequenceDiagram
  autonumber
  participant U as Usuario (UI)
  participant API as FastAPI (/ui/chat)
  participant R as Redis (historial)
  participant G as Grafo LangGraph
  participant T as Tools MINSAL
  participant V as Qdrant

  U->>API: 1) "Hola, soy Ana"
  API-->>U: Confirmación de usuario (sin invocar grafo)

  U->>API: 2) "farmacias en Lebu"
  API->>R: Leer historial (usuario_ana)
  API->>G: invoke({messages})
  G->>G: in_scope ✓ → guardrails ✓ → router=farmacias
  G->>T: getLocales (o fallback)
  G-->>API: format (texto MINSAL)
  API->>R: Persistir turno
  API-->>U: Lista de farmacias

  U->>API: 3) "¿y de turno hoy?"
  API->>R: Leer historial
  API->>G: invoke
  G->>G: router=turnos (hoy→día actual)
  G->>T: getLocalesTurnos
  G-->>API: format
  API->>R: Persistir turno
  API-->>U: Turnos hoy

  U->>API: 4) "efectos adversos del ibuprofeno"
  API->>R: Historial
  API->>G: invoke
  G->>G: router=meds → by_name
  G->>V: search (Qdrant)
  G-->>API: format (ficha factual)
  API->>R: Persistir
  API-->>U: Ficha ibuprofeno

  U->>API: 5) "¿cada cuánto puedo tomar?"
  API->>G: invoke
  G->>G: guardrails → blocked (dosis)
  G-->>API: policy_message
  API-->>U: Mensaje de política (sin dosis)

  U->>API: 6) "farmacias en Traiguén por O’Higgins 779"
  API->>G: invoke
  G->>G: router=farmacias + address_mode
  G->>T: getLocales → filtro por comuna + tokens dirección
  G-->>API: format
  API-->>U: Resultados por dirección
```

---

## El Grafo LangGraph en detalle

```mermaid
stateDiagram-v2
  [*] --> guardrails
  guardrails --> format: blocked (dosis/off-topic)
  guardrails --> router: ok
  router --> nodo_saludo: saludo
  router --> nodo_farmacias: farmacias
  router --> nodo_turnos: turnos
  router --> nodo_meds: meds
  nodo_saludo --> format
  nodo_farmacias --> format
  nodo_turnos --> format
  nodo_meds --> format
  format --> [*]
```

- guardrails
  - in_scope: LLM estructurado. Si off-topic → `blocked=true` con mensaje fijo.
  - Dosis/prescripción: heurística + LLM. Requiere que `policy_message` comience con “Lo siento, pero no puedo ofrecer recomendaciones médicas.” y añada una breve sugerencia.
- router
  - LLM estructurado → `route` en {saludo, farmacias, turnos, meds} y posibles filtros: `comuna`, `direccion`, `funcionamiento_dia`, `fecha`, `fk_region`, `local_nombre`, etc.
  - `routes` permite ejecutar varias rutas en secuencia (p.ej., farmacias y turnos).
- nodo_saludo
  - Devuelve texto de cortesía y encuadre del asistente.
- nodo_farmacias
  - Extrae comuna (patrones en español) y detecta modo dirección (número/keywords).
  - Llama `tool_minsal_locales` con filtro servidor; si vacío, descarga y filtra localmente; si sigue vacío, intenta fallback con `turnos` para la misma comuna.
  - Filtros adicionales: `localidad`, `local_nombre`, teléfono, `funcionamiento_hora_*`, `fk_*`.
- nodo_turnos
  - Similar a farmacias, pero con `getLocalesTurnos`.
  - Si `funcionamiento_dia` ∈ {hoy, ahora} → mapea al día actual.
  - `fecha` sólo si viene explícita (formatos upstream varían).
- nodo_meds
  - Intérprete de intención: `by_name` vs `list_by_*` (class/indications/mechanism/route/pregnancy).
  - Traduce tokens ES→EN para mejorar recall en Qdrant y singulariza variantes.
  - `by_name`: búsqueda + filtrado por token/alias; si no hay hits, intenta “para que sirve X”.
  - `list_by_*`: construye lista de nombres a partir del payload filtrado.
- format
  - Revalida in_scope; si off-topic → mensaje fijo.
  - Compone secciones: Farmacias, Turnos, Medicamentos (fichas o listas según flags) y cierra con “Ante una emergencia…”.

---

## Integraciones

### MINSAL (tools)
- Encabezados tipo navegador, reintentos y proxys públicos (AllOrigins, r.jina.ai) como último recurso.
- Endpoints propios de proxy: `/locales` y `/turnos` (para nubes con CORS/403).

### Qdrant (retrieval)
- `drug_dataset/DrugData.csv` → documentos con `page_content` + `metadata` filtrable.
- Embeddings `text-embedding-3-large` (dimensiones 256 por defecto).
- Si la colección no existe, se crea; si existe, se reutiliza.

---

## Endpoints y ejemplos

- UI: `/app/`
- Playground LangServe: `/chat/playground/` y `/graph/playground/`
- Chat UI helper: `POST /ui/chat { message, current_user? }`
- Chat (LangServe runnable): `POST /chat/invoke`
- Proxys MINSAL: `GET /locales`, `GET /turnos`
- Salud: `GET /healthz`
- Limpiar historial: `POST /history/clear { session_id }`

Ejemplos
```bash
# LangServe runnable con sesión
curl -s -X POST http://127.0.0.1:8000/chat/invoke \
  -H 'Content-Type: application/json' \
  -d '{
    "input": {"messages": [{"type":"human","content":"farmacias en Lebu"}]},
    "config": {"configurable": {"session_id": "usuario_ana"}}
  }'

# UI: fijar usuario
curl -s -X POST http://127.0.0.1:8000/ui/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Soy Ana"}'

# UI: consulta con usuario fijado
curl -s -X POST http://127.0.0.1:8000/ui/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"efectos adversos del ibuprofeno","current_user":"Ana"}'

# Proxys MINSAL
curl -s 'http://127.0.0.1:8000/locales?comuna_nombre=Traigu%C3%A9n'
curl -s 'http://127.0.0.1:8000/turnos?comuna_nombre=Traigu%C3%A9n'
```

---

## Variables de entorno

Mínimas
- `OPENAI_API_KEY` (acepta alias `openai_api_key`)
- `REDIS_URL` (si es Redis Cloud, usa `rediss://` y el puerto TLS; si hay problemas, `?ssl_cert_reqs=none`)

Retrieval/Qdrant
- `QDRANT_URL` — p. ej., tu instancia en Qdrant Cloud
- `QDRANT_API_KEY`
- `QDRANT_COLLECTION` (default: `med_agent_drugs`)

MINSAL (opcionales)
- `MINSAL_PROXY_URL` — recomendado en Fly
- `MINSAL_GET_LOCALES`, `MINSAL_GET_TURNOS` (defaults oficiales)

Otros
- `UI_HISTORY_LIMIT` — recorte de historial (default: 14)

---

## Ejecución local

1) Crear entorno y dependencias
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r medical-assistant/requirements.txt
```

2) Exportar variables (ejemplo mínimo)
```bash
export OPENAI_API_KEY=sk-...
export REDIS_URL=redis://localhost:6379/0
export QDRANT_URL=http://localhost:6333
export QDRANT_API_KEY= # si aplica
```

3) Levantar API
```bash
uvicorn med_agent.server:app --host 0.0.0.0 --port 8000 --reload
```

4) Probar
- UI: `http://127.0.0.1:8000/app/`
- Playground LangServe: `http://127.0.0.1:8000/chat/playground/`

5) CLI (opcional)
```bash
python -m med_agent.chat_cli
```

---

## Despliegue en Fly.io (resumen)

1) Instala y autentícate con `flyctl`.
2) Desde `medical-assistant/`, setea secretos (valores reales):
```bash
flyctl secrets set -a medical-assistant-proxy \
  OPENAI_API_KEY="..." \
  REDIS_URL="rediss://default:PASS@HOST:TLS_PORT?ssl_cert_reqs=none" \
  QDRANT_URL="https://TU-CLUSTER.qdrant.tech" \
  QDRANT_API_KEY="..." \
  MINSAL_PROXY_URL="https://medical-assistant-proxy.fly.dev"
```
3) Desplegar
```bash
flyctl deploy -a medical-assistant-proxy
```
4) Probar: abre la URL del app y los proxys `/locales` y `/turnos`.

---

## Troubleshooting
- Respuesta vacía/500 al iniciar chat: revisa `OPENAI_API_KEY`.
- MINSAL 403/429 en la nube: usa `MINSAL_PROXY_URL` (proxys integrados evitan CORS/403).
- Redis Cloud: usa `rediss://` y puerto TLS; si hace falta, `?ssl_cert_reqs=none`.
- Chat “procesando” por mucho tiempo: historial enorme. Ajusta `UI_HISTORY_LIMIT`.
- En `/chat/invoke`, si falta `session_id`: envíalo en `config.configurable.session_id`.

---

## Ética y seguridad
- No se dan dosis ni prescripciones personalizadas.
- Se citan fuentes: MINSAL (farmacias) y vademécum local (medicamentos).
- Se añade el recordatorio: “Ante una emergencia, acude a un hospital.”

---

## Referencia rápida: entradas → salidas esperadas

| Caso | Input | Salida esperada |
|------|-------|-----------------|
| Saludo/identificación | "Soy Ana" | Confirma usuario y pide consulta |
| Farmacias (comuna) | "farmacias en Lebu" | Lista con nombre, dirección y horario (fuente MINSAL) |
| Farmacias de turno | "¿qué farmacia hay de turno hoy en Traiguén?" | Lista de turno para la comuna y día correspondiente |
| Por dirección | "farmacia en O’Higgins 779, Traiguén" | Local(es) matcheando tokens de dirección |
| Medicamentos (by_name) | "efectos adversos del ibuprofeno" | Ficha factual (descripción breve + bullets) |
| Medicamentos (lista por clase) | "¿qué antibióticos existen?" | Lista de nombres (clase: Antibiotic) |
| Dosis/Prescripción | "¿cada cuánto puedo tomar ibuprofeno?" | Mensaje de política (sin recomendaciones) |
| Off-topic | "¿me das una receta de lentejas?" | Rechazo fijo de tópico |

---

¿Preguntas o quieres ampliar algo? Puedo añadir ejemplos extra, diagramas o guías específicas.
