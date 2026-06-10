from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()

_openai_client = None

def get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client

# Lazy accessor kept for callers that import openai_client by name
class _LazyClient:
    def __getattr__(self, name):
        return getattr(get_openai_client(), name)

openai_client = _LazyClient()
