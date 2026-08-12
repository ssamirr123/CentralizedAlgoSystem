# Milestone 4 setup — deploy the orchestration Lambda

Same as Milestone 3: everything below runs **on your machine, with your
AWS credentials** — paste back real output at each step, don't just say
"done."

## 0. Update your CLI user's policy (new permissions needed)

The policy file changed (`iam_cli_user_policy.json`, one level up) to add
Lambda-creation permissions. Same as before, this has to go through the
**Console** — a user can't grant itself new permissions via the CLI:

1. [console.aws.amazon.com](https://console.aws.amazon.com) → **IAM** →
   **Users** → `trading-control-cli` → **Permissions** tab → inline
   policy **TradingControlCLI** → **Edit**
2. **JSON** tab → replace with the updated `../iam_cli_user_policy.json`
   → **Save changes**

Verify from the CLI:
```powershell
aws iam get-user-policy --user-name trading-control-cli --policy-name TradingControlCLI --region ap-south-1
```

## 1. Create the Lambda execution role

```powershell
cd trading/infrastructure/lambda

aws iam create-role `
  --role-name TradingLambdaExecutionRole `
  --assume-role-policy-document file://iam_lambda_execution_role.json

aws iam put-role-policy `
  --role-name TradingLambdaExecutionRole `
  --policy-name TradingLambdaExecutionPolicy `
  --policy-document file://iam_lambda_execution_policy.json
```

## 2. Package the Lambda

No external dependencies beyond `boto3` (already included in every AWS
Lambda Python runtime), so packaging is just zipping the one file:

```powershell
Compress-Archive -Path orchestrator.py -DestinationPath orchestrator.zip -Force
```

## 3. Create the function

```powershell
aws lambda create-function `
  --function-name TradingOrchestrator `
  --runtime python3.12 `
  --handler orchestrator.lambda_handler `
  --role arn:aws:iam::471112713822:role/TradingLambdaExecutionRole `
  --zip-file fileb://orchestrator.zip `
  --timeout 30 `
  --environment "Variables={INSTANCE_ID=i-0f60543b5534c332f,REPO_PATH=C:\trading-app}" `
  --region ap-south-1
```

Expect a JSON blob with `"State": "Active"` (or `"Pending"` — wait a few
seconds and `get-function` to confirm it becomes Active).

## 4. Test it — one invocation per action

Each of these writes the Lambda's JSON response to a local file (`out.json`)
since `aws lambda invoke` doesn't print the body directly.

**get_algo_status** (safe, read-only — good first test):
```powershell
aws lambda invoke `
  --function-name TradingOrchestrator `
  --cli-binary-format raw-in-base64-out `
  --payload '{\"action\":\"get_algo_status\",\"algo_id\":\"example_strategy\"}' `
  --region ap-south-1 `
  out.json
Get-Content out.json
```

Expected: since the repo isn't cloned onto the instance yet (that's still
blocked on Milestone 3's pending repo-visibility step), this will likely
come back with an SSM-level failure (can't find `trading_agent.py` at
`REPO_PATH`) rather than a real algo status — that's fine, it proves the
Lambda → SSM → instance round trip works; the algo-specific result is
blocked on finishing that Milestone 3 loose end.

**start_ec2** (only if you want to actually test power control — skip if
the instance should stay running):
```powershell
aws lambda invoke `
  --function-name TradingOrchestrator `
  --cli-binary-format raw-in-base64-out `
  --payload '{\"action\":\"start_ec2\"}' `
  --region ap-south-1 `
  out.json
Get-Content out.json
```
Expected: `{"success": true, "server_id": "i-...", "status": "RUNNING"}`
(if already running) or `"STARTING"`.

## 5. Check CloudWatch logs

```powershell
aws logs tail /aws/lambda/TradingOrchestrator --region ap-south-1 --since 10m
```

Expect to see the structured `LAMBDA_INVOKED` / `LAMBDA_RESULT` JSON log
lines from `orchestrator.py`.

## What I need back from you

Paste the actual output of steps 1, 3, 4 (both invocations), and 5. Don't
paraphrase errors — the exact text tells us exactly what's missing.
