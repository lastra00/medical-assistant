from fastapi import FastAPI, Query
from dotenv import load_dotenv
from langserve import add_routes
from langgraph.checkpoint.memory import MemorySaver
from typing import Optional, Dict, Any
import requests

# Cargar .env ANTES de importar el grafo (para QDRANT_URL/API_KEY, etc.)
load_dotenv()
from .graph import build_graph
from .config import MINSAL_GET_LOCALES, MINSAL_GET_TURNOS


def create_app() -> FastAPI:
    app = FastAPI(title="Med Agent API")
    graph = build_graph()
    # Exponer grafo como runnable
    add_routes(app, graph, path="/graph")
    # Compatibilidad: añadir /chat como alias
    add_routes(app, graph, path="/chat")

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
        r = requests.get(MINSAL_GET_LOCALES, params=params, timeout=25)
        r.raise_for_status()
        return r.json()

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
        r = requests.get(MINSAL_GET_TURNOS, params=params, timeout=25)
        r.raise_for_status()
        return r.json()
    return app


app = create_app()


