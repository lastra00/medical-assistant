import os
import sys
import streamlit as st
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage
from langchain_community.chat_message_histories import RedisChatMessageHistory

# Asegurar que el repo root esté en sys.path para import absoluto en Streamlit Cloud
try:
    REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
except Exception:
    pass

from med_agent.graph import build_graph


def get_env():
    # 1) Intentar secrets de Streamlit
    try:
        if "OPENAI_API_KEY" in st.secrets:
            os.environ["OPENAI_API_KEY"] = st.secrets["OPENAI_API_KEY"]
        if "openai_api_key" in st.secrets and not os.getenv("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = st.secrets["openai_api_key"]
        if "REDIS_URL" in st.secrets:
            os.environ["REDIS_URL"] = st.secrets["REDIS_URL"]
        if "redis_url" in st.secrets and not os.getenv("REDIS_URL"):
            os.environ["REDIS_URL"] = st.secrets["redis_url"]
    except Exception:
        pass

    # 2) Cargar .env desde raíz del repo (intentar 1 nivel y 2 niveles)
    loaded = False
    for up in (1, 2):
        try:
            base = os.path.dirname(__file__)
            for _ in range(up):
                base = os.path.abspath(os.path.join(base, os.pardir))
            dotenv_path = os.path.join(base, ".env")
            if os.path.exists(dotenv_path):
                load_dotenv(dotenv_path)
                loaded = True
                break
        except Exception:
            continue
    if not loaded:
        load_dotenv()

    # alias openai_api_key desde .env si procede
    if not os.getenv("OPENAI_API_KEY"):
        alt = os.getenv("openai_api_key") or ""
        if alt:
            os.environ["OPENAI_API_KEY"] = alt

    return {
        "REDIS_URL": os.getenv("REDIS_URL") or os.getenv("redis_url") or "redis://localhost:6379/0",
    }


@st.cache_resource
def get_graph_cached():
    return build_graph()


def main():
    st.set_page_config(page_title="Agente Médico", page_icon="🩺", layout="centered")
    st.title("🩺 Agente Médico + Farmacias (con memoria en Redis)")
    st.caption("Identifícate diciendo: ‘soy [tu nombre]’ o ‘hola, acá [tu nombre]’ ")

    env = get_env()
    REDIS_URL = env["REDIS_URL"]
    # Lazy build: solo cuando se envía el primer mensaje
    if "graph" not in st.session_state:
        st.session_state.graph = None

    if "usuario_actual" not in st.session_state:
        st.session_state.usuario_actual = None

    # Sidebar
    with st.sidebar:
        st.subheader("Sesión")
        usuario = st.text_input("Usuario actual", value=st.session_state.usuario_actual or "", placeholder="Ej: Ana")
        col1, col2 = st.columns(2)
        if col1.button("Cambiar"):
            st.session_state.usuario_actual = usuario.strip() or None
        if col2.button("Limpiar historial") and usuario.strip():
            sid = f"usuario_{usuario.strip().lower()}"
            try:
                RedisChatMessageHistory(session_id=sid, url=REDIS_URL).clear()
                st.success(f"Historial de {usuario} limpiado")
            except Exception as e:
                st.error(f"No se pudo limpiar: {e}")

        st.divider()
        st.caption(f"Redis: {REDIS_URL}")
        # Salud rápida
        has_openai = bool(os.getenv("OPENAI_API_KEY"))
        st.caption(f"OPENAI_API_KEY: {'OK' if has_openai else 'FALTA'}")

    # Chat UI
    if "chat_log" not in st.session_state:
        st.session_state.chat_log = []

    for role, content in st.session_state.chat_log:
        with st.chat_message("assistant" if role == "ai" else "user"):
            st.markdown(content)

    prompt = st.chat_input("Escribe tu mensaje…")
    if not prompt:
        return

    # Detección de usuario automática
    # Reutilizamos la ruta natural desde el grafo (guardrails/router) y guardamos en Redis por usuario
    # 1) Si no hay usuario actual, intentar detectar con frase "soy X"/"acá X" a nivel simple
    #    Para mantener ligero, haremos un regex sencillo y si falla dejamos que el agente pida identificación
    if st.session_state.usuario_actual is None:
        import re
        m = re.search(r"\bsoy\s+([a-zA-ZÁÉÍÓÚáéíóúñÑ]+)", prompt)
        if not m:
            m = re.search(r"\bac[aá]s?\s+([a-zA-ZÁÉÍÓÚáéíóúñÑ]+)", prompt)
        if m:
            st.session_state.usuario_actual = m.group(1)

    if st.session_state.usuario_actual is None:
        st.session_state.chat_log.append(("ai", "Para recordar tus conversaciones, dime tu nombre. Ej: ‘Soy María’."))
        with st.chat_message("assistant"):
            st.markdown("Para recordar tus conversaciones, dime tu nombre. Ej: ‘Soy María’.")
        return

    # Persistencia en Redis manual (idéntico a CLI actualizado)
    try:
        # Construir el grafo si aún no está
        if st.session_state.graph is None:
            with st.spinner("Inicializando modelo..."):
                st.session_state.graph = get_graph_cached()
        sid = f"usuario_{st.session_state.usuario_actual.lower()}"
        history = RedisChatMessageHistory(session_id=sid, url=REDIS_URL)
        msgs_in = list(history.messages)
        msgs_in.append(HumanMessage(content=prompt))
        result = st.session_state.graph.invoke({"messages": msgs_in})
        out_messages = result.get("messages", [])
        last_ai = None
        for m in reversed(out_messages):
            if getattr(m, "type", "") == "ai":
                last_ai = m
                break
        if last_ai is None and out_messages:
            last_ai = out_messages[-1]
        history.add_user_message(prompt)
        ai_text = getattr(last_ai, "content", "") if last_ai else "(sin respuesta)"
        history.add_ai_message(ai_text)
    except Exception as e:
        ai_text = f"No se pudo procesar: {e}"

    # Mostrar en la UI
    st.session_state.chat_log.append(("user", prompt))
    st.session_state.chat_log.append(("ai", ai_text))
    with st.chat_message("assistant"):
        st.markdown(ai_text)


if __name__ == "__main__":
    main()


