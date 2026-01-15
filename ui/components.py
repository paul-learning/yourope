import html
from typing import Any, Dict, List

import streamlit as st


def inject_css():
    st.markdown(
        """
<style>
/* Inline tooltip for ℹ️ */
.eug-tooltip {
  position: relative;
  display: inline-block;
  cursor: help;
  user-select: none;
  line-height: 1;
}

.eug-tooltip .eug-tooltiptext {
  visibility: hidden;
  opacity: 0;
  transition: opacity 0.12s ease;
  position: absolute;
  z-index: 99999;

  width: 260px;
  max-width: 70vw;

  background: rgba(17, 17, 17, 0.95);
  color: #fff;
  text-align: left;

  padding: 8px 10px;
  border-radius: 8px;

  bottom: 130%;
  left: 50%;
  transform: translateX(-50%);
  box-shadow: 0 10px 24px rgba(0,0,0,0.35);
  font-size: 0.85rem;
  white-space: normal;
}

.eug-tooltip:hover .eug-tooltiptext {
  visibility: visible;
  opacity: 1;
}

/* small arrow */
.eug-tooltip .eug-tooltiptext::after {
  content: "";
  position: absolute;
  top: 100%;
  left: 50%;
  margin-left: -6px;
  border-width: 6px;
  border-style: solid;
  border-color: rgba(17, 17, 17, 0.95) transparent transparent transparent;
}

.eug-kv { margin: 0.15rem 0; }
.eug-kv-row{
  display:flex; justify-content:space-between; align-items:baseline;
  padding: 0.15rem 0;
  border-bottom: 1px solid rgba(255,255,255,0.06);
}
.eug-kv-label{ font-size: 0.88rem; opacity: 0.85; }
.eug-kv-value{ font-size: 0.95rem; font-weight: 600; }
</style>
""",
        unsafe_allow_html=True,
    )


VALUE_HELP = {
    "Wirtschaft": "Wachstum/Inflation/Haushalt. Niedrig → Zustimmung fällt schneller.",
    "Stabilität": "Regierungsfähigkeit/Protestresistenz. Niedrig → Krisenanfälligkeit.",
    "Militär": "Abschreckung/Verteidigung. Hilft bei hohem Threat/Frontline, kann innenpolitisch polarisieren.",
    "Diplomatie": "Fähigkeit zu Deals/Koalitionen/Sanktionen. Hoch → bessere Kompromisse.",
    "Öffentliche Zustimmung": "Rückendeckung. Niedrig → riskante Entscheidungen “kosten” stärker.",
    "EU Kohäsion": "Wie geschlossen die EU handelt. Höher = stabilere gemeinsame Linie.",
    "Threat": "Kriegs-/Eskalationsrisiko gesamt.",
    "Frontline": "Druck/Spannung an der EU-Ostflanke.",
    "Energy": "Energie-/Versorgungsdruck (Preise, Engpässe).",
    "Migration": "Migrationsdruck & innenpolitischer Stress.",
    "Disinfo": "Desinformation & Polarisierung.",
    "TradeWar": "Handelskonflikte / wirtschaftlicher Druck von außen.",
}


def compact_kv(label: str, value: Any, help_text: str | None = None):
    label_html = label
    if help_text:
        safe = html.escape(help_text)
        label_html = (
            f"""{label} <span class="eug-tooltip" style="margin-left:4px;">ℹ️"""
            f"""<span class="eug-tooltiptext">{safe}</span></span>"""
        )

    st.markdown(
        f"""
<div class="eug-kv">
  <div class="eug-kv-row">
    <div class="eug-kv-label">{label_html}</div>
    <div class="eug-kv-value">{value}</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def metric_with_info(label: str, value: Any, help_text: str) -> None:
    a, b = st.columns([0.86, 0.14])
    with a:
        st.metric(label, value)
    with b:
        safe = html.escape(help_text or "")
        st.markdown(
            f"""
<span class="eug-tooltip">ℹ️
  <span class="eug-tooltiptext">{safe}</span>
</span>
""",
            unsafe_allow_html=True,
        )
