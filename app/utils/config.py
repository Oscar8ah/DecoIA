from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    secret_key: str
    debug: bool = False
    allowed_hosts: str = "localhost"

    openai_api_key: str
    stability_api_key: str

    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()