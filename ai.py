# ai.py
from typing import Dict, Any

from mistralai import Mistral

from utils import content_to_text, parse_json_maybe


def build_action_prompt(
    *,
    country_display: str,
    metrics: Dict[str, Any],
    eu_cohesion: int,
    global_context: str,
    recent_actions_summary: str
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
"""


def generate_actions(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float = 0.9,
    top_p: float = 0.95,
    max_tokens: int = 900
) -> Dict[str, Any]:
    client = Mistral(api_key=api_key)
    response = client.chat.complete(
        model=model,
        messages=[
            {"role": "system", "content": "Antworte ausschließlich mit gültigem JSON. Kein Markdown."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
    )

    raw = content_to_text(response.choices[0].message.content)
    obj = parse_json_maybe(raw)

    # Minimalvalidierung
    for k in ("aggressiv", "moderate", "passiv"):
        if k not in obj:
            raise ValueError(f"Fehlender Key im JSON: {k}")

        if "aktion" not in obj[k] or "folgen" not in obj[k]:
            raise ValueError(f"Key '{k}' muss 'aktion' und 'folgen' enthalten.")

        folgen = obj[k]["folgen"] or {}
        if "land" not in folgen or "eu" not in folgen or "global_context" not in folgen:
            raise ValueError(f"'{k}.folgen' muss land/eu/global_context enthalten.")

    return obj
