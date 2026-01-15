import random
from typing import Dict, Any, List, Callable

import streamlit as st

from db import (
    get_external_events,
    get_domestic_events,
    get_recent_round_summaries,
    get_eu_state,
    set_eu_state,
    clear_external_events,
    upsert_external_event,
    clear_domestic_events,
    upsert_domestic_event,
    set_game_meta,
    get_policy_locks,
    get_policy_candidates,
    all_policies_locked,
    load_all_country_metrics,
    apply_country_deltas,
    insert_turn_history,
    upsert_round_summary,
    upsert_country_snapshot,
    get_max_snapshot_round,
    clear_round_data,
    set_game_over,
)

from ai_external import generate_external_moves, generate_domestic_events
from ai_round import resolve_round_all_countries, generate_round_summary


def _auto_modifiers_from_craziness(actor: str, craziness: int) -> Dict[str, int]:
    """
    Deterministic mapping: craziness (0..100) -> pressure deltas.
    Keeps GM UI simple; still gives a transparent preview.
    """
    c = max(0, min(100, int(craziness)))
    s = round((c - 50) / 10)  # approx -5..+5

    if actor == "Russia":
        return {
            "eu_cohesion_delta": -max(0, s),
            "threat_delta": max(0, s + 1),
            "frontline_delta": max(0, s),
            "energy_delta": max(0, s),
            "migration_delta": max(0, s - 1),
            "disinfo_delta": max(0, s + 1),
            "trade_war_delta": max(0, s - 1),
        }
    if actor == "China":
        return {
            "eu_cohesion_delta": -max(0, s - 1),
            "threat_delta": max(0, s - 1),
            "frontline_delta": max(0, s - 2),
            "energy_delta": max(0, s - 1),
            "migration_delta": max(0, s - 2),
            "disinfo_delta": max(0, s),
            "trade_war_delta": max(0, s + 1),
        }
    # USA
    return {
        "eu_cohesion_delta": -max(0, s - 2),
        "threat_delta": max(0, s - 1),
        "frontline_delta": max(0, s - 1),
        "energy_delta": max(0, s - 2),
        "migration_delta": max(0, s - 2),
        "disinfo_delta": max(0, s - 2),
        "trade_war_delta": max(0, s),
    }


def _render_external_preview(ext_events: List[Dict[str, Any]]) -> None:
    if not ext_events:
        st.caption("Noch keine Außenmächte-Moves generiert.")
        return

    for e in ext_events:
        actor = e.get("actor", "")
        headline = e.get("headline", "")
        quote = (e.get("quote") or "").strip()
        craziness = int(e.get("craziness", 0) or 0)
        mods = e.get("modifiers", {}) or {}

        st.markdown(f"**{actor}** (🎲 {craziness}/100): {headline}")
        if quote:
            st.caption(f"🗣️ {quote}")

        with st.expander("🔎 Preview: Auswirkung (auto Modifiers)", expanded=False):
            st.caption(
                f"EU Kohäsion Δ {mods.get('eu_cohesion_delta', 0)} | "
                f"Threat Δ {mods.get('threat_delta', 0)} | Frontline Δ {mods.get('frontline_delta', 0)} | "
                f"Energy Δ {mods.get('energy_delta', 0)} | Migration Δ {mods.get('migration_delta', 0)} | "
                f"Disinfo Δ {mods.get('disinfo_delta', 0)} | TradeWar Δ {mods.get('trade_war_delta', 0)}"
            )


def _render_domestic_preview(dom_events: List[Dict[str, Any]], countries_display: Dict[str, str]) -> None:
    if not dom_events:
        st.caption("Noch keine Innenpolitik-Headlines generiert.")
        return

    for e in dom_events:
        c = e.get("country", "")
        name = countries_display.get(c, c)
        headline = e.get("headline", "")
        details = (e.get("details") or "").strip()
        craziness = int(e.get("craziness", 0) or 0)

        st.markdown(f"**{name}** (🎲 {craziness}/100): {headline}")
        if details:
            st.caption(details)


def render_gm_controls(
    *,
    conn,
    api_key: str,
    round_no: int,
    phase: str,
    countries: List[str],
    countries_display: Dict[str, str],
    country_defs: Dict[str, Dict[str, Any]],
    external_crazy_baseline_ranges: Dict[str, tuple],
    apply_external_modifiers_to_eu: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]],
    decay_pressures: Callable[[Dict[str, Any]], Dict[str, Any]],
    progress_from_conditions: Callable[[Any], float],
    evaluate_all_countries,  # may be None
) -> None:
    """
    GM flow (clean):

    1) GM sets craziness sliders -> generates external moves + domestic headlines via AI.
       No manual edits; only preview after generation.
    2) GM starts player phase
    3) GM resolves once all players locked both domains
    """

    with st.expander("🎛️ Game Master Steuerung (sequenziell)", expanded=False):
        if phase == "game_over":
            st.warning("Game Over – nur Reset möglich.")
            st.stop()

        eu_before = get_eu_state(conn)
        ext_now = get_external_events(conn, round_no)
        dom_now = get_domestic_events(conn, round_no)

        have_external = len(ext_now) == 3
        have_domestic = len(dom_now) == len(countries)
        have_gm_inputs = have_external and have_domestic

        inputs_disabled = (phase == "actions_published")
        if inputs_disabled:
            st.info("Spielerphase läuft – GM-Generierung ist gesperrt.")

        # ---------------------
        # 1) GM Generate via AI (craziness-only)
        # ---------------------
        st.markdown("#### 1) GM: KI generiert Außenmächte + Innenpolitik (nur Craziness einstellen)")

        usa_min, usa_max = external_crazy_baseline_ranges["USA"]
        rus_min, rus_max = external_crazy_baseline_ranges["Russia"]
        chi_min, chi_max = external_crazy_baseline_ranges["China"]

        col1, col2 = st.columns(2)
        with col1:
            usa_c = st.slider(
                "Craziness USA",
                0, 100,
                int(ext_now and next((x["craziness"] for x in ext_now if x["actor"] == "USA"), random.randint(usa_min, usa_max)) or random.randint(usa_min, usa_max)),
                disabled=inputs_disabled,
                key=f"gm_crazy_usa_{round_no}",
            )
            rus_c = st.slider(
                "Craziness Russia",
                0, 100,
                int(ext_now and next((x["craziness"] for x in ext_now if x["actor"] == "Russia"), random.randint(rus_min, rus_max)) or random.randint(rus_min, rus_max)),
                disabled=inputs_disabled,
                key=f"gm_crazy_rus_{round_no}",
            )
            chi_c = st.slider(
                "Craziness China",
                0, 100,
                int(ext_now and next((x["craziness"] for x in ext_now if x["actor"] == "China"), random.randint(chi_min, chi_max)) or random.randint(chi_min, chi_max)),
                disabled=inputs_disabled,
                key=f"gm_crazy_chi_{round_no}",
            )

        with col2:
            dom_baseline = st.slider(
                "Craziness Innenpolitik (Baseline)",
                0, 100,
                55,
                disabled=inputs_disabled,
                help="Wird als grober Baseline-Ton genutzt; KI kann pro Land abweichen.",
                key=f"gm_crazy_dom_base_{round_no}",
            )
            st.caption("Keine manuellen Edits: nach Generierung gibt’s nur Preview.")

        gen_disabled = inputs_disabled or (not api_key)
        if st.button("🤖 Jetzt generieren (KI)", disabled=gen_disabled, use_container_width=True, key=f"gm_gen_all_{round_no}"):
            with st.spinner("KI generiert Außenmächte und Innenpolitik..."):
                recent_summaries = get_recent_round_summaries(conn, limit=3)

                # --- External moves ---
                craziness_by_actor = {"USA": int(usa_c), "Russia": int(rus_c), "China": int(chi_c)}
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

                # Write external events (but override/ensure modifiers from craziness for transparency & consistency)
                clear_external_events(conn, round_no)
                moves_clean = []
                for m in moves_obj.get("moves", []) or []:
                    actor = m.get("actor", "")
                    cz = int(m.get("craziness", craziness_by_actor.get(actor, 50)) or 0)
                    mods = _auto_modifiers_from_craziness(actor, cz)
                    moves_clean.append({
                        "actor": actor,
                        "headline": m.get("headline", ""),
                        "quote": m.get("quote", ""),
                        "craziness": cz,
                        "modifiers": mods,
                    })
                    upsert_external_event(
                        conn,
                        round_no,
                        actor=actor,
                        headline=m.get("headline", ""),
                        modifiers=mods,
                        quote=m.get("quote", ""),
                        craziness=cz,
                    )

                # Apply external modifiers to EU state (preview will show before/after)
                global_context = str(moves_obj.get("global_context", eu_before.get("global_context", "")) or "")
                eu_after = apply_external_modifiers_to_eu(eu_before, {"moves": moves_clean, "global_context": global_context})
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

                # --- Domestic events ---
                clear_domestic_events(conn, round_no)
                all_metrics = load_all_country_metrics(conn, countries)

                # optional: pass baseline via temperature influence; simplest: tweak temperature a bit
                temp_dom = 0.75 + (float(dom_baseline) / 100.0) * 0.25  # 0.75..1.0

                dom_obj = generate_domestic_events(
                    api_key=api_key,
                    model="mistral-small",
                    round_no=round_no,
                    eu_state=get_eu_state(conn),
                    countries=countries,
                    countries_metrics=all_metrics,
                    recent_round_summaries=recent_summaries,
                    recent_actions_by_country={},  # keep simple; not needed for GM
                    temperature=temp_dom,
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

        # ---------------------
        # Preview after generation (read-only)
        # ---------------------
        st.write("---")
        st.markdown("#### Preview (read-only)")

        ext_now = get_external_events(conn, round_no)
        dom_now = get_domestic_events(conn, round_no)
        eu_now = get_eu_state(conn)

        with st.expander("🌐 Außenmächte-Moves (Preview)", expanded=True):
            _render_external_preview(ext_now)

        with st.expander("🏠 Innenpolitik-Headlines (Preview)", expanded=True):
            _render_domestic_preview(dom_now, countries_display)

        with st.expander("🧮 EU-State Preview (Before/After)", expanded=False):
            st.caption(
                f"Vorher (Start Runde): Kohäsion {eu_before['cohesion']} | Threat {eu_before['threat_level']} | Frontline {eu_before['frontline_pressure']} | "
                f"Energy {eu_before['energy_pressure']} | Migration {eu_before['migration_pressure']} | Disinfo {eu_before['disinfo_pressure']} | TradeWar {eu_before['trade_war_pressure']}"
            )
            st.caption(
                f"Jetzt (nach Außenmächten): Kohäsion {eu_now['cohesion']} | Threat {eu_now['threat_level']} | Frontline {eu_now['frontline_pressure']} | "
                f"Energy {eu_now['energy_pressure']} | Migration {eu_now['migration_pressure']} | Disinfo {eu_now['disinfo_pressure']} | TradeWar {eu_now['trade_war_pressure']}"
            )
            if eu_now.get("global_context"):
                st.info(eu_now["global_context"])

        if have_gm_inputs:
            st.success("✅ GM Inputs vollständig (Außenmächte + Innenpolitik)")
        else:
            st.warning(
                "⏳ GM Inputs unvollständig: "
                + ("Außenmächte ✅ " if len(ext_now) == 3 else "Außenmächte ⏳ ")
                + ("Innenpolitik ✅" if len(dom_now) == len(countries) else "Innenpolitik ⏳")
            )

        # ---------------------
        # 2) Start player phase
        # ---------------------
        st.markdown("#### 2) Spielerphase starten")
        publish_disabled = (phase == "actions_published") or (not have_gm_inputs)
        if st.button(
            "🚦 Runde starten (Spieler generieren & locken)",
            disabled=publish_disabled,
            use_container_width=True,
            key=f"gm_publish_{round_no}",
        ):
            set_game_meta(conn, round_no, "actions_published")
            st.rerun()

        # ---------------------
        # 3) Resolve
        # ---------------------
        st.markdown("#### 3) Runde auflösen")

        have_all_locks = all_policies_locked(conn, round_no=round_no, countries=countries)

        if phase == "actions_published":
            locks_now = get_policy_locks(conn, round_no=round_no)
            ready = sum(
                1
                for c in countries
                if (locks_now.get(c) or {}).get("foreign") and (locks_now.get(c) or {}).get("domestic")
            )
            st.caption(f"Locked: {ready}/{len(countries)} Länder (Außen+Innen)")

        resolve_disabled = not (phase == "actions_published" and have_all_locks)
        if st.button("🧮 Ergebnis der Runde kalkulieren", disabled=resolve_disabled, use_container_width=True, key=f"gm_resolve_{round_no}"):
            with st.spinner("KI kalkuliert Gesamtergebnis der Runde..."):
                recent_summaries = get_recent_round_summaries(conn, limit=3)
                eu_before_resolve = get_eu_state(conn)
                ext_events = get_external_events(conn, round_no)
                dom_events = get_domestic_events(conn, round_no)
                locks_now = get_policy_locks(conn, round_no=round_no)
                all_metrics = load_all_country_metrics(conn, countries)

                actions_texts: Dict[str, Dict[str, str]] = {}
                locked_choices: Dict[str, str] = {}
                chosen_actions_lines: List[str] = []

                def _get_candidate_text(country: str, domain: str, slot: int) -> str:
                    candidates = get_policy_candidates(conn, round_no=round_no, country=country, domain=domain)
                    cand = next((x for x in candidates if int(x.get("slot")) == int(slot)), None)
                    return str((cand or {}).get("action_text", ""))

                for c in countries:
                    ls = locks_now.get(c) or {}
                    f_slot = int(ls.get("foreign") or 0)
                    d_slot = int(ls.get("domestic") or 0)

                    f_text = _get_candidate_text(c, "foreign", f_slot)
                    d_text = _get_candidate_text(c, "domestic", d_slot)

                    combined = f"[Außenpolitik | Option {f_slot}]\n{f_text}\n\n[Innenpolitik | Option {d_slot}]\n{d_text}".strip()

                    actions_texts[c] = {"chosen": combined}
                    locked_choices[c] = "chosen"
                    chosen_actions_lines.append(f"- {countries_display.get(c, c)}: Außen {f_slot} / Innen {d_slot}")

                chosen_actions_str = "\n".join(chosen_actions_lines)

                result = resolve_round_all_countries(
                    api_key=api_key,
                    model="mistral-small",
                    round_no=round_no,
                    eu_state=eu_before_resolve,
                    countries_metrics=all_metrics,
                    countries_display=countries_display,
                    actions_texts=actions_texts,
                    locked_choices=locked_choices,
                    recent_round_summaries=recent_summaries,
                    external_events=ext_events,
                    domestic_events=dom_events,
                    temperature=0.6,
                    top_p=0.95,
                    max_tokens=1700,
                )

                eu_after = dict(eu_before_resolve)
                eu_after["cohesion"] = eu_before_resolve["cohesion"] + int(result["eu"].get("kohäsion_delta", 0))
                eu_after["global_context"] = str(result["eu"].get("global_context", eu_before_resolve["global_context"]))
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

                # Baseline snapshot (round_no-1) if needed
                all_metrics_before = load_all_country_metrics(conn, countries)
                eu_before_for_progress = get_eu_state(conn)
                max_snap = get_max_snapshot_round(conn)
                need_baseline = (max_snap is None) and (round_no >= 1)

                if need_baseline:
                    if evaluate_all_countries is not None:
                        win_eval_before = evaluate_all_countries(
                            all_country_metrics=all_metrics_before,
                            eu_state=eu_before_for_progress,
                            country_defs=country_defs,
                        )
                        for c in countries:
                            res = win_eval_before.get(c, {})
                            progress_before = progress_from_conditions(res.get("results") or [])
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

                # Apply deltas + history
                for c in countries:
                    d = result["länder"].get(c) or {}
                    apply_country_deltas(conn, c, d)

                    chosen_action_text = actions_texts[c]["chosen"]
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
                    eu_before=eu_before_resolve,
                    eu_after=eu_after_fresh,
                    external_events=ext_events,
                    domestic_events=dom_events,
                    chosen_actions_str=chosen_actions_str,
                    result_obj=result,
                    temperature=0.4,
                    top_p=0.95,
                    max_tokens=520,
                )
                upsert_round_summary(conn, round_no, summary_text)

                # Snapshots + win check
                winners: List[str] = []
                all_metrics_now = load_all_country_metrics(conn, countries)
                eu_now = get_eu_state(conn)

                if evaluate_all_countries is not None:
                    win_eval = evaluate_all_countries(
                        all_country_metrics=all_metrics_now,
                        eu_state=eu_now,
                        country_defs=country_defs,
                    )
                    for c in countries:
                        res = win_eval.get(c, {})
                        is_winner_now = bool(res.get("is_winner"))
                        progress = progress_from_conditions(res.get("results") or [])
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

                # Clean round-specific choice data (candidates/locks) for this round
                clear_round_data(conn, round_no)

                if winners:
                    set_game_over(conn, winner_country=winners[0], winner_round=round_no, reason="win_conditions")
                else:
                    set_game_meta(conn, round_no + 1, "setup")

            st.success("Runde aufgelöst.")
            st.rerun()

        st.caption("Flow: GM KI-Generierung → Spieler generieren/locken → Resolve")
