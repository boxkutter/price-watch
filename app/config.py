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
    # Connector packages installed through the admin interface live here. It must be on storage
    # shared by the API and the worker (a volume in docker-compose.yml).
    installed_connectors_dir: str = "./data/connectors"
    max_package_bytes: int = 5_000_000
    # The published Android app (APK and latest.json) served at /download; see app/downloads.py.
    app_downloads_dir: str = "./data/downloads"
    # Inbound: limits on our own API so one caller cannot fan out into retailer traffic.
    api_rate_limit_per_minute: int = 300
    max_monitors_per_user: int = 50
    max_pending_searches_per_user: int = 3
    max_searches_per_hour: int = 20
    min_manual_check_interval_seconds: int = 60
    max_app_credentials_per_user: int = 10  # app sign-ins (one per phone); the oldest is replaced beyond this
    max_store_lookups_per_hour: int = 6     # store lookups by postcode a person may ask for
    max_my_stores: int = 15       # stores one person compares across
    max_compare_stores: int = 8   # stores checked live by one barcode comparison
    # Reference database (Open Food Facts): matches products by barcode and supplies details and
    # pictures. Looked up by the worker only, slowly, and cached.
    reference_enabled: bool = True
    reference_user_agent: str = "retail-monitor/0.1 (self-hosted price monitor; reference lookups)"
    reference_min_interval_seconds: float = 3.0
    reference_max_lookups_per_day: int = 300
    reference_recheck_days_found: int = 90
    reference_recheck_days_missing: int = 30
    max_image_upload_bytes: int = 8_000_000
    # Searching: how many results one store gives at first, when to also ask in Swedish, and how long
    # one search may take before it hands back what it has.
    search_page_size: int = 30
    search_translate_below: int = 8
    search_time_budget_seconds: int = 45
    search_max_parallel: int = 6
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
