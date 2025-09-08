from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from dotenv import load_dotenv
from langserve import add_routes
from langgraph.checkpoint.memory import MemorySaver
from typing import Optional, Dict, Any
import requests
import json
import time
from urllib.parse import urlencode, quote, urlsplit
from pydantic import BaseModel, Field
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_community.chat_message_histories import RedisChatMessageHistory
from fastapi.staticfiles import StaticFiles
import os
import re
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage

# Cargar .env ANTES de importar el grafo (para QDRANT_URL/API_KEY, etc.)
load_dotenv()
from .graph import build_graph
from .config import MINSAL_GET_LOCALES, MINSAL_GET_TURNOS, REDIS_URL


def create_app() -> FastAPI:
    app = FastAPI(title="Med Agent API")
    graph = build_graph()
    # Envolver con historial Redis y exponer session_id en Playground
    history_graph = RunnableWithMessageHistory(
        graph,
        lambda session_id: RedisChatMessageHistory(session_id=session_id, url=REDIS_URL),
        input_messages_key="messages",
        history_messages_key="messages",
    )

    # ============ Detección de usuario (como en chat_multi_usuario) ============
    class DeteccionUsuario(BaseModel):
        usuario_identificado: bool
        nombre_usuario: Optional[str] = None
        tipo_identificacion: Optional[str] = None

    detector_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(DeteccionUsuario)
    detector_prompt = ChatPromptTemplate.from_template(
        """
        Analiza el mensaje y decide si el usuario SE ESTÁ IDENTIFICANDO con su nombre.
        Devuelve JSON con:
          - usuario_identificado: true|false
          - nombre_usuario: string o null
          - tipo_identificacion: presentacion|referencia|ninguna

        Reglas estrictas (no adivines):
        - Marca true solo si el mensaje contiene una presentación explícita ("soy X", "me llamo X", "mi nombre es X", "aquí X")
          o si el mensaje consiste ÚNICAMENTE en un nombre probable: uno o dos tokens (nombre o nombre y apellido).
        - Si el texto es un saludo u otra frase sin auto-identificación ("hola", "buenas", "¿cómo estás?", etc.), devuelve false.
        - No aceptes palabras comunes como nombres (hola, buenas, gracias, ayuda, consulta, hey, hi, hello, ok, listo, porfa, etc.).
        - Si hay dudas, devuelve false.

        Ejemplos válidos → true:
        - "Soy Pablo" → "Pablo"
        - "Me llamo Ana" → "Ana"
        - "Hola, acá Juan" → "Juan"
        - "Pablo" → "Pablo"
        - "Pablo Lastra" → "Pablo Lastra"

        Ejemplos inválidos → false:
        - "hola"
        - "buenas tardes"
        - "quiero una farmacia"
        - "ok"
        - "gracias"

        Mensaje: "{mensaje}"
        """
    )
    detector_chain = detector_prompt | detector_llm

    # Heurística local adicional (defensiva) para aceptar SOLO nombres plausibles
    _STOP_SINGLE_TOKENS = {
        # saludos / relleno
        "hola", "holi", "holaa", "buenas", "buenos", "dias", "días", "tardes", "noches", "saludos",
        "hello", "hi", "hey", "buenas!", "buenas," , "buen día", "que", "qué", "tal",
        # otros términos comunes
        "gracias", "ayuda", "consulta", "ok", "vale", "listo", "si", "sí", "no", "menu", "menú",
        "start", "inicio", "comenzar", "help", "thanks", "porfa",
    }

    def _is_name_like_token(tok: str) -> bool:
        t = tok.strip()
        if not (2 <= len(t) <= 40):
            return False
        # solo letras/guiones/apóstrofes (con acentos)
        if not re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ'\-]+", t):
            return False
        if t.lower() in _STOP_SINGLE_TOKENS:
            return False
        return True

    def _is_valid_name_string(s: str) -> bool:
        # Aceptar 1 o 2 tokens nombre-like
        toks = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ'\-]+", s.strip())
        if not (1 <= len(toks) <= 2):
            return False
        return all(_is_name_like_token(t) for t in toks)

    def _heuristic_only_name(msg: str) -> Optional[str]:
        toks = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ'\-]+", msg.strip())
        if 1 <= len(toks) <= 2 and all(_is_name_like_token(t) for t in toks):
            # Normalizar capitalización amable (sin cambiar acentos)
            pretty = " ".join(t.capitalize() for t in toks)
            return pretty
        return None

    class SessionConfig(BaseModel):
        session_id: str = Field(default="anon", description="ID de sesión para historial en Redis")
    # Exponer grafo como runnable
    add_routes(app, graph, path="/graph")
    # Playground de LangServe disponible en /chat/playground
    # Permitir que el playground envíe {"configurable": {"session_id": "..."}}
    add_routes(app, history_graph, path="/chat", config_keys=["configurable"])  # langserve actual: usar 'configurable'

    # Servir UI estática en /app (si existe carpeta static)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount("/app", StaticFiles(directory=static_dir, html=True), name="app")

    @app.get("/", response_class=HTMLResponse)
    def root() -> HTMLResponse:
        return HTMLResponse("""
        <html>
          <head><meta http-equiv=\"refresh\" content=\"0; url=/app/\" /></head>
          <body>
            <a href=\"/app/\">Abrir Chat</a> | <a href=\"/chat/playground/\">Playground</a>
          </body>
        </html>
        """)

    # Encabezados tipo navegador para evitar 403
    DEFAULT_HEADERS: Dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
        "Origin": "https://midas.minsal.cl",
        "Referer": "https://midas.minsal.cl/",
        "X-Requested-With": "XMLHttpRequest",
    }

    def _http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Dict[str, Any]:
        last: Exception | None = None
        for attempt in range(2):
            try:
                r = requests.get(url, params=params, timeout=timeout, headers=DEFAULT_HEADERS)
                if r.status_code in (403, 429) and attempt == 0:
                    time.sleep(0.8)
                    continue
                r.raise_for_status()
                try:
                    return r.json()
                except ValueError:
                    txt = (r.text or "").lstrip("\ufeff\n\r ")
                    return json.loads(txt)
            except Exception as e:
                last = e
                time.sleep(0.2)
                continue
        raise last  # type: ignore[misc]

    def _proxy_try(primary_url: str, alt_url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # 1) primario
        try:
            return _http_get(primary_url, params)
        except Exception:
            pass
        # 2) alternativo oficial (farmanet)
        try:
            return _http_get(alt_url, params)
        except Exception:
            pass
        # 3) proxys públicos
        def via_proxy(url: str) -> Dict[str, Any]:
            full = url
            if params:
                qs = urlencode(params)
                sep = '&' if ('?' in full) else '?'
                full = f"{full}{sep}{qs}"
            # allorigins
            try:
                wrapped = f"https://api.allorigins.win/raw?url={quote(full, safe='')}"
                return _http_get(wrapped, None)
            except Exception:
                pass
            # r.jina.ai
            parts = urlsplit(full)
            pathq = parts.path + (f"?{parts.query}" if parts.query else "")
            wrapped = f"https://r.jina.ai/http://{parts.netloc}{pathq}"
            return _http_get(wrapped, None)

        try:
            return via_proxy(primary_url)
        except Exception:
            return via_proxy(alt_url)

    @app.get("/healthz")
    def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    # Endpoints proxy mínimos para MINSAL (para desplegar en Fly)
    @app.get("/locales")
    def proxy_locales(
        comuna_nombre: Optional[str] = Query(default=None),
        fk_region: Optional[str] = Query(default=None),
    ) -> Any:
        params: Dict[str, Any] = {}
        if comuna_nombre:
            params["comuna_nombre"] = comuna_nombre
        if fk_region:
            params["fk_region"] = fk_region
        try:
            return _proxy_try(MINSAL_GET_LOCALES, "https://farmanet.minsal.cl/index.php/ws/getLocales", params)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"upstream error: {e}")

    @app.get("/turnos")
    def proxy_turnos(
        comuna_nombre: Optional[str] = Query(default=None),
        fk_region: Optional[str] = Query(default=None),
    ) -> Any:
        params: Dict[str, Any] = {}
        if comuna_nombre:
            params["comuna_nombre"] = comuna_nombre
        if fk_region:
            params["fk_region"] = fk_region
        try:
            return _proxy_try(MINSAL_GET_TURNOS, "https://farmanet.minsal.cl/index.php/ws/getLocalesTurnos", params)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"upstream error: {e}")

    # Limpieza de historial por session_id
    class ClearReq(BaseModel):
        session_id: str

    @app.post("/history/clear")
    def clear_history(req: ClearReq) -> Dict[str, str]:
        try:
            RedisChatMessageHistory(session_id=req.session_id, url=REDIS_URL).clear()
            return {"status": "ok"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ============ Endpoint UI: detección + sesión + grafo ============
    class UIChatRequest(BaseModel):
        message: str
        current_user: Optional[str] = None

    @app.post("/ui/chat")
    def ui_chat(req: UIChatRequest) -> Dict[str, Any]:
        msg = (req.message or "").strip()
        if not msg:
            return {"text": "", "usuario_actual": req.current_user, "session_id": f"usuario_{(req.current_user or 'anon').lower()}"}

        # Detectar usuario en cada mensaje
        detected_user: Optional[str] = None
        try:
            det = detector_chain.invoke({"mensaje": msg})
            if det.usuario_identificado and det.nombre_usuario:
                # Validar salida del LLM con heurística local (defensiva)
                if _is_valid_name_string(det.nombre_usuario):
                    detected_user = det.nombre_usuario
        except Exception:
            detected_user = None

        # Heurística: aceptar si el MENSAJE es únicamente un nombre (1 o 2 tokens) válido
        heuristic_user = _heuristic_only_name(msg)

        if detected_user or heuristic_user:
            usuario = (detected_user or heuristic_user)  # preferimos lo detectado por LLM, si pasó validación
            session_id = f"usuario_{usuario.lower()}"
            # Respuesta breve de confirmación; evitamos pasar este mensaje al grafo (no es contenido de consulta)
            texto = (
                f"¡Gracias, {usuario}! Ya te identifiqué. "
                "¿Qué necesitas sobre farmacias o medicamentos?"
            )
            return {"text": texto, "usuario_actual": usuario, "session_id": session_id}

        usuario = detected_user or (req.current_user or None)

        # Si no hay usuario todavía, responder saludo/identificación sin invocar grafo
        if not usuario:
            texto = (
                "¡Hola! Soy tu asistente informativo sobre farmacias en Chile y sobre medicamentos del vademécum. "
                "Para poder recordar nuestras conversaciones, dime tu nombre o apodo. Ejemplos: 'Soy María' o 'Me llamo Juan'."
            )
            return {"text": texto, "usuario_actual": None, "session_id": "usuario_anon"}

        session_id = f"usuario_{usuario.lower()}"
        try:
            # Limitar historial para evitar prompts gigantes (p. ej., sesiones largas compartidas entre CLI y UI)
            hist_limit = 14
            try:
                hist_limit = int(os.getenv("UI_HISTORY_LIMIT", "14"))
            except Exception:
                hist_limit = 14

            history = RedisChatMessageHistory(session_id=session_id, url=REDIS_URL)
            prev = list(history.messages)
            if hist_limit > 0 and len(prev) > hist_limit:
                prev = prev[-hist_limit:]
            msgs_in = prev + [HumanMessage(content=msg)]

            result = graph.invoke({"messages": msgs_in})

            # Persistir manualmente (paridad con CLI/Streamlit):
            history.add_user_message(msg)
            out_messages = result.get("messages", [])
            last_ai = None
            for m in reversed(out_messages):
                if getattr(m, "type", "") == "ai":
                    last_ai = m
                    break
            if last_ai is None and out_messages:
                last_ai = out_messages[-1]
            ai_text = getattr(last_ai, "content", "") if last_ai else "(sin respuesta)"
            history.add_ai_message(ai_text)
            return {"text": ai_text or "", "usuario_actual": usuario, "session_id": session_id}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return app


app = create_app()


