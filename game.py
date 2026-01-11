import streamlit as st
from mistralai import Mistral
import sqlite3
from dotenv import load_dotenv
import os
import json
import re
import json

def init_game_state():
    st.session_state.game_started = True
    st.session_state.round = 1
    st.session_state.eu_cohesion = 75

    st.session_state.countries = {
        "Deutschland": {"wirtschaft": 95, "stabilität": 90, "militär": 70},
        "Frankreich": {"wirtschaft": 90, "stabilität": 85, "militär": 75},
        "Dänemark": {"wirtschaft": 85, "stabilität": 90, "militär": 60},
        "Polen": {"wirtschaft": 80, "stabilität": 80, "militär": 80},
        "Ungarn": {"wirtschaft": 70, "stabilität": 75, "militär": 65},
    }
    

    st.session_state.actions = None
    st.session_state.selected_actions = {}
    st.session_state.history = []


def content_to_text(content) -> str:
    # content kann str sein, oder eine Liste von Parts (je nach SDK)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            # SDK-Parts haben oft .text
            t = getattr(p, "text", None)
            if t:
                parts.append(t)
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    # Fallback
    return str(content)

def parse_json_maybe(text: str):
    s = (text or "").strip()
    if not s:
        raise ValueError("Leere Antwort vom Modell (kein JSON erhalten).")

    # Markdown-Codefences entfernen
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)

    # Erst direkt parsen
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Sonst: erstes JSON-Objekt/Array aus dem Text ziehen
    m = re.search(r"(\{.*\}|\[.*\])", s, flags=re.DOTALL)
    if not m:
        raise ValueError(f"Kein JSON in der Modellantwort gefunden. Anfang: {s[:200]!r}")
    return json.loads(m.group(1))


# API-Client initialisieren

load_dotenv()
api_key = os.getenv("MISTRAL_API_KEY")
client = Mistral(api_key=api_key)


# Datenbankverbindung
conn = sqlite3.connect("game.db")
cursor = conn.cursor()

# UI-Titel
st.title("EU Geopolitik-Prototyp (Deutschland)")
st.write("---")

# Aktuelle Metriken laden
cursor.execute("SELECT * FROM countries WHERE name = 'Germany'")
metrics = cursor.fetchone()

# Metriken anzeigen
col1, col2 = st.columns(2)
with col1:
    st.subheader("Aktuelle Metriken")
    st.write(f"- Militär: {metrics[1]}")
    st.write(f"- Stabilität: {metrics[2]}")
    st.write(f"- Wirtschaft: {metrics[3]}")
with col2:
    st.write(f"- Diplomatischer Einfluss: {metrics[4]}")
    st.write(f"- Öffentliche Zustimmung: {metrics[5]}")
    st.write(f"- Ambition: {metrics[6]}")

# AI-Aktionen generieren
if st.button("Aktionen generieren"):
    with st.spinner("AI generiert Aktionen..."):
        prompt = f"""
        Du bist ein AI-Agent in einem EU-Geopolitik-Spiel.
        Generiere **drei öffentliche Aktionen** für Deutschland basierend auf diesem Kontext:

        --- Kontext ---
        - Aktuelle Metriken: Militär={metrics[1]}, Stabilität={metrics[2]}, Wirtschaft={metrics[3]}, Diplomatischer Einfluss={metrics[4]}, Öffentliche Zustimmung={metrics[5]}.
        - Ambition: {metrics[6]}.
        - Letzte Aktion: Keine (erste Runde).
        - Globaler Kontext: EU-Kohäsion=75%, Russland droht mit Gaskürzungen.

        --- Aufgaben ---
        1. Gib **drei Aktionen** zurück: eine aggressive, eine moderate, eine passive.
        2. Formatiere die Antwort als JSON:
           {{
             "aggressiv": {{"aktion": "Beschreibung", "folgen": {{"deutschland": {{"wirtschaft": X, "stabilität": Y}}, "eu": {{"kohäsion": Z}}}}}},
             "moderate": {{...}},
             "passiv": {{...}}
           }}
        3. Jede Aktion sollte realistische Auswirkungen auf die Metriken haben.
        4. Antworte ausschließlich mit einem gültigen JSON-Objekt. Keine Erklärungen, kein Markdown.
        """
        response = client.chat.complete(
            model="mistral-small",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=900
        )
        raw = content_to_text(response.choices[0].message.content)

        # Debug-Hilfe (kannst du später entfernen)
        st.write("RAW (first 500 chars):", raw[:500])

        try:
            st.session_state.actions = parse_json_maybe(raw)
        except Exception as e:
            st.error(f"Konnte JSON nicht parsen: {e}")
            st.stop()

        st.json(st.session_state.actions)


# Aktionen auswählen (wenn generiert)
if "actions" in st.session_state:
    st.subheader("Wähle eine öffentliche Aktion:")
    choice = st.radio(
        "Aktion auswählen:",
        [
            st.session_state.actions["aggressiv"]["aktion"],
            st.session_state.actions["moderate"]["aktion"],
            st.session_state.actions["passiv"]["aktion"]
        ]
    )

    if st.button("Runde abschließen"):
        # Metriken aus der gewählten Aktion extrahieren
        if choice == st.session_state.actions["aggressiv"]["aktion"]:
            new_metrics = st.session_state.actions["aggressiv"]["folgen"]["deutschland"]
            global_context = "EU-Kohäsion: 80%, Russland erhöht Militär um 5 (Reaktion auf Sanktionen)."
        elif choice == st.session_state.actions["moderate"]["aktion"]:
            new_metrics = st.session_state.actions["moderate"]["folgen"]["deutschland"]
            global_context = "EU-Kohäsion: 75%, Russland bleibt neutral."
        else:
            new_metrics = st.session_state.actions["passiv"]["folgen"]["deutschland"]
            global_context = "EU-Kohäsion: 70%, Russland ignoriert Deutschland."

        # Neue Metriken in turn_history speichern
        cursor.execute("""
            INSERT INTO turn_history (
                country, military, stability, economy, diplomatic_influence, public_approval,
                action_public, action_private, action_internal, global_context
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "Germany",
            metrics[1] + new_metrics.get("wirtschaft", 0),
            metrics[2] + new_metrics.get("stabilität", 0),
            metrics[3] + new_metrics.get("wirtschaft", 0),  # Wirtschaft = economy
            metrics[4] + new_metrics.get("diplomatie", 0),
            metrics[5] + new_metrics.get("öffentliche_zustimmung", 0),
            choice,  # action_public
            "Keine",  # action_private (vereinfacht)
            "Keine",  # action_internal (vereinfacht)
            global_context
        ))

        # Aktuelle Metriken in countries aktualisieren
        cursor.execute(f"""
            UPDATE countries SET
            military = {metrics[1] + new_metrics.get("militär", 0)},
            stability = {metrics[2] + new_metrics.get("stabilität", 0)},
            economy = {metrics[3] + new_metrics.get("wirtschaft", 0)},
            diplomatic_influence = {metrics[4] + new_metrics.get("diplomatie", 0)},
            public_approval = {metrics[5] + new_metrics.get("öffentliche_zustimmung", 0)}
            WHERE name = 'Germany'
        """)

        conn.commit()
        st.success("Runde abgeschlossen! Neue Metriken gespeichert.")
        st.rerun()  # UI neu laden

# Datenbank schließen (wichtig!)
conn.close()
