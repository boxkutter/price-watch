from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./retail.db"
    app_secret: str = "dev-only-change-me"
    admin_email: str = "admin@example.invalid"
    admin_password: str = ""
    public_base_url: str = "http://localhost:8085"
    allow_http_webhooks: bool = False
    webhook_private_hosts: str = ""
    # Outbound: how we present ourselves to retailers. A generic browser string is a
    # compatibility header only; a refusal is never worked around by changing it.
    connector_user_agent: str = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    # Coop's data API needs the subscription key that coop.se's own pages send to every visitor.
    # It is configuration, never committed; see docs/implementation-status.md.
    coop_api_key: str = ""
    # Inbound: limits on our own API so one caller cannot fan out into retailer traffic.
    api_rate_limit_per_minute: int = 300
    max_monitors_per_user: int = 50
    max_pending_searches_per_user: int = 3
    max_searches_per_hour: int = 20
    min_manual_check_interval_seconds: int = 60
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
