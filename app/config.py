from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ollama_base_url: str = "http://ollama.ai-chat.svc.cluster.local:11434"
    ollama_model: str = "qwen3:8b"
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""
    kubeconfig: str = ""
    chroma_host: str = "chromadb.ai-chat.svc.cluster.local"
    chroma_ollama_embed_url: str = "http://ollama.ai-chat.svc.cluster.local:11434"
    chroma_embed_model: str = "nomic-embed-text"
    chroma_collection: str = "k8s-runbooks"
    chroma_port: int = 8000
    discord_bot_token: str = ""
    discord_channel_id: str = ""
    enable_auto_apply: bool = False
    auto_apply_actions: str = "restart_pod"         # comma-separated whitelist for auto-apply (no human click)
    auto_apply_confidence_threshold: str = "high"   # min confidence level — high/medium/low
    agent_timeout_seconds: int = 120
    max_concurrent_investigations: int = 3
    investigations_dir: str = "/data/investigations"
    max_investigations_per_scenario: int = 10

    class Config:
        env_file = ".env"


settings = Settings()
