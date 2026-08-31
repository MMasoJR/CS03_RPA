"""
scripts/retrain_anomaly_model.py

Retreina o Isolation Forest com o historico atual de
data/processed_readings.csv. Rode manualmente ou agende (ex: 1x/dia)
separado do ciclo do orquestrador, para nao pagar o custo de retreino a
cada ciclo de 30s.

Uso:
    python -m scripts.retrain_anomaly_model
"""
from src.intelligence.anomaly_model import AnomalyDetector
from src.utils.logger import get_logger

logger = get_logger("retrain_anomaly_model")


def main():
    detector = AnomalyDetector()
    retrained = detector.retrain()
    if retrained:
        logger.info("[Retrain] Modelo de anomalias atualizado com sucesso.")
    else:
        logger.info("[Retrain] Historico ainda insuficiente — continuando com fallback baseado em regra.")


if __name__ == "__main__":
    main()
