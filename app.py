import os
import html
import random
from pathlib import Path
from typing import Dict, Any, List

import streamlit as st
from dotenv import load_dotenv

from countries import (
    COUNTRY_DEFS,
    EU_DEFAULT,
    EXTERNAL_CRAZY_BASELINE_RANGES,
)
from ui.components import inject_css, VALUE_HELP, compact_kv, metric_with_info
from logic.helpers import (
    summarize_recent_actions,
    format_external_events,
    impact_preview_text,
)
from ui.panels import (
    render_my_metrics_panel,
    render_news_panel,
    render_public_dashboard,
    render_player_view,
    _progress_from_conditions,
)


from db import (
    get_conn,
    ensure_schema,
    seed_countries_if_missing,
    reset_all_countries,
    load_country_metrics,
    load_all_country_metrics,
    load_recent_history,
    get_eu_state,
    set_eu_state,
    get_game_meta,
    set_game_meta,
    set_game_over,
    clear_game_over,
    clear_round_data,
    upsert_round_actions,
    get_round_actions,
    get_round_action_impacts,
    lock_choice,
    get_locks,
    all_locked,
    apply_country_deltas,
    insert_turn_history,
    get_recent_round_summaries,
    upsert_round_summary,
    clear_all_round_summaries,
    # snapshots/dashboard
    upsert_country_snapshot,
    get_country_snapshots,
    clear_country_snapshots,
    # external
    clear_external_events,
    upsert_external_event,
    get_external_events,
    # auth
    create_user,
    verify_user,
    list_users,
    delete_user,
    get_max_snapshot_round,
    # domestic
    clear_domestic_events,
    upsert_domestic_event,
    get_domestic_events,
    clear_all_events_and_history,
)

from ai_round import generate_actions_for_country, resolve_round_all_countries, generate_round_summary
from ai_external import generate_external_moves, generate_domestic_events


# Optional: win.py (falls vorhanden)
try:
    from win import evaluate_all_countries, evaluate_country_win_conditions
except Exception:
    evaluate_all_countries = None
    evaluate_country_win_conditions = None


# ----------------------------
# Streamlit config
# ----------------------------
st.set_page_config(page_title="yourope", layout="wide")


# ----------------------------
# CSS
# ----------------------------
inject_css()



def load_env():
    env_path = Path(__file__).with_name(".env")
    load_dotenv(env_path)


# ----------------------------
# helpers
# ----------------------------



def build_action_prompt(
    *,
    country_display: str,
    metrics: Dict[str, Any],
    eu_state: Dict[str, Any],
    external_events: List[Dict[str, Any]],
    recent_actions_summary: str,
    domestic_headline: str
) -> str:
    external_str = format_external_events(external_events)

    return f"""
Du bist ein Spielleiter in einem EU-Geopolitik-Spiel.
Erzeuge drei öffentliche Aktionsoptionen für {country_display}: aggressiv, moderate, passiv.

Kontext:
- {country_display} Metriken: Militär={metrics["military"]}, Stabilität={metrics["stability"]}, Wirtschaft={metrics["economy"]}, Diplomatie={metrics["diplomatic_influence"]}, Öffentliche Zustimmung={metrics["public_approval"]}.
- Ambition: {metrics["ambition"]}.

EU-/Weltlage:
- EU-Kohäsion={eu_state["cohesion"]}%
- Threat Level={eu_state["threat_level"]}/100, Frontline Pressure={eu_state["frontline_pressure"]}/100
- Energy={eu_state["energy_pressure"]}/100, Migration={eu_state["migration_pressure"]}/100
- Disinfo={eu_state["disinfo_pressure"]}/100, TradeWar={eu_state["trade_war_pressure"]}/100
- Globaler Kontext: {eu_state["global_context"]}

Außenmächte-Moves dieser Runde:
{external_str}

Innenpolitisches Event (diese Runde):
- {domestic_headline}


Letzte Aktionen (für Variation, nicht wiederholen):
{recent_actions_summary}

Format:
Gib NUR gültiges JSON zurück (kein Markdown, keine Erklärungen).
Schema (genau so):
{{
  "aggressiv": {{
    "aktion": "...",
    "folgen": {{
      "land": {{"militär": 0, "stabilität": 0, "wirtschaft": 0, "diplomatie": 0, "öffentliche_zustimmung": 0}},
      "eu": {{"kohäsion": 0}},
      "global_context": "kurzer Satz zur Reaktion"
    }}
  }},
  "moderate": {{ ... }},
  "passiv": {{ ... }}
}}

Regeln:
- Folgen sind kleine, realistische Ganzzahlen (z.B. -12 bis +12).
- global_context ist ein kurzer Satz (max. 1 Zeile).
- Die drei Optionen sollen sich klar unterscheiden (Risiko/Ertrag).
- Baue öfter Sicherheitsdruck, innenpolitische Gegenreaktionen und diplomatische Deals ein.
""".strip()


def apply_external_modifiers_to_eu(eu_before: Dict[str, Any], moves_obj: Dict[str, Any]) -> Dict[str, Any]:
    eu = dict(eu_before)
    moves = moves_obj.get("moves", [])

    d_coh = d_threat = d_front = d_energy = d_migr = d_disinfo = d_trade = 0
    for m in moves:
        mods = m.get("modifiers", {}) or {}
        d_coh += int(mods.get("eu_cohesion_delta", 0))
        d_threat += int(mods.get("threat_delta", 0))
        d_front += int(mods.get("frontline_delta", 0))
        d_energy += int(mods.get("energy_delta", 0))
        d_migr += int(mods.get("migration_delta", 0))
        d_disinfo += int(mods.get("disinfo_delta", 0))
        d_trade += int(mods.get("trade_war_delta", 0))

    eu["cohesion"] = eu["cohesion"] + d_coh
    eu["threat_level"] = eu["threat_level"] + d_threat
    eu["frontline_pressure"] = eu["frontline_pressure"] + d_front
    eu["energy_pressure"] = eu["energy_pressure"] + d_energy
    eu["migration_pressure"] = eu["migration_pressure"] + d_migr
    eu["disinfo_pressure"] = eu["disinfo_pressure"] + d_disinfo
    eu["trade_war_pressure"] = eu["trade_war_pressure"] + d_trade

    if moves_obj.get("global_context"):
        eu["global_context"] = str(moves_obj["global_context"])

    return eu


def decay_pressures(eu: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(eu)
    out["threat_level"] = out["threat_level"] - 2
    out["frontline_pressure"] = out["frontline_pressure"] - 2
    out["energy_pressure"] = out["energy_pressure"] - 3
    out["migration_pressure"] = out["migration_pressure"] - 3
    out["disinfo_pressure"] = out["disinfo_pressure"] - 3
    out["trade_war_pressure"] = out["trade_war_pressure"] - 3
    return out


# ----------------------------
# UI helpers: compact list rows + tooltips
# ----------------------------



# ----------------------------
# App start
# ----------------------------
st.title("yourope - save europe, save yourself")

load_env()

api_key = (os.getenv("MISTRAL_API_KEY") or "").strip()
if not api_key:
    st.error("MISTRAL_API_KEY fehlt. Lege eine .env neben app.py an: MISTRAL_API_KEY=... ")
    st.stop()

gm_pin = (os.getenv("GM_PIN") or "").strip()

conn = get_conn()
ensure_schema(conn)
seed_countries_if_missing(conn, COUNTRY_DEFS)

countries = list(COUNTRY_DEFS.keys())
countries_display = {k: COUNTRY_DEFS[k]["display_name"] for k in countries}

# ----------------------------
# Auth gate
# ----------------------------
if "auth" not in st.session_state:
    st.session_state.auth = None

if st.session_state.auth is None:
    st.subheader("🔐 Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Passwort", type="password")
        submitted = st.form_submit_button("Einloggen")

    if submitted:
        user = verify_user(conn, username=username, password=password)
        if not user:
            st.error("Login fehlgeschlagen.")
        else:
            st.session_state.auth = user
            st.rerun()

    st.info("Bitte einloggen. (User werden vom Game Master erstellt.)")
    conn.close()
    st.stop()

auth = st.session_state.auth
is_gm = auth["role"] == "gm"
assigned_country = auth.get("country")

st.sidebar.write(f"👤 **{auth['username']}**")
st.sidebar.write(f"Rolle: **{auth['role']}**")
if assigned_country:
    st.sidebar.write(f"Land: **{assigned_country}**")

if st.sidebar.button("🚪 Logout"):
    st.session_state.auth = None
    st.rerun()

if is_gm and gm_pin:
    entered = st.sidebar.text_input("GM PIN", type="password")
    if entered != gm_pin:
        st.sidebar.warning("PIN erforderlich.")
        conn.close()
        st.stop()

# GM: Spieleransicht simulieren
if "gm_view_enabled" not in st.session_state:
    st.session_state.gm_view_enabled = False
if "gm_view_country" not in st.session_state:
    st.session_state.gm_view_country = None

if is_gm:
    with st.sidebar.expander("🕵️ Spieleransicht simulieren", expanded=False):
        st.session_state.gm_view_enabled = st.checkbox(
            "Spieleransicht aktivieren",
            value=st.session_state.gm_view_enabled,
        )
        if st.session_state.gm_view_enabled:
            opts = list(COUNTRY_DEFS.keys())
            if st.session_state.gm_view_country not in opts:
                st.session_state.gm_view_country = opts[0]
            st.session_state.gm_view_country = st.selectbox(
                "Als Spieler ansehen (Land)",
                options=opts,
                index=opts.index(st.session_state.gm_view_country),
            )
        else:
            st.session_state.gm_view_country = None

effective_country = None
is_simulating_player_view = False
if is_gm and st.session_state.get("gm_view_enabled") and st.session_state.get("gm_view_country"):
    effective_country = st.session_state.gm_view_country
    is_simulating_player_view = True
elif not is_gm:
    effective_country = assigned_country

if not is_gm and not effective_country:
    st.error("Kein Land zugewiesen. GM muss dir ein Land zuweisen.")
    conn.close()
    st.stop()

# ----------------------------
# DB states
# ----------------------------
meta = get_game_meta(conn)
round_no = meta["round"]
phase = meta["phase"]
winner_country = meta.get("winner_country")
winner_round = meta.get("winner_round")

eu = get_eu_state(conn)
if not eu["global_context"]:
    set_eu_state(
        conn,
        cohesion=EU_DEFAULT.get("cohesion", eu["cohesion"]),
        global_context=EU_DEFAULT.get("global_context", ""),
        threat_level=eu["threat_level"],
        frontline_pressure=eu["frontline_pressure"],
        energy_pressure=eu["energy_pressure"],
        migration_pressure=eu["migration_pressure"],
        disinfo_pressure=eu["disinfo_pressure"],
        trade_war_pressure=eu["trade_war_pressure"],
    )
    eu = get_eu_state(conn)

# ----------------------------
# Sidebar: Rundenstatus
# ----------------------------
with st.sidebar.expander("📊 Rundenstatus", expanded=False):
    st.write(f"**Runde:** {round_no}  |  **Phase:** {phase}")
    if phase == "game_over" and winner_country:
        st.success(f"🏆 Gewinner: {countries_display.get(winner_country, winner_country)} (R{winner_round})")

    locks = get_locks(conn, round_no)
    st.write("**Lock-Status (diese Runde)**")
    for c in countries:
        name = countries_display[c]
        if c in locks:
            if is_gm:
                st.success(f"{name}: ✅ eingelockt ({locks[c]})")
            else:
                st.success(f"{name}: ✅ eingelockt")
        else:
            st.warning(f"{name}: ⏳ nicht eingelockt")


# ----------------------------
# GM: User management
# ----------------------------
if is_gm:
    with st.sidebar.expander("👥 User verwalten", expanded=False):
        st.caption("User in SQLite. Passwörter: PBKDF2 + Salt + Pepper (.env).")
        with st.form("create_user_form"):
            new_u = st.text_input("Neuer Username")
            new_p = st.text_input("Neues Passwort", type="password")
            new_role = st.selectbox("Rolle", ["player", "gm"], index=0)
            new_country = None
            if new_role == "player":
                new_country = st.selectbox("Land zuweisen", list(COUNTRY_DEFS.keys()))
            submitted = st.form_submit_button("User anlegen/aktualisieren")
        if submitted:
            try:
                create_user(conn, username=new_u, password=new_p, role=new_role, country=new_country)
                st.success("User gespeichert.")
                st.rerun()
            except Exception as e:
                st.error(f"Fehler: {e}")

        st.write("---")
        st.write("**Bestehende User**")
        for u in list_users(conn):
            st.write(f"- {u['username']} ({u['role']}) {('→ ' + u['country']) if u['country'] else ''}")

        del_u = st.text_input("Username löschen")
        if st.button("User löschen"):
            delete_user(conn, del_u)
            st.success("Gelöscht.")
            st.rerun()

# ----------------------------
# Sidebar: reset (GM only)
# ----------------------------
if is_gm:
    st.sidebar.write("---")
    st.sidebar.subheader("Reset")
    if st.sidebar.button("💣 Reset alle"):
        reset_all_countries(conn, COUNTRY_DEFS)
        clear_all_round_summaries(conn)
        clear_country_snapshots(conn)
        clear_game_over(conn)

        clear_all_events_and_history(conn)  # <-- neu, statt loop

        set_eu_state(
            conn,
            cohesion=EU_DEFAULT.get("cohesion", 75),
            global_context=EU_DEFAULT.get("global_context", ""),
            threat_level=35,
            frontline_pressure=30,
            energy_pressure=25,
            migration_pressure=25,
            disinfo_pressure=25,
            trade_war_pressure=25,
        )
        set_game_meta(conn, 1, "setup")
        st.rerun()


# ----------------------------
# Layout: Center + Right
# ----------------------------
center, right = st.columns([0.66, 0.34], gap="large")
panel_country = effective_country if effective_country else assigned_country

# ----------------------------
# RIGHT: Eigene Werte → EU & Druckwerte → Siegfortschritt
# ----------------------------
if st.sidebar.button("🔄 Aktualisieren"):
    st.rerun()

with right:
    my_metrics = None
    if panel_country:
        my_metrics = load_country_metrics(conn, panel_country)
        if my_metrics:
            render_my_metrics_panel(my_metrics, countries_display[panel_country])
        else:
            st.warning("Eigene Länderwerte konnten nicht geladen werden.")
    else:
        st.info("Kein Land aktiv.")

    st.write("---")

    st.subheader("🇪🇺 EU & Druckwerte")
    metric_with_info("EU Kohäsion", f"{eu['cohesion']}%", VALUE_HELP["EU Kohäsion"])

    compact_kv("Threat", f"{eu['threat_level']}/100", VALUE_HELP["Threat"])
    compact_kv("Frontline", f"{eu['frontline_pressure']}/100", VALUE_HELP["Frontline"])
    compact_kv("Energy", f"{eu['energy_pressure']}/100", VALUE_HELP["Energy"])
    compact_kv("Migration", f"{eu['migration_pressure']}/100", VALUE_HELP["Migration"])
    with st.expander("Mehr Details (Druckwerte)", expanded=False):
        compact_kv("Disinfo", f"{eu['disinfo_pressure']}/100", VALUE_HELP["Disinfo"])
        compact_kv("TradeWar", f"{eu['trade_war_pressure']}/100", VALUE_HELP["TradeWar"])

    st.write("---")

    st.subheader("🏁 Siegfortschritt")
    if not panel_country or not my_metrics:
        st.caption("Siegfortschritt wird angezeigt, sobald ein Land aktiv ist.")
    elif evaluate_country_win_conditions is None:
        st.caption("Siegbedingungen-Modul nicht geladen.")
    else:
        eu_now = get_eu_state(conn)
        is_winner, cond_results = evaluate_country_win_conditions(
            panel_country,
            country_metrics=my_metrics,
            eu_state=eu_now,
            country_defs=COUNTRY_DEFS,
        )
        if not cond_results:
            st.warning("Für dieses Land sind noch keine Siegbedingungen definiert (countries.py: win_conditions).")
        else:
            prog = _progress_from_conditions(cond_results)
            st.progress(int(prog))
            st.caption(f"{prog:.0f}% der Siegbedingungen erfüllt.")
            if is_winner:
                st.success("✅ Siegbedingungen erfüllt! Du hast gewonnen.")
            for r in cond_results:
                st.write(("✅ " if r.ok else "❌ ") + f"{r.label} (aktuell: {r.current})")

# ----------------------------
# CENTER: Game Over Banner + News + Dashboard + Actions
# ----------------------------
with center:
    if phase == "game_over":
        if winner_country:
            st.success(f"🏆 GAME OVER — Gewinner: {countries_display.get(winner_country, winner_country)} (Runde {winner_round})")
        else:
            st.success("🏁 GAME OVER")
        st.balloons()
        st.write("---")

    if panel_country:
        render_news_panel(
            conn,
            round_no=round_no,
            eu=eu,
            countries=countries,
            countries_display=countries_display,
            my_country=panel_country,
        )
        st.write("---")
     # --- NEU: Runden-Historie (Außenmächte + Länderaktionen) ---
    with st.expander("🕰️ Runden-Historie (Außenmächte + Innenpolitik + Aktionen)", expanded=False):
        # Welche Runden existieren? (aus turn_history UND external_events)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT round FROM turn_history ORDER BY round DESC")
        rounds_from_turns = [int(r[0]) for r in cur.fetchall()]

        cur.execute("SELECT DISTINCT round FROM external_events ORDER BY round DESC")
        rounds_from_external = [int(r[0]) for r in cur.fetchall()]

        cur.execute("SELECT DISTINCT round FROM domestic_events ORDER BY round DESC")
        rounds_from_domestic = [int(r[0]) for r in cur.fetchall()]

        all_rounds = sorted(set(rounds_from_turns + rounds_from_external + rounds_from_domestic), reverse=True)


        if not all_rounds:
            st.caption("Noch keine Historie vorhanden.")
        else:
            # Optional: kompakt zuerst die neueste Runde anzeigen
            for r in all_rounds:
                with st.expander(f"Runde {r}", expanded=(r == all_rounds[0])):
                    # 1) Außenmächte dieser Runde
                    ext_events_r = get_external_events(conn, r)
                    if ext_events_r:
                        st.markdown("**🌐 Außenmächte**")
                        for e in ext_events_r:
                            c = int(e.get("craziness", 0) or 0)
                            st.markdown(f"- **{e['actor']}** (🎲 {c}/100): {e['headline']}")
                            q = (e.get("quote") or "").strip()
                            if q and q != "—":
                                st.caption(f"🗣️ {q}")
                    else:
                        st.caption("Keine Außenmächte-Moves für diese Runde.")

                    st.write("---")

                    # 2) Aktionen der Länder dieser Runde (aus turn_history)
                    st.markdown("**🏛️ Länderaktionen**")
                    cur.execute(
                        """
                        SELECT country, action_public, global_context
                        FROM turn_history
                        WHERE round = ?
                        ORDER BY country ASC
                        """,
                        (int(r),),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        st.caption("Keine Länderaktionen gespeichert (evtl. Runde noch nicht resolved).")
                    else:
                        for country, action_public, global_context in rows:
                            name = countries_display.get(country, country)
                            st.markdown(f"**{name}**")
                            st.write(action_public)
                            if global_context:
                                st.caption(f"Kontext: {global_context}")
    

    with st.expander("📊 Dashboard (öffentlich)", expanded=(phase == "game_over")):
        render_public_dashboard(conn, countries=countries, countries_display=countries_display)

    st.write("---")

    if is_gm:
        st.subheader("🎮 Spieleransicht (GM Simulation)")
        if not is_simulating_player_view or not effective_country:
            st.info("Aktiviere in der Sidebar 'Spieleransicht simulieren' und wähle ein Land.")
        else:
            render_player_view(
                conn=conn,
                round_no=round_no,
                phase=phase,
                eu=eu,
                countries_display=countries_display,
                my_country=effective_country,
                is_lock_disabled=False,
                is_gm=is_gm,
            )
    else:
        st.subheader("🎮 Aktionen")
        render_player_view(
            conn=conn,
            round_no=round_no,
            phase=phase,
            eu=eu,
            countries_display=countries_display,
            my_country=effective_country,
            is_lock_disabled=False,
            is_gm=is_gm,
        )

# ----------------------------
# GM controls
# ----------------------------
with right:
    if is_gm:
        st.write("---")
        with st.expander("🎛️ Game Master Steuerung (sequenziell)", expanded=False):
            actions_in_db = get_round_actions(conn, round_no)
            have_all_actions = all((c in actions_in_db and len(actions_in_db[c]) == 3) for c in countries)
            have_all_locks = all_locked(conn, round_no, countries)
            have_external = len(get_external_events(conn, round_no)) == 3

            if phase == "game_over":
                st.warning("Game Over – nur Reset möglich.")
                st.stop()

            # 1) External moves
            external_disabled = (phase == "actions_published")
            if st.button(
                "⚠️ Außenmächte-Moves und Innenpolitik-Headlines generieren",
                disabled=external_disabled,
                use_container_width=True,
            ):
                with st.spinner("Generiere Außenmächte-Moves..."):
                    recent_summaries = get_recent_round_summaries(conn, limit=3)
                    eu_before = get_eu_state(conn)

                    # --- NEW: Crazy pro Runde würfeln (mit Baselines) ---
                    usa_min, usa_max = EXTERNAL_CRAZY_BASELINE_RANGES["USA"]
                    rus_min, rus_max = EXTERNAL_CRAZY_BASELINE_RANGES["Russia"]
                    chi_min, chi_max = EXTERNAL_CRAZY_BASELINE_RANGES["China"]

                    craziness_by_actor = {
                        "USA": random.randint(usa_min, usa_max),
                        "Russia": random.randint(rus_min, rus_max),
                        "China": random.randint(chi_min, chi_max),
                    }

                    moves_obj = generate_external_moves(
                        api_key=api_key,
                        model="mistral-small",
                        round_no=round_no,
                        eu_state=eu_before,
                        recent_round_summaries=recent_summaries,
                        craziness_by_actor=craziness_by_actor,
                        temperature=0.8,
                        top_p=0.95,
                        max_tokens=1200,
                    )

                    clear_external_events(conn, round_no)

                    for m in moves_obj["moves"]:
                        upsert_external_event(
                            conn,
                            round_no,
                            actor=m["actor"],
                            headline=m["headline"],
                            modifiers=m.get("modifiers", {}),
                            quote=m.get("quote", ""),
                            craziness=int(m.get("craziness", 0) or 0),
                        )

                    eu_after = apply_external_modifiers_to_eu(eu_before, moves_obj)
                    set_eu_state(
                        conn,
                        cohesion=eu_after["cohesion"],
                        global_context=eu_after["global_context"],
                        threat_level=eu_after["threat_level"],
                        frontline_pressure=eu_after["frontline_pressure"],
                        energy_pressure=eu_after["energy_pressure"],
                        migration_pressure=eu_after["migration_pressure"],
                        disinfo_pressure=eu_after["disinfo_pressure"],
                        trade_war_pressure=eu_after["trade_war_pressure"],
                    )
                                    # --- NEU: Domestic headlines generieren & speichern ---
                    clear_domestic_events(conn, round_no)

                    all_metrics = load_all_country_metrics(conn, countries)

                    # recent actions je Land aus turn_history (kurz)
                    recent_actions_by_country = {}
                    for c in countries:
                        recent = load_recent_history(conn, c, limit=6)
                        recent_actions_by_country[c] = [r[1] for r in recent if r and r[1]]

                    dom_obj = generate_domestic_events(
                        api_key=api_key,
                        model="mistral-small",
                        round_no=round_no,
                        eu_state=eu_after,  # nutze updated EU state nach external modifiers
                        countries=countries,
                        countries_metrics=all_metrics,
                        recent_round_summaries=recent_summaries,
                        recent_actions_by_country=recent_actions_by_country,
                        temperature=0.85,
                        top_p=0.95,
                        max_tokens=1400,
                    )

                    for c in countries:
                        e = (dom_obj.get("events", {}) or {}).get(c, {}) or {}
                        upsert_domestic_event(
                            conn,
                            round_no,
                            c,
                            e.get("headline", ""),
                            details=e.get("details", ""),
                            craziness=int(e.get("craziness", 0) or 0),
                        )

                    set_game_meta(conn, round_no, "external_generated")
                st.rerun()

            # 2) Generate actions for all
            gen_disabled = not (phase in ("external_generated", "actions_generated") and have_external) or (phase == "actions_published")
            if st.button("⚙️ Aktionen für alle generieren", disabled=gen_disabled, use_container_width=True):
                with st.spinner("Generiere Aktionen für alle Länder..."):
                    eu_now = get_eu_state(conn)
                    ext_now = get_external_events(conn, round_no)

                    all_metrics = load_all_country_metrics(conn, countries)
                    dom_map = {e["country"]: e for e in get_domestic_events(conn, round_no)}
                    for c in countries:
                        m = all_metrics[c]
                        recent = load_recent_history(conn, c, limit=12)
                        domestic_headline = (dom_map.get(c, {}) or {}).get("headline", "Keine auffälligen Ereignisse gemeldet.")
                        prompt = build_action_prompt(
                            country_display=countries_display[c],
                            metrics=m,
                            eu_state=eu_now,
                            external_events=ext_now,
                            recent_actions_summary=summarize_recent_actions(recent),
                            domestic_headline=domestic_headline,
                        )

                        actions_obj, _raw_first, _used_repair = generate_actions_for_country(
                            api_key=api_key,
                            model="mistral-small",
                            prompt=prompt,
                            temperature=0.9,
                            top_p=0.95,
                            max_tokens=900,
                        )
                        upsert_round_actions(conn, round_no, c, actions_obj)

                    set_game_meta(conn, round_no, "actions_generated")
                st.rerun()

            # 3) Publish
            publish_disabled = not (phase == "actions_generated" and have_all_actions and have_external)
            if st.button("🚦 Runde starten (Optionen veröffentlichen)", disabled=publish_disabled, use_container_width=True):
                set_game_meta(conn, round_no, "actions_published")
                st.rerun()

            # 4) Resolve
            resolve_disabled = not (phase == "actions_published" and have_all_locks)
            if st.button("🧮 Ergebnis der Runde kalkulieren", disabled=resolve_disabled, use_container_width=True):
                with st.spinner("KI kalkuliert Gesamtergebnis der Runde..."):
                    recent_summaries = get_recent_round_summaries(conn, limit=3)
                    eu_before = get_eu_state(conn)
                    ext_now = get_external_events(conn, round_no)
                    dom_now = get_domestic_events(conn, round_no)
                    actions_texts = get_round_actions(conn, round_no)
                    locks_now = get_locks(conn, round_no)
                    all_metrics = load_all_country_metrics(conn, countries)

                    chosen_actions_lines = []
                    for c in countries:
                        v = locks_now[c]
                        chosen_actions_lines.append(f"- {countries_display[c]} ({c}): {v} -> {actions_texts[c][v]}")
                    chosen_actions_str = "\n".join(chosen_actions_lines)

                    result = resolve_round_all_countries(
                        api_key=api_key,
                        model="mistral-small",
                        round_no=round_no,
                        eu_state=eu_before,
                        countries_metrics=all_metrics,
                        countries_display=countries_display,
                        actions_texts=actions_texts,
                        locked_choices=locks_now,
                        recent_round_summaries=recent_summaries,
                        external_events=ext_now,
                        domestic_events=dom_now,
                        temperature=0.6,
                        top_p=0.95,
                        max_tokens=1700,
                    )

                    eu_after = dict(eu_before)
                    eu_after["cohesion"] = eu_before["cohesion"] + int(result["eu"].get("kohäsion_delta", 0))
                    eu_after["global_context"] = str(result["eu"].get("global_context", eu_before["global_context"]))
                    eu_after = decay_pressures(eu_after)

                    set_eu_state(
                        conn,
                        cohesion=eu_after["cohesion"],
                        global_context=eu_after["global_context"],
                        threat_level=eu_after["threat_level"],
                        frontline_pressure=eu_after["frontline_pressure"],
                        energy_pressure=eu_after["energy_pressure"],
                        migration_pressure=eu_after["migration_pressure"],
                        disinfo_pressure=eu_after["disinfo_pressure"],
                        trade_war_pressure=eu_after["trade_war_pressure"],
                    )
                    all_metrics_before = load_all_country_metrics(conn, countries)
                    eu_before_for_progress = get_eu_state(conn)

                    max_snap = get_max_snapshot_round(conn)
                    need_baseline = (max_snap is None) and (round_no >= 1)

                    if need_baseline:
                        if evaluate_all_countries is not None:
                            win_eval_before = evaluate_all_countries(
                                all_country_metrics=all_metrics_before,
                                eu_state=eu_before_for_progress,
                                country_defs=COUNTRY_DEFS,
                            )
                            for c in countries:
                                res = win_eval_before.get(c, {})
                                progress_before = _progress_from_conditions(res.get("results") or [])
                                upsert_country_snapshot(
                                    conn,
                                    round_no=round_no - 1,
                                    country=c,
                                    metrics=all_metrics_before[c],
                                    victory_progress=progress_before,
                                    is_winner=bool(res.get("is_winner")),
                                )
                        else:
                            for c in countries:
                                upsert_country_snapshot(
                                    conn,
                                    round_no=round_no - 1,
                                    country=c,
                                    metrics=all_metrics_before[c],
                                    victory_progress=0.0,
                                    is_winner=False,
                                )

                    for c in countries:
                        d = result["länder"][c] or {}
                        apply_country_deltas(conn, c, d)

                        chosen_variant = locks_now[c]
                        chosen_action_text = actions_texts[c][chosen_variant]
                        insert_turn_history(
                            conn,
                            country=c,
                            round_no=round_no,
                            action_public=chosen_action_text,
                            global_context=eu_after["global_context"],
                            deltas=d,
                        )

                    eu_after_fresh = get_eu_state(conn)

                    summary_text = generate_round_summary(
                        api_key=api_key,
                        model="mistral-small",
                        round_no=round_no,
                        memory_in=recent_summaries,
                        eu_before=eu_before,
                        eu_after=eu_after_fresh,
                        external_events=ext_now,
                        domestic_events=dom_now,
                        chosen_actions_str=chosen_actions_str,
                        result_obj=result,
                        temperature=0.4,
                        top_p=0.95,
                        max_tokens=520,
                    )
                    upsert_round_summary(conn, round_no, summary_text)

                    winners: List[str] = []
                    all_metrics_now = load_all_country_metrics(conn, countries)
                    eu_now = get_eu_state(conn)

                    if evaluate_all_countries is not None:
                        win_eval = evaluate_all_countries(
                            all_country_metrics=all_metrics_now,
                            eu_state=eu_now,
                            country_defs=COUNTRY_DEFS,
                        )
                        for c in countries:
                            res = win_eval.get(c, {})
                            is_winner_now = bool(res.get("is_winner"))
                            progress = _progress_from_conditions(res.get("results") or [])
                            upsert_country_snapshot(
                                conn,
                                round_no=round_no,
                                country=c,
                                metrics=all_metrics_now[c],
                                victory_progress=progress,
                                is_winner=is_winner_now,
                            )
                            if is_winner_now:
                                winners.append(c)
                    else:
                        for c in countries:
                            upsert_country_snapshot(
                                conn,
                                round_no=round_no,
                                country=c,
                                metrics=all_metrics_now[c],
                                victory_progress=0.0,
                                is_winner=False,
                            )

                    clear_round_data(conn, round_no)
                    # clear_external_events(conn, round_no)

                    if winners:
                        set_game_over(conn, winner_country=winners[0], winner_round=round_no, reason="win_conditions")
                    else:
                        set_game_meta(conn, round_no + 1, "setup")

                st.success("Runde aufgelöst.")
                st.rerun()

            st.caption("Flow: Außenmächte → Aktionen generieren → Veröffentlichen → Lock → Resolve")

conn.close()
