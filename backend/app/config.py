from pydantic import PositiveFloat, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    typesafe_api_key: str = ""
    helmcode_base_url: str = "https://api.helmcode.com/v1"
    helmcode_api_key: str = ""
    supervisor_model: str = ""
    watcher_tau: float = 0.6
    watcher_persistence: int = 1
    action_gate: float = 0.7
    short_term_n: int = 20
    run_log_dir: str = "/var/lib/hackspain/runs"
    neo4j_uri: str = "bolt://neo4j-hackspain:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "hackspain-local"
    neo4j_database: str = "neo4j"
    neo4j_enabled: bool = True
    action_step_delay: float = 0.35
    action_dispatch_token: str = ""
    happyrobot_api_key: str = ""
    happyrobot_hook_url: str = ""
    happyrobot_api_base: str = ""
    oncall_phone: str = ""
    oncall_name: str = ""
    happyrobot_poll_interval: PositiveFloat = 1.5
    happyrobot_poll_timeout: PositiveFloat = 240

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True
    )

    @field_validator("typesafe_api_key", "helmcode_api_key", mode="before")
    @classmethod
    def _strip_wrapped_quotes(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().strip('"').strip("'")
        return value


settings = Settings()
