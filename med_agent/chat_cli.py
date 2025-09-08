#!/usr/bin/env python3
"""
CLI de chat conversacional para el agente médico con historial en Redis.

Uso:
  python -m final_proyect.med_agent.chat_cli
Comandos útiles:
  usuario [nombre]   → establece el usuario/sesión actual
  cambiar [nombre]   → alias de 'usuario'
  historial [nombre] → muestra historial guardado para ese usuario
  limpiar [nombre]   → limpia el historial de ese usuario
  estado             → muestra usuario actual
  salir              → termina
"""

import os
import sys
from typing import List

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage
from langchain_community.chat_message_histories import RedisChatMessageHistory
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from urllib.parse import urlparse
import redis


def main(argv: List[str] | None = None) -> int:
    # Cargar .env desde la raíz del repo antes de importar el grafo
    try:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
        dotenv_path = os.path.join(repo_root, ".env")
        if os.path.exists(dotenv_path):
            load_dotenv(dotenv_path)
        else:
            load_dotenv()
    except Exception:
        load_dotenv()
    # Garantizar OPENAI_API_KEY si está en .env con alias
    if not os.getenv("OPENAI_API_KEY"):
        alt = os.getenv("openai_api_key") or ""
        if alt:
            os.environ["OPENAI_API_KEY"] = alt

    # Importar el grafo después de cargar variables de entorno
    from .graph import build_graph
    graph = build_graph()

    # Tomar REDIS_URL desde .env (repo root) — imitar enfoque de chat_multi_usuario: respetar URL tal cual
    redis_url = os.getenv("REDIS_URL") or os.getenv("redis_url") or "redis://localhost:6379/0"
    # Sugerencia: si parece endpoint TLS pero esquema es redis://, mostrar advertencia (no forzar conversión)
    try:
        u = urlparse(redis_url)
        host = u.hostname or ""
        if u.scheme == "redis" and ("redis-cloud.com" in host or ".redns." in host):
            print("⚠️ Aviso: Este endpoint de Redis Cloud suele requerir TLS. Usa 'rediss://' y el puerto TLS que indica el panel.")
    except Exception:
        pass
    # Permitir desactivar verificación de certificado si hay problemas TLS (opcional, solo si rediss://)
    ssl_verify = (os.getenv("REDIS_SSL_VERIFY") or "true").strip().lower()
    if redis_url.startswith("rediss://") and ssl_verify in {"0", "no", "false"}:
        sep = "&" if "?" in redis_url else "?"
        if "ssl_cert_reqs=" not in redis_url:
            redis_url = f"{redis_url}{sep}ssl_cert_reqs=none"
    # Mostrar destino (oculta credenciales)
    try:
        safe_url = redis_url
        if "@" in safe_url:
            safe_url = ("rediss://" if safe_url.startswith("rediss://") else "redis://") + safe_url.split("@", 1)[1]
        print(f"🔗 Redis: {safe_url}")
    except Exception:
        pass

    # Preflight: comprobar conexión a Redis (evitar errores crípticos luego)
    try:
        test_client = redis.from_url(redis_url, decode_responses=True)
        test_client.ping()
    except Exception as e:
        print("❌ No se pudo conectar a Redis (preflight). Revisa REDIS_URL, esquema y puerto (TLS vs no TLS).")
        print(f"   Detalles: {e}")
        print("   Sugerencia: usa exactamente el endpoint del panel. Si es TLS, rediss:// y puerto TLS; si es sin TLS, redis:// y su puerto.")
        return 1

    # Detector de usuario (lenguaje natural → nombre)
    class DeteccionUsuario(BaseModel):
        usuario_identificado: bool = Field(description="True si el usuario se está identificando")
        nombre_usuario: str | None = Field(default=None, description="Nombre extraído")
        tipo_identificacion: str | None = Field(default=None, description="presentacion|referencia|ninguna")

    llm_detector = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(DeteccionUsuario)
    prompt_detector = ChatPromptTemplate.from_template(
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
        - "¿cómo estás?"
        - "ok"
        - "gracias"

        Mensaje: "{mensaje}"
        """
    )
    cadena_detector = prompt_detector | llm_detector

    current_user: str | None = None

    print("\n🩺 Chat del Agente Médico (memoria persistente en Redis)")
    print("Comandos: usuario [nombre] | cambiar [nombre] | historial [nombre] | limpiar [nombre] | estado | salir\n")
    while True:
        try:
            user = input("👤 Tú: ").strip()
            if not user:
                continue
            if user.lower() in {"salir", "exit", "quit"}:
                print("👋 Hasta luego")
                return 0

            # Comandos especiales
            if user.lower() == "estado":
                print(f"👥 Usuario actual: {current_user or 'Sin identificar'}")
                continue

            if user.lower().startswith("usuario ") or user.lower().startswith("cambiar "):
                try:
                    nombre = user.split(" ", 1)[1].strip()
                    current_user = nombre
                    print(f"🔄 Usuario cambiado a: {current_user}")
                    continue
                except Exception:
                    print("❌ Uso: usuario [nombre] | cambiar [nombre]")
                    continue

            if user.lower().startswith("historial "):
                try:
                    nombre = user.split(" ", 1)[1].strip()
                    session_id = f"usuario_{nombre.lower()}"
                    history = RedisChatMessageHistory(session_id=session_id, url=redis_url)
                    msgs = history.messages
                    if not msgs:
                        print(f"📋 Historial de {nombre}: (vacío)")
                    else:
                        print(f"\n📋 Historial de {nombre}:")
                        print("-" * 50)
                        for i, m in enumerate(msgs, 1):
                            if isinstance(m, HumanMessage):
                                print(f"{i}. 👤 {nombre}: {m.content}")
                            elif isinstance(m, AIMessage):
                                print(f"{i}. 🤖 Asistente: {m.content}")
                        print("-" * 50)
                    continue
                except Exception as e:
                    print(f"❌ Error leyendo historial: {e}")
                    continue

            if user.lower().startswith("limpiar "):
                try:
                    nombre = user.split(" ", 1)[1].strip()
                    session_id = f"usuario_{nombre.lower()}"
                    history = RedisChatMessageHistory(session_id=session_id, url=redis_url)
                    history.clear()
                    print(f"🗑️ Historial de {nombre} limpiado")
                    continue
                except Exception as e:
                    print(f"❌ Error limpiando historial: {e}")
                    continue

            # Intentar detección de usuario SIEMPRE (permite cambiar de usuario en lenguaje natural)
            try:
                det = cadena_detector.invoke({"mensaje": user})
                if det.usuario_identificado and det.nombre_usuario:
                    detected = det.nombre_usuario
                    if not current_user or detected.lower() != current_user.lower():
                        current_user = detected
                        print(f"🔄 Usuario identificado: {current_user}")
            except Exception:
                pass

            # Si aún no hay usuario actual, solicitar identificación
            if not current_user:
                print("🤖 Indícame tu nombre. Ejemplos: 'usuario Ana', 'cambiar Carlos' o di 'Soy [tu nombre]'.")
                continue

            # Invocar el grafo y persistir manualmente en Redis (evitar errores de handshake del wrapper)
            try:
                session_id = f"usuario_{current_user.lower()}"
                # 1) Recuperar historial previo desde Redis y construir contexto
                history = RedisChatMessageHistory(session_id=session_id, url=redis_url)
                msgs_in = list(history.messages)
                msgs_in.append(HumanMessage(content=user))
                # 2) Invocar grafo con historial completo
                result = graph.invoke({"messages": msgs_in})
                # 3) Añadir al historial lo enviado y lo recibido
                out_messages = result.get("messages", [])
                last_ai = None
                for m in reversed(out_messages):
                    if getattr(m, "type", "") == "ai":
                        last_ai = m
                        break
                if last_ai is None and out_messages:
                    last_ai = out_messages[-1]
                history.add_user_message(user)
                if last_ai:
                    history.add_ai_message(getattr(last_ai, "content", ""))
            except Exception as e:
                msg = str(e)
                if "Connection refused" in msg or "connecting to" in msg:
                    print("❌ No se pudo conectar a Redis. Configura REDIS_URL (por ejemplo, Redis Cloud) o levanta Redis local en redis://localhost:6379/0.")
                    print("   Ejemplo temporal: export REDIS_URL=redis://usuario:password@host:puerto")
                    print(f"   Detalles: {e}")
                else:
                    print(f"❌ Error: {e}")
                continue

            # Seleccionar el último mensaje de tipo AI (evitar System/Human)
            out_messages = result.get("messages", [])
            last_ai = None
            for m in reversed(out_messages):
                if getattr(m, "type", "") == "ai":
                    last_ai = m
                    break
            if last_ai is None:
                last_ai = out_messages[-1] if out_messages else None
            content = getattr(last_ai, "content", "") if last_ai else "(sin respuesta)"
            print(f"🤖 Agente: {content}\n")

            # No es necesario añadir manualmente al historial; ya fue persistido en Redis
        except KeyboardInterrupt:
            print("\n👋 Interrumpido. Hasta luego")
            return 0
        except EOFError:
            print("\n👋 Hasta luego")
            return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


