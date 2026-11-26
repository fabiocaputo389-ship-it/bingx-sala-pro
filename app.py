def format_signal_full_spec(item: dict) -> str:
    lv = item.get("levels") or {}
    rp = item.get("recommended_params") or {}
    tp_plan = item.get("tp_plan") or {}
    side_label = (
        item.get("certain_signal")
        or item.get("signal")
        or ((item.get("early_setup") or {}).get("direction"))
        or "LONG"
    )
    entry_px = lv.get("entry", item.get("price", "--"))
    sl_px = lv.get("sl", "--")
    tp1_px = lv.get("tp1", "--")
    tp2_px = lv.get("tp2", "--")
    tp3_px = lv.get("tp3", "--")
    lev_val = rp.get("leverage", "--")
    qty_val = rp.get("qty", 0)
    plan_lbl = tp_plan.get("label", "")

    spec = (
        f"{item.get('bingx_name') or item.get('symbol')} {side_label} | "
        f"Entry: {entry_px} | TP1: {tp1_px} | TP2: {tp2_px} | TP3: {tp3_px} | "
        f"SL: {sl_px} | Lev: {lev_val}x | Qty: {qty_val}"
    )
    if plan_lbl:
        spec += f" ({plan_lbl})"
    return spec


def record_new_candle_written_alert(interval: str, candle_timing: dict, analyses_map: dict):
    """Registra un avviso scritto di nuova candela con tutti i TP, Entry, SL e Leva per i soli asset liberi."""
    if not candle_timing:
        return None

    tf_key = f"{interval}_{candle_timing.get('candle_index')}"
    prev_idx = AUTO_SCAN_STATE["last_seen_candle_index_by_tf"].get(interval)
    curr_idx = candle_timing.get("candle_index")

    if prev_idx is None:
        AUTO_SCAN_STATE["last_seen_candle_index_by_tf"][interval] = curr_idx
        return AUTO_SCAN_STATE.get("latest_new_candle_alert")

    if curr_idx is None or curr_idx <= prev_idx:
        return AUTO_SCAN_STATE.get("latest_new_candle_alert")

    AUTO_SCAN_STATE["last_seen_candle_index_by_tf"][interval] = curr_idx
    open_clock = candle_timing.get("open_clock_rome") or time.strftime("%H:%M:%S")

    free_candidates = []
    for sym, item in (analyses_map or {}).items():
        if not item:
            continue
        ew = item.get("entry_window") or {}
        sn = item.get("sn_rejection") or {}
        ms = item.get("manual_sign") or {}
        sess = item.get("session") or {}

        is_blocked = (
            bool(ew.get("exceeded"))
            or bool(sn.get("rejected"))
            or ms.get("status") in ("CANCELLED_REVERSAL", "REJECTED_SN", "OVER_THRESHOLD")
            or not bool(sess.get("session_ok", True))
        )
        if is_blocked:
            continue

        dir_str = str(item.get("certain_signal") or (item.get("early_setup") or {}).get("direction") or item.get("signal") or "")
        if "LONG" in dir_str or "SHORT" in dir_str:
            free_candidates.append(item)

    free_candidates.sort(
        key=lambda x: (
            1 if x.get("certain_signal") else 0,
            float(x.get("confidence") or 0.0)
        ),
        reverse=True
    )

    if free_candidates:
        top_items = free_candidates[:3]
        parts = [format_signal_full_spec(c) for c in top_items]

        best = top_items[0]
        lv0 = best.get("levels") or {}
        rp0 = best.get("recommended_params") or {}
        side0 = best.get("certain_signal") or best.get("signal") or ((best.get("early_setup") or {}).get("direction"))

        alert_obj = {
            "id": tf_key,
            "time": open_clock,
            "timeframe": interval,
            "blocked": False,
            "free_count": len(free_candidates),
            "symbol": best.get("bingx_name") or best.get("symbol"),
            "direction": side0,
            "entry": lv0.get("entry", best.get("price")),
            "tp1": lv0.get("tp1", "--"),
            "tp2": lv0.get("tp2", "--"),
            "tp3": lv0.get("tp3", "--"),
            "sl": lv0.get("sl", "--"),
            "leverage": rp0.get("leverage", "--"),
            "qty": rp0.get("qty", 0),
            "text": f"Nuova candela {interval} aperta alle {open_clock} — LIBERI ({len(free_candidates)}): " + " • ".join(parts)
        }
    else:
        sn_count = sum(
            1 for v in (analyses_map or {}).values()
            if v and (v.get("sn_rejection") or {}).get("rejected")
        )
        over_count = sum(
            1 for v in (analyses_map or {}).values()
            if v and (v.get("entry_window") or {}).get("exceeded")
        )
        reason_parts = []
        if sn_count:
            reason_parts.append(f"{sn_count} rifiutati per SN candela alta opposta")
        if over_count:
            reason_parts.append(f"{over_count} oltre soglia")
        reason = ", ".join(reason_parts) if reason_parts else "nessun asset libero da blocchi"

        alert_obj = {
            "id": tf_key,
            "time": open_clock,
            "timeframe": interval,
            "blocked": True,
            "free_count": 0,
            "reason": reason,
            "text": f"Nuova candela {interval} aperta alle {open_clock} — NESSUN ASSET LIBERO ({reason})."
        }

    AUTO_SCAN_STATE["latest_new_candle_alert"] = alert_obj
    AUTO_SCAN_STATE["new_candle_alerts"].insert(0, alert_obj)
    AUTO_SCAN_STATE["new_candle_alerts"] = AUTO_SCAN_STATE["new_candle_alerts"][:30]
    return alert_obj
