import html
from typing import Dict, Any, List, Optional, Tuple

import streamlit as st
from mistralai import Mistral

from ui.components import VALUE_HELP, compact_kv, metric_with_info
from logic.helpers import impact_preview_text, summarize_recent_actions, format_external_events
from utils import content_to_text, parse_json_maybe

from db import (
    load_country_metrics,
    load_recent_history,
    get_external_events,
    get_domestic_events,
    get_country_snapshots,
    # NEW policy flow
    get_policy_candidates,
    count_policy_candidates,
    upsert_policy_candidate,
    lock_policy_slot,
    get_policy_locks,
)

from countries import COUNTRY_DEFS

# Optional: win.py (falls vorhanden)
try:
    from win import evaluate_country_win_conditions
except Exception:
    evaluate_country_win_conditions = None


# -----------------------------
# Small AI helpers (single-policy JSON)
# -----------------------------
def _chat(client: Mistral, model: str, messages, temperature: float, top_p: float, max_tokens: int) -> str:
    resp = client.chat.complete(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
    )
    return content_to_text(resp.choices[0].message.content)


def _repair_to_valid_json(client: Mistral, model: str, bad_text: str, schema_hint: str) -> Dict[str, Any]:
    repair_prompt = f"""
Du bist ein Validator/Formatter. Wandle die folgende Ausgabe in **gültiges JSON** um.

Wichtig:
- Gib **NUR** JSON zurück (keine Erklärungen, kein Markdown).
- Nutze **nur** doppelte Anführungszeichen.
- Keine trailing commas.
- Schema MUSS exakt passen.

Schema:
{schema_hint}

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
        max_tokens=1200,
    )
    return parse_json_maybe(fixed_raw)


def _build_policy_prompt(
    *,
    domain: str,  # "foreign" | "domestic"
    aggressiveness: int,
    country_display: str,
    metrics: Dict[str, Any],
    eu_state: Dict[str, Any],
    external_events: List[Dict[str, Any]],
    domestic_headline: str,
    recent_actions_summary: str,
) -> str:
    ext_str = format_external_events(external_events)

    if domain == "foreign":
        domain_label = "Außenpolitik / Geopolitik / Sicherheit / Diplomatie"
        focus = """
Fokus:
- Abschreckung, Bündnisse, Sanktionen, Diplomatie, militärische Bereitschaft, internationale Kommunikation.
- Berücksichtige Threat/Frontline/Energy/Migration/Disinfo/TradeWar-Druck.
"""
    else:
        domain_label = "Innenpolitik / Gesellschaft / Wirtschaft / Stabilität"
        focus = """
Fokus:
- Innenpolitische Stabilität, Zustimmung, Reformen, Wirtschaft, Medien, Krisenmanagement, gesellschaftliche Spannungen.
- Berücksichtige innenpolitisches Event (Headline) stark.
"""

    scale = f"""
Aggressivitätsskala ({aggressiveness}/100):
- 0–20: extrem vorsichtig, deeskalierend, risikoscheu
- 21–40: eher vorsichtig, defensive Politik
- 41–60: ausgewogen, moderate Risiken
- 61–80: offensiv, hoher Einsatz, spürbare Risiken
- 81–100: maximal aggressiv, sehr risikoreich (kann Zustimmung/Stabilität kosten)
"""

    schema_hint = """
{
  "aktion": "...",
  "folgen": {
    "land": {"militär": 0, "stabilität": 0, "wirtschaft": 0, "diplomatie": 0, "öffentliche_zustimmung": 0},
    "eu": {"kohäsion": 0},
    "global_context": "..."
  }
}
""".strip()

    return f"""
Du bist eine Simulations-Engine in einem EU-Geopolitik-Spiel.

Erzeuge GENAU EINE öffentliche Aktion für {country_display}.
Domain: {domain_label}

{focus}

{scale}

Kontext:
- {country_display} Metriken: Militär={metrics["military"]}, Stabilität={metrics["stability"]}, Wirtschaft={metrics["economy"]},
  Diplomatie={metrics["diplomatic_influence"]}, Öffentliche Zustimmung={metrics["public_approval"]}.
- Ambition: {metrics["ambition"]}.

EU-/Weltlage:
- EU-Kohäsion={eu_state["cohesion"]}%
- Threat Level={eu_state["threat_level"]}/100, Frontline Pressure={eu_state["frontline_pressure"]}/100
- Energy={eu_state["energy_pressure"]}/100, Migration={eu_state["migration_pressure"]}/100
- Disinfo={eu_state["disinfo_pressure"]}/100, TradeWar={eu_state["trade_war_pressure"]}/100
- Globaler Kontext: {eu_state["global_context"]}

Außenmächte-Moves dieser Runde:
{ext_str}

Innenpolitisches Event (diese Runde, Land):
- {domestic_headline}

Letzte Aktionen (für Variation, nicht wiederholen):
{recent_actions_summary}

Output Regeln:
- Gib NUR gültiges JSON zurück (kein Markdown, keine Erklärungen).
- Folgen sind kleine realistische Ganzzahlen (typisch -12..+12).
- global_context ist ein kurzer Satz (max 1 Zeile).
- Achte darauf, dass die Aktion zur Domain passt.

Schema:
{schema_hint}
""".strip()


def _generate_policy_candidate(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float = 0.85,
    top_p: float = 0.95,
    max_tokens: int = 900,
) -> Tuple[Dict[str, Any], str]:
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

    schema_hint = """
{
  "aktion": "...",
  "folgen": {
    "land": {"militär": 0, "stabilität": 0, "wirtschaft": 0, "diplomatie": 0, "öffentliche_zustimmung": 0},
    "eu": {"kohäsion": 0},
    "global_context": "..."
  }
}
""".strip()

    try:
        obj = parse_json_maybe(raw)
    except Exception:
        obj = _repair_to_valid_json(client, model, raw, schema_hint)

    # validate minimal keys
    if "aktion" not in obj or "folgen" not in obj:
        raise ValueError("Policy-JSON muss 'aktion' und 'folgen' enthalten.")
    folgen = obj.get("folgen") or {}
    if "land" not in folgen or "eu" not in folgen or "global_context" not in folgen:
        raise ValueError("'folgen' muss land/eu/global_context enthalten.")

    return obj, raw


# -----------------------------
# UI panels
# -----------------------------
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


def _render_domain_block(
    *,
    conn,
    api_key: Optional[str],
    round_no: int,
    eu: Dict[str, Any],
    countries_display: Dict[str, str],
    my_country: str,
    domain: str,  # "foreign" | "domestic"
    is_lock_disabled: bool,
    already_locked_slot: Optional[int],
) -> None:
    domain_title = "🌍 Außenpolitik" if domain == "foreign" else "🏠 Innenpolitik"
    st.markdown(f"### {domain_title}")

    candidates = get_policy_candidates(conn, round_no=round_no, country=my_country, domain=domain)
    count = len(candidates)

    # slider defaults: keep last used aggressiveness if any
    last_aggr = candidates[-1]["aggressiveness"] if candidates else (55 if domain == "foreign" else 45)
    aggressiveness = st.slider(
        f"Aggressivität ({domain_title})",
        0, 100,
        int(last_aggr),
        help="0 = deeskalierend / vorsichtig, 100 = maximal aggressiv / riskant.",
        disabled=bool(already_locked_slot),
        key=f"aggr_{domain}_{round_no}_{my_country}",
    )

    gen_disabled = (
        bool(already_locked_slot)
        or is_lock_disabled
        or (count >= 3)
        or (not api_key)
    )

    gen_label = "⚙️ KI generieren" if count == 0 else f"⚙️ KI generieren (Option {count+1}/3)"
    if not api_key:
        st.error("API Key fehlt (GM muss api_key an render_player_view übergeben).")

    if st.button(gen_label, disabled=gen_disabled, use_container_width=True, key=f"gen_{domain}_{round_no}_{my_country}"):
        with st.spinner("KI generiert Option..."):
            metrics = load_country_metrics(conn, my_country)
            if not metrics:
                st.error("Konnte Länderwerte nicht laden.")
                return

            ext = get_external_events(conn, round_no)
            dom_events = get_domestic_events(conn, round_no)
            dom_map = {e["country"]: e for e in dom_events}
            domestic_headline = (dom_map.get(my_country) or {}).get("headline") or "Keine auffälligen Ereignisse gemeldet."

            recent = load_recent_history(conn, my_country, limit=12)
            recent_summary = summarize_recent_actions(recent)

            prompt = _build_policy_prompt(
                domain=domain,
                aggressiveness=int(aggressiveness),
                country_display=countries_display.get(my_country, my_country),
                metrics=metrics,
                eu_state=eu,
                external_events=ext,
                domestic_headline=domestic_headline,
                recent_actions_summary=recent_summary,
            )

            slot = count_policy_candidates(conn, round_no=round_no, country=my_country, domain=domain) + 1
            if slot > 3:
                st.warning("Du hast bereits 3 Optionen generiert.")
                return

            obj, _raw = _generate_policy_candidate(
                api_key=api_key,
                model="mistral-small",
                prompt=prompt,
                temperature=0.85,
                top_p=0.95,
                max_tokens=900,
            )

            action_text = str(obj.get("aktion", "")).strip()
            folgen = obj.get("folgen", {}) or {}

            upsert_policy_candidate(
                conn,
                round_no=round_no,
                country=my_country,
                domain=domain,
                slot=int(slot),
                aggressiveness=int(aggressiveness),
                action_text=action_text,
                impact=folgen,
            )

        st.rerun()

    # show status
    if already_locked_slot:
        st.success(f"✅ Gelockt: Option {already_locked_slot}")
    else:
        st.caption(f"Optionen erstellt: {count}/3")

    if not candidates:
        st.info("Noch keine Option erstellt. Stelle Aggressivität ein und klicke auf „KI generieren“.")
        return

    # selection
    # Build labels with aggressiveness and short preview
    def _shorten(s: str, n: int = 90) -> str:
        s = " ".join((s or "").split())
        return s if len(s) <= n else s[: n - 1] + "…"

    labels = []
    slot_by_label: Dict[str, int] = {}
    for c in candidates:
        slot = int(c["slot"])
        ag = int(c["aggressiveness"])
        txt = _shorten(c.get("action_text", ""))
        label = f"Option {slot} (Agg {ag}/100) — {txt}"
        labels.append(label)
        slot_by_label[label] = slot

    default_idx = 0
    if already_locked_slot:
        # select locked by default
        for i, lab in enumerate(labels):
            if slot_by_label[lab] == int(already_locked_slot):
                default_idx = i
                break
    else:
        default_idx = min(1, len(labels) - 1)  # default to option 2 if exists

    chosen_label = st.radio(
        "Option auswählen:",
        labels,
        index=default_idx,
        disabled=bool(already_locked_slot),
        key=f"choice_{domain}_{round_no}_{my_country}",
    )
    chosen_slot = slot_by_label[chosen_label]

    chosen_candidate = next((x for x in candidates if int(x["slot"]) == int(chosen_slot)), None) or {}
    folgen = chosen_candidate.get("impact") or {}
    st.caption("**Voraussichtliche Wirkung:** " + impact_preview_text(folgen) if folgen else "Voraussichtliche Wirkung: (keine Daten)")

    with st.expander("Alle Optionen vergleichen", expanded=False):
        for c in candidates:
            slot = int(c["slot"])
            ag = int(c["aggressiveness"])
            st.write(f"**Option {slot} — Agg {ag}/100**")
            st.write(c.get("action_text", ""))
            fol = c.get("impact") or {}
            if fol:
                st.caption(impact_preview_text(fol))
            st.write("---")

    lock_disabled = bool(already_locked_slot) or is_lock_disabled
    if st.button("✅ Diese Option locken", use_container_width=True, disabled=lock_disabled, key=f"lock_{domain}_{round_no}_{my_country}"):
        lock_policy_slot(conn, round_no=round_no, country=my_country, domain=domain, slot=int(chosen_slot))
        st.rerun()


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
    api_key: Optional[str] = None,  # <-- PATCH app.py: pass api_key here
):
    """
    New player flow:
    - Only active in phase == actions_published
    - Players can generate up to 3 candidates per domain (foreign/domestic), each with its own aggressiveness slider value.
    - Then they choose 1 of the up to 3 and lock the slot.
    """
    if phase == "game_over":
        st.info("Game Over – keine Aktionen mehr möglich.")
        return

    if phase != "actions_published":
        st.info("Spielerphase noch nicht aktiv. Warte auf den Game Master.")
        return

    locks = get_policy_locks(conn, round_no=round_no)
    my_locks = locks.get(my_country) or {}
    locked_foreign = my_locks.get("foreign")
    locked_domestic = my_locks.get("domestic")

    st.subheader("🎮 Deine Aktionen (max. 3 KI-Versuche je Bereich)")

    if locked_foreign and locked_domestic:
        st.success("✅ Beide Bereiche gelockt. Warte auf den GM (Resolve).")
    elif locked_foreign or locked_domestic:
        st.warning(f"⏳ Teilweise gelockt: Außen {locked_foreign or '—'} | Innen {locked_domestic or '—'}")
    else:
        st.warning("⏳ Noch nichts gelockt.")

    # Auto-refresh while waiting (players only)
    is_waiting = bool(locked_foreign and locked_domestic)
    if (not is_gm) and is_waiting and phase != "game_over":
        st.autorefresh(interval=4000, key="player_wait_refresh_new")

    # Domain blocks
    _render_domain_block(
        conn=conn,
        api_key=api_key,
        round_no=round_no,
        eu=eu,
        countries_display=countries_display,
        my_country=my_country,
        domain="foreign",
        is_lock_disabled=is_lock_disabled,
        already_locked_slot=(int(locked_foreign) if locked_foreign else None),
    )

    st.write("---")

    _render_domain_block(
        conn=conn,
        api_key=api_key,
        round_no=round_no,
        eu=eu,
        countries_display=countries_display,
        my_country=my_country,
        domain="domestic",
        is_lock_disabled=is_lock_disabled,
        already_locked_slot=(int(locked_domestic) if locked_domestic else None),
    )

    # Turn history
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

    # NOTE: app.py patch needed:
    # render_player_view(..., api_key=api_key, ...)
