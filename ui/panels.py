import html
from typing import Dict, Any, List

import streamlit as st

from ui.components import VALUE_HELP, compact_kv, metric_with_info
from logic.helpers import impact_preview_text

from db import (
    load_country_metrics,
    load_recent_history,
    get_external_events,
    get_domestic_events,
    get_round_actions,
    get_round_action_impacts,
    lock_choice,
    get_locks,
    get_country_snapshots,
)
from countries import (
    COUNTRY_DEFS,
)
# Optional: win.py (falls vorhanden)
try:
    from win import evaluate_country_win_conditions
except Exception:
    evaluate_country_win_conditions = None


def render_my_metrics_panel(metrics: Dict[str, Any], country_display_name: str) -> None:
    st.subheader(f"🏳️ {country_display_name} — Werte")
    compact_kv("Wirtschaft", metrics["economy"], VALUE_HELP["Wirtschaft"])
    compact_kv("Stabilität", metrics["stability"], VALUE_HELP["Stabilität"])
    compact_kv("Militär", metrics["military"], VALUE_HELP["Militär"])
    compact_kv("Diplomatie", metrics["diplomatic_influence"], VALUE_HELP["Diplomatie"])
    compact_kv("Öffentliche Zustimmung", metrics["public_approval"], VALUE_HELP["Öffentliche Zustimmung"])

    if metrics.get("ambition"):
        with st.expander("🎯 Ambition", expanded=False):
            st.write(metrics["ambition"])


def _progress_from_conditions(cond_results) -> float:
    try:
        total = len(cond_results)
        if total <= 0:
            return 0.0
        ok = sum(1 for r in cond_results if getattr(r, "ok", False))
        return round(ok / total * 100.0, 2)
    except Exception:
        return 0.0


def render_news_panel(
    conn,
    *,
    round_no: int,
    eu: Dict[str, Any],
    countries: List[str],
    countries_display: Dict[str, str],
    my_country: str,
) -> None:
    st.subheader("🗞️ News")
    st.write("Hallo " + COUNTRY_DEFS[my_country]["Leader"] + "!"),
    if eu.get("global_context"):
        st.info(eu["global_context"])

    # --- Aktuelle Runde: Außenmächte (wie bisher) ---
    ext_events_now = get_external_events(conn, round_no)
    if ext_events_now:
        with st.expander("🌐 Außenmächte-Moves (aktuelle Runde)", expanded=True):
            for e in ext_events_now:
                c = int(e.get("craziness", 0) or 0)
                st.markdown(f"**{e['actor']}** (🎲 {c}/100): {e['headline']}")
                q = (e.get("quote") or "").strip()
                if q and q != "—":
                    st.caption(f"🗣️ {q}")
    else:
        st.caption("Keine Außenmächte-Moves (noch nicht generiert).")

    # --- Aktuelle Runde: Innenpolitik (NEU) ---
    dom_now = get_domestic_events(conn, round_no)
    if dom_now:
        with st.expander("🏠 Innenpolitik (aktuelle Runde)", expanded=True):
            for e in dom_now:
                name = countries_display.get(e["country"], e["country"])
                c = int(e.get("craziness", 0) or 0)
                st.markdown(f"**{name}** (🎲 {c}/100): {e['headline']}")
                if e.get("details"):
                    st.caption(e["details"])
    else:
        st.caption("Keine Innenpolitik-Headlines (noch nicht generiert).")


def render_public_dashboard(conn, *, countries: List[str], countries_display: Dict[str, str]):
    st.subheader("📊 Öffentliches Dashboard")

    snapshots = get_country_snapshots(conn)
    if not snapshots:
        st.caption("Noch keine Daten: Dashboard füllt sich nach dem ersten Resolve (Runde 1).")
        return

    latest_by_country: Dict[str, Dict[str, Any]] = {}
    for row in snapshots:
        c = row["country"]
        if c not in latest_by_country or row["round"] > latest_by_country[c]["round"]:
            latest_by_country[c] = row

    leaderboard = sorted(
        latest_by_country.values(),
        key=lambda x: (x["victory_progress"], x["public_approval"]),
        reverse=True,
    )

    cols = st.columns([0.26, 0.14, 0.12, 0.12, 0.12, 0.12, 0.12])
    cols[0].markdown("**Land**")
    cols[1].markdown("**Sieg %**")
    cols[2].markdown("**Approval**")
    cols[3].markdown("**Stabilität**")
    cols[4].markdown("**Wirtschaft**")
    cols[5].markdown("**Militär**")
    cols[6].markdown("**Diplomatie**")

    for r in leaderboard:
        name = countries_display.get(r["country"], r["country"])
        badge = "🏆 " if r["is_winner"] else ""
        cols = st.columns([0.26, 0.14, 0.12, 0.12, 0.12, 0.12, 0.12])
        cols[0].write(f"{badge}{name}")
        cols[1].write(f"{r['victory_progress']:.0f}%")
        cols[2].write(r["public_approval"])
        cols[3].write(r["stability"])
        cols[4].write(r["economy"])
        cols[5].write(r["military"])
        cols[6].write(r["diplomatic_influence"])

    st.write("---")

    try:
        import pandas as pd
    except Exception:
        st.caption("pandas nicht verfügbar → Charts deaktiviert.")
        return

    df = pd.DataFrame(snapshots)
    df["country_name"] = df["country"].map(lambda x: countries_display.get(x, x))

    metric = st.selectbox(
        "Chart-Metrik",
        ["victory_progress", "economy", "stability", "military", "diplomatic_influence", "public_approval"],
        index=0,
    )

    pivot = df.pivot_table(index="round", columns="country_name", values=metric, aggfunc="max").sort_index()
    st.line_chart(pivot, height=280)

    if metric != "victory_progress":
        st.caption("Tipp: Stelle auf `victory_progress`, um den Siegfokus zu sehen.")


def render_player_view(
    *,
    conn,
    round_no: int,
    phase: str,
    eu: Dict[str, Any],
    countries_display: Dict[str, str],
    my_country: str,
    is_lock_disabled: bool,
    is_gm: bool,
):
    actions_texts = get_round_actions(conn, round_no)
    action_impacts = get_round_action_impacts(conn, round_no)
    locks_now = get_locks(conn, round_no)

    if phase == "game_over":
        st.info("Game Over – keine Aktionen mehr möglich.")
        return

    if phase != "actions_published":
        st.info("Optionen sind noch nicht veröffentlicht. Warte auf den Game Master.")
        return

    a = actions_texts.get(my_country, {})
    if not a or len(a) < 3:
        st.warning("Optionen fehlen noch (GM muss Aktionen generieren und veröffentlichen).")
        return

    st.subheader("🎮 Öffentliche Aktion wählen")

    if my_country in locks_now:
        st.success("✅ Eingelockt. (Welche Variante bleibt für andere verborgen.)")
    else:
        st.warning("⏳ Noch nicht eingelockt.")

    # Auto-refresh (built-in Streamlit)
    is_waiting = (phase != "actions_published") or (phase == "actions_published" and my_country in get_locks(conn, round_no))
    if (not is_gm) and is_waiting and phase != "game_over":
        st.autorefresh(interval=4000, key="player_wait_refresh")

    options = {
        "aggressiv": a["aggressiv"],
        "moderate": a["moderate"],
        "passiv": a["passiv"],
    }
    labels = [options["aggressiv"], options["moderate"], options["passiv"]]
    choice_label = st.radio("Option:", labels, index=1)
    chosen_variant = next(k for k, v in options.items() if v == choice_label)

    folgen = (action_impacts.get(my_country, {}) or {}).get(chosen_variant, {}) or {}
    if folgen:
        st.caption("**Voraussichtliche Wirkung:** " + impact_preview_text(folgen))
    else:
        st.caption("Voraussichtliche Wirkung: (noch keine Daten / alte Runde ohne Impact gespeichert)")

    with st.expander("Alle Wirkungen vergleichen", expanded=False):
        for v in ("aggressiv", "moderate", "passiv"):
            folgen_v = (action_impacts.get(my_country, {}) or {}).get(v, {}) or {}
            st.write(f"**{v.capitalize()}**")
            st.write(options[v])
            if folgen_v:
                st.caption(impact_preview_text(folgen_v))
            else:
                st.caption("(keine Impact-Daten)")
            st.write("---")

    if st.button("✅ Auswahl einlocken", use_container_width=True, disabled=is_lock_disabled):
        lock_choice(conn, round_no, my_country, chosen_variant)
        st.rerun()

    with st.expander("📜 Turn-History (Mein Land)", expanded=False):
        rows = load_recent_history(conn, my_country, limit=12)
        if not rows:
            st.write("Noch keine Runden gespielt.")
        else:
            for r in rows:
                st.markdown(
                    f"""
**Runde {r[0]}**  
Aktion: {r[1]}  
Δ Militär {r[2]}, Δ Stabilität {r[3]}, Δ Wirtschaft {r[4]}, Δ Diplomatie {r[5]}, Δ Zustimmung {r[6]}  
Kontext: {r[7]}
"""
                )
