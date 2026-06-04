"""
DB helpers for recording live trades and maintaining local position snapshots.

Important:
- This is a local DB snapshot, not the source of truth (exchange is).
- We keep it best-effort to support UI display and strategy state.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from app.utils.db import get_db_connection


def normalize_strategy_symbol(symbol: str) -> str:
    """
    Canonical symbol for qd_strategy_positions / qd_strategy_trades (e.g. BTC/USDT).

    Mixed formats (BTCUSDT vs BTC/USDT) previously broke position lookup, so closes
    had no local entry_price and profit stayed NULL.
    """
    s = str(symbol or "").strip().upper().replace("-", "")
    if not s:
        return ""
    if "/" in s:
        return s
    for quote in ("USDT", "USDC", "USD", "BUSD", "EUR"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[: -len(quote)]}/{quote}"
    return s


def _position_symbol_candidates(symbol: str) -> List[str]:
    """Unique symbol strings to try when resolving a position row."""
    raw = str(symbol or "").strip()
    if not raw:
        return []
    norm = normalize_strategy_symbol(raw)
    compact = norm.replace("/", "")
    raw_compact = raw.upper().replace("/", "").replace("-", "")
    out: List[str] = []
    for x in (raw, raw.upper(), norm, compact, raw_compact):
        if x and x not in out:
            out.append(x)
    return out


def _fetch_position_fuzzy(strategy_id: int, symbol: str, side: str) -> Tuple[Dict[str, Any], str]:
    """
    Find a non-empty position row; return (row, db_symbol_to_use).
    If none, db_symbol_to_use is the canonical form for new rows.
    """
    side_l = str(side or "").strip().lower()
    for sym in _position_symbol_candidates(symbol):
        row = _fetch_position(strategy_id, sym, side_l)
        if row and float(row.get("size") or 0.0) > 0:
            db_sym = str(row.get("symbol") or sym).strip()
            return row, db_sym or sym
    canon = normalize_strategy_symbol(symbol) or str(symbol or "").strip()
    return {}, canon


def _resolve_write_symbol(current: Dict[str, Any], cur_size: float, input_symbol: str) -> str:
    """Use existing DB symbol when updating a row; otherwise canonical new key."""
    if cur_size > 0 and current and str(current.get("symbol") or "").strip():
        return str(current.get("symbol") or "").strip()
    return normalize_strategy_symbol(input_symbol) or str(input_symbol or "").strip()


def _get_user_id_from_strategy(strategy_id: int) -> int:
    """Get user_id from strategy table. Defaults to 1 if not found."""
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute("SELECT user_id FROM qd_strategies_trading WHERE id = %s", (strategy_id,))
            row = cur.fetchone()
            cur.close()
        return int((row or {}).get('user_id') or 1)
    except Exception:
        return 1


def record_trade(
    *,
    strategy_id: int,
    symbol: str,
    trade_type: str,
    price: float,
    amount: float,
    commission: float = 0.0,
    commission_ccy: str = "",
    profit: Optional[float] = None,
    user_id: int = None,
) -> None:
    value = float(amount or 0.0) * float(price or 0.0)
    if user_id is None:
        user_id = _get_user_id_from_strategy(strategy_id)
    sym_out = normalize_strategy_symbol(symbol) or str(symbol or "").strip()
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_strategy_trades
            (user_id, strategy_id, symbol, type, price, amount, value, commission, commission_ccy, profit, created_at)
            VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                int(user_id),
                int(strategy_id),
                sym_out,
                str(trade_type),
                float(price or 0.0),
                float(amount or 0.0),
                float(value),
                float(commission or 0.0),
                str(commission_ccy or ""),
                profit,
            ),
        )
        db.commit()
        cur.close()


def _fetch_position(strategy_id: int, symbol: str, side: str) -> Dict[str, Any]:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT * FROM qd_strategy_positions WHERE strategy_id = %s AND symbol = %s AND side = %s",
            (int(strategy_id), str(symbol), str(side)),
        )
        row = cur.fetchone() or {}
        cur.close()
    return row if isinstance(row, dict) else {}


def mark_breakeven_activated(strategy_id: int, symbol: str, side: str) -> None:
    """一旦浮盈达到保本触发线，将该持仓的 breakeven_activated 标记为 True（不可逆）。"""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "UPDATE qd_strategy_positions SET breakeven_activated = TRUE WHERE strategy_id = %s AND symbol = %s AND side = %s",
            (int(strategy_id), str(symbol), str(side)),
        )
        db.commit()
        cur.close()


def _delete_position(strategy_id: int, symbol: str, side: str) -> None:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "DELETE FROM qd_strategy_positions WHERE strategy_id = %s AND symbol = %s AND side = %s",
            (int(strategy_id), str(symbol), str(side)),
        )
        db.commit()
        cur.close()


def upsert_position(
    *,
    strategy_id: int,
    symbol: str,
    side: str,
    size: float,
    entry_price: float,
    current_price: float,
    highest_price: float = 0.0,
    lowest_price: float = 0.0,
    user_id: int = None,
    stop_loss_price: float = None,
    breakout_vol: float = None,
    sl_order_id: str = None,
) -> None:
    if user_id is None:
        user_id = _get_user_id_from_strategy(strategy_id)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_strategy_positions
            (user_id, strategy_id, symbol, side, size, entry_price, current_price, highest_price, lowest_price,
             stop_loss_price, breakout_vol, sl_order_id, updated_at)
            VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT(strategy_id, symbol, side) DO UPDATE SET
                size = excluded.size,
                entry_price = excluded.entry_price,
                current_price = excluded.current_price,
                highest_price = CASE WHEN excluded.highest_price > 0 THEN excluded.highest_price ELSE qd_strategy_positions.highest_price END,
                lowest_price = CASE WHEN excluded.lowest_price > 0 THEN excluded.lowest_price ELSE qd_strategy_positions.lowest_price END,
                stop_loss_price = CASE WHEN excluded.stop_loss_price > 0 THEN excluded.stop_loss_price ELSE qd_strategy_positions.stop_loss_price END,
                breakout_vol = CASE WHEN excluded.breakout_vol > 0 THEN excluded.breakout_vol ELSE qd_strategy_positions.breakout_vol END,
                sl_order_id = CASE WHEN excluded.sl_order_id IS NOT NULL AND excluded.sl_order_id <> '' THEN excluded.sl_order_id ELSE qd_strategy_positions.sl_order_id END,
                updated_at = NOW()
            """,
            (
                int(user_id), int(strategy_id), str(symbol), str(side),
                float(size or 0.0), float(entry_price or 0.0), float(current_price or 0.0),
                float(highest_price or 0.0), float(lowest_price or 0.0),
                float(stop_loss_price or 0.0), float(breakout_vol or 0.0),
                str(sl_order_id or ""),
            ),
        )
        db.commit()
        cur.close()


def update_sl_order_id(*, strategy_id: int, symbol: str, side: str, sl_order_id: str) -> None:
    """开仓后记录交易所止损单 ID，或止损单更新后刷新。"""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            "UPDATE qd_strategy_positions SET sl_order_id = %s WHERE strategy_id = %s AND symbol = %s AND side = %s",
            (str(sl_order_id or ""), int(strategy_id), str(symbol), str(side)),
        )
        db.commit()
        cur.close()


def get_sl_order_id(*, strategy_id: int, symbol: str, side: str) -> str:
    """读取持仓的交易所止损单 ID。"""
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "SELECT sl_order_id FROM qd_strategy_positions WHERE strategy_id = %s AND symbol = %s AND side = %s",
                (int(strategy_id), str(symbol), str(side)),
            )
            row = cur.fetchone()
            cur.close()
            return str((row or {}).get("sl_order_id") or "")
    except Exception:
        return ""


def apply_fill_to_local_position(
    *,
    strategy_id: int,
    symbol: str,
    signal_type: str,
    filled: float,
    avg_price: float,
    stop_loss_price: float = None,
    breakout_vol: float = None,
) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """
    Apply a fill to the local position snapshot.

    Returns (profit, updated_position_row_or_none)
    - profit is only calculated on close/reduce fills (best-effort, based on local entry_price).
    """
    sig = (signal_type or "").strip().lower()
    filled_qty = float(filled or 0.0)
    px = float(avg_price or 0.0)
    if filled_qty <= 0 or px <= 0:
        return None, None

    if "long" in sig:
        side = "long"
    elif "short" in sig:
        side = "short"
    else:
        return None, None

    is_open = sig.startswith("open_") or sig.startswith("add_")
    is_close = sig.startswith("close_") or sig.startswith("reduce_")

    sid = int(strategy_id)
    current, _matched = _fetch_position_fuzzy(sid, symbol, side)
    cur_size = float(current.get("size") or 0.0)
    cur_entry = float(current.get("entry_price") or 0.0)
    cur_high = float(current.get("highest_price") or 0.0)
    cur_low = float(current.get("lowest_price") or 0.0)
    sym_key = _resolve_write_symbol(current, cur_size, symbol)

    profit: Optional[float] = None

    if is_open:
        new_size = cur_size + filled_qty
        if new_size <= 0:
            return None, None
        # Weighted average entry.
        if cur_size > 0 and cur_entry > 0:
            new_entry = (cur_size * cur_entry + filled_qty * px) / new_size
        else:
            new_entry = px
        new_high = max(cur_high or px, px)
        new_low = min(cur_low or px, px)
        upsert_position(
            strategy_id=sid,
            symbol=sym_key,
            side=side,
            size=new_size,
            entry_price=new_entry,
            current_price=px,
            highest_price=new_high,
            lowest_price=new_low,
            stop_loss_price=stop_loss_price,
            breakout_vol=breakout_vol,
        )
        return None, _fetch_position(sid, sym_key, side)

    if is_close:
        # Calculate PnL using local entry price.
        if cur_size > 0 and cur_entry > 0:
            close_qty = min(cur_size, filled_qty)
            if side == "long":
                profit = (px - cur_entry) * close_qty
            else:
                profit = (cur_entry - px) * close_qty

        new_size = cur_size - filled_qty
        if new_size <= 0:
            _delete_position(sid, sym_key, side)
            return profit, None
        # Keep entry price for remaining position.
        new_high = max(cur_high or px, px)
        new_low = min(cur_low or px, px)
        upsert_position(
            strategy_id=sid,
            symbol=sym_key,
            side=side,
            size=new_size,
            entry_price=cur_entry if cur_entry > 0 else px,
            current_price=px,
            highest_price=new_high,
            lowest_price=new_low,
        )
        return profit, _fetch_position(sid, sym_key, side)

    return None, None


