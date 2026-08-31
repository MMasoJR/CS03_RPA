"""
src/models/database.py
Modelos do banco de dados e configuração do SQLAlchemy.

Tabelas:
  - assets            : Cadastro dos motores (dados mestres)
  - sensor_readings   : Leituras brutas dos sensores IoT
  - processed_readings: Leituras normalizadas/processadas
  - pipeline_logs     : Rastreabilidade de cada execução do pipeline
  - asset_snapshots   : Histórico de estado consolidado do ativo
  - asset_events      : Log histórico de eventos/alertas (CS03)
"""
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    DateTime, Boolean, Text, ForeignKey, Enum, JSON
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
import enum

Base = declarative_base()


# ─────────────────────────────────────────────
#  Enums
# ─────────────────────────────────────────────
class AssetStatus(str, enum.Enum):
    OPERATIONAL = "OPERATIONAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    OFFLINE = "OFFLINE"
    MAINTENANCE = "MAINTENANCE"


class PipelineStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class EventSeverity(str, enum.Enum):
    INFO = "INFO"
    ALERTA = "ALERTA"
    CRITICO = "CRITICO"


# ─────────────────────────────────────────────
#  Tabela: assets
# ─────────────────────────────────────────────
class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_tag = Column(String(50), unique=True, nullable=False, index=True,
                       comment="TAG único do ativo (ex: MTR-001)")
    name = Column(String(120), nullable=False, comment="Nome descritivo do motor")
    location = Column(String(200), nullable=False, comment="Localização física (planta/setor/linha)")
    manufacturer = Column(String(100))
    model = Column(String(100))
    serial_number = Column(String(100), unique=True)
    rated_power_kw = Column(Float, nullable=False, comment="Potência nominal em kW")
    rated_voltage_v = Column(Float, nullable=False, comment="Tensão nominal em Volts")
    rated_current_a = Column(Float, nullable=False, comment="Corrente nominal em Ampères")
    rated_rpm = Column(Integer, nullable=False, comment="Rotação nominal em RPM")
    installation_date = Column(DateTime)
    last_maintenance_date = Column(DateTime)
    status = Column(Enum(AssetStatus), default=AssetStatus.OPERATIONAL, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relacionamentos
    sensor_readings = relationship("SensorReading", back_populates="asset", lazy="dynamic")
    processed_readings = relationship("ProcessedReading", back_populates="asset", lazy="dynamic")
    snapshots = relationship("AssetSnapshot", back_populates="asset", lazy="dynamic")
    events = relationship("AssetEvent", back_populates="asset", lazy="dynamic")


# ─────────────────────────────────────────────
#  Tabela: sensor_readings (dados brutos)
# ─────────────────────────────────────────────
class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, comment="Origem: iot_simulator | csv | manual | modbus")
    collected_at = Column(DateTime, nullable=False, index=True,
                          comment="Timestamp da coleta original")
    ingested_at = Column(DateTime, default=datetime.utcnow,
                         comment="Timestamp de ingestão no sistema")

    raw_temperature = Column(Float, comment="Temperatura bruta (unidade variável)")
    raw_vibration = Column(Float, comment="Vibração bruta")
    raw_current = Column(Float, comment="Corrente bruta")
    raw_voltage = Column(Float, comment="Tensão bruta")
    raw_rpm = Column(Float, comment="Rotação bruta")
    raw_power_factor = Column(Float, comment="Fator de potência bruto")
    raw_operating_hours = Column(Float, comment="Horas de operação brutas")

    raw_unit_temperature = Column(String(10), default="C", comment="Unidade original da temperatura")
    raw_unit_vibration = Column(String(10), default="mm/s", comment="Unidade original da vibração")

    raw_payload = Column(JSON, comment="Payload original completo do sensor (para auditoria)")
    is_processed = Column(Boolean, default=False, index=True)
    has_anomaly = Column(Boolean, default=False)
    notes = Column(Text)

    asset = relationship("Asset", back_populates="sensor_readings")


# ─────────────────────────────────────────────
#  Tabela: processed_readings (dados normalizados)
# ─────────────────────────────────────────────
class ProcessedReading(Base):
    __tablename__ = "processed_readings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False, index=True)
    raw_reading_id = Column(Integer, ForeignKey("sensor_readings.id"), nullable=False)
    processed_at = Column(DateTime, default=datetime.utcnow, index=True)

    temperature_c = Column(Float, comment="Temperatura em °C")
    vibration_mm_s = Column(Float, comment="Vibração em mm/s (RMS)")
    current_a = Column(Float, comment="Corrente em Ampères")
    voltage_v = Column(Float, comment="Tensão em Volts")
    rpm = Column(Integer, comment="Rotação em RPM")
    power_factor = Column(Float, comment="Fator de potência (0-1)")
    operating_hours = Column(Float, comment="Horas de operação acumuladas")
    active_power_kw = Column(Float, comment="Potência ativa calculada em kW")

    temp_deviation_pct = Column(Float, comment="Desvio de temperatura em % da nominal")
    current_load_pct = Column(Float, comment="Carga de corrente em % da nominal")
    rpm_deviation_pct = Column(Float, comment="Desvio de RPM em % do nominal")
    health_score = Column(Float, comment="Score de saúde 0-100 calculado")

    is_valid = Column(Boolean, default=True)
    validation_errors = Column(JSON, comment="Lista de erros de validação, se houver")

    asset = relationship("Asset", back_populates="processed_readings")
    raw_reading = relationship("SensorReading")


# ─────────────────────────────────────────────
#  Tabela: pipeline_logs (rastreabilidade)
# ─────────────────────────────────────────────
class PipelineLog(Base):
    __tablename__ = "pipeline_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, index=True, comment="UUID da execução do pipeline")
    stage = Column(String(50), nullable=False, comment="Etapa: collect | transform | validate | load | anomaly_detection | rules_engine")
    status = Column(Enum(PipelineStatus), nullable=False)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)
    records_input = Column(Integer, default=0)
    records_output = Column(Integer, default=0)
    records_failed = Column(Integer, default=0)
    error_message = Column(Text)
    extra_metadata = Column(JSON, comment="Metadados adicionais da execução")


# ─────────────────────────────────────────────
#  Tabela: asset_snapshots (histórico consolidado)
# ─────────────────────────────────────────────
class AssetSnapshot(Base):
    __tablename__ = "asset_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False, index=True)
    snapshot_at = Column(DateTime, default=datetime.utcnow, index=True)
    status = Column(Enum(AssetStatus), nullable=False)

    avg_temperature_c = Column(Float)
    avg_vibration_mm_s = Column(Float)
    avg_current_a = Column(Float)
    avg_voltage_v = Column(Float)
    avg_rpm = Column(Float)
    avg_power_factor = Column(Float)
    avg_health_score = Column(Float)
    min_health_score = Column(Float)
    max_health_score = Column(Float)

    readings_count = Column(Integer, default=0)
    anomalies_count = Column(Integer, default=0)
    period_hours = Column(Float, comment="Janela temporal do snapshot em horas")

    asset = relationship("Asset", back_populates="snapshots")


# ─────────────────────────────────────────────
#  Tabela: asset_events (CS03 — log de eventos/alertas)
# ─────────────────────────────────────────────
class AssetEvent(Base):
    __tablename__ = "asset_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False, index=True)
    event_type = Column(String(30), nullable=False, comment="ANOMALY_DETECTED | STATUS_CHANGE")
    severity = Column(Enum(EventSeverity), nullable=False)
    metric = Column(String(50), comment="Métrica dominante: temperature | vibration | current_load | rpm | power_factor")
    metric_value = Column(Float)
    threshold_used = Column(Float)
    anomaly_score = Column(Float)
    technical_context = Column(JSON, comment="Snapshot do contexto técnico (scraper) usado na decisão")
    suggested_action = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    asset = relationship("Asset", back_populates="events")


# ─────────────────────────────────────────────
#  Factory de engine e session
# ─────────────────────────────────────────────
def get_engine(database_url: str):
    return create_engine(
        database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )


def get_session_factory(engine):
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_database(database_url: str):
    """Cria todas as tabelas se não existirem."""
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    return engine
