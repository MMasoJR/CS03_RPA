"""
src/collector/sensor_simulator.py
Simulador de dados de sensores IoT de motores elétricos industriais.

Simula três fontes de dados distintas:
  1. IoTSource     - Leituras contínuas de sensores embarcados (MQTT-like)
  2. CsvSource     - Arquivo CSV representando sistema legado
  3. ManualEntry   - Entrada manual simulada (formulário de inspeção)

Cada motor tem parâmetros operacionais realistas e pode entrar em diferentes
regimes de degradação para testar o pipeline de detecção.
"""
import uuid
import random
import json
import csv
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
from pathlib import Path
import numpy as np

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────
#  Parâmetros nominais de cada motor
# ─────────────────────────────────────────────
MOTOR_CATALOG = [
    {
        "asset_tag": "MTR-001",
        "name": "Motor Bomba Centrífuga - Linha A",
        "location": "Planta 1 / Setor de Utilidades / Linha A",
        "manufacturer": "WEG",
        "model": "W22 IR3",
        "serial_number": "WEG2024001",
        "rated_power_kw": 75.0,
        "rated_voltage_v": 380.0,
        "rated_current_a": 145.0,
        "rated_rpm": 1760,
    },
    {
        "asset_tag": "MTR-002",
        "name": "Motor Compressor de Ar - Linha A",
        "location": "Planta 1 / Sala de Compressores / Linha A",
        "manufacturer": "ABB",
        "model": "M2BAX 315",
        "serial_number": "ABB2024002",
        "rated_power_kw": 110.0,
        "rated_voltage_v": 380.0,
        "rated_current_a": 210.0,
        "rated_rpm": 1480,
    },
    {
        "asset_tag": "MTR-003",
        "name": "Motor Esteira Transportadora - Linha B",
        "location": "Planta 2 / Linha B / Transporte",
        "manufacturer": "Siemens",
        "model": "1LE1002",
        "serial_number": "SIE2024003",
        "rated_power_kw": 45.0,
        "rated_voltage_v": 380.0,
        "rated_current_a": 87.0,
        "rated_rpm": 1750,
    },
    {
        "asset_tag": "MTR-004",
        "name": "Motor Agitador Industrial - Reator R01",
        "location": "Planta 3 / Área Química / Reator R01",
        "manufacturer": "WEG",
        "model": "W21 IR2",
        "serial_number": "WEG2024004",
        "rated_power_kw": 30.0,
        "rated_voltage_v": 380.0,
        "rated_current_a": 58.0,
        "rated_rpm": 900,
    },
    {
        "asset_tag": "MTR-005",
        "name": "Motor Ventilador Cooling Tower",
        "location": "Planta 1 / Torres de Resfriamento / CT-02",
        "manufacturer": "Weg",
        "model": "W22 IR3 Premium",
        "serial_number": "WEG2024005",
        "rated_power_kw": 22.0,
        "rated_voltage_v": 380.0,
        "rated_current_a": 43.0,
        "rated_rpm": 1460,
    },
]


@dataclass
class RawSensorPayload:
    """Payload bruto de sensor — pode conter inconsistências propositais."""
    asset_tag: str
    source: str
    collected_at: str
    raw_temperature: float
    raw_vibration: float
    raw_current: float
    raw_voltage: float
    raw_rpm: float
    raw_power_factor: float
    raw_operating_hours: float
    raw_unit_temperature: str = "C"
    raw_unit_vibration: str = "mm/s"
    has_anomaly: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class MotorState:
    """
    Mantém o estado operacional de um motor ao longo do tempo,
    incluindo degradação gradual e eventos de anomalia.
    """
    def __init__(self, catalog: dict):
        self.catalog = catalog
        self.operating_hours = random.uniform(500, 8000)
        self.degradation_factor = 1.0  # 1.0 = novo, > 1.0 = degradado
        self.anomaly_mode = None       # None | "overheating" | "vibration" | "overload"
        self._step()

    def _step(self):
        """Incrementa horas e simula degradação gradual."""
        self.operating_hours += random.uniform(0.008, 0.02)  # ~30s a tempo real
        # Degradação muito lenta (~0.001% por ciclo)
        self.degradation_factor += random.uniform(0, 0.0001)

        # 1% de chance de entrar em modo anomalia por ciclo
        if self.anomaly_mode is None and random.random() < 0.01:
            self.anomaly_mode = random.choice(["overheating", "vibration", "overload"])
            logger.warning(
                f"[ANOMALY] Motor {self.catalog['asset_tag']} entrou em modo: {self.anomaly_mode}"
            )
        # 20% de chance de sair do modo anomalia
        elif self.anomaly_mode and random.random() < 0.20:
            self.anomaly_mode = None

    def sample(self) -> RawSensorPayload:
        self._step()
        cat = self.catalog
        d = self.degradation_factor
        mode = self.anomaly_mode

        # ─── Temperatura base ───
        base_temp = 45.0 + (d - 1.0) * 200
        if mode == "overheating":
            base_temp += random.uniform(20, 50)
        temperature = base_temp + random.gauss(0, 1.5)

        # Simula 5% dos sensores reportando em Fahrenheit
        unit_temp = "C"
        if random.random() < 0.05:
            temperature = temperature * 9 / 5 + 32
            unit_temp = "F"

        # ─── Vibração ───
        base_vib = 2.5 + (d - 1.0) * 50
        if mode == "vibration":
            base_vib += random.uniform(4, 12)
        vibration = max(0.1, base_vib + random.gauss(0, 0.3))

        # Simula 3% reportando em in/s
        unit_vib = "mm/s"
        if random.random() < 0.03:
            vibration = vibration / 25.4
            unit_vib = "in/s"

        # ─── Corrente ───
        load_factor = random.uniform(0.6, 0.95)
        if mode == "overload":
            load_factor = random.uniform(1.0, 1.15)
        current = cat["rated_current_a"] * load_factor + random.gauss(0, 2)

        # ─── Tensão ───
        voltage = cat["rated_voltage_v"] + random.gauss(0, 5)

        # ─── RPM ───
        slip = random.uniform(0.02, 0.05)
        rpm = cat["rated_rpm"] * (1 - slip) + random.gauss(0, 10)
        if mode == "vibration":
            rpm += random.gauss(0, 40)

        # ─── Fator de potência ───
        power_factor = random.uniform(0.80, 0.95)

        return RawSensorPayload(
            asset_tag=cat["asset_tag"],
            source="iot_simulator",
            collected_at=datetime.utcnow().isoformat(),
            raw_temperature=round(temperature, 2),
            raw_vibration=round(vibration, 3),
            raw_current=round(current, 2),
            raw_voltage=round(voltage, 1),
            raw_rpm=round(rpm, 1),
            raw_power_factor=round(power_factor, 3),
            raw_operating_hours=round(self.operating_hours, 2),
            raw_unit_temperature=unit_temp,
            raw_unit_vibration=unit_vib,
            has_anomaly=(mode is not None),
            notes=f"anomaly_mode={mode}" if mode else "",
        )


class IoTSource:
    """Simula leituras contínuas de sensores IoT embarcados."""

    def __init__(self, motor_catalog: list = None):
        catalog = motor_catalog or MOTOR_CATALOG
        self.motor_states = {c["asset_tag"]: MotorState(c) for c in catalog}

    def collect(self) -> List[RawSensorPayload]:
        readings = []
        for state in self.motor_states.values():
            payload = state.sample()
            readings.append(payload)
            logger.debug(f"[IoT] Coletado: {payload.asset_tag} | T={payload.raw_temperature}{payload.raw_unit_temperature}")
        return readings


class CsvSource:
    """
    Simula coleta de arquivo CSV gerado por sistema legado SCADA.
    Gera um arquivo CSV de exemplo e faz sua leitura.
    """

    def __init__(self, csv_path: str = "data/legacy_export.csv"):
        self.csv_path = Path(csv_path)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)

    def generate_sample_csv(self, rows: int = 20):
        """Gera um CSV simulando exportação de sistema legado."""
        fieldnames = [
            "TAG", "DATETIME", "TEMP_C", "VIB_MM_S", "CURR_A",
            "VOLT_V", "RPM", "PF", "OP_HOURS", "ALARM"
        ]
        with open(self.csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for i in range(rows):
                tag = random.choice(MOTOR_CATALOG)
                ts = datetime.utcnow() - timedelta(hours=rows - i)
                writer.writerow({
                    "TAG": tag["asset_tag"],
                    "DATETIME": ts.strftime("%d/%m/%Y %H:%M:%S"),  # Formato brasileiro (legado)
                    "TEMP_C": round(random.uniform(40, 75), 1),
                    "VIB_MM_S": round(random.uniform(1.0, 8.0), 2),
                    "CURR_A": round(tag["rated_current_a"] * random.uniform(0.5, 1.1), 1),
                    "VOLT_V": round(tag["rated_voltage_v"] + random.gauss(0, 5), 1),
                    "RPM": round(tag["rated_rpm"] * random.uniform(0.95, 1.0)),
                    "PF": round(random.uniform(0.78, 0.95), 3),
                    "OP_HOURS": round(random.uniform(1000, 9000), 1),
                    "ALARM": random.choice(["", "", "", "HIGH_TEMP", "HIGH_VIB"]),
                })
        logger.info(f"[CSV] Arquivo legado gerado: {self.csv_path} ({rows} linhas)")

    def collect(self) -> List[RawSensorPayload]:
        if not self.csv_path.exists():
            self.generate_sample_csv()

        readings = []
        with open(self.csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    # Data em formato brasileiro → ISO
                    dt = datetime.strptime(row["DATETIME"], "%d/%m/%Y %H:%M:%S")
                    readings.append(RawSensorPayload(
                        asset_tag=row["TAG"],
                        source="csv_legacy",
                        collected_at=dt.isoformat(),
                        raw_temperature=float(row["TEMP_C"]),
                        raw_vibration=float(row["VIB_MM_S"]),
                        raw_current=float(row["CURR_A"]),
                        raw_voltage=float(row["VOLT_V"]),
                        raw_rpm=float(row["RPM"]),
                        raw_power_factor=float(row["PF"]),
                        raw_operating_hours=float(row["OP_HOURS"]),
                        has_anomaly=row["ALARM"] != "",
                        notes=f"legacy_alarm={row['ALARM']}" if row["ALARM"] else "",
                    ))
                except (ValueError, KeyError) as e:
                    logger.warning(f"[CSV] Linha inválida ignorada: {e}")

        logger.info(f"[CSV] {len(readings)} leituras carregadas do arquivo legado")
        return readings


class DataCollector:
    """Agregador de todas as fontes de dados."""

    def __init__(self):
        self.iot = IoTSource()
        self.csv = CsvSource()

    def collect_all(self) -> List[RawSensorPayload]:
        readings = []
        readings.extend(self.iot.collect())
        # CSV apenas no primeiro ciclo ou quando o arquivo existir
        try:
            readings.extend(self.csv.collect())
        except Exception as e:
            logger.warning(f"[Collector] CSV ignorado: {e}")
        logger.info(f"[Collector] Total coletado: {len(readings)} leituras")
        return readings
