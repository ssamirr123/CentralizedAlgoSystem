# Milestone 3 setup — AWS SSM control of the EC2 instance

Everything below has to be run **by you**, with your AWS credentials — I
have no AWS access from this environment. Run each step, then paste me
the actual output (not just "it worked") so I can confirm before we move
on, per your own rule about not assuming things worked.

Target: Windows EC2 instance `i-0f60543b5534c332f` in `ap-south-1`.

## 1. Create an IAM user and configure the AWS CLI locally

The IAM user has to be created through the **AWS Console (browser)**, not
the CLI — the CLI needs credentials to do anything, so it can't create
the very first credentials itself.

1. [console.aws.amazon.com](https://console.aws.amazon.com) → **IAM** →
   **Users** → **Create user** → name it `trading-control-cli` → Next →
   "Attach policies directly" → skip → Next → **Create user**
2. Click into the new user → **Permissions** tab → **Add permissions** →
   **Create inline policy** → **JSON** tab → paste the contents of
   `iam_cli_user_policy.json` (this folder) → Next → name it
   `TradingControlCLI` → **Create policy**
3. Still on the user's page → **Security credentials** tab → **Access
   keys** → **Create access key** → use case: **Command Line Interface
   (CLI)** → Next → **Create access key** — copy both values now, the
   secret is shown only this once

Install the CLI locally:
```powershell
winget install Amazon.AWSCLI
# or download https://awscli.amazonaws.com/AWSCLIV2.msi

aws --version
```

Configure it with the access key from step 3:
```powershell
aws configure
# AWS Access Key ID: <paste>
# AWS Secret Access Key: <paste>
# Default region: ap-south-1
# Default output format: json
```

Verify:
```powershell
aws sts get-caller-identity
```
Expect a JSON blob with your Account/UserId/Arn — if this fails, nothing
below will work.

## 2. Create the EC2 instance role (lets the instance talk to SSM)

This is a **different** role from your CLI user above — this one is
assumed by the EC2 instance itself.

```powershell
cd trading/infrastructure

aws iam create-role `
  --role-name TradingEC2SSMRole `
  --assume-role-policy-document file://iam_ec2_ssm_trust_policy.json

aws iam attach-role-policy `
  --role-name TradingEC2SSMRole `
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam create-instance-profile `
  --instance-profile-name TradingEC2SSMProfile

aws iam add-role-to-instance-profile `
  --instance-profile-name TradingEC2SSMProfile `
  --role-name TradingEC2SSMRole
```

`AmazonSSMManagedInstanceCore` is the AWS-managed policy with exactly
what the SSM Agent needs (register itself, receive commands, send
results) — nothing broader.

## 3. Attach the instance profile to your existing instance

```powershell
aws ec2 associate-iam-instance-profile `
  --instance-id i-0f60543b5534c332f `
  --iam-instance-profile Name=TradingEC2SSMProfile `
  --region ap-south-1
```

If the instance already had a different instance profile attached, this
will fail — tell me the error and we'll handle replacing it
(`aws ec2 describe-iam-instance-profile-associations` first).

## 4. Verify the SSM Agent picks it up

Takes a minute or two after step 3. Check via the console:
**AWS Console → Systems Manager → Fleet Manager (or "Managed instances")**
— `i-0f60543b5534c332f` should appear with status "Online" / "Managed".

Or via CLI:
```powershell
aws ssm describe-instance-information `
  --filters "Key=InstanceIds,Values=i-0f60543b5534c332f" `
  --region ap-south-1
```
Expect a non-empty `InstanceInformationList` with `"PingStatus": "Online"`.

If it doesn't show up after a few minutes: RDP into the instance and
check the agent service directly:
```powershell
Get-Service AmazonSSMAgent
# Expect: Status = Running
# If missing entirely (unusual on standard AWS Windows AMIs), download
# from https://docs.aws.amazon.com/systems-manager/latest/userguide/agent-install-windows.html
```

## 5. Install boto3 locally and run the connectivity test

```powershell
pip install boto3
```

```powershell
python trading/infrastructure/ssm_invoke.py `
  --instance-id i-0f60543b5534c332f `
  --region ap-south-1 `
  raw "Write-Output 'hello from SSM'; hostname"
```

Expected output: JSON with `"Status": "Success"` and
`"StandardOutputContent"` containing `hello from SSM` and the instance's
hostname. **This alone proves the SSM control-plane path works** — that's
the actual Milestone 3 deliverable.

## 6. (Optional, needs the repo deployed on the instance) — real algo control test

This additionally requires this repo + Python to actually be present on
the EC2 instance at some path. If it's not there yet, the simplest way to
get it there for now (proper deployment automation is a later concern):

```powershell
python trading/infrastructure/ssm_invoke.py `
  --instance-id i-0f60543b5534c332f --region ap-south-1 `
  raw "cd C:\; git clone https://github.com/<your-repo-url> trading-app"
```

Then:
```powershell
python trading/infrastructure/ssm_invoke.py `
  --instance-id i-0f60543b5534c332f --region ap-south-1 `
  --repo-path "C:\trading-app" `
  algo START_ALGO example_strategy
```

Expected: JSON with `"Status": "Success"` and `StandardOutputContent`
containing the same `{"algo": "example_strategy", "status": "RUNNING", ...}`
line you saw running `trading_agent.py` locally in Milestone 2.

## What I need back from you

Paste the actual output of steps 1, 4 (or the `describe-instance-information`
JSON), and 5. If any step errors, paste the exact error — don't paraphrase
it, the exact text usually tells us exactly what's missing.
