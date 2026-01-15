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
    load_recent_history,
    apply_country_deltas,
    insert_turn_history,
    upsert_round_summary,
    upsert_country_snapshot,
    get_max_snapshot_round,
    clear_round_data,
    set_game_over,
)

# Optional: GM can still auto-generate drafts as a starting point
from ai_external import generate_external_moves, generate_domestic_events

from ai_round import resolve_round_all_countries, generate_round_summary


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
    """GM control flow (new 2026-01):

    1) GM creates external moves + domestic headlines (manual, optional draft)
    2) GM starts player phase (players generate up to 3 candidates per domain and lock slots)
    3) GM resolves once all players locked both domains
    """

    with st.expander("🎛️ Game Master Steuerung (sequenziell)", expanded=False):
        if phase == "game_over":
            st.warning("Game Over – nur Reset möglich.")
            st.stop()

        ext_now = get_external_events(conn, round_no)
        dom_now = get_domestic_events(conn, round_no)

        have_external = len(ext_now) == 3
        have_domestic = len(dom_now) == len(countries)
        have_gm_inputs = have_external and have_domestic

        have_all_locks = all_policies_locked(conn, round_no=round_no, countries=countries)

        # ---------------------
        # 1) GM inputs
        # ---------------------
        st.markdown("#### 1) GM: Außenmächte + Innenpolitik")

        inputs_disabled = (phase == "actions_published")
        if inputs_disabled:
            st.info("Spielerphase läuft – GM-Eingaben sind gesperrt.")

        colA, colB = st.columns(2)
        with colA:
            if st.button(
                "🤖 Draft generieren (optional)",
                disabled=inputs_disabled or (not api_key),
                use_container_width=True,
            ):
                with st.spinner("Generiere Draft..."):
                    recent_summaries = get_recent_round_summaries(conn, limit=3)
                    eu_before = get_eu_state(conn)

                    usa_min, usa_max = external_crazy_baseline_ranges["USA"]
                    rus_min, rus_max = external_crazy_baseline_ranges["Russia"]
                    chi_min, chi_max = external_crazy_baseline_ranges["China"]
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
                    for m in moves_obj.get("moves", []):
                        upsert_external_event(
                            conn,
                            round_no,
                            actor=m.get("actor", ""),
                            headline=m.get("headline", ""),
                            modifiers=m.get("modifiers", {}) or {},
                            quote=m.get("quote", ""),
                            craziness=int(m.get("craziness", 0) or 0),
                        )

                    # Domestic draft
                    clear_domestic_events(conn, round_no)
                    all_metrics = load_all_country_metrics(conn, countries)
                    recent_actions_by_country: Dict[str, List[str]] = {}
                    for c in countries:
                        recent = load_recent_history(conn, c, limit=6)
                        recent_actions_by_country[c] = [r[1] for r in recent if r and r[1]]

                    dom_obj = generate_domestic_events(
                        api_key=api_key,
                        model="mistral-small",
                        round_no=round_no,
                        eu_state=eu_before,
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

        with colB:
            st.caption("Tipp: Draft ist nur ein Startpunkt – danach oben manuell justieren.")

        # Manual edit form
        eu_before = get_eu_state(conn)
        ext_now = get_external_events(conn, round_no)
        dom_now = get_domestic_events(conn, round_no)

        ext_by_actor = {e["actor"]: e for e in ext_now}
        dom_by_country = {e["country"]: e for e in dom_now}

        with st.form(f"gm_inputs_form_{round_no}"):
            st.caption("Außenmächte: Headline + Quote + Modifiers (wirken sofort auf EU-Druckwerte).")
            actors = ["USA", "Russia", "China"]
            moves: List[Dict[str, Any]] = []
            for a in actors:
                e = ext_by_actor.get(a) or {}
                with st.expander(f"{a}", expanded=True):
                    headline = st.text_input(f"Headline ({a})", value=str(e.get("headline", "")), disabled=inputs_disabled)
                    quote = st.text_input(f"Quote ({a})", value=str(e.get("quote", "")), disabled=inputs_disabled)
                    craziness = st.slider(
                        f"Craziness ({a})",
                        0,
                        100,
                        int(e.get("craziness", 0) or 0),
                        disabled=inputs_disabled,
                    )

                    mods = e.get("modifiers", {}) or {}
                    cols = st.columns(3)
                    eu_coh = cols[0].number_input(
                        "EU Kohäsion Δ",
                        value=int(mods.get("eu_cohesion_delta", 0) or 0),
                        step=1,
                        disabled=inputs_disabled,
                        key=f"gm_mod_eu_coh_{round_no}_{a}",
                    )
                    threat = cols[1].number_input(
                        "Threat Δ",
                        value=int(mods.get("threat_delta", 0) or 0),
                        step=1,
                        disabled=inputs_disabled,
                        key=f"gm_mod_threat_{round_no}_{a}",
                    )
                    front = cols[2].number_input(
                        "Frontline Δ",
                        value=int(mods.get("frontline_delta", 0) or 0),
                        step=1,
                        disabled=inputs_disabled,
                        key=f"gm_mod_front_{round_no}_{a}",
                    )

                    cols = st.columns(3)
                    energy = cols[0].number_input(
                        "Energy Δ",
                        value=int(mods.get("energy_delta", 0) or 0),
                        step=1,
                        disabled=inputs_disabled,
                        key=f"gm_mod_energy_{round_no}_{a}",
                    )
                    migr = cols[1].number_input(
                        "Migration Δ",
                        value=int(mods.get("migration_delta", 0) or 0),
                        step=1,
                        disabled=inputs_disabled,
                        key=f"gm_mod_migr_{round_no}_{a}",
                    )
                    disinfo = cols[2].number_input(
                        "Disinfo Δ",
                        value=int(mods.get("disinfo_delta", 0) or 0),
                        step=1,
                        disabled=inputs_disabled,
                        key=f"gm_mod_disinfo_{round_no}_{a}",
                    )

                    trade = st.number_input(
                        "TradeWar Δ",
                        value=int(mods.get("trade_war_delta", 0) or 0),
                        step=1,
                        disabled=inputs_disabled,
                        key=f"gm_mod_trade_{round_no}_{a}",
                    )

                    moves.append(
                        {
                            "actor": a,
                            "headline": headline,
                            "quote": quote,
                            "craziness": int(craziness),
                            "modifiers": {
                                "eu_cohesion_delta": int(eu_coh),
                                "threat_delta": int(threat),
                                "frontline_delta": int(front),
                                "energy_delta": int(energy),
                                "migration_delta": int(migr),
                                "disinfo_delta": int(disinfo),
                                "trade_war_delta": int(trade),
                            },
                        }
                    )

            st.write("---")
            st.caption("Innenpolitik: pro Land eine Headline (optional Details + Craziness).")
            dom_inputs = []
            for c in countries:
                e = dom_by_country.get(c) or {}
                name = countries_display.get(c, c)
                with st.expander(name, expanded=False):
                    headline = st.text_input(f"Headline ({name})", value=str(e.get("headline", "")), disabled=inputs_disabled)
                    details = st.text_area(f"Details ({name})", value=str(e.get("details", "")), disabled=inputs_disabled)
                    crazy = st.slider(f"Craziness ({name})", 0, 100, int(e.get("craziness", 0) or 0), disabled=inputs_disabled)
                    dom_inputs.append((c, headline, details, int(crazy)))

            st.write("---")
            global_context = st.text_area(
                "Globaler Kontext (optional überschreiben)",
                value=str(eu_before.get("global_context", "")),
                disabled=inputs_disabled,
            )

            saved = st.form_submit_button("💾 Speichern (GM Inputs)", disabled=inputs_disabled, use_container_width=True)

        if saved:
            eu_before = get_eu_state(conn)

            clear_external_events(conn, round_no)
            for m in moves:
                upsert_external_event(
                    conn,
                    round_no,
                    actor=m["actor"],
                    headline=m.get("headline", ""),
                    modifiers=m.get("modifiers", {}) or {},
                    quote=m.get("quote", ""),
                    craziness=int(m.get("craziness", 0) or 0),
                )

            clear_domestic_events(conn, round_no)
            for c, headline, details, crazy in dom_inputs:
                upsert_domestic_event(conn, round_no, c, headline, details=details, craziness=int(crazy))

            # Apply external modifiers + context
            moves_obj = {"moves": moves, "global_context": global_context}
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

            set_game_meta(conn, round_no, "external_generated")
            st.rerun()

        if have_gm_inputs:
            st.success("✅ GM Inputs vollständig (Außenmächte + Innenpolitik)")
        else:
            st.warning(
                "⏳ GM Inputs unvollständig: "
                + ("Außenmächte ✅ " if have_external else "Außenmächte ⏳ ")
                + ("Innenpolitik ✅" if have_domestic else "Innenpolitik ⏳")
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
        ):
            set_game_meta(conn, round_no, "actions_published")
            st.rerun()

        # ---------------------
        # 3) Resolve
        # ---------------------
        st.markdown("#### 3) Runde auflösen")

        if phase == "actions_published":
            locks_now = get_policy_locks(conn, round_no=round_no)
            ready = sum(
                1
                for c in countries
                if (locks_now.get(c) or {}).get("foreign") and (locks_now.get(c) or {}).get("domestic")
            )
            st.caption(f"Locked: {ready}/{len(countries)} Länder (Außen+Innen)")

        resolve_disabled = not (phase == "actions_published" and have_all_locks)
        if st.button("🧮 Ergebnis der Runde kalkulieren", disabled=resolve_disabled, use_container_width=True):
            with st.spinner("KI kalkuliert Gesamtergebnis der Runde..."):
                recent_summaries = get_recent_round_summaries(conn, limit=3)
                eu_before = get_eu_state(conn)
                ext_now = get_external_events(conn, round_no)
                dom_now = get_domestic_events(conn, round_no)
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
                    eu_state=eu_before,
                    countries_metrics=all_metrics,
                    countries_display=countries_display,
                    actions_texts=actions_texts,
                    locked_choices=locked_choices,
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

        st.caption("Flow: GM Inputs → Spieler generieren/locken → Resolve")
