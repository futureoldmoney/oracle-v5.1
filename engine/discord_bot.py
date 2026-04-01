"""
Discord Bot v5.2
=================
Polls Discord channel for !commands, responds via webhook.

Commands:
  !status  — bot health, bankroll, running P&L
  !trades  — recent trades with outcomes
  !pnl     — full P&L summary with running balance
  !mode    — switch paper/live
  !config  — show current config
  !set     — update config value
  !help    — command list
"""

import os
import time
import asyncio
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("oracle.discord")


class DiscordBot:
    """Polls Discord channel for commands, responds via webhook."""

    def __init__(self):
        self._token = os.environ["DISCORD_BOT_TOKEN"]
        self._channel_id = os.environ["DISCORD_CHANNEL_ID"]
        self._webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

        from supabase import create_client
        self.sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

        from engine.db import DatabaseOps
        self.db = DatabaseOps(self.sb)

        self._last_message_id = None
        self._poll_interval = 2.0

    async def run(self):
        """Main polling loop."""
        logger.info("Discord bot started")
        headers = {"Authorization": f"Bot {self._token}"}

        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            while True:
                try:
                    url = f"https://discord.com/api/v10/channels/{self._channel_id}/messages"
                    params = {"limit": 5}
                    if self._last_message_id:
                        params["after"] = self._last_message_id

                    resp = await client.get(url, params=params)
                    if resp.status_code == 200:
                        messages = resp.json()
                        # Process oldest first
                        for msg in reversed(messages):
                            self._last_message_id = msg["id"]
                            content = msg.get("content", "").strip()
                            # Ignore bot messages and non-commands
                            if msg.get("author", {}).get("bot"):
                                continue
                            if content.startswith("!"):
                                await self._handle_command(content)
                    elif resp.status_code == 429:
                        retry = resp.json().get("retry_after", 5)
                        await asyncio.sleep(retry)
                except Exception as e:
                    logger.debug(f"Discord poll error: {e}")

                await asyncio.sleep(self._poll_interval)

    async def _handle_command(self, content: str):
        """Route command to handler."""
        parts = content.split()
        cmd = parts[0].lower()
        args = parts[1:]

        try:
            if cmd == "!status":
                await self._cmd_status()
            elif cmd == "!trades":
                await self._cmd_trades()
            elif cmd == "!pnl":
                await self._cmd_pnl()
            elif cmd == "!mode":
                await self._cmd_mode(args)
            elif cmd == "!config":
                await self._cmd_config()
            elif cmd == "!set":
                await self._cmd_set(args)
            elif cmd == "!help":
                await self._cmd_help()
            else:
                await self._reply(f"Unknown command: `{cmd}`. Try `!help`")
        except Exception as e:
            await self._reply(f"Error: {e}")

    async def _cmd_status(self):
        """Show bot health with running P&L."""
        try:
            # Get latest heartbeat
            hb = self.sb.table("heartbeats_v2").select(
                "*").order("created_at", desc=True).limit(1).execute()

            # Get P&L summary
            mode = await self._get_mode()
            pnl = await self.db.get_pnl_summary(mode)

            if hb.data:
                h = hb.data[0]
                age = "?"
                if h.get("created_at"):
                    created = datetime.fromisoformat(h["created_at"].replace("Z", "+00:00"))
                    age = f"{int((datetime.now(timezone.utc) - created).total_seconds())}s ago"

                # Show running balance
                balance = pnl["current_balance"]
                total_pnl = pnl["total_pnl"]
                pnl_emoji = "📈" if total_pnl >= 0 else "📉"
                pnl_sign = "+" if total_pnl >= 0 else ""

                await self._reply(
                    f"**Oracle Bot v5**\n"
                    f"Mode: `{h.get('bot_mode', '?')}`\n"
                    f"Balance: `${balance:.2f}` {pnl_emoji} `{pnl_sign}${total_pnl:.2f}`\n"
                    f"Trades: `{pnl['total_trades']}` | W/L: `{pnl['wins']}/{pnl['losses']}` | WR: `{pnl['win_rate']}%`\n"
                    f"Status: `{h.get('status', '?')}` | Heartbeat: `{age}`")
            else:
                await self._reply("No heartbeat data found")
        except Exception as e:
            await self._reply(f"Status failed: {e}")

    async def _cmd_trades(self):
        """Show recent trades with v5 columns."""
        mode = await self._get_mode()
        trades = await self.db.get_recent_trades(mode, limit=5)
        if not trades:
            await self._reply("No trades found")
            return

        lines = ["**Recent Trades:**"]
        for t in trades:
            won = t.get("won")
            if won is True:
                emoji = "✅"
            elif won is False:
                emoji = "❌"
            else:
                emoji = "⏳"

            side = t.get("side", "?")
            direction = t.get("implied_direction", "?")
            fill = float(t.get("fill_price") or t.get("hypothetical_price") or 0)
            size = float(t.get("size_usd") or t.get("hypothetical_size_usdc") or 0)
            pnl = float(t.get("net_pnl") or t.get("pnl_usdc") or 0)
            edge = float(t.get("edge_pct") or 0)

            pnl_str = f"${pnl:+.2f}" if won is not None else "pending"

            lines.append(
                f"{emoji} {side} {direction} | "
                f"fill=${fill:.3f} edge={edge:.1f}% | "
                f"${size:.2f} → **{pnl_str}**")

        await self._reply("\n".join(lines))

    async def _cmd_pnl(self):
        """Show full P&L summary with running balance."""
        mode = await self._get_mode()
        pnl = await self.db.get_pnl_summary(mode)

        total = pnl["total_pnl"]
        pnl_sign = "+" if total >= 0 else ""
        pnl_emoji = "📈" if total >= 0 else "📉"

        await self._reply(
            f"**P&L Summary ({mode})** {pnl_emoji}\n"
            f"Starting: `${pnl['starting_bankroll']:.2f}`\n"
            f"Current:  `${pnl['current_balance']:.2f}` (`{pnl_sign}${total:.2f}`)\n"
            f"Trades: `{pnl['total_trades']}` | "
            f"W/L: `{pnl['wins']}/{pnl['losses']}` | "
            f"Win rate: `{pnl['win_rate']}%`")

    async def _cmd_mode(self, args):
        """Switch mode."""
        if not args:
            mode = await self._get_mode()
            await self._reply(f"Current mode: `{mode}`\nUsage: `!mode paper` or `!mode live`")
            return

        new_mode = args[0].lower()
        if new_mode not in ("paper", "live"):
            await self._reply("Mode must be `paper` or `live`")
            return

        success = await self.db.set_mode(new_mode)
        if success:
            await self._reply(f"Mode switched to `{new_mode}` ✓")
        else:
            await self._reply("Mode switch failed")

    async def _cmd_config(self):
        """Show current config."""
        config = await self.db.load_config()
        if not config:
            await self._reply("Config not found")
            return

        keys = ["bankroll", "fractional_kelly", "min_edge_pct", "min_position_pct",
                "max_position_pct", "taker_fee_rate", "max_daily_loss_pct"]
        lines = ["**Config:**"]
        for k in keys:
            if k in config:
                lines.append(f"`{k}`: {config[k]}")
        await self._reply("\n".join(lines))

    async def _cmd_set(self, args):
        """Update config value."""
        if len(args) < 2:
            await self._reply("Usage: `!set <key> <value>`")
            return

        key, value = args[0], args[1]
        try:
            value = float(value)
        except ValueError:
            await self._reply("Value must be a number")
            return

        success = await self.db.update_config(key, value)
        if success:
            await self._reply(f"Config `{key}` → `{value}` ✓")
        else:
            await self._reply(f"Failed to update `{key}`")

    async def _cmd_help(self):
        await self._reply(
            "**Oracle Bot v5 Commands:**\n"
            "`!status` — health + running P&L\n"
            "`!trades` — recent trades\n"
            "`!pnl` — full P&L breakdown\n"
            "`!mode [paper|live]` — switch mode\n"
            "`!config` — show config\n"
            "`!set <key> <value>` — update config")

    async def _reply(self, message: str):
        """Send message via webhook."""
        if not self._webhook_url:
            logger.info(f"Discord reply (no webhook): {message[:100]}")
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(self._webhook_url, json={"content": message})
        except Exception as e:
            logger.error(f"Webhook send failed: {e}")

    async def _get_mode(self) -> str:
        try:
            resp = self.sb.table("bot_control").select("value").eq(
                "key", "mode").limit(1).execute()
            if resp.data:
                return resp.data[0]["value"]
        except Exception:
            pass
        return "paper"
