import json
import httpx
from collections.abc import AsyncIterator
from app.models.turn import ChatMessage
from app.models.provider_config import ProviderConfigRead

class LLMProvider:
    """Client for interacting with OpenAI-compatible APIs (like Ollama, llama.cpp, OpenAI, etc.)"""

    async def generate_stream(
        self,
        messages: list[ChatMessage],
        config: ProviderConfigRead,
        api_key: str | None = None
    ) -> AsyncIterator[str]:
        """Streams tokens from the LLM provider."""
        url = f"{config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": config.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            # We can pass temperature, max_tokens, etc. if needed later
        }

        async with httpx.AsyncClient(trust_env=False, timeout=60.0) as client:
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        yield f"[Error: LLM returned status {response.status_code}. Detail: {error_body.decode()}]"
                        return

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        
                        line = line.strip()
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            
                            try:
                                data = json.loads(data_str)
                                delta = data.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                # Sometimes models print non-JSON debug output, skip or yield it
                                pass
            except httpx.RequestError as e:
                yield f"[Connection Error: Failed to reach LLM. {str(e)}]"

    async def check_connection(self, base_url: str, model_name: str, api_key: str | None = None) -> bool:
        """Verifies if the LLM provider is reachable and supports the requested model."""
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
            "stream": False
        }

        async with httpx.AsyncClient(trust_env=False, timeout=10.0) as client:
            try:
                response = await client.post(url, headers=headers, json=payload)
                return response.status_code == 200
            except Exception:
                return False
