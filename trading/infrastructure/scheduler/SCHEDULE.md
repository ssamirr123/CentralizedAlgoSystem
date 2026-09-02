# Milestone 11 setup — automatic market schedule (EventBridge Scheduler)

Same pattern as Milestones 3/4: run this yourself with your AWS
credentials, paste back real output at each step.

## What this actually wires, vs. the master prompt's schedule literally

```
08:30 IST  start_ec2
08:50 IST  update_all_algos  <- git pull (10-min buffer before start)
09:00 IST  start_all_algos   <- algos start at market open
15:15 IST  stop_all_algos   <- "square-off" -- see note below
15:30 IST  stop_all_algos   <- safety-net retry, idempotent if 15:15 already worked
16:00 IST  stop_ec2
```

Two deviations from the literal spec, both explained, neither faked:

- **Square-off (15:15)**: a true "square off but keep the algo running"
  command would need a third command type beyond today's binary run/stop
  (a new IPC signal, a new strategy-interface hook) — real new plumbing,
  not scheduling infrastructure. What's actually wired: `stop_algo` at
  15:15, which triggers each strategy's existing `on_stop()` — the
  correct extension point for real square-off logic once a real strategy
  has real positions to close. `strategy.py`'s example block already
  shows the reporting pattern for this.
- **Log upload (15:45)**: not a separate step — Milestone 9's `LogShipper`
  already ships trading-significant events continuously as they happen,
  so there's nothing to batch-upload at day's end. Skipped rather than
  building a redundant no-op action.

## 0. Update your CLI user's policy (new permissions needed)

Console → IAM → Users → `trading-control-cli` → Permissions → inline
policy `TradingControlCLI` → Edit → JSON tab → replace with the updated
`../iam_cli_user_policy.json` → Save changes.

## 1. Create the EventBridge Scheduler execution role

```powershell
cd trading/infrastructure/scheduler

aws iam create-role `
  --role-name TradingSchedulerRole `
  --assume-role-policy-document file://iam_scheduler_role.json

aws iam put-role-policy `
  --role-name TradingSchedulerRole `
  --policy-name TradingSchedulerInvokeLambda `
  --policy-document file://iam_scheduler_policy.json
```

## 2. Redeploy the Lambda with the new env vars

The bulk actions (`start_all_algos` etc.) need `API_BASE_URL` and
`CONTROL_API_KEY` set on the Lambda — same values as your Vercel deploy's
env vars for those.

```powershell
cd ../lambda
Compress-Archive -Path orchestrator.py -DestinationPath orchestrator.zip -Force

aws lambda update-function-code `
  --function-name TradingOrchestrator `
  --zip-file fileb://orchestrator.zip `
  --region ap-south-1

aws lambda update-function-configuration `
  --function-name TradingOrchestrator `
  --environment "Variables={INSTANCE_ID=i-0f60543b5534c332f,REPO_PATH=C:\trading-app,API_BASE_URL=<your-vercel-api-url>,CONTROL_API_KEY=<your-control-api-key>}" `
  --region ap-south-1
```

## 3. Create the 6 schedules

All use `Asia/Kolkata` as the schedule's own timezone directly (EventBridge
Scheduler supports this natively — no manual UTC conversion, no DST-edge
bugs, and it matches "explicitly configured for Asia/Kolkata" exactly).
`MON-FRI` only — no weekend trading.

```powershell
cd ../scheduler
$lambdaArn = "arn:aws:lambda:ap-south-1:471112713822:function:TradingOrchestrator"
$roleArn = "arn:aws:iam::471112713822:role/TradingSchedulerRole"

aws scheduler create-schedule --name TradingSchedule-StartEC2 `
  --schedule-expression "cron(30 8 ? * MON-FRI *)" `
  --schedule-expression-timezone "Asia/Kolkata" `
  --flexible-time-window '{"Mode":"OFF"}' `
  --target "{\"Arn\":\"$lambdaArn\",\"RoleArn\":\"$roleArn\",\"Input\":\"{\\\"action\\\":\\\"start_ec2\\\"}\"}"

aws scheduler create-schedule --name TradingSchedule-UpdateAlgos `
  --schedule-expression "cron(50 8 ? * MON-FRI *)" `
  --schedule-expression-timezone "Asia/Kolkata" `
  --flexible-time-window '{"Mode":"OFF"}' `
  --target "{\"Arn\":\"$lambdaArn\",\"RoleArn\":\"$roleArn\",\"Input\":\"{\\\"action\\\":\\\"update_all_algos\\\"}\"}"

aws scheduler create-schedule --name TradingSchedule-StartAlgos `
  --schedule-expression "cron(0 9 ? * MON-FRI *)" `
  --schedule-expression-timezone "Asia/Kolkata" `
  --flexible-time-window '{"Mode":"OFF"}' `
  --target "{\"Arn\":\"$lambdaArn\",\"RoleArn\":\"$roleArn\",\"Input\":\"{\\\"action\\\":\\\"start_all_algos\\\"}\"}""

aws scheduler create-schedule --name TradingSchedule-SquareOff `
  --schedule-expression "cron(15 15 ? * MON-FRI *)" `
  --schedule-expression-timezone "Asia/Kolkata" `
  --flexible-time-window '{"Mode":"OFF"}' `
  --target "{\"Arn\":\"$lambdaArn\",\"RoleArn\":\"$roleArn\",\"Input\":\"{\\\"action\\\":\\\"stop_all_algos\\\"}\"}"

aws scheduler create-schedule --name TradingSchedule-StopAlgos `
  --schedule-expression "cron(30 15 ? * MON-FRI *)" `
  --schedule-expression-timezone "Asia/Kolkata" `
  --flexible-time-window '{"Mode":"OFF"}' `
  --target "{\"Arn\":\"$lambdaArn\",\"RoleArn\":\"$roleArn\",\"Input\":\"{\\\"action\\\":\\\"stop_all_algos\\\"}\"}"

aws scheduler create-schedule --name TradingSchedule-StopEC2 `
  --schedule-expression "cron(0 16 ? * MON-FRI *)" `
  --schedule-expression-timezone "Asia/Kolkata" `
  --flexible-time-window '{"Mode":"OFF"}' `
  --target "{\"Arn\":\"$lambdaArn\",\"RoleArn\":\"$roleArn\",\"Input\":\"{\\\"action\\\":\\\"stop_ec2\\\"}\"}"
```

## 4. Verify

```powershell
aws scheduler list-schedules --region ap-south-1
```
Expect 6 schedules named `TradingSchedule-*`, all `State: ENABLED`.

Test one manually without waiting for its trigger time:
```powershell
aws lambda invoke --function-name TradingOrchestrator `
  --cli-binary-format raw-in-base64-out `
  --payload '{\"action\":\"start_all_algos\"}' `
  --region ap-south-1 `
  out.json
Get-Content out.json
```
Expected: JSON with `results` covering every algo currently in `GET
/api/algos` with `enabled: true` — empty `results: []` is correct if none
are registered/enabled yet.

## Stage 15 update — deployed state + a coordination gap

Deployed and verified against the EC2 backend (2026-08-29):

- Lambda `TradingOrchestrator` code updated (adds `restart_ec2` + the
  safe-stop guard); env repointed to `API_BASE_URL=http://<backend-ec2>`
  and the backend's `CONTROL_API_KEY`; `INSTANCE_ID` = the strategy box.
- Inline policy `TradingOrchestratorEC2SSMExplicit` on
  `TradingLambdaExecutionRole` grants `ec2:Start/Stop/RebootInstances` +
  `ssm:SendCommand` on the two explicit instance ARNs (see
  `../lambda/iam_orchestrator_ec2ssm_policy.json`) — IAM role, no keys.
- All 6 `TradingSchedule-*` exist, ENABLED, `Asia/Kolkata`, `MON-FRI`.
- Verified `check_ec2_health` / `start_ec2` / `stop_ec2` / `restart_ec2`
  for an explicit `instance_id`; safe-stop guard blocks `stop_ec2` /
  `restart_ec2` while a strategy process is alive; `stop_all_algos` reads
  the algo list from the EC2 backend and stops each via SSM.
- Legacy schedules `ec2_start` / `ec2_stop` still exist (predate the
  `TradingSchedule-*` set) — review and delete; they are redundant.

**Coordination gap (needs a decision):** Stage 14's strategy box runs a
systemd watchdog (`centralized-algo-strategy-watchdog@<algo>.timer`) that
restarts the algo within ~20s whenever its unit is `active` but the
process isn't RUNNING. So `stop_all_algos` at 15:15 / 15:30 stops the
*process*, the watchdog restarts it, and `stop_ec2` at 16:00 then hits
the safe-stop guard and **refuses to stop the box** (correct, but the box
never powers down). Pick one:

1. Add a schedule step / action that also `systemctl stop`s the
   per-strategy units before 16:00 (a deliberate end-of-day stop), or
2. Make `STOP_ALGO` (in `trading_agent.py` or the Lambda's SSM command)
   also `systemctl stop centralized-algo-strategy@<algo>` when that unit
   exists, or
3. Have `stop_ec2`'s schedule pass `force:true` (loses the safety check —
   not recommended).

Until one of these is done, the market-day auto-stop of the strategy EC2
will not complete while the watchdog is enabled.

## What I need back from you

Paste the actual output of steps 1, 3 (`list-schedules`), and the manual
`start_all_algos` invoke in step 4.

## Fleet EC2 power — replaces the single-instance StartEC2 / StopEC2

`TradingSchedule-StartEC2` / `-StopEC2` only ever act on the Lambda's
`INSTANCE_ID` env var (one hard-coded box). Any server added later via
`POST /api/servers` was never powered on/off (e.g. `samir_linux` ran 24/7).

Fix: orchestrator actions `start_all_ec2` / `stop_all_ec2` iterate every
server in `GET /api/servers` (same pattern as `*_all_algos`), so a new
server is auto-covered with no per-server rule. `stop_all_ec2` keeps each
box's own safe-stop guard. The control-plane backend EC2 is **not**
registered as a server, so it is never a target.

```powershell
$lambdaArn = "arn:aws:lambda:ap-south-1:471112713822:function:TradingOrchestrator"
$roleArn   = "arn:aws:iam::471112713822:role/TradingSchedulerRole"

# 1. redeploy the Lambda with the new actions
cd trading/infrastructure/lambda
Compress-Archive -Path orchestrator.py -DestinationPath orchestrator.zip -Force
aws lambda update-function-code --function-name TradingOrchestrator `
  --zip-file fileb://orchestrator.zip --region ap-south-1

# 2. drop the single-instance EC2 schedules
aws scheduler delete-schedule --name TradingSchedule-StartEC2 --region ap-south-1
aws scheduler delete-schedule --name TradingSchedule-StopEC2  --region ap-south-1

# 3. create the fleet schedules (same times: 08:30 / 16:00 IST, MON-FRI)
aws scheduler create-schedule --name TradingSchedule-StartAllEC2 `
  --schedule-expression "cron(30 8 ? * MON-FRI *)" `
  --schedule-expression-timezone "Asia/Kolkata" `
  --flexible-time-window '{"Mode":"OFF"}' `
  --target "{\"Arn\":\"$lambdaArn\",\"RoleArn\":\"$roleArn\",\"Input\":\"{\\\"action\\\":\\\"start_all_ec2\\\"}\"}"

aws scheduler create-schedule --name TradingSchedule-StopAllEC2 `
  --schedule-expression "cron(0 16 ? * MON-FRI *)" `
  --schedule-expression-timezone "Asia/Kolkata" `
  --flexible-time-window '{"Mode":"OFF"}' `
  --target "{\"Arn\":\"$lambdaArn\",\"RoleArn\":\"$roleArn\",\"Input\":\"{\\\"action\\\":\\\"stop_all_ec2\\\"}\"}"
```

Verify:
```powershell
aws lambda invoke --function-name TradingOrchestrator `
  --cli-binary-format raw-in-base64-out `
  --payload '{\"action\":\"start_all_ec2\"}' --region ap-south-1 out.json
Get-Content out.json
```
Expect `results` with one entry per row in `GET /api/servers`
(`server_name` + `status`). `stop_all_ec2` entries for a box with a live
strategy come back `success:false, safe_stop:"blocked"` — expected; that
box stays up until its algos are stopped (15:30 `stop_all_algos`).

Note the Stage-15 coordination gap above still applies per box: the
strategy watchdog units must be torn down (or `STOP_ALGO` must also
`systemctl stop` them) for `stop_all_ec2` to actually power a box down at
16:00.
