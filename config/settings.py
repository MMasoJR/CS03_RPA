"""
config/settings.py
Configurações centralizadas da aplicação via variáveis de ambiente.
Usa pydantic-settings para validação automática dos valores.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Banco de dados
    # Sem "env=" explícito: o pydantic-settings já casa DB_HOST -> db_host
    # automaticamente (case_sensitive=False acima)
    db_host: str = Field(default="localhost")
    db_port: int = Field(default=5432)
    db_user: str = Field(default="motor_admin")
    db_password: str = Field(default="motor_pass_2024")
    db_name: str = Field(default="motor_monitoring")
    # "prefer" tenta SSL e cai para conexão normal se o servidor não suportar
    # (funciona tanto local/Docker quanto no Supabase, que exige SSL).
    db_sslmode: str = Field(default="prefer")

    # Pipeline
    pipeline_mode: str = Field(default="batch")
    batch_interval_seconds: int = Field(default=30)
    num_motors: int = Field(default=5)

    # Logs
    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="logs")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?sslmode={self.db_sslmode}"
        )


# Instância global
settings = Settings()
