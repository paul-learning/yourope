from typing import Dict, Any, List


def build_action_prompt(
    *,
    country_display: str,
    metrics: Dict[str, Any],
    eu_state: Dict[str, Any],
    external_events: List[Dict[str, Any]],
    recent_actions_summary: str,
    domestic_headline: str
) -> str:
    # NOTE: format_external_events muss aus deinem bestehenden logic/helpers.py kommen
    # (oder wo du es in Schritt 1 hin ausgelagert hast).
    from logic.helpers import format_external_events

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
