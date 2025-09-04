import os


OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# Acepta alias en minúsculas desde .env
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("openai_api_key") or ""

# Embeddings (forzados por código)
EMBEDDINGS_MODEL = "text-embedding-3-large"
EMBEDDINGS_DIMENSIONS = 256

# CSV local
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DRUGS_CSV_PATH = os.getenv(
    "DRUGS_CSV_PATH",
    os.path.join(BASE_DIR, "drug_dataset", "DrugData.csv"),
)

# Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# MINSAL endpoints
MINSAL_GET_LOCALES = os.getenv(
    "MINSAL_GET_LOCALES",
    "https://midas.minsal.cl/farmacia_v2/WS/getLocales.php",
)
MINSAL_GET_TURNOS = os.getenv(
    "MINSAL_GET_TURNOS",
    "https://midas.minsal.cl/farmacia_v2/WS/getLocalesTurnos.php",
)

# Proxy opcional para MINSAL (Fly/Cloudflare/etc.)
MINSAL_PROXY_URL = os.getenv("MINSAL_PROXY_URL", "")

# Índice local
INDEX_DIR = os.getenv(
    "INDEX_DIR",
    os.path.join(BASE_DIR, "med_agent_index"),
)

# Qdrant (vector DB para medicamentos)
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
QDRANT_COLLECTION = "med_agent_drugs"

# Métricas
METRICS_LOG_PATH = os.getenv(
    "METRICS_LOG_PATH",
    os.path.join(BASE_DIR, "metrics.jsonl"),
)


