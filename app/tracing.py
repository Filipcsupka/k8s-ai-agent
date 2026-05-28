"""
Langfuse tracing — optional. Returns None if keys not configured.
Agent degrades gracefully with no tracing when Langfuse is unavailable.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_langfuse_client = None
_initialized = False


def _init():
    global _langfuse_client, _initialized
    if _initialized:
        return
    _initialized = True

    from app.config import settings
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.info("Langfuse keys not set — tracing disabled")
        return

    try:
        from langfuse import Langfuse
        _langfuse_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        logger.info("Langfuse tracing enabled → %s", settings.langfuse_host)
    except Exception as e:
        logger.warning("Langfuse init failed: %s — tracing disabled", e)


def get_langfuse_handler(name: str, metadata: Optional[dict] = None):
    """Return a LangchainCallbackHandler for this trace, or None."""
    _init()
    if _langfuse_client is None:
        return None

    try:
        from langfuse.callback import CallbackHandler
        return CallbackHandler(
            public_key=_langfuse_client.public_key,
            secret_key=_langfuse_client.secret_key,
            host=_langfuse_client.base_url,
            trace_name=name,
            metadata=metadata or {},
        )
    except Exception as e:
        logger.warning("Failed to create Langfuse handler: %s", e)
        return None
