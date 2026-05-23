"""
ChatLLM (Abacus.AI RouteLLM) client.

OpenAI-compatible API at https://routellm.abacus.ai/v1/chat/completions.
Auth: Bearer token via CHATLLM_API_KEY env var.

Used for both text generation (Gemini 3.1 Pro Preview, thinking mode) and
image generation (Nano Banana Pro).

IMPORTANT:
- thinking-mode models (gemini-3.1-pro-preview) must NOT receive `temperature`.
- image generation response shape: choices[0].message.images[0].image_url.url
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

LOG = logging.getLogger(__name__)


class ChatLLMError(RuntimeError):
    """Raised for any failure talking to ChatLLM/RouteLLM."""


class ChatLLMClient:
    BASE_URL = "https://routellm.abacus.ai/v1/chat/completions"

    # Models known to be thinking-mode; we never send `temperature` for these.
    THINKING_MODELS = {
        "gemini-3.1-pro-preview",
    }

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("CHATLLM_API_KEY")
        if not self.api_key:
            raise ChatLLMError("CHATLLM_API_KEY is not set")

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post(
        self,
        payload: Dict[str, Any],
        timeout: int,
        max_retries: int,
        retry_base_sleep: float,
    ) -> Dict[str, Any]:
        last_err: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(
                    self.BASE_URL,
                    headers=self._headers(),
                    json=payload,
                    timeout=timeout,
                )
            except requests.RequestException as e:
                last_err = e
                LOG.warning("ChatLLM network error (attempt %d): %s", attempt + 1, e)
            else:
                if resp.status_code < 400:
                    try:
                        return resp.json()
                    except ValueError as e:
                        last_err = ChatLLMError(
                            f"Invalid JSON response: {e} body={resp.text[:300]}"
                        )
                        LOG.warning("ChatLLM JSON parse error: %s", last_err)
                else:
                    # 4xx is usually deterministic, don't retry except 408/429.
                    body = resp.text[:500]
                    err = ChatLLMError(f"HTTP {resp.status_code}: {body}")
                    if resp.status_code in (408, 425, 429) or resp.status_code >= 500:
                        last_err = err
                        LOG.warning("ChatLLM retriable error: %s", err)
                    else:
                        raise err
            if attempt < max_retries:
                sleep = retry_base_sleep * (2 ** attempt)
                LOG.info("Retrying in %.1fs", sleep)
                time.sleep(sleep)
        assert last_err is not None
        raise ChatLLMError(f"ChatLLM call failed after {max_retries + 1} attempts: {last_err}")

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------
    def chat(
        self,
        model: str,
        system: Optional[str] = None,
        user: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        response_format: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: int = 180,
        max_retries: int = 2,
    ) -> str:
        """
        Call a chat-completion model and return the textual content of the
        first choice. Raises ChatLLMError on persistent failure.
        """
        if messages is None:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            if user is not None:
                messages.append({"role": "user", "content": user})
        if not messages:
            raise ChatLLMError("chat() needs either `messages` or `user`")

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
        }

        # response_format: "json" -> json_object
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        elif isinstance(response_format, dict):
            payload["response_format"] = response_format

        # Only set temperature if model is non-thinking AND caller supplied one.
        if temperature is not None and model not in self.THINKING_MODELS:
            payload["temperature"] = temperature

        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        data = self._post(payload, timeout=timeout, max_retries=max_retries, retry_base_sleep=4.0)

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ChatLLMError(
                f"Unexpected chat response shape: {e} body={json.dumps(data)[:500]}"
            ) from e

    def generate_image(
        self,
        model: str,
        prompt: str,
        aspect_ratio: str = "1:1",
        resolution: str = "2K",
        num_images: int = 1,
        timeout: int = 300,
        max_retries: int = 1,
    ) -> str:
        """
        Generate an image via the chat-completions endpoint with
        modalities=["image"]. Returns the image URL (typically a data URL).
        """
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image"],
            "image_config": {
                "num_images": num_images,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            },
        }
        data = self._post(
            payload, timeout=timeout, max_retries=max_retries, retry_base_sleep=8.0
        )

        try:
            images = data["choices"][0]["message"]["images"]
            if not images:
                raise ChatLLMError("Empty `images` array in response")
            url = images[0]["image_url"]["url"]
            if not isinstance(url, str) or not url:
                raise ChatLLMError(f"Bad image URL: {url!r}")
            return url
        except (KeyError, IndexError, TypeError) as e:
            raise ChatLLMError(
                f"Unexpected image response shape: {e} body={json.dumps(data)[:500]}"
            ) from e
