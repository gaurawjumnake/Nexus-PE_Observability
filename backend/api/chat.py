"""
RAG Chat Router
===============
Chroma-backed document Q&A over uploaded Nexus documents.

Flow:
  1. Documents are uploaded through /documents/upload and saved as chunks.
  2. /chat/index/{document_id} pushes those chunks into ChromaDB.
  3. /chat/query retrieves relevant chunks and asks the configured LLM
     to answer only from the retrieved context.
"""

import os
from pathlib import Path
from typing import Any, Optional

import chromadb
from chromadb.api.types import EmbeddingFunction, Documents, Embeddings
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import backend.kpi_extractor.app.db.db_client as db
from backend.utilites.app_logger import Logger
from backend.utilites.llm_models import get_llm_client

load_dotenv()

log = Logger()
router = APIRouter(prefix="/chat", tags=["chat"])

CHROMA_PATH = os.getenv("CHROMA_PATH", "backend/kpi_extractor/app/db/chroma")
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "nexus_document_chunks")


class IndexDocumentRequest(BaseModel):
    company_id: str
    period: Optional[str] = None
    file_name: Optional[str] = None


class ChatQueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    company_id: str
    document_ids: Optional[list[str]] = None
    top_k: int = Field(default=8, ge=1, le=20)


class Citation(BaseModel):
    document_id: str
    chunk_id: str
    chunk_index: Optional[int] = None
    page_number: Optional[int] = None
    section_title: Optional[str] = None
    excerpt: str


class ChatQueryResponse(BaseModel):
    answer: str
    citations: list[Citation]


class GeminiEmbeddingFunction(EmbeddingFunction):
    def __init__(self):
        from google import genai

        log.log_info("Initializing Gemini embedding function")
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = os.getenv("NEXUS_GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
        log.log_info(f"Gemini embedding model: {self.model}")

    def __call__(self, input: Documents) -> Embeddings:
        log.log_info(f"Creating Gemini embeddings for {len(input)} text item(s)")
        embeddings = []
        for text in input:
            response = self.client.models.embed_content(
                model=self.model,
                contents=text,
            )
            values = response.embeddings[0].values
            embeddings.append(values)
        log.log_info(f"Created {len(embeddings)} Gemini embedding(s)")
        return embeddings


class AzureOpenAIEmbeddingFunction(EmbeddingFunction):
    def __init__(self):
        from openai import AzureOpenAI

        log.log_info("Initializing Azure OpenAI embedding function")
        self.client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        )
        self.deployment = os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]
        log.log_info(f"Azure OpenAI embedding deployment: {self.deployment}")

    def __call__(self, input: Documents) -> Embeddings:
        log.log_info(f"Creating Azure OpenAI embeddings for {len(input)} text item(s)")
        response = self.client.embeddings.create(
            model=self.deployment,
            input=list(input),
        )
        embeddings = [item.embedding for item in response.data]
        log.log_info(f"Created {len(embeddings)} Azure OpenAI embedding(s)")
        return embeddings


def get_embedding_function() -> EmbeddingFunction:
    provider = os.getenv("NEXUS_EMBEDDING_PROVIDER", os.getenv("NEXUS_LLM_PROVIDER", "gemini")).lower()
    log.log_info(f"Selected embedding provider: {provider}")

    if provider == "gemini":
        return GeminiEmbeddingFunction()
    if provider in {"azure", "azure_openai"}:
        return AzureOpenAIEmbeddingFunction()

    raise ValueError(f"Unsupported embedding provider: {provider}")


def get_collection():
    log.log_info(f"Opening Chroma collection '{CHROMA_COLLECTION}' at '{CHROMA_PATH}'")
    Path(CHROMA_PATH).mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = client.get_or_create_collection(
        name=CHROMA_COLLECTION,
        embedding_function=get_embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )
    log.log_info(f"Chroma collection ready: {CHROMA_COLLECTION}")
    return collection


def build_where(company_id: str, document_ids: Optional[list[str]] = None) -> dict[str, Any]:
    if document_ids:
        return {
            "$and": [
                {"company_id": company_id},
                {"document_id": {"$in": document_ids}},
            ]
        }
    return {"company_id": company_id}


@router.post("/index/{document_id}")
async def index_document(document_id: str, request: IndexDocumentRequest):
    log.log_info(
        f"Index request started for document_id={document_id}, "
        f"company_id={request.company_id}, period={request.period}"
    )
    try:
        chunks = db.get_chunks(document_id)
        log.log_info(f"Fetched {len(chunks)} chunk(s) from SQLite for document_id={document_id}")
        if not chunks:
            log.log_warning(f"No chunks found for document_id={document_id}")
            raise HTTPException(status_code=404, detail="Document chunks not found")

        collection = get_collection()
        ids = []
        documents = []
        metadatas = []

        for chunk in chunks:
            chunk_dict = dict(chunk)
            chunk_id = chunk_dict["chunk_id"]
            ids.append(chunk_id)
            documents.append(chunk_dict["chunk_text"])
            metadatas.append({
                "document_id": document_id,
                "company_id": request.company_id,
                "period": request.period or "",
                "file_name": request.file_name or "",
                "chunk_index": chunk_dict.get("chunk_index", 0),
                "page_number": chunk_dict.get("page_number") or 0,
                "section_title": chunk_dict.get("section_title") or "",
            })

        log.log_info(f"Upserting {len(ids)} chunk(s) into Chroma for document_id={document_id}")
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        log.log_info(f"Index request completed for document_id={document_id}")

        return {
            "document_id": document_id,
            "company_id": request.company_id,
            "chunks_indexed": len(ids),
            "vector_store": "chroma",
            "collection": CHROMA_COLLECTION,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.log_error(f"Index request failed for document_id={document_id}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Chat indexing failed: {e}")


@router.post("/query", response_model=ChatQueryResponse)
async def query_chat(request: ChatQueryRequest):
    log.log_info(
        f"Chat query started for company_id={request.company_id}, "
        f"document_ids={request.document_ids}, top_k={request.top_k}"
    )
    try:
        collection = get_collection()
        where_filter = build_where(request.company_id, request.document_ids)
        log.log_info(f"Chroma query filter: {where_filter}")
        results = collection.query(
            query_texts=[request.question],
            n_results=request.top_k,
            where=where_filter,
        )

        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        ids = results.get("ids", [[]])[0]
        distances = results.get("distances", [[]])[0]
        log.log_info(f"Chroma returned {len(documents)} result(s)")

        if ids:
            log.log_info(f"Top retrieved chunk ids: {ids[:5]}")
        if distances:
            log.log_info(f"Top retrieved distances: {distances[:5]}")

        if not documents:
            log.log_warning("No relevant Chroma results found for chat query")
            return ChatQueryResponse(
                answer="I could not find relevant information in the indexed documents.",
                citations=[],
            )

        context_blocks = []
        citations = []
        for idx, text in enumerate(documents):
            metadata = metadatas[idx] or {}
            chunk_id = ids[idx]
            label = f"[source {idx + 1} | chunk_id={chunk_id}]"
            context_blocks.append(f"{label}\n{text}")
            citations.append(Citation(
                document_id=str(metadata.get("document_id", "")),
                chunk_id=str(chunk_id),
                chunk_index=metadata.get("chunk_index"),
                page_number=metadata.get("page_number") or None,
                section_title=metadata.get("section_title") or None,
                excerpt=text[:700],
            ))

        system_prompt = """
You are Nexus RAG Chat, a document analysis assistant.
Answer the user using only the provided document excerpts.
Give a detailed, analytical answer when the excerpts support it.
If the excerpts do not contain enough information, say that clearly.
Do not invent facts, numbers, companies, dates, or conclusions.
Reference source numbers inline when making important claims.
""".strip()

        user_prompt = f"""
Question:
{request.question}

Document excerpts:
{chr(10).join(context_blocks)}
""".strip()

        log.log_info(
            f"Sending chat prompt to LLM with {len(context_blocks)} context block(s), "
            f"prompt_chars={len(user_prompt)}"
        )
        answer = get_llm_client().complete(
            user=user_prompt,
            system=system_prompt,
            temperature=0.0,
            max_tokens=2048,
        )
        log.log_info(f"LLM answer generated, answer_chars={len(answer or '')}")

        return ChatQueryResponse(answer=answer, citations=citations)
    except Exception as e:
        log.log_error(f"Chat query failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Chat query failed: {e}")
