"""
CrewAI RAG chat router.

Indexes persisted document chunks into Chroma and answers document questions
using a CrewAI analyst agent grounded only in retrieved chunks.
"""

import os
from pathlib import Path
from typing import Any, Optional

import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import backend.kpi_extractor.app.db.db_client as db
from backend.utilites.app_logger import Logger

load_dotenv()

log = Logger()
router = APIRouter(prefix="/chat", tags=["chat"])

CHROMA_PATH = os.getenv("CHROMA_PATH", "backend/kpi_extractor/app/db/chroma")
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "nexus_document_chunks")
DEFAULT_RAG_MODEL = os.getenv("NEXUS_RAG_MODEL", os.getenv("GEMINI_MODEL", "gemini/gemini-2.5-flash"))


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


class RetrievedContext(BaseModel):
    context_blocks: list[str]
    citations: list[Citation]
    retrieved_count: int


class ChatQueryResponse(BaseModel):
    answer: str
    citations: list[Citation]


class GeminiEmbeddingFunction(EmbeddingFunction):
    def __init__(self) -> None:
        from google import genai

        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY for Gemini embeddings")

        self.client = genai.Client(api_key=api_key)
        self.model = os.getenv("NEXUS_GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")

    def __call__(self, input: Documents) -> Embeddings:
        embeddings = []
        for text in input:
            response = self.client.models.embed_content(
                model=self.model,
                contents=text,
            )
            embeddings.append(response.embeddings[0].values)
        return embeddings


class AzureOpenAIEmbeddingFunction(EmbeddingFunction):
    def __init__(self) -> None:
        from openai import AzureOpenAI

        self.client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        )
        self.deployment = os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]

    def __call__(self, input: Documents) -> Embeddings:
        response = self.client.embeddings.create(
            model=self.deployment,
            input=list(input),
        )
        return [item.embedding for item in response.data]


def get_embedding_function() -> EmbeddingFunction:
    provider = os.getenv(
        "NEXUS_EMBEDDING_PROVIDER",
        os.getenv("NEXUS_LLM_PROVIDER", "gemini"),
    ).lower()

    if provider == "gemini":
        return GeminiEmbeddingFunction()
    if provider in {"azure", "azure_openai"}:
        return AzureOpenAIEmbeddingFunction()

    raise ValueError(f"Unsupported embedding provider: {provider}")


def get_crewai_llm():
    from crewai import LLM

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY for CrewAI Gemini LLM")

    return LLM(model=DEFAULT_RAG_MODEL, api_key=api_key, temperature=0)


def get_collection():
    Path(CHROMA_PATH).mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    return client.get_or_create_collection(
        name=CHROMA_COLLECTION,
        embedding_function=get_embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )


def build_where(company_id: str, document_ids: Optional[list[str]] = None) -> dict[str, Any]:
    if document_ids:
        return {
            "$and": [
                {"company_id": company_id},
                {"document_id": {"$in": document_ids}},
            ]
        }
    return {"company_id": company_id}


def retrieve_context(request: ChatQueryRequest) -> RetrievedContext:
    collection = get_collection()
    results = collection.query(
        query_texts=[request.question],
        n_results=request.top_k,
        where=build_where(request.company_id, request.document_ids),
    )

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    ids = results.get("ids", [[]])[0]

    context_blocks: list[str] = []
    citations: list[Citation] = []
    for idx, text in enumerate(documents):
        metadata = metadatas[idx] or {}
        chunk_id = str(ids[idx])
        source_label = f"[source {idx + 1} | chunk_id={chunk_id}]"
        context_blocks.append(f"{source_label}\n{text}")
        citations.append(
            Citation(
                document_id=str(metadata.get("document_id", "")),
                chunk_id=chunk_id,
                chunk_index=metadata.get("chunk_index"),
                page_number=metadata.get("page_number") or None,
                section_title=metadata.get("section_title") or None,
                excerpt=text[:700],
            )
        )

    return RetrievedContext(
        context_blocks=context_blocks,
        citations=citations,
        retrieved_count=len(context_blocks),
    )


def answer_with_rag(question: str, context: RetrievedContext) -> str:
    if not context.context_blocks:
        return "I could not find relevant information in the indexed documents."

    from crewai import Agent, Crew, Process, Task

    rag_agent = Agent(
        role="Nexus Document RAG Analyst",
        goal="Answer questions using only retrieved Nexus document excerpts.",
        backstory=(
            "You are a careful analyst. You ground every claim in the supplied "
            "source excerpts and clearly say when the excerpts are insufficient."
        ),
        llm=get_crewai_llm(),
        verbose=False,
        allow_delegation=False,
    )
    rag_task = Task(
        description=(
            "Answer the question using only the provided excerpts.\n\n"
            "Question:\n{question}\n\n"
            "Document excerpts:\n{context}\n\n"
            "Rules:\n"
            "- Reference source numbers inline for important claims.\n"
            "- Do not invent facts, numbers, companies, dates, or conclusions.\n"
            "- If evidence is incomplete, say what is missing."
        ),
        expected_output="Grounded markdown answer with source references.",
        agent=rag_agent,
    )
    crew = Crew(
        agents=[rag_agent],
        tasks=[rag_task],
        process=Process.sequential,
        verbose=False,
    )
    result = crew.kickoff(
        inputs={
            "question": question,
            "context": "\n\n".join(context.context_blocks),
        }
    )
    return str(getattr(result, "raw", result)).strip()


async def run_rag_query(request: ChatQueryRequest) -> ChatQueryResponse:
    context = retrieve_context(request)
    answer = answer_with_rag(request.question, context)
    return ChatQueryResponse(answer=answer, citations=context.citations)


@router.post("/index/{document_id}")
async def index_document(document_id: str, request: IndexDocumentRequest):
    try:
        chunks = db.get_chunks(document_id)
        if not chunks:
            raise HTTPException(status_code=404, detail="Document chunks not found")

        ids = []
        documents = []
        metadatas = []
        for chunk in chunks:
            chunk_dict = dict(chunk)
            chunk_id = chunk_dict["chunk_id"]
            ids.append(chunk_id)
            documents.append(chunk_dict["chunk_text"])
            metadatas.append(
                {
                    "document_id": document_id,
                    "company_id": request.company_id,
                    "period": request.period or "",
                    "file_name": request.file_name or "",
                    "chunk_index": chunk_dict.get("chunk_index", 0),
                    "page_number": chunk_dict.get("page_number") or 0,
                    "section_title": chunk_dict.get("section_title") or "",
                }
            )

        get_collection().upsert(ids=ids, documents=documents, metadatas=metadatas)
        return {
            "document_id": document_id,
            "company_id": request.company_id,
            "chunks_indexed": len(ids),
            "vector_store": "chroma",
            "collection": CHROMA_COLLECTION,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.log_error(f"Chat indexing failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Chat indexing failed: {exc}")


@router.post("/query", response_model=ChatQueryResponse)
async def query_chat(request: ChatQueryRequest):
    try:
        return await run_rag_query(request)
    except Exception as exc:
        log.log_error(f"Chat query failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Chat query failed: {exc}")
