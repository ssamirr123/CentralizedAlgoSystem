# Stage 13 — Backend EC2 deployment runbook

Deploy **only** the centralized FastAPI backend + its local PostgreSQL.
**No trading strategies. `TRADING_MODE=paper`.**

```
Internet ─▶ EC2 (ap-south-1) ─▶ Nginx :443/:80 ─▶ 127.0.0.1:8000 (uvicorn/FastAPI)
                                                 └─▶ PostgreSQL (container, private network only)
```

---

## Why you run this, not me

I checked the account from here (`trading-control-cli`,
`471112713822`, `ap-south-1`) and **cannot complete this stage**:

- `trading-control-cli` has **no IAM permissions** (`iam:GetRole`,
  `iam:CreateRole`, `iam:ListInstanceProfiles` all denied) — it cannot
  create the instance role or attach an instance profile.
- The target box is **not in SSM** (no instance profile → SSM agent can't
  register), so it can't be managed remotely.
- SSH is locked to `122.171.17.136/32` with key `samirec2key`; I have
  neither.

Everything below is for you to run with an admin/console identity + SSH.
Paste the real output of the verification step back so it can be
confirmed (per your own "don't assume it worked" rule).

---

## What already exists (discovered, read-only)

| Thing | Value |
|---|---|
| Instance | `i-0f344752a1ca2811b`  ·  name `algo-backend`  ·  **t3.medium**  ·  **running**  ·  launched today |
| Public IP | **Elastic IP `13.232.95.211`** (was ephemeral `13.206.203.145` until Stage 17 — the old IP rotated on a stop/start and got reassigned elsewhere; an EIP is now attached so it is stable) |
| AZ / VPC / subnet | `ap-south-1a` / `vpc-0100f18cab8c38e01` / `subnet-0acefe8fe1b871c81` |
| Security group | `sg-04771e006e4097181` (`trading-sg`) — **only** inbound rule today: `tcp/22` from `122.171.17.136/32` |
| Key pair | `samirec2key` |
| Instance profile | **none** (this is the main gap) |
| Platform | Linux/UNIX (assume Amazon Linux 2023) |

> The instance is **t3.medium**, not the `t3.small` named in the Stage 13
> brief. The master architecture says the Backend EC2 is t3.medium, so
> keeping it is fine. If you want t3.small: `aws ec2 stop-instances` →
> `aws ec2 modify-instance-attribute --instance-type t3.small` → start.

---

## Decisions you need to make first

1. **Domain / TLS.** Do you have a DNS name to point at `13.206.203.145`
   (or an Elastic IP)? If yes → Let's Encrypt via `certbot --nginx`. If
   not yet → serve plain HTTP on `:80` for the smoke test, then add TLS
   before anything real uses it. **Do not leave it HTTP-only.**
2. **Security group.** Recommended: a **dedicated** SG
   (`centralized-algo-backend-sg`) rather than adding web ports to the
   shared `trading-sg`. Steps below create one.
3. **SSH source.** Keep `122.171.17.136/32`, or your current IP —
   `curl -s https://checkip.amazonaws.com`.

---

## 1. IAM instance role  *(admin identity)*

```bash
ACCT=471112713822 ; REGION=ap-south-1
ROLE=CentralizedAlgoBackendRole

aws iam create-role --role-name "$ROLE" \
  --assume-role-policy-document file://trading/infrastructure/backend/iam_backend_role_trust_policy.json

# managed policies cover SSM + CloudWatch agent (preferred over the inline JSON)
aws iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

aws iam create-instance-profile --instance-profile-name "$ROLE"
aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE"

# attach to the running instance (needs iam:PassRole on the role)
aws ec2 associate-iam-instance-profile --region "$REGION" \
  --instance-id i-0f344752a1ca2811b \
  --iam-instance-profile Name="$ROLE"
```

`trading/infrastructure/backend/iam_backend_role_policy.json` is the
explicit least-privilege equivalent if you prefer an inline policy you can
read. It contains **no** database, broker, or `lambda:InvokeFunction`
grant — the backend needs none of those for Stage 13. **No AWS access
keys anywhere** — the instance role is the only credential source.

Wait ~2 min, then confirm SSM sees it:

```bash
aws ssm describe-instance-information --region "$REGION" \
  --filters Key=InstanceIds,Values=i-0f344752a1ca2811b \
  --query "InstanceInformationList[].PingStatus"
# -> [ "Online" ]
```

---

## 2. Security group  *(admin identity)*

```bash
REGION=ap-south-1 ; VPC=vpc-0100f18cab8c38e01
MY_IP="$(curl -s https://checkip.amazonaws.com)/32"

SG=$(aws ec2 create-security-group --region "$REGION" \
  --group-name centralized-algo-backend-sg \
  --description "Backend EC2: HTTPS/HTTP in, SSH from operator only" \
  --vpc-id "$VPC" --query GroupId --output text)

aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" \
  --ip-permissions \
    IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges='[{CidrIp=0.0.0.0/0,Description=https}]' \
    IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges='[{CidrIp=0.0.0.0/0,Description=http-redirect+acme}]' \
    IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges="[{CidrIp=$MY_IP,Description=ssh-operator}]"

# swap the instance onto the new SG (replaces trading-sg for this box)
aws ec2 modify-instance-attribute --region "$REGION" \
  --instance-id i-0f344752a1ca2811b --groups "$SG"
```

- **No `5432` rule** — PostgreSQL is never reachable from outside.
- Only `443`, `80`, and `22` (SSH from your IP only) are open.
- Outbound stays default-allow.

---

## 3. Platform: SSH in, install Docker + Nginx  *(SSH)*

```bash
ssh -i /path/to/samirec2key.pem ec2-user@13.206.203.145
```

Either paste `trading/infrastructure/backend/user-data.sh` (it installs
git, nginx, docker, docker-compose v2, the CloudWatch agent, makes the
dirs, clones the repo), or, on AL2023, by hand:

```bash
sudo dnf install -y git nginx docker amazon-cloudwatch-agent
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user   # re-login for this to take effect
sudo mkdir -p /usr/libexec/docker/cli-plugins
sudo curl -sSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o /usr/libexec/docker/cli-plugins/docker-compose
sudo chmod +x /usr/libexec/docker/cli-plugins/docker-compose

sudo mkdir -p /opt/centralized-algo /var/lib/centralized-algo/pgdata \
              /var/log/centralized-algo /etc/centralized-algo /etc/ssl/centralized-algo
sudo chmod 700 /etc/centralized-algo
```

---

## 4. Application code  *(SSH)*

```bash
sudo git clone --branch web-base-algo-trading-control \
  https://github.com/ssamirr123/CentralizedAlgoSystem.git /opt/centralized-algo/app
sudo chown -R ec2-user:ec2-user /opt/centralized-algo/app
cd /opt/centralized-algo/app
```

---

## 5. Environment configuration  *(SSH — secrets stay off git and out of the image)*

**5a. compose interpolation file** — `/opt/centralized-algo/app/.env`
(git-ignored; only `${VAR}` substitution for the compose files):

```dotenv
POSTGRES_USER=algo
POSTGRES_PASSWORD=<generate: openssl rand -hex 24>
POSTGRES_DB=centralized_algo
BACKEND_PORT=127.0.0.1:8000          # <-- binds uvicorn to loopback only
```

**5b. runtime env** — `/etc/centralized-algo/backend.env`
(root-owned, `chmod 600`, referenced by `docker-compose.prod.yml`):

```dotenv
DATABASE_URL=postgresql://algo:<same POSTGRES_PASSWORD>@postgres:5432/centralized_algo
CONTROL_API_KEY=<generate: openssl rand -hex 32>
TRADING_MODE=paper
LOG_LEVEL=INFO
# DISABLE_BACKGROUND_WATCHER  -> leave UNSET: this is a long-lived box, keep the watcher on
# NO AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  -> the instance role provides AWS creds
# TELEGRAM_* / broker creds  -> not needed for Stage 13
```

```bash
sudo tee /etc/centralized-algo/backend.env >/dev/null <<'EOF'
DATABASE_URL=postgresql://algo:REPLACE@postgres:5432/centralized_algo
CONTROL_API_KEY=REPLACE
TRADING_MODE=paper
LOG_LEVEL=INFO
EOF
sudo chmod 600 /etc/centralized-algo/backend.env
```

---

## 6. PostgreSQL + 7. Alembic + 8. Start the stack  *(SSH)*

PostgreSQL runs as the `postgres` container (Stage 11's compose), data in
`/var/lib/centralized-algo/pgdata`, **no host port**. The `backend`
container's entrypoint runs **`alembic upgrade head`** before uvicorn
serves — so migrations are applied on every start.

```bash
cd /opt/centralized-algo/app
docker compose \
  -f docker-compose.yml \
  -f trading/infrastructure/backend/docker-compose.prod.yml \
  up -d --build

docker compose ... logs backend | grep 402052c22dd1   # baseline migration ran
docker compose ... ps                                  # both healthy
curl -s localhost:8000/api/health                      # {"status":"ok",...,"database":"connected"}
```

(If this box ever points at a pre-existing DB that already has the tables
but no `alembic_version`: run `alembic stamp head` once inside the backend
container first.)

---

## 9. System startup  *(SSH)*

```bash
sudo cp trading/infrastructure/backend/systemd/centralized-algo-backend.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now centralized-algo-backend
sudo reboot        # then re-check /api/health after it comes back
```

---

## 10. Nginx + TLS  *(SSH)*

```bash
sudo cp trading/infrastructure/backend/nginx/centralized-algo-backend.conf \
        /etc/nginx/conf.d/centralized-algo-backend.conf
sudo rm -f /etc/nginx/conf.d/default.conf
sudo nginx -t && sudo systemctl reload nginx

# with a domain:
sudo dnf install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your.domain.example --redirect --agree-tos -m you@example.com
# without a domain yet: edit the conf to serve plain :80 -> 127.0.0.1:8000,
# smoke-test, then come back and do TLS.
```

---

## 11. Health monitoring  *(SSH + admin)*

```bash
# CloudWatch agent (host metrics + nginx logs)
sudo cp trading/infrastructure/backend/cloudwatch-agent-config.json \
        /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

# /api/health canary -> CloudWatch metric CentralizedAlgo/Backend HealthOK
( crontab -l 2>/dev/null; echo "* * * * * /opt/centralized-algo/app/trading/infrastructure/backend/healthcheck-canary.sh" ) | crontab -
```

Then, with an admin identity, create alarms (SNS topic → your email):

```bash
aws cloudwatch put-metric-alarm --region ap-south-1 \
  --alarm-name centralized-algo-backend-health \
  --namespace CentralizedAlgo/Backend --metric-name HealthOK \
  --dimensions Name=InstanceId,Value=i-0f344752a1ca2811b \
  --statistic Minimum --period 60 --evaluation-periods 3 --threshold 1 \
  --comparison-operator LessThanThreshold --treat-missing-data breaching \
  --alarm-actions <sns-topic-arn>

aws cloudwatch put-metric-alarm --region ap-south-1 \
  --alarm-name centralized-algo-backend-statuscheck \
  --namespace AWS/EC2 --metric-name StatusCheckFailed \
  --dimensions Name=InstanceId,Value=i-0f344752a1ca2811b \
  --statistic Maximum --period 60 --evaluation-periods 2 --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --alarm-actions <sns-topic-arn>
```

Also worth adding: disk `used_percent` > 85, `mem_used_percent` > 90.

---

## Verification  (paste the real output back)

```bash
# 1. from OUTSIDE the instance (your laptop):
curl -s https://your.domain.example/api/health            # or http://13.206.203.145/api/health pre-TLS
#    expect: {"status":"ok","service":"centralized-algo-backend","timestamp":"...","database":"connected"}

curl -s -o /dev/null -w '%{http_code}\n' https://your.domain.example/docs      # 200
curl -s https://your.domain.example/api/algos -H "X-API-Key: <CONTROL_API_KEY>"  # []

# 2. PostgreSQL is NOT reachable from outside:
nc -zv 13.206.203.145 5432        # expect: timeout / filtered / refused

# 3. only 22/80/443 open:
aws ec2 describe-security-groups --region ap-south-1 --group-ids <new-sg-id> \
  --query "SecurityGroups[].IpPermissions[].{p:IpProtocol,f:FromPort,t:ToPort,c:IpRanges[].CidrIp}"

# 4. survives reboot: `sudo reboot`, wait, repeat check 1.

# 5. no strategies, paper mode:
docker compose ... exec backend python -c "from trading.core.config import load_settings as s; print(s().trading_mode, s().is_live)"
#    expect: paper False
curl -s https://your.domain.example/api/algos -H "X-API-Key: <key>"    # [] -- nothing registered
```

---

## Guardrails honored

- **No AWS keys on the box** — the instance role is the only credential
  source; `backend.env` has none.
- **PostgreSQL never exposed** — container has no host port; no `5432` SG
  rule.
- **Only 22 / 80 / 443 open**; SSH from the operator IP only.
- **No strategies deployed**; `TRADING_MODE=paper`, `is_live` false.
- Nothing here creates a second instance or deletes anything.
