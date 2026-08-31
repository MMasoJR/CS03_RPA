"""
src/utils/logger.py
Configuração centralizada de logging com suporte a arquivo e console.
Usa formato estruturado para facilitar parsing de logs em ferramentas externas.
"""
import logging
import sys
from pathlib import Path
from datetime import datetime
import os

LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Cria o diretório de logs se não existir
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Nome do arquivo de log com timestamp do dia
LOG_FILE = LOG_DIR / f"pipeline_{datetime.utcnow().strftime('%Y%m%d')}.log"


class StructuredFormatter(logging.Formatter):
    """Formato: timestamp | level | logger | message"""
    def format(self, record):
        ts = datetime.utcnow().isoformat(timespec="milliseconds")
        return f"{ts} | {record.levelname:<8} | {record.name:<40} | {record.getMessage()}"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:  # Evita duplicação de handlers
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    fmt = StructuredFormatter()

    # Handler para console
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Handler para arquivo
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger
