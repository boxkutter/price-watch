from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./retail.db"
    app_secret: str = "dev-only-change-me"
    admin_email: str = "admin@example.invalid"
    admin_password: str = ""
    public_base_url: str = "http://localhost:8085"
    allow_http_webhooks: bool = False
    webhook_private_hosts: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
