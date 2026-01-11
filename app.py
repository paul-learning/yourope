# app.py
import os
from pathlib import Path
from typing import Dict, Any

import streamlit as st
from dotenv import load_dotenv
from mistralai import Mistral

from countries import COUNTRY_DEFS, EU_DEFAULT
from db import (
    get_conn,
    ensure_schema,
    seed_countries_if_missing,
    load_country_metrics,
    update_country_metrics,
    insert_turn_history,
    load_recent_history,
    reset_country_to_defaults,
    reset_all_countries,
)

from utils import content_to_text, parse_json_maybe


# ----------------------------
# Env + Session
# ----------------------------
def load_env():
    env_path = Path(__file__).with_name(".env")
    load_dotenv(env_path)


def init_game_state():
    st.session_state.game = {
        "round": 1,
        "current_country": "Germany",
        "eu": {
            "cohesion": EU_DEFAULT["cohesion"],
            "global_context": EU_DEFAULT["global_context"],
        },
        # actions pro Land getrennt (damit Switch nicht nervt)
        "actions_by_country": {},  # {country: actions_json}
        # Debug: letzte Rohantwort der KI pro Land
        "last_ai_raw_by_country": {},  # {country: raw_text}
        "last_ai_prompt_by_country": {},  # {country: prompt_text}
        "last_ai_used_repair_by_country": {},  # {country: bool}
    }


def summarize_recent_actions(rows) -> str:
    if not rows:
        return "Keine."
    items = []
    for r in rows[:6]:
        items.append(f"R{r[0]}: {r[1]}")
    return " | ".join(items)


# ----------------------------
# Prompt + AI (inline Fix + Debug)
# ----------------------------
def build_action_prompt(
    *,
    country_display: str,
    metrics: Dict[str, Any],
    eu_cohesion: int,
    global_context: str,
    recent_actions_summary: str,
) -> str:
    return f"""
Du bist ein Spielleiter in einem EU-Geopolitik-Spiel.
Erzeuge drei öffentliche Aktionsoptionen für {country_display}: aggressiv, moderate, passiv.

Kontext:
- {country_display} Metriken: Militär={metrics["military"]}, Stabilität={metrics["stability"]}, Wirtschaft={metrics["economy"]}, Diplomatie={metrics["diplomatic_influence"]}, Öffentliche Zustimmung={metrics["public_approval"]}.
- Ambition: {metrics["ambition"]}.
- EU: Kohäsion={eu_cohesion}%.
- Globaler Kontext: {global_context}
- Letzte Aktionen (für Variation, nicht wiederholen): {recent_actions_summary}

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
- Folgen sind kleine, realistische Ganzzahlen (z.B. -10 bis +10).
- global_context ist ein kurzer Satz (max. 1 Zeile).
- Die drei Optionen sollen sich klar unterscheiden (Risiko/Ertrag).
- Vermeide wiederkehrende Standardfloskeln; sei spezifisch zum Land und Kontext.
""".strip()


def _chat(client: Mistral, model: str, messages, temperature: float, top_p: float, max_tokens: int) -> str:
    resp = client.chat.complete(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
    )
    return content_to_text(resp.choices[0].message.content)


def _repair_to_valid_json(client: Mistral, model: str, bad_text: str, max_tokens: int = 1200) -> Dict[str, Any]:
    repair_prompt = f"""
Du bist ein Validator/Formatter. Wandle die folgende Ausgabe in **gültiges JSON** um.

Wichtig:
- Gib **NUR** JSON zurück (keine Erklärungen, kein Markdown).
- Nutze **nur** doppelte Anführungszeichen.
- Keine trailing commas.
- Schema MUSS exakt passen:
{{
  "aggressiv": {{
    "aktion": "...",
    "folgen": {{
      "land": {{"militär": 0, "stabilität": 0, "wirtschaft": 0, "diplomatie": 0, "öffentliche_zustimmung": 0}},
      "eu": {{"kohäsion": 0}},
      "global_context": "..."
    }}
  }},
  "moderate": {{ ... }},
  "passiv": {{ ... }}
}}

Hier ist die zu reparierende Ausgabe:
{bad_text}
""".strip()

    fixed_raw = _chat(
        client,
        model,
        messages=[
            {"role": "system", "content": "Du gibst ausschließlich gültiges JSON zurück. Kein Markdown."},
            {"role": "user", "content": repair_prompt},
        ],
        temperature=0.2,
        top_p=1.0,
        max_tokens=max_tokens,
    )
    return parse_json_maybe(fixed_raw)


def generate_actions_with_repair_and_debug(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float = 0.9,
    top_p: float = 0.95,
    max_tokens: int = 900,
) -> tuple[Dict[str, Any], str, bool]:
    """
    Returns: (actions_obj, raw_text_first_call, used_repair)
    """
    client = Mistral(api_key=api_key)

    raw = _chat(
        client,
        model,
        messages=[
            {"role": "system", "content": "Antworte ausschließlich mit gültigem JSON. Kein Markdown."},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
    )

    used_repair = False
    try:
        obj = parse_json_maybe(raw)
    except Exception:
        used_repair = True
        obj = _repair_to_valid_json(client, model, raw)

    # Minimalvalidierung
    for k in ("aggressiv", "moderate", "passiv"):
        if k not in obj:
            raise ValueError(f"Fehlender Key im JSON: {k}")
        if "aktion" not in obj[k] or "folgen" not in obj[k]:
            raise ValueError(f"Key '{k}' muss 'aktion' und 'folgen' enthalten.")
        folgen = obj[k].get("folgen") or {}
        if "land" not in folgen or "eu" not in folgen or "global_context" not in folgen:
            raise ValueError(f"'{k}.folgen' muss land/eu/global_context enthalten.")

    return obj, raw, used_repair


# ----------------------------
# App start
# ----------------------------
st.set_page_config(page_title="EU Geopolitik (Multi-Country)", layout="centered")
st.title("EU Geopolitik-Prototyp (5 Länder)")

load_env()
api_key = (os.getenv("MISTRAL_API_KEY") or "").strip()

conn = get_conn()
ensure_schema(conn)
seed_countries_if_missing(conn, COUNTRY_DEFS)

if "game" not in st.session_state:
    init_game_state()

game = st.session_state.game

# Sidebar controls
st.sidebar.header("Steuerung")

# Land wählen (Dropdown zeigt display_name, intern bleibt Key wie "Germany")
country_keys = list(COUNTRY_DEFS.keys())
country_labels = [COUNTRY_DEFS[k]["display_name"] for k in country_keys]

# aktuellen Index über den internen Key bestimmen
current_idx = country_keys.index(game["current_country"]) if game["current_country"] in COUNTRY_DEFS else 0

selected_label = st.sidebar.selectbox(
    "Land auswählen",
    country_labels,
    index=current_idx,
)

# zurück mappen auf internen Key
country = country_keys[country_labels.index(selected_label)]
game["current_country"] = country
country_display = COUNTRY_DEFS[country]["display_name"]


st.sidebar.write(f"Runde: **{game['round']}**")
st.sidebar.write(f"EU-Kohäsion: **{game['eu']['cohesion']}%**")
st.sidebar.caption(game["eu"]["global_context"])

st.sidebar.write("---")

col_a, col_b = st.sidebar.columns(2)
if col_a.button("🔄 Reset Land"):
    reset_country_to_defaults(conn, country, COUNTRY_DEFS[country])
    game["actions_by_country"].pop(country, None)
    game["last_ai_raw_by_country"].pop(country, None)
    game["last_ai_prompt_by_country"].pop(country, None)
    game["last_ai_used_repair_by_country"].pop(country, None)
    st.rerun()

if col_b.button("💣 Reset alle"):
    reset_all_countries(conn, COUNTRY_DEFS)
    game["actions_by_country"] = {}
    game["last_ai_raw_by_country"] = {}
    game["last_ai_prompt_by_country"] = {}
    game["last_ai_used_repair_by_country"] = {}
    game["eu"]["cohesion"] = EU_DEFAULT["cohesion"]
    game["eu"]["global_context"] = EU_DEFAULT["global_context"]
    game["round"] = 1
    st.rerun()

st.write("---")

if not api_key:
    st.error("MISTRAL_API_KEY fehlt. Lege eine .env neben app.py an: MISTRAL_API_KEY=... ")
    st.stop()

# Load metrics
metrics = load_country_metrics(conn, country)
if not metrics:
    st.error(f"Konnte {country} nicht aus der DB laden.")
    st.stop()

# Display metrics
st.subheader(f"Aktuelle Metriken ({country_display})")
c1, c2, c3 = st.columns(3)
c1.metric("Wirtschaft", metrics["economy"])
c2.metric("Stabilität", metrics["stability"])
c3.metric("Militär", metrics["military"])

c4, c5 = st.columns(2)
c4.metric("Diplomatie", metrics["diplomatic_influence"])
c5.metric("Öffentliche Zustimmung", metrics["public_approval"])

with st.expander("Ambition"):
    st.write(metrics["ambition"])

st.write("---")

# ----------------------------
# Generate actions
# ----------------------------
st.subheader("Runde: Öffentliche Aktion")

recent_rows = load_recent_history(conn, country, limit=12)
recent_summary = summarize_recent_actions(recent_rows)

if st.button("Aktionen generieren"):
    with st.spinner("AI generiert Aktionen..."):
        prompt = build_action_prompt(
            country_display=country_display,
            metrics=metrics,
            eu_cohesion=int(game["eu"]["cohesion"]),
            global_context=str(game["eu"]["global_context"]),
            recent_actions_summary=recent_summary,
        )
        try:
            actions_obj, raw_first, used_repair = generate_actions_with_repair_and_debug(
                api_key=api_key,
                model="mistral-small",
                prompt=prompt,
                temperature=0.9,
                top_p=0.95,
                max_tokens=900,
            )
        except Exception as e:
            st.error(f"Aktionen konnten nicht generiert werden: {e}")
            st.info("Debug: Das liegt fast immer an ungültigem JSON (Quotes/Kommas/Text außerhalb des JSON).")
            # Wenn wir an dieser Stelle sind, gab es entweder totalen Murks oder auch Repair ist gescheitert.
            # Wir speichern trotzdem Prompt, damit man ihn sehen kann.
            game["last_ai_prompt_by_country"][country] = prompt
            st.stop()

        game["actions_by_country"][country] = actions_obj
        game["last_ai_raw_by_country"][country] = raw_first
        game["last_ai_prompt_by_country"][country] = prompt
        game["last_ai_used_repair_by_country"][country] = used_repair

        if used_repair:
            st.warning("Hinweis: Die KI-Ausgabe war nicht direkt valides JSON – wurde automatisch repariert.")
        st.success("Aktionen generiert.")

actions = game["actions_by_country"].get(country)

# Debug UI (zeigt letzten Prompt + Raw)
with st.expander("Debug: Letzter KI-Prompt & Rohantwort"):
    p = game["last_ai_prompt_by_country"].get(country)
    r = game["last_ai_raw_by_country"].get(country)
    used = game["last_ai_used_repair_by_country"].get(country)

    if p:
        st.caption("Letzter Prompt")
        st.code(p)
    else:
        st.caption("Noch kein Prompt für dieses Land.")

    if r:
        st.caption("Letzte Rohantwort (aus dem ersten KI-Call)")
        st.code(r)
        if used is True:
            st.caption("Auto-Repair wurde verwendet: JA")
        elif used is False:
            st.caption("Auto-Repair wurde verwendet: NEIN")
    else:
        st.caption("Noch keine Rohantwort für dieses Land.")

# ----------------------------
# Choose + apply action
# ----------------------------
if actions:
    st.subheader("Wähle eine Aktion")
    options = {
        "aggressiv": actions["aggressiv"]["aktion"],
        "moderate": actions["moderate"]["aktion"],
        "passiv": actions["passiv"]["aktion"],
    }

    selected_label = st.radio(
        "Aktion auswählen:",
        [options["aggressiv"], options["moderate"], options["passiv"]],
        index=1,
    )

    chosen_key = next(k for k, v in options.items() if v == selected_label)

    if st.button("Runde abschließen"):
        chosen = actions[chosen_key]
        folgen = chosen.get("folgen", {}) or {}
        land_delta = folgen.get("land", {}) or {}
        eu_delta = folgen.get("eu", {}) or {}
        new_global_context = folgen.get("global_context") or game["eu"]["global_context"]

        update_country_metrics(conn, country, land_delta)

        game["eu"]["cohesion"] = int(game["eu"]["cohesion"]) + int(eu_delta.get("kohäsion", 0))
        game["eu"]["cohesion"] = max(0, min(100, int(game["eu"]["cohesion"])))
        game["eu"]["global_context"] = str(new_global_context)

        insert_turn_history(
            conn,
            country=country,
            round_no=int(game["round"]),
            action_public=str(chosen.get("aktion", "")),
            global_context=str(game["eu"]["global_context"]),
            deltas=land_delta,
        )

        game["round"] += 1
        game["actions_by_country"].pop(country, None)

        st.success("Runde abgeschlossen! Neue Metriken gespeichert.")
        st.rerun()

st.write("---")

# ----------------------------
# Show history
# ----------------------------
with st.expander("Turn-History (Land)"):
    rows = load_recent_history(conn, country, limit=15)
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

conn.close()
