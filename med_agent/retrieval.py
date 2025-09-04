import os
from typing import List, Dict, Optional

import pandas as pd
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse, ResponseHandlingException

from .config import (
    DRUGS_CSV_PATH,
    QDRANT_URL,
    QDRANT_API_KEY,
    QDRANT_COLLECTION,
    EMBEDDINGS_MODEL,
    EMBEDDINGS_DIMENSIONS,
)


class QdrantDrugRetrieval:
    """Almacena DrugData.csv en Qdrant y realiza búsqueda semántica vía retriever."""

    def __init__(
        self,
        csv_path: str = DRUGS_CSV_PATH,
        collection_name: str = QDRANT_COLLECTION,
        qdrant_url: str = QDRANT_URL,
        qdrant_api_key: str = QDRANT_API_KEY,
        embeddings_model: str = EMBEDDINGS_MODEL,
        embeddings_dimensions: Optional[int] = EMBEDDINGS_DIMENSIONS,
    ):
        self.csv_path = csv_path
        self.collection_name = collection_name
        self.qdrant_url = qdrant_url
        self.qdrant_api_key = qdrant_api_key
        # Crear embeddings (permite dimensiones si el modelo lo soporta)
        if embeddings_dimensions is not None:
            self.embeddings = OpenAIEmbeddings(model=embeddings_model, dimensions=embeddings_dimensions)
        else:
            self.embeddings = OpenAIEmbeddings(model=embeddings_model)
        self.client = QdrantClient(url=self.qdrant_url, api_key=self.qdrant_api_key or None)
        self.vector_store: QdrantVectorStore | None = None

    def _row_to_text(self, row: pd.Series) -> str:
        fields = [
            "Drug ID",
            "Drug Name",
            "Generic Name",
            "Drug Class",
            "Indications",
            "Dosage Form",
            "Strength",
            "Route of Administration",
            "Mechanism of Action",
            "Side Effects",
            "Contraindications",
            "Interactions",
            "Warnings and Precautions",
            "Pregnancy Category",
        ]
        parts = []
        for f in fields:
            if f in row and pd.notna(row[f]):
                parts.append(f"{f}: {row[f]}")
        return "\n".join(parts)

    def build_or_load(self) -> None:
        # Si la colección no existe, la creamos indexando el CSV
        existing = [c.name for c in self.client.get_collections().collections]
        if self.collection_name in existing:
            self.vector_store = QdrantVectorStore(
                client=self.client,
                collection_name=self.collection_name,
                embedding=self.embeddings,
            )
            return

        # Crear colección indexando documentos desde el CSV
        df = pd.read_csv(self.csv_path)
        docs: List[Document] = []
        for _, row in df.iterrows():
            text = self._row_to_text(row)
            metadata = {
                "Drug ID": str(row.get("Drug ID", "")),
                "Drug Name": str(row.get("Drug Name", "")),
                # Indexar campos clave como payload para filtros/agrupaciones
                "Drug Class": str(row.get("Drug Class", "")),
                "Indications": str(row.get("Indications", "")),
                "Mechanism of Action": str(row.get("Mechanism of Action", "")),
                "Route of Administration": str(row.get("Route of Administration", "")),
                "Pregnancy Category": str(row.get("Pregnancy Category", "")),
            }
            docs.append(Document(page_content=text, metadata=metadata))

        self.vector_store = QdrantVectorStore.from_documents(
            documents=docs,
            embedding=self.embeddings,
            url=self.qdrant_url,
            api_key=(self.qdrant_api_key or None),
            collection_name=self.collection_name,
            force_recreate=True,
        )

    def search(self, query: str, k: int = 5) -> List[Dict]:
        if self.vector_store is None:
            self.build_or_load()
        assert self.vector_store is not None
        retriever = self.vector_store.as_retriever(search_type="mmr", search_kwargs={"k": k, "fetch_k": max(10, k*4)})
        try:
            docs = retriever.invoke(query)
        except UnexpectedResponse as e:
            # Colección borrada o no encontrada → recrear e intentar una vez
            if "doesn't exist" in str(e) or "Not found" in str(e):
                self.vector_store = None
                self.build_or_load()
                assert self.vector_store is not None
                retriever = self.vector_store.as_retriever(search_type="mmr", search_kwargs={"k": k, "fetch_k": max(10, k*4)})
                docs = retriever.invoke(query)
            else:
                raise
        except ResponseHandlingException:
            # Problema de transporte (p.ej. connection refused) → propagar vacío para manejar upstream
            return []
        out: List[Dict] = []
        for d in docs:
            out.append({
                "drug_name": d.metadata.get("Drug Name"),
                "drug_id": d.metadata.get("Drug ID"),
                "drug_class": d.metadata.get("Drug Class"),
                "indications": d.metadata.get("Indications"),
                "mechanism": d.metadata.get("Mechanism of Action"),
                "route": d.metadata.get("Route of Administration"),
                "pregnancy": d.metadata.get("Pregnancy Category"),
                "content": d.page_content,
            })
        return out

    def list_by_field(self, field_label: str, value_en: str, synonyms: Optional[List[str]] = None, k: int = 100) -> List[str]:
        """Devuelve nombres únicos cuyo payload de 'field_label' contenga value_en o sus sinónimos.
        Implementado como búsqueda vectorial guiada + filtrado en memoria sobre metadata.
        """
        if self.vector_store is None:
            self.build_or_load()
        assert self.vector_store is not None
        synonyms = synonyms or []
        query = f"{field_label}: {value_en}"
        retriever = self.vector_store.as_retriever(search_type="mmr", search_kwargs={"k": k, "fetch_k": max(20, k*4)})
        docs = retriever.invoke(query)
        targets = [value_en.strip().lower()]
        targets.extend([s.strip().lower() for s in synonyms if s.strip()])
        names: List[str] = []
        seen = set()
        for d in docs:
            meta_val = str(d.metadata.get(field_label, "")).lower()
            if any(t in meta_val for t in targets):
                n = str(d.metadata.get("Drug Name", "")).strip()
                if n and n not in seen:
                    seen.add(n)
                    names.append(n)
            if len(names) >= 25:
                break
        return names


