from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ollama_base_url: str = "http://ollama.ai-chat.svc.cluster.local:11434"
    ollama_model: str = "qwen3:8b"
    slack_webhook_url: str = ""
    kubeconfig: str = ""
    chroma_host: str = "chromadb.ai-chat.svc.cluster.local"
    chroma_port: int = 8001
    enable_auto_apply: bool = False
    agent_timeout_seconds: int = 120
    max_concurrent_investigations: int = 3
    investigations_dir: str = "/data/investigations"

    class Config:
        env_file = ".env"


settings = Settings()
