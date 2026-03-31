"""
Database Operations v5.1
=========================
Updated for heartbeat observability schema.

eval_heartbeat: EVERY evaluation cycle (~3s) — what the bot sees and thinks
paper_settled / live_settled: ONLY executed trades with outcomes
"""

import os
import time
import logging
from typing import Optional, Dict, List
from datetime import datetime, timezone

logger = logging.getLogger("oracle.db")


def _window_phase(seconds_remaining: int) -> str:
    if seconds_remaining >= 240: return "EARLY"
    if seconds_remaining >= 180: return "MID"
    if seconds_remaining >= 120: return "SWEET_SPOT"
    if seconds_remaining >= 60:  return "PRIMARY"
    if seconds_remaining >= 20:  return "LATE"
    return "TOO_LATE"


def _gate_number(reason: str) -> int:
    if not reason: return 8
    r = reason.lower()
    if "already traded" in r: return 1
    if "too late" in r: return 2
    if "no chainlink" in r or "open price" in r or "current" in r: return 3
    if "magnitude" in r: return 4
    if "ltp" in r and "contradicts" in r: return 5
    if "book" in r or "no book" in r: return 6
    if "edge" in r: return 7
    if "sizer" in r or "bankroll" in r or "kelly" in r: return 7
    return 0


class DatabaseOps:
    def __init__(self, supabase_client):
        self._sb = supabase_client

    async def load_config(self) -> Optional[Dict]:
        try:
            resp = self._sb.table("bot_config").select("*").eq("id", 1).limit(1).execute()
            return resp.data[0] if resp.data else None
        except Exception as e:
            logger.warning(f"Config load failed: {e}")
            return None

    async def update_config(self, key: str, value) -> bool:
        try:
            self._sb.table("bot_config").update(
                {key: value, "updated_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", 1).execute()
            return True
        except Exception as e:
            logger.error(f"Config update failed: {e}")
            return False

    async def set_mode(self, mode: str) -> bool:
        try:
            self._sb.table("bot_control").upsert(
                {"key": "mode", "value": mode.lower(),
                 "updated_at": datetime.now(timezone.utc).isoformat()},
                on_conflict="key"
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Mode set failed: {e}")
            return False

    async def write_heartbeat(self, mode: str, balance: float, cycle_count: int):
        try:
            self._sb.table("heartbeats_v2").insert({
                "bot_mode": mode, "balance_usdc": round(balance, 2),
                "status": "ALIVE", "trades_today": 0,
                "active_orders": 0, "pending_settlements": 0,
            }).execute()
        except Exception as e:
            logger.debug(f"Heartbeat write failed: {e}")

    async def log_eval_heartbeat(self, decision, market: Dict, market_data: Dict,
                                  cycle_number: int, eval_duration_ms: float,
                                  mode: str) -> Optional[str]:
        try:
            secs = decision.seconds_remaining
            spread = None
            if market_data.get("best_bid_yes") and market_data.get("best_ask_yes"):
                spread = int((market_data["best_ask_yes"] - market_data["best_bid_yes"]) * 10000)

            cl_move = None
            if decision.window_open_price and decision.chainlink_price and decision.window_open_price > 0:
                cl_move = round((decision.chainlink_price - decision.window_open_price) / decision.window_open_price * 100, 6)

            row = {
                "window_ts": market.get("window_ts"),
                "seconds_remaining": secs,
                "window_phase": _window_phase(secs),
                "chainlink_open": round(decision.window_open_price, 2) if decision.window_open_price else None,
                "chainlink_current": round(decision.chainlink_price, 2) if decision.chainlink_price else None,
                "chainlink_move_pct": cl_move,
                "binance_price": round(market_data.get("binance_price", 0), 2) or None,
                "coinbase_price": round(decision.coinbase_price, 2) if decision.coinbase_price else None,
                "best_bid_yes": market_data.get("best_bid_yes"),
                "best_ask_yes": market_data.get("best_ask_yes"),
                "spread_bps": spread,
                "ltp": market_data.get("ltp"),
                "tick_velocity": round(decision.tick_velocity, 6) if decision.tick_velocity else None,
                "deribit_pcr": market_data.get("deribit_pcr"),
                "fear_greed_index": market_data.get("fear_greed_index"),
                "magnitude_pct": round(decision.magnitude_pct, 6) if decision.magnitude_pct else None,
                "required_magnitude": round(market_data.get("required_magnitude", 0), 4) or None,
                "implied_direction": decision.direction if decision.direction != "NEUTRAL" else None,
                "fair_value": round(decision.confidence, 4) if decision.confidence else None,
                "fill_price_estimate": round(decision.fill_price, 4) if decision.fill_price else None,
                "edge_pct": round(decision.edge_pct, 2) if decision.edge_pct else None,
                "min_edge_required": market_data.get("min_edge_pct", 3.0),
                "position_size_usd": round(decision.size_usd, 2) if decision.size_usd else None,
                "position_size_pct": round(decision.size_pct, 4) if decision.size_pct else None,
                "bankroll": round(market_data.get("bankroll", 0), 2) or None,
                "ltp_confirms": decision.ltp_confirms,
                "pcr_adjustment": round(decision.pcr_adjustment, 4) if decision.pcr_adjustment else None,
                "sentiment_adjustment": round(decision.sentiment_adjustment, 4) if decision.sentiment_adjustment else None,
                "coinbase_agrees": market_data.get("coinbase_agrees"),
                "trade_intention": decision.should_trade,
                "gate_reached": _gate_number(decision.reason) if not decision.should_trade else 8,
                "skip_reason": decision.reason if not decision.should_trade else None,
                "side": decision.side if decision.should_trade else None,
                "execution_mode": decision.execution_mode if decision.should_trade else None,
                "cycle_number": cycle_number,
                "eval_duration_ms": round(eval_duration_ms, 2),
                "engine_version": "5.1.0",
                "bot_mode": mode,
            }
            resp = self._sb.table("eval_heartbeat").insert(row).execute()
            if resp.data:
                return resp.data[0].get("id")
            return None
        except Exception as e:
            logger.error(f"Eval heartbeat write failed: {e}")
            return None

    async def log_trade(self, decision, market: Dict, result, heartbeat_id: Optional[str], mode: str):
        table = "paper_settled" if mode == "paper" else "live_settled"
        try:
            row = {
                "eval_heartbeat_id": heartbeat_id,
                "market_id": market.get("condition_id", ""),
                "market_slug": f"btc-updown-5m-{market.get('window_ts', 0)}",
                "market_question": market.get("question", ""),
                "window_ts": market.get("window_ts"),
                "token_id": market.get("yes_token_id") if decision.side == "YES" else market.get("no_token_id"),
                "side": decision.side,
                "implied_direction": decision.direction,
                "seconds_remaining": decision.seconds_remaining,
                "chainlink_open": round(decision.window_open_price, 2),
                "chainlink_at_trade": round(decision.chainlink_price, 2),
                "chainlink_move_pct": round(decision.magnitude_pct, 6),
                "fair_value": round(decision.confidence, 4),
                "fill_price": round(decision.fill_price, 4),
                "edge_pct": round(decision.edge_pct, 2),
                "size_usd": round(decision.size_usd, 2),
                "size_pct": round(decision.size_pct, 4),
                "execution_mode": decision.execution_mode,
                "order_id": result.order_id if result else None,
                "ltp_confirms": decision.ltp_confirms,
                "pcr_adjustment": round(decision.pcr_adjustment, 4),
                "sentiment_adjustment": round(decision.sentiment_adjustment, 4),
                "coinbase_price": round(decision.coinbase_price, 2) if decision.coinbase_price else None,
                "tick_velocity": round(decision.tick_velocity, 6) if decision.tick_velocity else None,
                "bankroll_at_trade": round(decision.size_usd / decision.size_pct, 2) if decision.size_pct > 0 else None,
                "engine_version": "5.1.0",
                "bot_mode": mode,
            }
            self._sb.table(table).insert(row).execute()
        except Exception as e:
            logger.error(f"Trade log failed: {e}")

    async def check_settlements(self, mode: str) -> List[Dict]:
        table = "paper_settled" if mode == "paper" else "live_settled"
        try:
            resp = self._sb.table(table).select(
                "id, side, fill_price, size_usd, market_id, window_ts, resolved_outcome"
            ).is_("settled_at", "null").limit(20).execute()
            return [{"id": r["id"], "side": r["side"], "outcome": r["resolved_outcome"],
                     "fill_price": float(r.get("fill_price", 0.50)),
                     "size_usd": float(r.get("size_usd", 0))}
                    for r in (resp.data or []) if r.get("resolved_outcome")]
        except Exception as e:
            logger.debug(f"Settlement check failed: {e}")
            return []

    async def settle_trade(self, trade_id: str, result: Dict, pnl: Dict, mode: str):
        table = "paper_settled" if mode == "paper" else "live_settled"
        try:
            self._sb.table(table).update({
                "won": result["won"], "gross_pnl": pnl["gross_pnl"],
                "taker_fee": pnl["fee"], "net_pnl": pnl["net_pnl"],
                "shares": pnl["shares"],
                "settled_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", trade_id).execute()
        except Exception as e:
            logger.error(f"Settlement write failed: {e}")

    async def sync_wallet_balance(self, clob_client) -> Optional[float]:
        try:
            if hasattr(clob_client, 'get_balance'):
                bal = clob_client.get_balance()
                if bal is not None: return float(bal)
            from web3 import Web3
            rpc = os.environ.get("POLYGON_RPC_URL", "")
            funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
            if rpc and funder:
                w3 = Web3(Web3.HTTPProvider(rpc))
                USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                abi = [{"constant": True, "inputs": [{"name": "owner", "type": "address"}],
                        "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
                contract = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=abi)
                return contract.functions.balanceOf(Web3.to_checksum_address(funder)).call() / 1e6
        except Exception as e:
            logger.debug(f"Balance sync failed: {e}")
        return None

    async def get_recent_trades(self, mode: str, limit: int = 10) -> List[Dict]:
        table = "paper_settled" if mode == "paper" else "live_settled"
        try:
            resp = self._sb.table(table).select("*").order("created_at", desc=True).limit(limit).execute()
            return resp.data or []
        except Exception:
            return []

    async def get_pnl_summary(self, mode: str) -> Dict:
        table = "paper_settled" if mode == "paper" else "live_settled"
        try:
            resp = self._sb.table(table).select("won, net_pnl").not_.is_("won", "null").execute()
            trades = resp.data or []
            if not trades:
                return {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0, "win_rate": 0}
            wins = sum(1 for t in trades if t.get("won"))
            total_pnl = sum(float(t.get("net_pnl", 0)) for t in trades)
            return {"total_trades": len(trades), "wins": wins, "losses": len(trades) - wins,
                    "total_pnl": round(total_pnl, 2), "win_rate": round(wins / len(trades) * 100, 1)}
        except Exception:
            return {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0, "win_rate": 0}
