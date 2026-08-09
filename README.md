# Central Strategy Monitoring System

Production-ready FastAPI service that receives 30-second heartbeats from strategy instances running across EC2 servers.

## Folder Structure

```text
CentralizedAlgoSystem/
├── backend/
│   ├── __init__.py
│   ├── main.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   └── requirements.txt
├── dashboard/
│   └── streamlit_app.py
├── strategy_agent/
│   └── agent.py
├── alerts/
│   └── telegram.py
├── analytics/
│   └── reports.py
├── data/
│   ├── .gitkeep
│   └── tracker.db   # created automatically at runtime
├── pyproject.toml
└── README.md
```

## Data Tracked Per Strategy Heartbeat

- Strategy Name
- Server Name
- Status (`RUNNING`, `STOPPED`, `ERROR`)
- Current MTM
- Day P&L
- Number of Trades
- Last Update Time
- Received At (server-side timestamp)

## API Endpoints

- `POST /update_strategy`: create or update latest heartbeat for a strategy/server pair
- `GET /strategies`: list all strategy states (latest first)
- `GET /health`: service health and UTC timestamp

## Startup Instructions

```bash
cd /Users/samirkuila/myproject/CentralizedAlgoSystem
python3 -m pip install -r backend/requirements.txt
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

Open API docs in browser:

- `http://127.0.0.1:8000/docs`

## Basic Test Commands Using curl

```bash
curl -s http://127.0.0.1:8000/health
```

```bash
curl -s -X POST http://127.0.0.1:8000/update_strategy \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_name": "mean_reversion_v1",
    "server_name": "ec2-ap-south-1a-i-01",
    "status": "RUNNING",
    "current_mtm": 1540.25,
    "day_pnl": 420.75,
    "number_of_trades": 18,
    "last_update_time": "2026-07-24T10:00:00Z"
  }'
```

```bash
curl -s http://127.0.0.1:8000/strategies
```

## Streamlit Dashboard

Install dashboard dependencies:

```bash
cd /Users/samirkuila/myproject/CentralizedAlgoSystem
python3 -m pip install -r dashboard/requirements.txt
```

Run dashboard:

```bash
streamlit run dashboard/streamlit_app.py
```

Dashboard highlights:

- KPI cards: running strategies, stopped/error strategies, total day P&L, total MTM
- Color-coded status table: green (`RUNNING`), red (`STOPPED`/`ERROR`), yellow for stale heartbeat (>2 minutes)
- Auto refresh every 30 seconds
- Plotly charts for strategy-wise day P&L and MTM

## Lightweight Strategy Agent (Per Strategy Process)

Use `strategy_agent/agent.py` to run a non-blocking heartbeat sender with `requests` and retry logic.

Install dependency:

```bash
cd /Users/samirkuila/myproject/CentralizedAlgoSystem
python3 -m pip install -r strategy_agent/requirements.txt
```

Example integration file:

- `strategy_agent/sample_trading_strategy.py`

Core API exposed by the agent:

- `update_metrics(mtm, pnl, trade_count, status)`

Quick run (with backend running on port 8000):

```bash
cd /Users/samirkuila/myproject/CentralizedAlgoSystem
python3 -m strategy_agent.sample_trading_strategy
```
