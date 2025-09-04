from typing import Dict, Any, Tuple, Optional, Literal, List
from datetime import datetime
import os
import unicodedata
import re
import json

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, MessagesState, START, END
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, Field

from .config import OPENAI_MODEL, OPENAI_API_KEY
from .tools import tool_minsal_locales, tool_minsal_turnos
from .retrieval import QdrantDrugRetrieval


def build_graph():
    # Asegurar OPENAI_API_KEY en entorno
    if OPENAI_API_KEY and not os.getenv("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0)
    # Lazy init del retriever para reducir memoria al inicio
    retriever_ref: Dict[str, Any] = {"obj": None}

    # ============================
    # Modelos Pydantic (agentes)
    # ============================

    class GuardrailsDecision(BaseModel):
        blocked: bool = Field(description="True si el mensaje solicita prescripción/dosis o una acción médica.")
        policy_message: Optional[str] = Field(default=None, description="Mensaje de política si está bloqueado.")

    class RouterDecision(BaseModel):
        route: Literal["farmacias", "turnos", "meds", "saludo"] = Field(description="Ruta a la que enviar el mensaje.")
        routes: Optional[List[Literal["farmacias", "turnos", "meds", "saludo"]]] = Field(default=None, description="Lista de rutas si hay múltiples intenciones.")
        comuna: Optional[str] = Field(default=None, description="Comuna extraída si aplica.")
        address_mode: Optional[bool] = Field(default=None, description="True si la consulta menciona dirección específica (número/avenida/calle/etc.).")
        # Posibles campos de filtro (si el usuario los mencionó explícitamente)
        localidad: Optional[str] = Field(default=None)
        direccion: Optional[str] = Field(default=None)
        fecha: Optional[str] = Field(default=None)
        funcionamiento_dia: Optional[str] = Field(default=None)
        fk_region: Optional[str] = Field(default=None)
        fk_comuna: Optional[str] = Field(default=None)
        fk_localidad: Optional[str] = Field(default=None)
        local_nombre: Optional[str] = Field(default=None)
        local_telefono: Optional[str] = Field(default=None)
        local_lat: Optional[float] = Field(default=None)
        local_lng: Optional[float] = Field(default=None)
        funcionamiento_hora_apertura: Optional[str] = Field(default=None)
        funcionamiento_hora_cierre: Optional[str] = Field(default=None)

    # ============================
    # Agente Guardrails (LLM → Pydantic)
    # ============================

    guardrails_llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0).with_structured_output(GuardrailsDecision)
    guardrails_prompt = ChatPromptTemplate.from_messages([
        ("system", (
            "Eres un agente de seguridad especializado en detectar solicitudes médicas. "
            "Si el usuario pide recomendaciones médicas, dosis, qué tomar, prescribir, dosificación, etc., "
            "bloquea la solicitud. Devuelve JSON estructurado. "
            "NO bloquees si la persona solo pide información general o factual sobre un fármaco (p. ej.: 'qué me puedes decir de paracetamol', 'información sobre ibuprofeno', 'efectos adversos de X', 'contraindicaciones de Y', 'mecanismo de acción de Z'). "
            "Bloquea únicamente cuando exista una solicitud de consejo/indicación terapéutica, dosis, frecuencia, qué tomar o uso personalizado. "
            "Cuando 'blocked' sea true, el campo 'policy_message' DEBE comenzar exactamente con: "
            "'Lo siento, pero no puedo ofrecer recomendaciones médicas.' "
            "Luego añade UNA frase breve (1–2 líneas) en español sugiriendo consultar a un profesional de la salud o revisar fuentes oficiales como MINSAL."
        )),
        ("human", "{input}")
    ])
    # Casteo a dict para evitar objetos Pydantic en logs de stream
    guardrails_chain = guardrails_prompt | guardrails_llm | RunnableLambda(lambda m: m.dict())

    # ============================
    # Agente Router (LLM → Pydantic)
    # ============================

    router_llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0).with_structured_output(RouterDecision)
    router_prompt = ChatPromptTemplate.from_messages([
        ("system", (
            "Eres un agente router.\n"
            "1) Clasifica el mensaje del usuario en una o varias rutas: 'farmacias', 'turnos', 'meds' o 'saludo'.\n"
            "2) Extrae campos de filtro SOLO si están explícitos en el texto (no inventes datos).\n"
            "   Campos soportados (si presentes):\n"
            "   - comuna, localidad, direccion, fecha, funcionamiento_dia,\n"
            "   - fk_region, fk_comuna, fk_localidad, local_nombre, local_telefono,\n"
            "   - local_lat, local_lng, funcionamiento_hora_apertura, funcionamiento_hora_cierre.\n"
            "3) Si el mensaje es un saludo o small talk (p.ej., 'hola', 'buenos días', 'cómo estás'), usa 'saludo' como ruta y no intentes extraer filtros.\n"
            "4) 'address_mode' = true si el usuario menciona una dirección concreta (número de calle o términos como avenida/calle/ohiggins).\n"
            "5) Si el usuario pregunta por MÁS DE UNA COSA (p.ej., farmacias y turnos), llena 'routes' con TODAS las rutas aplicables (y deja 'route' con la principal).\n"
            "6) Devuelve SIEMPRE un JSON estrictamente con las claves del esquema. Si un campo no aparece, déjalo null.\n"
        )),
        ("human", "{input}")
    ])
    # Casteo a dict para evitar objetos Pydantic en logs de stream
    router_chain = router_prompt | router_llm | RunnableLambda(lambda m: m.dict())

    # ============================
    # Intérprete LLM de intención (medicamentos)
    # ============================

    class MedsIntent(BaseModel):
        mode: Literal[
            "by_name",
            "list_by_class",
            "list_by_indications",
            "list_by_mechanism",
            "list_by_route",
            "list_by_pregnancy_category",
        ]
        target_es: Optional[str] = None

    meds_intent_llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0).with_structured_output(MedsIntent)
    meds_intent_prompt = ChatPromptTemplate.from_messages([
        ("system", (
            "Eres un intérprete de intención para consultas de medicamentos. "
            "Clasifica la consulta en: by_name | list_by_class | list_by_indications | list_by_mechanism | list_by_route | list_by_pregnancy_category. "
            "Si detectas frase tipo 'qué X existen' (p.ej., antibióticos), mapea a la dimensión correcta (clase=antibiotics, indicaciones, mecanismo, vía, categoría de embarazo). "
            "IMPORTANTE: si la consulta menciona un fármaco específico (p.ej., 'para qué sirve la morfina', 'efectos adversos de ibuprofeno', 'qué es el omeprazol', 'contraindicaciones de amoxicilina'), clasifica como 'by_name'. "
            "Usa listados (list_by_*) solo cuando el usuario pida una LISTA de medicamentos por clase/indicación/mecanismo/vía/categoría (p.ej., '¿qué analgésicos existen?', 'medicamentos para asma?'). "
            "Devuelve JSON con 'mode' y 'target_es' (texto objetivo en español si aplica)."
        )),
        ("human", "{input}")
    ])
    meds_intent_chain = meds_intent_prompt | meds_intent_llm | RunnableLambda(lambda m: m.dict())

    def _normalize(s: str) -> str:
        s = s.strip().lower()
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # quitar tildes
        s = re.sub(r"[^a-z0-9\s]", " ", s)  # quitar puntuación/apóstrofes
        s = re.sub(r"\s+", " ", s)
        return s

    def _extract_location(text: str) -> Tuple[str, str]:
        """Extrae comuna tras 'en ...'; ignora palabras como hoy/ahora y signos finales."""
        text_norm = _normalize(text)
        comuna = ""
        region = ""
        # patrones comunes: "en traiguen hoy", "en la comuna de traiguen", "farmacias de lebu"
        patterns = [
            r"\ben\s+(?:la\s+comuna\s+de\s+)?([a-zñ\s]+?)(?:\s+(?:hoy|ahora|ayer|manana|y|e|que|cuales?|me|puedes|puede|podrias|podria|dime|dame|por|favor|direccion|farmacia|farmacias|donde|cerca|cercanas?|una|un|la|el|los|las)|[\?\.,;!]|$)",
            r"\bcomuna\s+de\s+([a-zñ\s]+?)(?:\s+(?:hoy|ahora|ayer|manana|y|e|que|cuales?|me|puedes|puede|podrias|podria|dime|dame|por|favor|direccion|farmacia|farmacias|donde|cerca|cercanas?|una|un|la|el|los|las)|[\?\.,;!]|$)",
            r"\bfarmacias?\s+de\s+(?:la\s+comuna\s+de\s+)?([a-zñ\s]+?)(?:\s+(?:hoy|ahora|ayer|manana|y|e|que|cuales?|me|puedes|puede|podrias|podria|dime|dame|por|favor|direccion|farmacia|farmacias|donde|cerca|cercanas?|una|un|la|el|los|las)|[\?\.,;!]|$)",
        ]
        for pat in patterns:
            m = re.search(pat, text_norm)
            if m:
                comuna = m.group(1).strip()
                break
        return comuna, region

    # ============================
    # Utilidad: traducir token de fármaco ES→EN con LLM
    # ============================

    def _translate_drug_token(token: str) -> List[str]:
        token = (token or "").strip()
        if not token:
            return []
        sys = SystemMessage(content=(
            "Eres un traductor de nombres de FÁRMACOS y CLASES farmacológicas al inglés (US). "
            "Devuelve SOLO una lista separada por comas con hasta 3 alias en inglés (incluye el original si ya está en inglés). "
            "Ejemplos: 'paracetamol' -> paracetamol, acetaminophen | 'antibióticos' -> antibiotics, antibiotic, antibacterial."
        ))
        human = HumanMessage(content=token)
        try:
            resp = llm.invoke([sys, human])
            text = (resp.content or "").strip()
            if not text:
                return []
            items = [t.strip() for t in text.split(",") if t.strip()]
            norm_items: List[str] = []
            for it in items:
                norm = _normalize(it)
                if len(norm) > 3:
                    norm_items.append(norm)
            return norm_items
        except Exception:
            return []

    def router_node(state: MessagesState):
        # Agente Router (LLM)
        last_user = state["messages"][-1].content
        decision: Dict[str, Any] = router_chain.invoke({"input": last_user})
        out: Dict[str, Any] = {"route": decision.get("route")}
        if decision.get("routes"):
            out["routes"] = decision.get("routes")
        if decision.get("comuna"):
            out["comuna"] = decision.get("comuna")
        if decision.get("address_mode") is not None:
            out["address_mode"] = decision.get("address_mode")
        # Propagar posibles filtros adicionales
        for k in [
            "localidad","direccion","fecha","funcionamiento_dia","fk_region","fk_comuna","fk_localidad",
            "local_nombre","local_telefono","local_lat","local_lng","funcionamiento_hora_apertura","funcionamiento_hora_cierre",
        ]:
            v = decision.get(k)
            if v is not None:
                out[k] = v
        return out

    def guardrails_node(state: MessagesState):
        last_user = state["messages"][-1].content
        nz = _normalize(last_user)
        required = "Lo siento, pero no puedo ofrecer recomendaciones médicas."
        default_tail = "Te sugiero que consultes a un profesional de la salud o revises fuentes oficiales como MINSAL para obtener información precisa."
        default_policy = f"{required} {default_tail}"
        # Marcadores de consulta informativa segura (no deben bloquearse por sí solos)
        safe_markers = [
            "que me puedes", "me puedes decir", "informacion de", "informacion sobre",
            "que es ", "efectos adversos", "contraindicaciones", "mecanismo de accion", "indicaciones"
        ]
        # Si hay marcadores seguros y NO aparecen términos claramente clínicos, no bloquear
        clinical_markers = ["tomar", "dosis", "posologia", "posología", "cada cuanto"]
        if any(sm in nz for sm in safe_markers) and not any(cm in nz for cm in clinical_markers):
            return {"blocked": False}
        trigger_phrases = [
            "puedo tomar", "que puedo tomar", "qué puedo tomar", "me recomiendas", "que me recomiendas", "qué me recomiendas",
            "dosis", "posologia", "posología", "cada cuanto", "me hara bien", "me hace bien", "debo tomar", "deberia tomar",
        ]
        heuristic_block = any(tp in nz for tp in trigger_phrases) or ("tomar" in nz and any(w in nz for w in ["puedo","debo","deberia","recomiendas"]))

        if heuristic_block:
            # Pedimos al LLM el mensaje para mantener variación; si falla, usamos default
            try:
                decision_h: Dict[str, Any] = guardrails_chain.invoke({"input": last_user})
                pm = (decision_h.get("policy_message") or "").strip()
            except Exception:
                pm = ""
            if not pm or not pm.startswith(required):
                pm = default_policy
            return {
                "blocked": True,
                "policy_message": pm,
            }

        decision: Dict[str, Any] = guardrails_chain.invoke({"input": last_user})
        if decision.get("blocked"):
            pm = (decision.get("policy_message") or "").strip()
            if not pm or not pm.startswith(required):
                pm = default_policy
            return {
                "blocked": True,
                "policy_message": pm,
            }
        return {"blocked": False}

    def nodo_saludo(state: MessagesState):
        intro = (
            "¡Hola! Soy tu asistente informativo sobre farmacias en Chile y sobre medicamentos del vademécum. "
            "Estoy muy bien, gracias por preguntar. ¿Te gustaría que te ayude a encontrar farmacias (abiertas o de turno) "
            "o prefieres información factual sobre un medicamento?"
        )
        return {
            "small_talk": True,
            "small_talk_text": intro,
        }

    def nodo_farmacias(state: MessagesState):
        last = state["messages"][-1].content
        comuna_router = state.get("comuna")
        addr_mode_router = state.get("address_mode")
        comuna, region = _extract_location(last)
        if comuna_router:
            comuna = comuna_router
        # Obtener datos (tolerante a fallos de filtro en servidor)
        data = tool_minsal_locales(comuna=None, region=None)
        if isinstance(data, dict) and "data" in data:
            rows = data["data"]
        else:
            rows = data if isinstance(data, list) else []
        if comuna:
            comuna_norm = _normalize(comuna)
            exact = [r for r in rows if _normalize(str(r.get("comuna_nombre", ""))) == comuna_norm]
            if exact:
                rows = exact
            else:
                # fallback: coincidencia parcial contiene la palabra
                rows = [r for r in rows if comuna_norm in _normalize(str(r.get("comuna_nombre", "")))]
        # Filtros adicionales desde el router
        localidad_router = state.get("localidad")
        if localidad_router:
            loc_norm = _normalize(localidad_router)
            rows = [r for r in rows if loc_norm in _normalize(str(r.get("localidad_nombre", "")))]
        local_nombre_router = state.get("local_nombre")
        if local_nombre_router:
            name_norm = _normalize(local_nombre_router)
            rows = [r for r in rows if name_norm in _normalize(str(r.get("local_nombre", "")))]
        # Nota: no aplicamos filtros de fecha/día en listado general de farmacias,
        # para evitar vaciar resultados por términos relativos como 'hoy'.
        for fk_key in ["fk_region","fk_comuna","fk_localidad"]:
            fk_val = state.get(fk_key)
            if fk_val is not None:
                rows = [r for r in rows if str(r.get(fk_key)) == str(fk_val)]
        tel_router = state.get("local_telefono")
        if tel_router:
            digits = re.sub(r"\D", "", str(tel_router))
            rows = [r for r in rows if digits in re.sub(r"\D", "", str(r.get("local_telefono","")))]
        for hour_key in ["funcionamiento_hora_apertura","funcionamiento_hora_cierre"]:
            h = state.get(hour_key)
            if h:
                rows = [r for r in rows if str(r.get(hour_key)) == str(h)]
        # Filtrado adicional por dirección si la consulta parece contener una dirección
        q_norm = _normalize(last)
        has_number = bool(re.search(r"\b\d{1,6}\b", q_norm)) or bool(addr_mode_router)
        addr_kws = {"libertador", "bernardo", "higgins", "ohiggins", "avenida", "av", "calle", "numero", "nro", "direccion"}
        has_addr_kw = any(kw in q_norm for kw in addr_kws)
        direccion_router = state.get("direccion")
        if has_number or has_addr_kw or direccion_router:
            m = re.search(r"\ben\s+(.+)", q_norm)
            addr_segment = direccion_router if direccion_router else (m.group(1).strip() if m else q_norm)
            stop = {"que", "farmacia", "hay", "de", "en", "hoy", "se", "llama", "la", "el", "cual", "queda", "donde", "ubicada", "es"}
            tokens = [t for t in addr_segment.split() if (t.isdigit() or t in addr_kws or (t not in stop and len(t) > 3))]
            def match_addr(r: Dict[str, Any]) -> bool:
                d = _normalize(str(r.get("local_direccion", "")))
                return all(tok in d for tok in tokens) if tokens else True
            filtered = [r for r in rows if match_addr(r)]
            if filtered:
                rows = filtered

        # Fallback: si no hay locales listados en el endpoint general para la comuna,
        # intentamos con el endpoint de turnos y aplicamos el mismo filtro de comuna.
        fallback_from_turnos = False
        if comuna and not rows:
            data_t = tool_minsal_turnos(comuna=None, region=None)
            if isinstance(data_t, dict) and "data" in data_t:
                rows_t = data_t["data"]
            else:
                rows_t = data_t if isinstance(data_t, list) else []
            comuna_norm = _normalize(comuna)
            rows_t = [r for r in rows_t if _normalize(str(r.get("comuna_nombre", ""))) == comuna_norm]
            if rows_t:
                rows = rows_t
                fallback_from_turnos = True
        preview = json.dumps(rows[:50])[:4000]
        return {
            "messages": [HumanMessage(content=f"RESULTADOS_FARMACIAS: {preview}")],
            "farmacias_rows": rows[:50],
            "farmacias_fallback_turnos": fallback_from_turnos,
        }\

    def nodo_turnos(state: MessagesState):
        last = state["messages"][-1].content
        comuna, region = _extract_location(last)
        # Filtros del router
        comuna_router = state.get("comuna")
        if comuna_router:
            comuna = comuna_router
        # Obtener datos (tolerante a fallos de filtro en servidor)
        data = tool_minsal_turnos(comuna=None, region=None)
        if isinstance(data, dict) and "data" in data:
            rows = data["data"]
        else:
            rows = data if isinstance(data, list) else []
        if comuna:
            comuna_norm = _normalize(comuna)
            exact = [r for r in rows if _normalize(str(r.get("comuna_nombre", ""))) == comuna_norm]
            if exact:
                rows = exact
            else:
                rows = [r for r in rows if comuna_norm in _normalize(str(r.get("comuna_nombre", "")))]
        localidad_router = state.get("localidad")
        if localidad_router:
            loc_norm = _normalize(localidad_router)
            rows = [r for r in rows if loc_norm in _normalize(str(r.get("localidad_nombre", "")))]
        fecha_router = state.get("fecha")
        # Si la fecha es 'hoy'/'ahora', no filtramos por fecha exacta (formatos MINSAL varían).
        if fecha_router and _normalize(str(fecha_router)) not in {"hoy", "ahora"}:
            fecha_norm = _normalize(str(fecha_router))
            rows = [r for r in rows if fecha_norm == _normalize(str(r.get("fecha", "")))]
        dia_router = state.get("funcionamiento_dia")
        if dia_router:
            dia_norm_in = _normalize(str(dia_router))
            if dia_norm_in == "hoy" or dia_norm_in == "ahora":
                # Convertir al nombre del día en español
                dias = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
                dia_norm = dias[datetime.now().weekday()]
            else:
                dia_norm = dia_norm_in
            rows = [r for r in rows if dia_norm == _normalize(str(r.get("funcionamiento_dia", "")))]
        for fk_key in ["fk_region","fk_comuna","fk_localidad"]:
            fk_val = state.get(fk_key)
            if fk_val is not None:
                rows = [r for r in rows if str(r.get(fk_key)) == str(fk_val)]
        preview = json.dumps(rows[:50])[:4000]
        return {
            "messages": [HumanMessage(content=f"RESULTADOS_TURNOS: {preview}")],
            "turnos_rows": rows[:50],
        }\

    def nodo_meds(state: MessagesState):
        query = state["messages"][-1].content
        
        # 1) Interpretar intención (LLM)
        try:
            intent = meds_intent_chain.invoke({"input": query})
        except Exception:
            intent = {"mode": "by_name", "target_es": None}

        mode = intent.get("mode", "by_name")
        target_es = intent.get("target_es")

        # Salvaguarda: si la pregunta parece referirse a un FÁRMACO específico (no a una lista), forzar by_name
        q_norm_for_mode = _normalize(query)
        by_name_clues = [
            "para que sirve", "que es ", "qué es ", "informacion sobre", "información sobre",
            "efectos adversos de", "contraindicaciones de", "mecanismo de accion de", "mecanismo de acción de"
        ]
        if any(cl in q_norm_for_mode for cl in by_name_clues):
            mode = "by_name"

        # 2) Listados por campo usando payload en Qdrant
        field_map = {
            "list_by_class": "Drug Class",
            "list_by_indications": "Indications",
            "list_by_mechanism": "Mechanism of Action",
            "list_by_route": "Route of Administration",
            "list_by_pregnancy_category": "Pregnancy Category",
        }
        if mode in field_map:
            pivot = target_es or query
            # Evitar usar texto largo tipo explicación como pivot: si supera 8 palabras, usar el último token significativo
            pivot_norm = _normalize(pivot)
            pivot_tokens = [t for t in pivot_norm.split() if len(t) > 3]
            if len(pivot_tokens) > 8:
                pivot = pivot_tokens[-1]
            aliases = _translate_drug_token(pivot)
            # singularizaciones simples para mejorar recall
            def _sing(w: str) -> str:
                if w.endswith("ies") and len(w) > 3:
                    return w[:-3] + "y"
                if w.endswith("es") and len(w) > 2:
                    return w[:-2]
                if w.endswith("s") and len(w) > 1:
                    return w[:-1]
                return w
            variants = list({*aliases, *[_sing(a) for a in aliases]})
            try:
                if retriever_ref["obj"] is None:
                    retriever_ref["obj"] = QdrantDrugRetrieval()
                names = retriever_ref["obj"].list_by_field(field_map[mode], variants[0] if variants else pivot, synonyms=variants[1:])
            except Exception:
                names = []
            meds_not_found = len(names) == 0
            preview = json.dumps({"field": field_map[mode], "target": pivot, "names": names})[:4000]
            return {
                "messages": [HumanMessage(content=f"RESULTADOS_MEDICAMENTOS: {preview}")],
                "meds_results": [],
                "meds_not_found": meds_not_found,
                "meds_query": query,
                "meds_list_mode": True,
                "meds_class": pivot if mode == "list_by_class" else None,
                "meds_list_names": names,
            }

        # 3) Modo por nombre (defecto)
        # Si el usuario escribió el fármaco en español, traducimos token objetivo a aliases EN para ampliar recall
        if retriever_ref["obj"] is None:
            retriever_ref["obj"] = QdrantDrugRetrieval()
        hits = retriever_ref["obj"].search(query, k=12)
        # Filtrado enfocado en el fármaco mencionado (tolerante ES→EN vía LLM)
        q_norm = _normalize(query)
        stopwords = {
            "para","que","sirve","de","la","del","los","las","un","una","unos","unas","el","al","en","por","con","y","o","u",
            "me","dime","dame","podrias","podria","puedes","puede","porfa","favor","como","cual","cuales","qué","cuál","cuáles",
            "informacion","sobre","uso","utilidad","tengo","necesito","quiero","es","hay"
        }
        q_tokens_all = q_norm.split()
        q_tokens = [t for t in q_tokens_all if len(t) > 3 and t not in stopwords]

        # Elegir el token candidato (más largo no stopword)
        target_token: Optional[str] = max(q_tokens, key=len) if q_tokens else None

        def _hit_matches_any(hit: Dict[str, Any], toks: List[str]) -> bool:
            name_norm = _normalize(str(hit.get("drug_name", "")))
            content_norm = _normalize(str(hit.get("content", "")))
            return any(tok in name_norm or tok in content_norm for tok in toks)

        meds_not_found = False
        tokens_to_match: List[str] = []
        if target_token:
            tokens_to_match.append(target_token)
            en_aliases = _translate_drug_token(target_token)
            tokens_to_match.extend(en_aliases)

        if tokens_to_match:
            filtered = [h for h in hits if _hit_matches_any(h, tokens_to_match)]
            if not filtered and len(tokens_to_match) > 1:
                # Reintento: consultar explícitamente por el primer alias EN en Qdrant
                alias_query = tokens_to_match[1]
                hits_alias = retriever_ref["obj"].search(alias_query, k=5)
                filtered = [h for h in hits_alias if _hit_matches_any(h, tokens_to_match[1:])]
            hits = filtered
            if not hits:
                meds_not_found = True
        else:
            # Intento adicional: si la consulta contiene 'para que sirve X', usar X directamente como query
            m = re.search(r"para\s+que\s+sirve\s+([a-zñ\s]+)", q_norm)
            if m:
                direct = m.group(1).strip()
                hits_direct = retriever_ref["obj"].search(direct, k=8)
                if hits_direct:
                    hits = hits_direct
                else:
                    hits = []
                    meds_not_found = True
            else:
                hits = []
                meds_not_found = True

        # Sin fallback a clase aquí (lo maneja el intérprete)
        preview = json.dumps({"results": hits})[:4000]
        # Guardamos resultados y flag para formateo final
        return {
            "messages": [HumanMessage(content=f"RESULTADOS_MEDICAMENTOS: {preview}")],
            "meds_results": hits,
            "meds_not_found": meds_not_found,
            "meds_query": query,
        }

    def format_final(state: MessagesState):
        # LLM resume respuesta factual y recuerda política. Instrucciones claras para no mezclar listados.
        # Si guardrails bloqueó, devolvemos directamente el mensaje de política (sin invocar al LLM de síntesis)
        if state.get("blocked"):
            pm = state.get("policy_message") or (
                "Lo siento, pero no puedo ofrecer recomendaciones médicas. "
                "Te sugiero que consultes a un profesional de la salud o revises fuentes oficiales como MINSAL para obtener información precisa."
            )
            return {"messages": [AIMessage(content=pm)]}

        farmacias_rows = state.get("farmacias_rows", [])
        turnos_rows = state.get("turnos_rows", [])
        meds_results = state.get("meds_results", [])
        farmacias_fallback_turnos = bool(state.get("farmacias_fallback_turnos", False))
        meds_not_found = bool(state.get("meds_not_found", False))
        meds_query = state.get("meds_query", "")
        # Respuesta directa para saludos / small talk
        if state.get("small_talk"):
            text = state.get("small_talk_text", "Hola, ¿en qué puedo ayudarte con farmacias o medicamentos?")
            return {"messages": [AIMessage(content=text)]}
        sys = SystemMessage(content=(
            "Eres un asistente informativo. No das recomendaciones médicas.\n"
            "Si hay resultados de farmacias y de turnos, separa en dos secciones: 'Farmacias disponibles' y 'Farmacias de turno hoy'.\n"
            "En cada ítem, muestra nombre, dirección y horario. Cita la fuente: MINSAL.\n"
            "Si hay resultados de medicamentos, agrega una sección 'Información de medicamentos' (vademécum).\n"
            "En esa sección, antes de los bullets, incluye una descripción breve (máximo 2 líneas) de cada medicamento, clara y factual, sin dosis.\n"
            "Para cada medicamento, luego sintetiza en bullets: Nombre, Indicación(es), Mecanismo (si aplica), Contraindicaciones, Interacciones y Advertencias.\n"
            "No des dosis ni recomendaciones terapéuticas; solo información factual del vademécum local.\n"
            "No mezcles listados: no presentes farmacias generales como si fueran de turno. Si un listado está vacío, omítelo.\n"
            "Si 'farmacias_fallback_turnos' es true y no existe sección de turnos, aclara que estás mostrando farmacias de turno porque el listado general no devolvió resultados para la comuna."
            "Si no hay resultados en 'meds' pero 'meds_not_found' es true, indica explícitamente que no hay información del medicamento consultado en el vademécum local (menciona el nombre si se infiere del texto) y no inventes.\n"
            "Si 'meds_list_mode' es true y recibes 'meds_class' y 'meds_list_names', en vez de fichas individuales entrega una lista clara de nombres pertenecientes a esa clase (bullets o separados por comas), indicando la clase (p.ej., \"Antibiotic: ...\").\n"
            "Utiliza un tono amable y profesional. Al final, añade: 'Ante una emergencia, acude a un hospital.' Nunca devuelvas solo ese recordatorio; la respuesta principal debe ir antes."
        ))
        structured = HumanMessage(content=json.dumps({
            "farmacias": farmacias_rows,
            "turnos": turnos_rows,
            "meds": meds_results,
            "flags": {
                "farmacias_fallback_turnos": farmacias_fallback_turnos,
                "meds_not_found": meds_not_found,
                "meds_query": meds_query,
                "meds_list_mode": bool(state.get("meds_list_mode", False)),
                "meds_class": state.get("meds_class"),
                "meds_list_names": state.get("meds_list_names", []),
            }
        })[:4000])
        messages = [sys, structured] + state["messages"]
        resp = llm.invoke(messages)
        return {"messages": [resp]}

    builder = StateGraph(MessagesState)
    builder.add_node("guardrails", guardrails_node)
    builder.add_node("router", router_node)
    builder.add_node("nodo_saludo", nodo_saludo)
    builder.add_node("nodo_farmacias", nodo_farmacias)
    builder.add_node("nodo_turnos", nodo_turnos)
    builder.add_node("nodo_meds", nodo_meds)
    builder.add_node("format", format_final)

    builder.add_edge(START, "guardrails")
    # Guardrails → condicional: bloqueado va directo a format, si no a router
    builder.add_conditional_edges(
        "guardrails",
        lambda s: "blocked" if s.get("blocked") else "ok",
        {"blocked": "format", "ok": "router"},
    )
    # Router → condicional a cada nodo de tool
    def _router_fanout(state: Dict[str, Any]):
        # Soportar múltiples intenciones: si 'routes' está presente, retornamos la lista.
        routes = state.get("routes")
        if routes and isinstance(routes, list) and len(routes) > 0:
            return routes
        return [state.get("route", "meds")]

    # Ejecutar los nodos correspondientes en secuencia: farmacias → turnos → meds (según lo que venga)
    builder.add_conditional_edges(
        "router",
        _router_fanout,
        {"saludo": "nodo_saludo", "farmacias": "nodo_farmacias", "turnos": "nodo_turnos", "meds": "nodo_meds"},
    )
    builder.add_edge("nodo_saludo", "format")
    builder.add_edge("nodo_farmacias", "format")
    builder.add_edge("nodo_turnos", "format")
    builder.add_edge("nodo_meds", "format")
    builder.add_edge("format", END)

    return builder.compile()


