import streamlit as st
import pandas as pd
import os
import sys

# Streamlit não roda com "-m", então a raiz do projeto não entra
# automaticamente no sys.path (diferente de "python -m src.pipeline...").
# Adiciona a raiz manualmente pra "config" e "src" serem importáveis.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.settings import settings
from src.models.database import get_engine

# Configuração da página
st.set_page_config(page_title="Digital Twin Operacional", layout="wide")

st.title("🏭 Digital Twin - Painel Operacional")

STATUS_COLOR = {
    "OPERATIONAL": "🟢",
    "WARNING": "🟡",
    "CRITICAL": "🔴",
    "OFFLINE": "⚪",
    "MAINTENANCE": "🔧",
}

SEVERITY_COLOR = {
    "INFO": "🟢",
    "ALERTA": "🟡",
    "CRITICO": "🔴",
}


@st.cache_resource
def get_db_engine():
    return get_engine(settings.database_url)


# Função para carregar os dados
@st.cache_data(ttl=30)
def carregar_dados():
    caminho_cadastro = "data/cadastro_ativos.csv"
    if not os.path.exists(caminho_cadastro):
        st.error("data/cadastro_ativos.csv não encontrado! Rode o RPA de cadastro (rpa_cadastro.py) antes.")
        st.stop()
    df_cad = pd.read_csv(caminho_cadastro)

    engine = get_db_engine()

    # Histórico de leituras processadas, já com o asset_tag (join com assets)
    df_hist = pd.read_sql(
        """
        SELECT a.asset_tag, pr.processed_at AS collected_at, pr.temperature_c,
               pr.vibration_mm_s, pr.current_a, pr.voltage_v, pr.rpm, pr.health_score
        FROM processed_readings pr
        JOIN assets a ON a.id = pr.asset_id
        ORDER BY pr.processed_at
        """,
        engine,
    )

    # Snapshot mais recente por ativo
    df_snap = pd.read_sql(
        """
        SELECT DISTINCT ON (a.asset_tag) a.asset_tag, s.snapshot_at, s.status,
               s.avg_temperature_c, s.avg_vibration_mm_s, s.avg_current_a,
               s.avg_voltage_v, s.avg_rpm, s.avg_health_score
        FROM asset_snapshots s
        JOIN assets a ON a.id = s.asset_id
        ORDER BY a.asset_tag, s.snapshot_at DESC
        """,
        engine,
    )

    # Status atual de cada ativo (campo Asset.status)
    df_status = pd.read_sql(
        "SELECT asset_tag, status, updated_at FROM assets",
        engine,
    )

    # Log de eventos/alertas (CS03)
    df_eventos = pd.read_sql(
        """
        SELECT a.asset_tag, e.event_type, e.severity, e.metric, e.metric_value,
               e.threshold_used, e.anomaly_score, e.suggested_action, e.created_at
        FROM asset_events e
        JOIN assets a ON a.id = e.asset_id
        ORDER BY e.created_at DESC
        """,
        engine,
    )

    return df_cad, df_snap, df_hist, df_status, df_eventos


df_cadastro, df_snapshot, df_historico, df_status, df_eventos = carregar_dados()

# BARRA LATERAL (Navegação Hierárquica)
st.sidebar.header("🔍 Estrutura de Navegação")

plantas = df_cadastro['PLANTA'].unique()
planta_selecionada = st.sidebar.selectbox("1. Selecione a Planta", plantas)

areas = df_cadastro[df_cadastro['PLANTA'] == planta_selecionada]['AREA'].unique()
area_selecionada = st.sidebar.selectbox("2. Selecione a Área", areas)

tags = df_cadastro[(df_cadastro['PLANTA'] == planta_selecionada) &
                   (df_cadastro['AREA'] == area_selecionada)]['TAG'].unique()
tag_selecionada = st.sidebar.selectbox("3. Selecione o Equipamento (TAG)", tags)

# CS03: resumo de status na sidebar (visão geral de todos os ativos)
if not df_status.empty:
    st.sidebar.divider()
    st.sidebar.subheader("📟 Status geral dos ativos")
    for _, row in df_status.iterrows():
        emoji = STATUS_COLOR.get(row["status"], "⚪")
        st.sidebar.markdown(f"{emoji} `{row['asset_tag']}` — {row['status']}")

# PAINEL PRINCIPAL
st.header(f"Monitoramento: Ativo `{tag_selecionada}`")
st.markdown(f"**Localização Técnica:** `{planta_selecionada} > {area_selecionada}`")

# CS03: status operacional atual, em destaque no topo
status_ativo = df_status[df_status["asset_tag"] == tag_selecionada]
if not status_ativo.empty:
    row = status_ativo.iloc[0]
    snap_row = df_snapshot[df_snapshot["asset_tag"] == tag_selecionada]
    health = snap_row.iloc[0]["avg_health_score"] if not snap_row.empty else "—"
    emoji = STATUS_COLOR.get(row["status"], "⚪")
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Status operacional", f"{emoji} {row['status']}")
    col_b.metric("Health Score", health)
    col_c.metric("Última atualização", str(row.get("updated_at", "—"))[:19])
else:
    st.info("Este ativo ainda não passou pelo pipeline — rode o orquestrador pelo menos uma vez.")

st.divider()

# GRÁFICOS TEMPORAIS (HISTÓRICO)
historico_ativo = df_historico[df_historico['asset_tag'] == tag_selecionada].copy()

if not historico_ativo.empty:
    historico_ativo['collected_at'] = pd.to_datetime(historico_ativo['collected_at'], format='mixed')
    historico_ativo = historico_ativo.sort_values(by='collected_at')
    historico_ativo.set_index('collected_at', inplace=True)

    st.subheader("📈 Análise de Tendências (Histórico)")

    aba1, aba2, aba3 = st.tabs(["⚡ Elétrica (Tensão/Corrente)", "🌡️ Temperatura", "⚙️ Mecânica (RPM/Vibração)"])

    with aba1:
        st.markdown("**Evolução de Corrente (A) e Tensão (V)**")
        st.line_chart(historico_ativo[['current_a', 'voltage_v']])

    with aba2:
        st.markdown("**Evolução de Temperatura (°C)**")
        st.line_chart(historico_ativo[['temperature_c']], color="#ff4b4b")

    with aba3:
        st.markdown("**Rotação (RPM) e Vibração (mm/s)**")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("*Rotação (RPM)*")
            st.line_chart(historico_ativo[['rpm']], color="#ffa500")
        with col2:
            st.markdown("*Vibração (mm/s)*")
            st.line_chart(historico_ativo[['vibration_mm_s']], color="#8a2be2")
else:
    st.warning(f"Ainda não há dados históricos para o ativo {tag_selecionada}.")

st.divider()

# SNAPSHOT (FOTO ATUAL)
st.subheader("📊 Última Leitura (Snapshot)")
snapshot_ativo = df_snapshot[df_snapshot['asset_tag'] == tag_selecionada]
if not snapshot_ativo.empty:
    st.dataframe(snapshot_ativo, use_container_width=True)
else:
    st.info("O ativo ainda não possui snapshot gerado.")

st.divider()

# CS03: LOG DE EVENTOS/ALERTAS (Pilar 2)
st.subheader("🚨 Eventos e Alertas (Apoio à Decisão)")
eventos_ativo = df_eventos[df_eventos["asset_tag"] == tag_selecionada].copy()

if not eventos_ativo.empty:
    eventos_ativo["created_at"] = pd.to_datetime(eventos_ativo["created_at"], format="mixed")
    eventos_ativo = eventos_ativo.sort_values(by="created_at", ascending=False)

    eventos_relevantes = eventos_ativo[eventos_ativo["severity"] != "INFO"]

    if not eventos_relevantes.empty:
        for _, ev in eventos_relevantes.head(20).iterrows():
            emoji = SEVERITY_COLOR.get(ev["severity"], "⚪")
            with st.expander(
                f"{emoji} {ev['severity']} — {ev['metric']} = {ev['metric_value']:.2f} "
                f"({str(ev['created_at'])[:19]})"
            ):
                st.markdown(f"**Ação sugerida:** {ev.get('suggested_action', '—')}")
                st.markdown(f"**Score de anomalia:** {ev.get('anomaly_score', '—')}")
                if pd.notna(ev.get("threshold_used")):
                    st.markdown(f"**Limiar técnico usado:** {ev['threshold_used']}")
    else:
        st.success("Nenhum alerta registrado para este ativo — operação dentro do esperado.")
else:
    st.info("Ainda não há eventos registrados para este ativo — rode o pipeline com o motor de regras (CS03) ativo.")
