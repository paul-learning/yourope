import os
import re
import json
import sqlite3
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from mistralai import Mistral


# ----------------------------
# Helpers: dotenv, JSON parsing
# ----------------------------
def load_env():
    # Lädt .env aus dem gleichen Ordner wie game.py (robust für streamlit run von überall)
    env_path = Path(__file__).with_name(".env")
    load_dotenv(env_path)


def content_to_text(content) -> str:
    """mistralai SDK kann content als str oder Liste von Parts liefern."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            t = getattr(p, "text", None)
            if t:
                parts.append(t)
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return str(content)


def parse_json_maybe(text: str):
    """Parst JSON auch dann, wenn ```json ... ``` oder Text drumherum vorkommt."""
    s = (text or "").strip()
    if not s:
        raise ValueError("Leere Antwort vom Modell (kein JSON erhalten).")

    # Codefences entfernen
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)

    # Direkt versuchen
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Erstes JSON-Objekt/Array extrahieren
    m = re.search(r"(\{.*\}|\[.*\])", s, flags=re.DOTALL)
    if not m:
        raise ValueError(f"Kein JSON gefunden. Anfang der Antwort: {s[:200]!r}")
    return json.loads(m.group(1))


# ----------------------------
# DB setup / access
# ----------------------------
DB_PATH = "game.db"

GERMANY_DEFAULT = {
    "military": 70,
    "stability": 90,
    "economy": 95,
    "diplomatic_influence": 80,
    "public_approval": 75,
    "ambition": "AfD schwächen, EU führen, Energiewende vorantreiben",
}

EU_DEFAULT = {
    "cohesion": 75,
    "global_context": "Russland droht mit Gaskürzungen. USA drohen mit Übernahme Grönlands. China liebäugelt mit Invasion Taiwans.",
}


def get_conn():
    # check_same_thread=False ist bei Streamlit hilfreich (reruns)
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def ensure_schema(conn: sqlite3.Connection):
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS countries (
        name TEXT PRIMARY KEY,
        military INTEGER NOT NULL,
        stability INTEGER NOT NULL,
        economy INTEGER NOT NULL,
        diplomatic_influence INTEGER NOT NULL,
        public_approval INTEGER NOT NULL,
        ambition TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS turn_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP,
        country TEXT NOT NULL,
        round INTEGER NOT NULL,
        action_public TEXT NOT NULL,
        global_context TEXT NOT NULL,
        delta_military INTEGER NOT NULL,
        delta_stability INTEGER NOT NULL,
        delta_economy INTEGER NOT NULL,
        delta_diplomatic_influence INTEGER NOT NULL,
        delta_public_approval INTEGER NOT NULL
    )
    """)

    conn.commit()


def seed_germany_if_missing(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("SELECT name FROM countries WHERE name = ?", ("Germany",))
    if cur.fetchone() is None:
        cur.execute("""
        INSERT INTO countries (name, military, stability, economy, diplomatic_influence, public_approval, ambition)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "Germany",
            GERMANY_DEFAULT["military"],
            GERMANY_DEFAULT["stability"],
            GERMANY_DEFAULT["economy"],
            GERMANY_DEFAULT["diplomatic_influence"],
            GERMANY_DEFAULT["public_approval"],
            GERMANY_DEFAULT["ambition"],
        ))
        conn.commit()


def load_germany_metrics(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("SELECT name, military, stability, economy, diplomatic_influence, public_approval, ambition FROM countries WHERE name = ?", ("Germany",))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "name": row[0],
        "military": row[1],
        "stability": row[2],
        "economy": row[3],
        "diplomatic_influence": row[4],
        "public_approval": row[5],
        "ambition": row[6],
    }


def update_germany_metrics(conn: sqlite3.Connection, deltas: dict):
    cur = conn.cursor()
    cur.execute("""
    UPDATE countries SET
        military = military + ?,
        stability = stability + ?,
        economy = economy + ?,
        diplomatic_influence = diplomatic_influence + ?,
        public_approval = public_approval + ?
    WHERE name = ?
    """, (
        int(deltas.get("militär", 0)),
        int(deltas.get("stabilität", 0)),
        int(deltas.get("wirtschaft", 0)),
        int(deltas.get("diplomatie", 0)),
        int(deltas.get("öffentliche_zustimmung", 0)),
        "Germany",
    ))
    conn.commit()


def reset_db_to_start(conn: sqlite3.Connection):
    cur = conn.cursor()
    # metrics zurücksetzen
    cur.execute("""
    UPDATE countries SET
        military = ?,
        stability = ?,
        economy = ?,
        diplomatic_influence = ?,
        public_approval = ?,
        ambition = ?
    WHERE name = ?
    """, (
        GERMANY_DEFAULT["military"],
        GERMANY_DEFAULT["stability"],
        GERMANY_DEFAULT["economy"],
        GERMANY_DEFAULT["diplomatic_influence"],
        GERMANY_DEFAULT["public_approval"],
        GERMANY_DEFAULT["ambition"],
        "Germany",
    ))

    # history leeren
    cur.execute("DELETE FROM turn_history WHERE country = ?", ("Germany",))
    conn.commit()


# ----------------------------
# Game state
# ----------------------------
def init_game_state():
    st.session_state.game = {
        "round": 1,
        "eu": {
            "cohesion": EU_DEFAULT["cohesion"],
            "global_context": EU_DEFAULT["global_context"],
        },
        "actions": None,          # zuletzt generierte Aktionen (dict)
        "choice_key": None,       # "aggressiv"/"moderate"/"passiv"
    }


def reset_game(conn: sqlite3.Connection):
    init_game_state()
    reset_db_to_start(conn)


# ----------------------------
# App start
# ----------------------------
st.set_page_config(page_title="EU Geopolitik-Prototyp", layout="centered")
st.title("EU Geopolitik-Prototyp (Deutschland)")

load_env()
api_key = (os.getenv("MISTRAL_API_KEY") or "").strip()

conn = get_conn()
ensure_schema(conn)
seed_germany_if_missing(conn)

if "game" not in st.session_state:
    init_game_state()

game = st.session_state.game

# Sidebar controls
st.sidebar.header("Steuerung")

if st.sidebar.button("🔄 Spiel zurücksetzen (alles auf Anfang)"):
    reset_game(conn)
    st.rerun()

st.sidebar.write(f"Runde: **{game['round']}**")
st.sidebar.write(f"EU-Kohäsion: **{game['eu']['cohesion']}%**")
st.sidebar.caption(game["eu"]["global_context"])

st.write("---")

# Key check
if not api_key:
    st.error("MISTRAL_API_KEY fehlt. Lege eine .env neben game.py an: MISTRAL_API_KEY=... ")
    st.stop()

client = Mistral(api_key=api_key)

# Load metrics
metrics = load_germany_metrics(conn)
if not metrics:
    st.error("Konnte Germany nicht aus der DB laden.")
    st.stop()

# Display metrics
st.subheader("Aktuelle Metriken (Deutschland)")
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

if st.button("Aktionen generieren"):
    with st.spinner("AI generiert Aktionen..."):
        prompt = f"""
Du bist ein Spielleiter in einem EU-Geopolitik-Spiel.
Erzeuge drei öffentliche Aktionsoptionen für Deutschland (aggressiv, moderate, passiv).

Kontext:
- Deutschland Metriken: Militär={metrics["military"]}, Stabilität={metrics["stability"]}, Wirtschaft={metrics["economy"]}, Diplomatie={metrics["diplomatic_influence"]}, Öffentliche Zustimmung={metrics["public_approval"]}.
- Ambition: {metrics["ambition"]}.
- EU: Kohäsion={game["eu"]["cohesion"]}%.
- Globaler Kontext: {game["eu"]["global_context"]}

Format:
Gib NUR gültiges JSON zurück (kein Markdown, keine Erklärungen).
Schema (genau so):
{{
  "aggressiv": {{
    "aktion": "...",
    "folgen": {{
      "deutschland": {{"militär": 0, "stabilität": 0, "wirtschaft": 0, "diplomatie": 0, "öffentliche_zustimmung": 0}},
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
"""

        response = client.chat.complete(
            model="mistral-small",
            messages=[
                {"role": "system", "content": "Antworte ausschließlich mit gültigem JSON. Kein Markdown."},
                {"role": "user", "content": prompt},
            ],
            # wenn deine SDK-Version max_tokens nicht kennt, einfach entfernen
            max_tokens=900,
            temperature=0.9,
            top_p=0.95,
        )

        raw = content_to_text(response.choices[0].message.content)

        try:
            actions_obj = parse_json_maybe(raw)
        except Exception as e:
            st.error(f"JSON konnte nicht geparst werden: {e}")
            st.text("RAW (erste 800 Zeichen):")
            st.code(raw[:800])
            st.stop()

        # Minimal-Validierung
        for k in ("aggressiv", "moderate", "passiv"):
            if k not in actions_obj:
                st.error(f"Fehlender Key im JSON: {k}")
                st.json(actions_obj)
                st.stop()

        game["actions"] = actions_obj
        game["choice_key"] = None
        st.success("Aktionen generiert.")


# ----------------------------
# Choose + apply action
# ----------------------------
if game.get("actions"):
    actions = game["actions"]

    st.subheader("Wähle eine Aktion")
    options = {
        "aggressiv": actions["aggressiv"]["aktion"],
        "moderate": actions["moderate"]["aktion"],
        "passiv": actions["passiv"]["aktion"],
    }

    selected_label = st.radio(
        "Aktion auswählen:",
        [options["aggressiv"], options["moderate"], options["passiv"]],
        index=1  # default: moderate
    )

    # map back to key
    chosen_key = next(k for k, v in options.items() if v == selected_label)

    if st.button("Runde abschließen"):
        chosen = actions[chosen_key]
        folgen = chosen.get("folgen", {})
        de_delta = folgen.get("deutschland", {}) or {}
        eu_delta = folgen.get("eu", {}) or {}
        new_global_context = folgen.get("global_context") or game["eu"]["global_context"]

        # DB update
        update_germany_metrics(conn, de_delta)

        # EU state update (session)
        game["eu"]["cohesion"] = int(game["eu"]["cohesion"]) + int(eu_delta.get("kohäsion", 0))
        game["eu"]["global_context"] = str(new_global_context)

        # History (DB)
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO turn_history (
            country, round, action_public, global_context,
            delta_military, delta_stability, delta_economy, delta_diplomatic_influence, delta_public_approval
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "Germany",
            int(game["round"]),
            str(chosen.get("aktion", "")),
            str(game["eu"]["global_context"]),
            int(de_delta.get("militär", 0)),
            int(de_delta.get("stabilität", 0)),
            int(de_delta.get("wirtschaft", 0)),
            int(de_delta.get("diplomatie", 0)),
            int(de_delta.get("öffentliche_zustimmung", 0)),
        ))
        conn.commit()

        # Next round
        game["round"] += 1
        game["actions"] = None
        game["choice_key"] = None

        st.success("Runde abgeschlossen! Neue Metriken gespeichert.")
        st.rerun()


# ----------------------------
# Show history (optional)
# ----------------------------
with st.expander("Turn-History (Debug)"):
    cur = conn.cursor()
    cur.execute("""
    SELECT round, action_public, delta_military, delta_stability, delta_economy, delta_diplomatic_influence, delta_public_approval, global_context
    FROM turn_history
    WHERE country = ?
    ORDER BY id DESC
    LIMIT 15
    """, ("Germany",))
    rows = cur.fetchall()
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
