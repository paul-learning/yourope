# db.py
import sqlite3
from typing import Dict, Any, List, Tuple

from utils import clamp_int


DB_PATH = "game.db"


def get_conn() -> sqlite3.Connection:
    # check_same_thread=False ist bei Streamlit hilfreich (reruns)
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def ensure_schema(conn: sqlite3.Connection) -> None:
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


def seed_countries_if_missing(conn: sqlite3.Connection, country_defs: Dict[str, Dict[str, Any]]) -> None:
    cur = conn.cursor()
    for name, data in country_defs.items():
        cur.execute("SELECT 1 FROM countries WHERE name = ?", (name,))
        if cur.fetchone() is None:
            cur.execute("""
                INSERT INTO countries (name, military, stability, economy, diplomatic_influence, public_approval, ambition)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                int(data["military"]),
                int(data["stability"]),
                int(data["economy"]),
                int(data["diplomatic_influence"]),
                int(data["public_approval"]),
                str(data["ambition"]),
            ))
    conn.commit()


def reset_country_to_defaults(conn: sqlite3.Connection, country: str, defaults: Dict[str, Any]) -> None:
    cur = conn.cursor()
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
        int(defaults["military"]),
        int(defaults["stability"]),
        int(defaults["economy"]),
        int(defaults["diplomatic_influence"]),
        int(defaults["public_approval"]),
        str(defaults["ambition"]),
        country,
    ))
    cur.execute("DELETE FROM turn_history WHERE country = ?", (country,))
    conn.commit()


def reset_all_countries(conn: sqlite3.Connection, country_defs: Dict[str, Dict[str, Any]]) -> None:
    for country, defs in country_defs.items():
        reset_country_to_defaults(conn, country, defs)


def load_country_metrics(conn: sqlite3.Connection, country: str) -> Dict[str, Any] | None:
    cur = conn.cursor()
    cur.execute("""
        SELECT name, military, stability, economy, diplomatic_influence, public_approval, ambition
        FROM countries
        WHERE name = ?
    """, (country,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "name": row[0],
        "military": int(row[1]),
        "stability": int(row[2]),
        "economy": int(row[3]),
        "diplomatic_influence": int(row[4]),
        "public_approval": int(row[5]),
        "ambition": row[6],
    }


def update_country_metrics(conn: sqlite3.Connection, country: str, deltas: Dict[str, Any]) -> None:
    """
    Erwartet Keys wie im Prompt:
    militär, stabilität, wirtschaft, diplomatie, öffentliche_zustimmung
    """
    dm = int(deltas.get("militär", 0))
    ds = int(deltas.get("stabilität", 0))
    de = int(deltas.get("wirtschaft", 0))
    dd = int(deltas.get("diplomatie", 0))
    dp = int(deltas.get("öffentliche_zustimmung", 0))

    cur = conn.cursor()
    cur.execute("""
        UPDATE countries SET
            military = military + ?,
            stability = stability + ?,
            economy = economy + ?,
            diplomatic_influence = diplomatic_influence + ?,
            public_approval = public_approval + ?
        WHERE name = ?
    """, (dm, ds, de, dd, dp, country))
    conn.commit()

    # clamp auf 0..100 (nachträglich, simpel & robust)
    cur.execute("""
        SELECT military, stability, economy, diplomatic_influence, public_approval
        FROM countries WHERE name = ?
    """, (country,))
    m, s, e, d, p = cur.fetchone()
    cur.execute("""
        UPDATE countries SET
            military = ?,
            stability = ?,
            economy = ?,
            diplomatic_influence = ?,
            public_approval = ?
        WHERE name = ?
    """, (
        clamp_int(m), clamp_int(s), clamp_int(e), clamp_int(d), clamp_int(p), country
    ))
    conn.commit()


def insert_turn_history(
    conn: sqlite3.Connection,
    *,
    country: str,
    round_no: int,
    action_public: str,
    global_context: str,
    deltas: Dict[str, Any]
) -> None:
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO turn_history (
            country, round, action_public, global_context,
            delta_military, delta_stability, delta_economy, delta_diplomatic_influence, delta_public_approval
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        country,
        int(round_no),
        str(action_public),
        str(global_context),
        int(deltas.get("militär", 0)),
        int(deltas.get("stabilität", 0)),
        int(deltas.get("wirtschaft", 0)),
        int(deltas.get("diplomatie", 0)),
        int(deltas.get("öffentliche_zustimmung", 0)),
    ))
    conn.commit()


def load_recent_history(
    conn: sqlite3.Connection,
    country: str,
    limit: int = 12
) -> List[Tuple]:
    cur = conn.cursor()
    cur.execute("""
        SELECT round, action_public,
               delta_military, delta_stability, delta_economy, delta_diplomatic_influence, delta_public_approval,
               global_context
        FROM turn_history
        WHERE country = ?
        ORDER BY id DESC
        LIMIT ?
    """, (country, int(limit)))
    return cur.fetchall()
