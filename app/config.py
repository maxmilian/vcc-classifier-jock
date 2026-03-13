from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    anthropic_model_fast: str = "claude-sonnet-4-6"
    anthropic_model_strong: str = "claude-opus-4-6"
    analyze_batch_size: int = 120
    analyze_max_tokens: int = 8192
    ppt_markdown_max_tokens: int = 4096
    gamma_api_key: str = ""
    gamma_theme_id: str = ""
    gamma_input_max_chars: int = 32000
    gamma_image_model: str = "flux-1-quick"
    gamma_image_style: str = "professional, clean, modern business"
    gamma_prompt_file: str = "app/prompts/gamma_prompt.txt"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
