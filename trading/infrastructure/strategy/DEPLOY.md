# Strategy EC2 deployment runbook

Deploy **one strategy at a time**, **paper only** (`TRADING_MODE=paper`).
Routine process control goes through **Dashboard/API → Lambda → SSM →
`trading_agent.py`**, never SSH.

```
API  ──▶ TradingOrchestrator (Lambda) ──▶ SSM SendCommand ──▶ Strategy EC2
                                                              trading_agent.py START|STOP|RESTART|STATUS|LOGS
                                                              └─▶ example_strategy/main.py  (PaperBroker)
                                                                    ├─ heartbeat  → POST /api/heartbeat
                                                                    ├─ log ship   → POST /api/logs
                                                                    └─ pnl/pos    → POST /api/pnl,/positions
```

## Requirements met by this layout

| Requirement | How |
|---|---|
| IAM instance role + SSM | `TradingEC2SSMProfile` (SSM core) on the instance; box shows `Online` in SSM |
| no public application port | the strategy has no inbound listener; only `:22` is bound |
| process management | `trading/agent/trading_agent.py` — START/STOP/RESTART/STATUS/UPDATE/LOGS, PID + `state.json` + stop-flag files under `trading/data/` |
| automatic restart / recovery | `centralized-algo-strategy@<algo>.service` (boot) + `centralized-algo-strategy-watchdog@<algo>.timer` (every 20s; re-STARTs if the unit is active but STATUS≠RUNNING; respects `systemctl stop`) |
| graceful shutdown | `STOP_ALGO` / `systemctl stop` writes the stop-flag; the algo's `GracefulShutdown` finishes the tick, flushes state, disconnects the broker |
| logs | local rotating JSON at `trading/logs/<algo>.log`; curated events shipped to `POST /api/logs`; `trading_agent.py LOGS <algo> --lines N` |
| heartbeat | `ControlCenterHeartbeatAgent` → `POST /api/heartbeat` every `CONTROL_HEARTBEAT_INTERVAL_SECONDS` (10s); backend sets `algos.status` + `last_heartbeat` |
| paper only | `TRADING_MODE=paper`, `BROKER=paper`; `main.py` + adapters refuse live orders unless `TRADING_MODE` is exactly `live` |

## Steps

1. **Instance** — a **t3.small** in `ap-south-1` with `TradingEC2SSMProfile`
   attached, running, `Online` in SSM. (`aws ssm describe-instance-information`.)

2. **Code + venv** (via SSM `AWS-RunShellScript`, as root; `export HOME=/root`):
   ```sh
   git config --global --add safe.directory /trading-app
   git -C /trading-app fetch --depth 1 origin web-base-algo-trading-control
   git -C /trading-app reset --hard HEAD && git -C /trading-app clean -fd
   git -C /trading-app checkout -fB web-base-algo-trading-control origin/web-base-algo-trading-control
   chown -R ec2-user:ec2-user /trading-app
   cd /trading-app && sudo -u ec2-user python3 -m venv venv
   sudo -u ec2-user venv/bin/pip install -r trading/algos/example_strategy/requirements.txt
   ```

3. **Runtime env** — write `/trading-app/trading/.env` from `dot-env.example`
   (chmod 600, `chown ec2-user`). `API_BASE_URL` = the backend EC2;
   `CONTROL_API_KEY` = the backend's key.

4. **Register with the backend**:
   ```sh
   curl -X POST $API/api/servers -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"server_id":"strategy-01","ec2_instance_id":"i-...","region":"ap-south-1","os":"linux","repo_path":"/trading-app","auto_provision":false}'
   curl -X POST $API/api/algos   -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"algo_id":"example_strategy","server_id":"strategy-01"}'
   ```

5. **Install the units** (via SSM):
   ```sh
   install -m0755 /trading-app/trading/infrastructure/strategy/centralized-algo-agent /usr/local/bin/
   install -m0755 /trading-app/trading/infrastructure/strategy/centralized-algo-strategy-watchdog /usr/local/bin/
   cp /trading-app/trading/infrastructure/strategy/systemd/*.{service,timer} /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now centralized-algo-strategy@example_strategy.service
   systemctl enable --now centralized-algo-strategy-watchdog@example_strategy.timer
   ```

6. **Wire the API → Lambda control path** (admin identity):
   - `TradingOrchestrator` env → `API_BASE_URL` = backend EC2, `CONTROL_API_KEY`
     = backend key, `INSTANCE_ID`/`SERVER_NAME`/`REPO_PATH` for this box (or
     rely on per-server routing from the `servers` row).
   - Add `ssm:SendCommand` on this instance's ARN + `AWS-RunShellScript` to
     `TradingLambdaExecutionRole`.
   - Backend: `LAMBDA_FUNCTION_NAME=TradingOrchestrator` in
     `/etc/centralized-algo/backend.env`; add an inline `lambda:InvokeFunction`
     policy (scoped to the orchestrator ARN) to the backend's instance role.

## Verify (via API → Lambda → SSM, or SSM RunCommand of the same commands)

```
STATUS   -> {"status":"STOPPED"|...}
START    -> {"status":"RUNNING","pid":N}          ; /api/algos last_heartbeat starts advancing
LOGS     -> tail of trading/logs/<algo>.log       ; /api/logs shows START etc.
STOP     -> {"status":"STOPPED","graceful":true}
RESTART  -> {"start":{"status":"RUNNING",...}}
recovery -> kill -9 the pid; within ~20s the watchdog logs
            "status=ERROR while unit active -> START_ALGO" and a new pid appears;
            heartbeats resume
```

Keep `TRADING_MODE=paper`. No live orders.
