from fastapi import FastAPI
from dotenv import load_dotenv
from langserve import add_routes
from langgraph.checkpoint.memory import MemorySaver

# Cargar .env ANTES de importar el grafo (para QDRANT_URL/API_KEY, etc.)
load_dotenv()
from .graph import build_graph


def create_app() -> FastAPI:
    app = FastAPI(title="Med Agent API")
    graph = build_graph()
    # Exponer grafo como runnable
    add_routes(app, graph, path="/graph")
    # Compatibilidad: añadir /chat como alias
    add_routes(app, graph, path="/chat")
    return app


app = create_app()


