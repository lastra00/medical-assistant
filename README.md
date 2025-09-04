# 🩺 Asistente Farmacéutico — Guía Completa

Esta aplicación entrega información de farmacias (generales y de turno) del MINSAL y fichas de medicamentos basadas en un vademécum local (CSV) con búsqueda semántica. Incluye:

- API y UI web en FastAPI/LangServe
- Orquestación de la conversación con LangGraph (guardrails → router → tools → formato)
- Memoria por usuario en Redis
- Retrieval sobre Qdrant con OpenAI Embeddings
- Proxys a MINSAL para operación estable en la nube

Demo (despliegue en Fly): [medical-assistant-proxy.fly.dev](https://medical-assistant-proxy.fly.dev)

> Importante: El asistente no entrega recomendaciones médicas ni pautas de dosificación. Sólo brinda información factual. Ante dudas de salud, consulta a un profesional.

---

## 1) Arquitectura general

```mermaid
graph TD
  UI["UI Web (HTML/JS)"] --> API["FastAPI + LangServe (/chat, /graph, /ui/chat)"]
  CLI["CLI (chat_cli.py)"] --> API
  API --> LG["LangGraph (orquestador)"]
  LG --> GR["Guardrails (bloqueo dosis/prescripción)"]
  LG --> RT["Router (intención)"]
  RT --> NF["Nodo Farmacias"]
  RT --> NT["Nodo Turnos"]
  RT --> NM["Nodo Medicamentos"]
  NF --> MINSAL["APIs MINSAL (con proxy)"]
  NT --> MINSAL
  NM --> QDRANT["Qdrant (vector DB)"]
  API -.memoria.-> REDIS["Redis (historial por usuario)"]
```

### Componentes
- `final_proyect/med_agent/server.py`: API FastAPI + LangServe, UI estática, proxys `/locales` y `/turnos`, endpoint de chat `/ui/chat`, salud `/healthz` y limpieza de historial.
- `final_proyect/med_agent/graph.py`: construcción del grafo LangGraph con nodos y lógica de negocio.
- `final_proyect/med_agent/tools.py`: llamadas HTTP robustas a MINSAL (encabezados tipo navegador, reintentos y proxys públicos si es necesario).
- `final_proyect/med_agent/retrieval.py`: indexa `drug_dataset/DrugData.csv` en Qdrant y realiza búsquedas semánticas.
- `final_proyect/med_agent/static/index.html`: UI mínima de chat.
- `final_proyect/med_agent/chat_cli.py`: cliente de consola con memoria en Redis y detector de usuario.

---

## 2) Flujo de conversación (paso a paso)

```mermaid
sequenceDiagram
  participant U as Usuario (UI/CLI)
  participant API as FastAPI/LangServe
  participant G as Grafo (LangGraph)
  participant T as Tools/MINSAL
  participant V as Qdrant
  participant R as Redis

  U->>API: POST /ui/chat {message}
  API->>API: Detectar usuario (si no está fijado)
  API->>R: Leer historial (session_id=usuario_<nombre>) [límite configurable]
  API->>G: invoke({messages: historial + mensaje})
  G->>G: guardrails() (bloquea dosis/prescripción)
  G->>G: router() → {farmacias | turnos | meds | saludo}
  alt farmacias
    G->>T: GET getLocales.php (o proxy)
  else turnos
    G->>T: GET getLocalesTurnos.php (o proxy)
  else meds
    G->>V: Retrieval Qdrant (OpenAI Embeddings)
  end
  G-->>API: format() (respuesta final)
  API->>R: Guardar turno de conversación (usuario/AI)
  API-->>U: Texto final
```

### Memoria por usuario
- El `session_id` es `usuario_<nombre>`. La UI/CLI detecta el nombre (“soy Ana”, “hola, aquí Juan”) y fija la sesión.
- El backend recorta el historial a los últimos N mensajes (por defecto 14) para evitar prompts gigantes. Puedes ajustar con `UI_HISTORY_LIMIT`.

---

## 3) El Grafo LangGraph en detalle

```mermaid
stateDiagram-v2
  [*] --> guardrails
  guardrails --> format: bloqueado
  guardrails --> router: ok
  router --> nodo_saludo: "saludo"
  router --> nodo_farmacias: "farmacias"
  router --> nodo_turnos: "turnos"
  router --> nodo_meds: "meds"
  nodo_saludo --> format
  nodo_farmacias --> format
  nodo_turnos --> format
  nodo_meds --> format
  format --> [*]
```

### 3.1 Guardrails (seguridad)
Bloquea solicitudes de dosis/indicaciones terapéuticas. Lógica combinada:

- Heurística local: detecta frases como “¿cuánto puedo tomar?”, “dosis”, “posología”, etc.
- Verificación con un LLM estructurado que devuelve `{blocked, policy_message}`.
- Mensaje requerido si bloquea: “Lo siento, pero no puedo ofrecer recomendaciones médicas.” + sugerencia breve (consultar profesional o fuentes oficiales).

### 3.2 Router (intención + filtros)
Un LLM estructurado clasifica a una de estas rutas: `saludo`, `farmacias`, `turnos`, `meds` y extrae filtros explícitos cuando existen (p. ej., `comuna`, `direccion`, `funcionamiento_dia`, `fecha`, `fk_region`, `local_nombre`, etc.).

### 3.3 Nodo Farmacias
1. Obtiene locales desde MINSAL vía `tool_minsal_locales`.
2. Si se indicó `comuna` y el upstream no filtra, aplica filtrado local robusto (normalización, match exacto → parcial).
3. Si la consulta parece contener dirección, tokeniza y filtra `local_direccion` por coincidencia de tokens.
4. Fallback: si con `comuna` no hay resultados, trae `turnos` y filtra por la misma comuna.

```mermaid
flowchart LR
  A["Texto usuario"] --> B["normalizar (lower, sin tildes, sin puntuación)"]
  B --> C{patrones de comuna}
  C -->|match| D["comuna = ..."]
  B --> E{direccion o numeros o keywords}
  E -->|si| F["tokenizar direccion"]
  F --> G["match tokens en local_direccion"]
```

### 3.4 Nodo Turnos
Similar a Farmacias, pero llamado a `getLocalesTurnos.php` y con filtros extra:
- `funcionamiento_dia`: si el usuario dice “hoy/ahora” se mapea al día actual.
- `fecha`: sólo se usa cuando llega explícita (los formatos upstream varían).

### 3.5 Nodo Medicamentos
Dos modos:
1) Listas por campo (`list_by_class`, `list_by_indications`, `list_by_mechanism`, `list_by_route`, `list_by_pregnancy_category`). Usa un mini intérprete LLM que decide el modo y un traductor ES→EN para mejorar recall (por ejemplo, “antibióticos” → `antibiotics`). Luego filtra los resultados por metadata en Qdrant.
2) Por nombre: busca en Qdrant con la consulta y filtra resultados para el fármaco mencionado (tolerante a alias EN). Si no encuentra, intenta directamente el token objetivo.

### 3.6 Formateo final
Compone secciones claras:
- Farmacias (y Turnos si existen), citando fuente MINSAL
- Información de medicamentos (descripción breve y bullets: nombre, indicaciones, mecanismo, contraindicaciones, interacciones y advertencias)
- Nota fija al final: “Ante una emergencia, acude a un hospital.”

---

## 4) Búsqueda semántica y Qdrant

El archivo `drug_dataset/DrugData.csv` se indexa en Qdrant. Cada fila se convierte en un `Document` con:

- `page_content` combinando campos clave (Drug Name, Class, Indications, etc.)
- `metadata` con campos normalizados para filtrado rápido

Embeddings: `OpenAIEmbeddings` con modelo `text-embedding-3-large` (dimensiones configurables). Si la colección no existe, se crea; si existe, se reutiliza.

---

## 5) Integración con MINSAL (tools)

Las funciones `_http_get` y `_http_get_with_fallback` usan encabezados de navegador, reintentos y proxys públicos de último recurso (AllOrigins, r.jina.ai) cuando el upstream falla. Además, puedes enrutar todo tráfico MINSAL vía la propia app en Fly exportando:

- `MINSAL_PROXY_URL=https://medical-assistant-proxy.fly.dev`

La API expone `/locales` y `/turnos` que actúan como proxy estable hacia MINSAL y evitan CORS/403 en la nube.

---

## 6) Variables de entorno (resumen)

Mínimas:

- `OPENAI_API_KEY` (acepta alias `openai_api_key`)
- `REDIS_URL` (en Redis Cloud, usa `rediss://` y puerto TLS; si hay problemas, `?ssl_cert_reqs=none`)

Retrieval/Qdrant:

- `QDRANT_URL` — por ejemplo, tu instancia en Qdrant Cloud
- `QDRANT_API_KEY`
- `QDRANT_COLLECTION` (default: `med_agent_drugs`)

MINSAL (opcionales):

- `MINSAL_PROXY_URL` — recomendado en Fly
- `MINSAL_GET_LOCALES`, `MINSAL_GET_TURNOS` (defaults oficiales)

Otros:

- `UI_HISTORY_LIMIT` — límite de mensajes previos que se envían al LLM en `/ui/chat` (default: 14)

---

## 7) Ejecución local

1. Crear entorno y dependencias

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r final_proyect/requirements.txt
```

2. Exportar variables (ejemplo mínimo)

```bash
export OPENAI_API_KEY=sk-...
export REDIS_URL=redis://localhost:6379/0
export QDRANT_URL=http://localhost:6333
export QDRANT_API_KEY= # si aplica
```

3. Levantar API

```bash
uvicorn final_proyect.med_agent.server:app --host 0.0.0.0 --port 8000 --reload
```

4. Probar

- UI: [http://127.0.0.1:8000/app/](http://127.0.0.1:8000/app/)
- Playground LangServe: [http://127.0.0.1:8000/chat/playground/](http://127.0.0.1:8000/chat/playground/)

5. CLI (opcional)

```bash
python -m final_proyect.med_agent.chat_cli
```

Comandos del CLI: `usuario [nombre]`, `cambiar [nombre]`, `historial [nombre]`, `limpiar [nombre]`, `estado`, `salir`.

---

## 8) Invocaciones por HTTP (ejemplos)

### 8.1 Chat (LangServe runnable)

```bash
curl -s -X POST http://127.0.0.1:8000/chat/invoke \
  -H 'Content-Type: application/json' \
  -d '{
    "input": {"messages": [{"type":"human","content":"farmacias en Lebu"}]},
    "config": {"configurable": {"session_id": "usuario_pepito"}}
  }'
```

### 8.2 Chat UI helper (/ui/chat)

```bash
curl -s -X POST http://127.0.0.1:8000/ui/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"hola, aquí Ana"}'
```

### 8.3 Proxys MINSAL

```bash
curl -s 'http://127.0.0.1:8000/turnos?comuna_nombre=Traigu%C3%A9n'
curl -s 'http://127.0.0.1:8000/locales?comuna_nombre=Traigu%C3%A9n'
```

---

## 9) Despliegue en Fly.io (resumen)

1. Instalar y autenticarse con `flyctl`.
2. Desde `final_proyect/`, setear secretos (ajusta valores reales):

```bash
flyctl secrets set -a medical-assistant-proxy \
  OPENAI_API_KEY="..." \
  REDIS_URL="rediss://default:PASS@HOST:TLS_PORT?ssl_cert_reqs=none" \
  QDRANT_URL="https://TU-CLUSTER.qdrant.tech" \
  QDRANT_API_KEY="..." \
  MINSAL_PROXY_URL="https://medical-assistant-proxy.fly.dev"
```

3. Desplegar:

```bash
flyctl deploy -a medical-assistant-proxy
```

4. Probar: abre la URL de la app y los proxys `/locales` y `/turnos`.

---

## 10) Solución de problemas

- Respuesta vacía / 500 al iniciar chat: revisa `OPENAI_API_KEY`.
- En la nube MINSAL falla (403/429): usa `MINSAL_PROXY_URL` (los proxys integrados evitan CORS/403).
- Redis Cloud no conecta: usa `rediss://` y el puerto TLS; si hace falta, `?ssl_cert_reqs=none`.
- El chat queda “procesando”: puede ser historial enorme. Ajusta `UI_HISTORY_LIMIT` (p. ej., 14) y redeploy.
- `/chat/invoke` devuelve error “Missing keys ['session_id']”: envía `config.configurable.session_id`.
- `ResponseValidationError` en `/locales`/`/turnos`: ya se retornan objetos `Any` para admitir las formas reales del upstream.

---

## 11) Ética y seguridad

- No entrega dosis ni indicaciones personalizadas.
- Cita fuentes: MINSAL para farmacias; vademécum local para medicamentos.
- Mensaje final fijo: “Ante una emergencia, acude a un hospital.”

---

## 12) Estructura del proyecto

```
final_proyect/
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

¿Preguntas o quieres ampliar alguna sección? Puedo agregar ejemplos de prompts, más diagramas o guías específicas de despliegue.
