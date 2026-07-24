# Signal Desk (Python) — pump.fun momentum trading bot

A Python port of [Signal Desk](https://github.com/robnye0-tech/Pump.fun) — a self-hosted
bot that watches real pump.fun token launches and trades in real time (via
[PumpPortal](https://pumpportal.fun)'s free data feed), paper-trades a momentum
strategy against them by default, and can optionally place real trades from a wallet
you control. Same design and dashboard as the Node.js version, same safety rails —
this is a language port, not a different bot. The dashboard (`public/`) is unchanged
byte-for-byte between the two; only the backend differs (FastAPI + `asyncio` instead
of Express + Node's event loop).

**Paper trading is the default and always available.** A separate, off-by-default
**live trading** mode can place real trades with real SOL from a wallet you control —
it requires deliberately setting it up and re-arming it every server start. Read the
live trading section in full before enabling it; it moves real money.

## Setup

Requires Python 3.11+.

### Easiest: double-click to launch

- **Mac:** double-click `Start-Mac.command`. First time, macOS Gatekeeper will block
  it — right-click, choose "Open", confirm. After that it opens normally.
- **Windows:** double-click `Start-Windows.bat`.
- **Linux:** double-click `Start-Linux.sh`, or `./Start-Linux.sh` in a terminal (may
  need `chmod +x Start-Linux.sh` once).

Any of these creates a virtual environment and installs dependencies on first run
(needs internet the very first time), then starts the server and opens the dashboard
in your browser. Leave the terminal window alone — closing it stops the bot.

### Manual

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Then open **http://localhost:3000**. On your phone, if reachable on your network (or
deployed with a real URL), open that URL and use "Add to Home Screen" — same PWA setup
as the Node version.

### Building a double-clickable .exe (optional, Windows)

`Start-Windows.bat` already gives double-click launching without a compiled binary. If
you want a single `.exe`, build it **on your own Windows machine** with
[PyInstaller](https://pyinstaller.org/):

```powershell
pip install pyinstaller
pyinstaller --onefile --add-data "public;public" --name SignalDesk server.py
```

**This has not been run or tested** — build it yourself and verify it works before
relying on it. The `.exe` still reads/writes `data/` and `wallet/` next to wherever you
run it from. Compiling it changes nothing about the risk profile — read and trust the
source before running it with a funded wallet either way.

## Getting real data

Same as the Node version: flip "Live pump.fun" in the Data Source panel for free
new-token/migration data. A [PumpPortal](https://pumpportal.fun/data-api/real-time/)
API key (small SOL-funded wallet on their end, metered) enables real price/volume
momentum. If PumpPortal's message schema changes, use "show raw feed (debug)" and
adjust `_handle_pump_message` in `server.py`.

## Live trading (real funds) — read this before touching it

There is a real execution layer: the **Live trading** panel in the dashboard places
actual trades with actual SOL from a wallet you control. Off by default, and stays off
across restarts no matter what — you re-arm it every time the server starts.

**How it works, and why it's built this way:**

- Non-custodial. Your private key lives in one local file (`wallet/keypair.json` by
  default, gitignored — never committed, never sent anywhere, never leaves this
  process except to sign a transaction locally).
- Trades are built by [PumpPortal's local transaction API](https://pumpportal.fun/local-trading-api/trading-api/)
  — the same provider your data feed uses. They return an *unsigned* transaction;
  `live_trader.py` signs it locally with `solders`, **simulates it on-chain first**,
  and only then submits it. A failed simulation aborts before anything is sent.
- **This could not be tested against the live network or even installed from the
  sandbox this was built in** — it blocks essentially all outbound traffic including
  PyPI, so `pip install` itself was never run here, let alone a live trade. Two things
  specifically were written from recollection rather than verified against running
  code:
  1. PumpPortal's `/api/trade-local` request/response shape — check
     https://pumpportal.fun/local-trading-api/trading-api/ against the `httpx.post`
     call in `live_trader.py`.
  2. The `solders` Python API used to deserialize/sign/serialize the transaction — if
     your installed `solders` version's method names differ, you'll get an
     `AttributeError` pointing exactly at what changed; check
     https://github.com/kevinheavey/solders for the current API.

  Test with the smallest possible amount first (`maxSolPerTrade: 0.01` or lower)
  regardless of how confident this looks.

### Setting it up

1. **Get a keypair file** at `wallet/keypair.json` — a JSON array of 64 numbers (the
   standard Solana secret-key format). `solana-keygen new -o wallet/keypair.json`, or
   export raw secret-key bytes from Phantom/Solflare. Treat this file like a seed
   phrase.
2. Fund that wallet with a small amount of SOL — a dedicated hot wallet, not your main
   one.
3. Get a real RPC endpoint (Helius/QuickNode/Triton free tier — the public default is
   rate-limited and slow for competitive entries). Paste it into "Solana RPC URL".
4. Set `Max SOL per trade` and `Daily loss limit (SOL)` to amounts you're genuinely
   fine losing entirely.
5. Type `ENABLE LIVE TRADING` exactly into the confirm box and enable. If the wallet
   file can't be found/parsed or the RPC is unreachable, it tells you why and stays
   off.
6. Live trading only runs while data mode is **Live pump.fun** and while enabled — the
   red **STOP LIVE TRADING NOW** button disables it immediately, and any unexpected
   error in the live loop disables it automatically.

### Safety rails specific to live trading

- `Max SOL per trade` × `Max concurrent positions` hard-caps total exposure.
- `Daily loss limit (SOL)` halts new entries for the day once hit; open positions still
  manage their own exits.
- Reuses the same momentum/take-profit/stop-loss/trailing-stop thresholds from
  Parameters — self-tune only ever learns from paper trades, never live ones.
- Every trade logs its transaction signature linked to Solscan.

### Before you actually turn this on

- Compare live-market behavior against what the simulator predicted for weeks, not
  days, before trusting real size.
- Start with position sizes far smaller than your final target — paper fills are
  frictionless in a way real fills on pump.fun (slippage, failed transactions,
  front-running, rug pulls) are not.
- pump.fun tokens are extremely volatile; a good paper run is not a guarantee.

## Daily reports / noon check-in

Same as the Node version: set an [ntfy.sh](https://ntfy.sh) topic in the dashboard for
a push notification at noon server time and whenever a trading day rolls over.

## Running it continuously

`python server.py` runs in the foreground. For real background operation, use a
process manager (`pm2 start server.py --interpreter venv/bin/python --name signal-desk`,
or `systemd`/`supervisord`), or a `screen`/`tmux` session. For 24/7 uptime independent
of your own machine, deploy to a small always-on host (Railway, Render, Fly.io, a
cheap VPS) — set the `PORT` environment variable if required.

## Paper trading balance

Same as the Node version — a virtual account starting at $1000, adjustable in the
dashboard, used only to size simulated trades.

## Safety rails (paper trading)

Same daily loss limit / profit floor+cap / self-tune behavior as the Node version —
see the dashboard's Parameters panel.

## Project structure

```
signal-desk-python/
  server.py       — FastAPI + WebSocket server, PumpPortal client, trading engine loop
  engine.py        — pure trading logic: momentum signal, self-tuning, feedback text
  state_store.py   — simple JSON file persistence (./data/state.json)
  wallet.py        — loads your local Solana keypair, minimal raw JSON-RPC client
  live_trader.py   — builds/signs/simulates/submits real trades via PumpPortal
  wallet/          — put your keypair.json here (gitignored, never committed)
  public/          — the dashboard (plain HTML/CSS/JS, identical to the Node version)
  requirements.txt
```
