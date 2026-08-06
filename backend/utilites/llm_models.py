import os
from dotenv import load_dotenv
load_dotenv()

from backend.config import DEFAULT_GEMINI_MODEL

# Hard ceiling on any single LLM call. Kept comfortably under API Gateway's
# fixed 29s integration timeout so a slow/unresponsive provider raises a
# catchable exception instead of hanging the Lambda invocation to its own
# (much longer) timeout — see AWS_DEPLOYMENT.md for the 503/hang incident.
LLM_CALL_TIMEOUT_SECONDS = 20


def get_crewai_llm(model: str | None = None, temperature: float = 0):
    # Imported here so crewai's import-time side effects (filesystem writes,
    # heavy sub-imports) only happen when this function is actually called,
    # not on Lambda cold start.
    from crewai import LLM
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY")
    return LLM(
        model=model or DEFAULT_GEMINI_MODEL,
        api_key=api_key,
        temperature=temperature,
        timeout=LLM_CALL_TIMEOUT_SECONDS,
    )


from abc import ABC, abstractmethod
from typing import Optional

class BaseLLMClient(ABC):

    @abstractmethod
    def complete(
        self,
        user: str,
        system: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> str:
        pass

   

class AzureOpenAIClient(BaseLLMClient):

    def __init__(self):

        from openai import AzureOpenAI

        self.client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.getenv(
                "AZURE_OPENAI_API_VERSION",
                "2025-01-01-preview"
            ),
            azure_endpoint=os.environ[
                "AZURE_OPENAI_ENDPOINT"
            ],
            timeout=LLM_CALL_TIMEOUT_SECONDS,
            max_retries=1,
        )

        self.deployment = os.environ[
            "AZURE_OPENAI_DEPLOYMENT"
        ]

    def complete( #type:ignore
        self,
        user,
        system=None,
        max_tokens=2048,
        temperature=0
    ):

        messages = []

        if system:
            messages.append({
                "role": "system",
                "content": system
            })

        messages.append({
            "role": "user",
            "content": user
        })

        response = self.client.chat.completions.create(
            model=self.deployment,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        return response.choices[0].message.content
    

class GeminiClient(BaseLLMClient):

    def __init__(self):

        from google import genai
        from google.genai import types

        self.client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options=types.HttpOptions(timeout=LLM_CALL_TIMEOUT_SECONDS * 1000),
        )

        self.model = os.getenv(
            "NEXUS_GEMINI_MODEL",
            "gemini-2.5-pro"
        )

    def complete( #type:ignore
        self,
        user,
        system=None,
        max_tokens=2048,
        temperature=0,
    ):

        prompt = user

        if system:
            prompt = f"{system}\n\n{user}"

        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt
        )

        return response.text
    
# # Calling Entry point ----------------------------------------

from functools import lru_cache

@lru_cache(maxsize=1)
def get_llm_client() -> BaseLLMClient:

    provider = DEFAULT_GEMINI_MODEL.split('/')[0].lower()

    providers = {
        # "anthropic": AnthropicClient,
        "azure_openai": AzureOpenAIClient,
        "gemini": GeminiClient,
    }

    if provider not in providers:
        raise ValueError(
            f"Unsupported provider: {provider}"
        )

    return providers[provider]()
