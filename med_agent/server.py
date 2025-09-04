from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from dotenv import load_dotenv
from langserve import add_routes
from langgraph.checkpoint.memory import MemorySaver
from typing import Optional, Dict, Any
import requests
import json
import time
from urllib.parse import urlencode, quote, urlsplit

# Cargar .env ANTES de importar el grafo (para QDRANT_URL/API_KEY, etc.)
load_dotenv()
from .graph import build_graph
from .config import MINSAL_GET_LOCALES, MINSAL_GET_TURNOS


def create_app() -> FastAPI:
    app = FastAPI(title="Med Agent API")
    graph = build_graph()
    # Exponer grafo como runnable
    add_routes(app, graph, path="/graph")
    # Playground de LangServe disponible en /chat/playground
    add_routes(app, graph, path="/chat")

    @app.get("/", response_class=HTMLResponse)
    def root() -> HTMLResponse:
        # Redirigir al playground de LangServe del chat
        return HTMLResponse("""
        <html>
          <head><meta http-equiv="refresh" content="0; url=/chat/playground/" /></head>
          <body>
            <a href="/chat/playground/">Ir al Playground</a>
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
    ) -> Dict[str, Any]:
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
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if comuna_nombre:
            params["comuna_nombre"] = comuna_nombre
        if fk_region:
            params["fk_region"] = fk_region
        try:
            return _proxy_try(MINSAL_GET_TURNOS, "https://farmanet.minsal.cl/index.php/ws/getLocalesTurnos", params)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"upstream error: {e}")
    return app


app = create_app()


