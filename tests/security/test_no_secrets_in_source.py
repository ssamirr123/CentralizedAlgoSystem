"""Stage 20 -- no hard-coded credentials in tracked source."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# a quoted assignment to a credential-shaped name that is NOT reading from
# the environment / a config object.
_CRED_NAMES = (
    "clientid|client_id|apikey|api_key|api_secret|secret_key|mpin|password|passwd|"
    "totp_secret|session_token|access_token|refresh_token|auth_secret_key|control_api_key"
)
_LITERAL_CRED = re.compile(
    rf"""^\s*#?\s*({_CRED_NAMES})\s*=\s*['"][^'"]{{4,}}['"]""", re.IGNORECASE
)
_ENV_READ = re.compile(r"_env|_g\(|_angel_creds|os\.environ|os\.getenv|getenv|config\.", re.IGNORECASE)

ALGO_CONFIGS = sorted((ROOT / "trading" / "algos").glob("*/config.py"))
ALGO_CONNECTAPI = sorted((ROOT / "trading" / "algos").glob("*/connectapi.py"))


@pytest.mark.parametrize("path", ALGO_CONFIGS + ALGO_CONNECTAPI, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_literal_credential_assignments(path: Path):
    offenders = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _LITERAL_CRED.match(line) and not _ENV_READ.search(line):
            offenders.append(i)  # never store the value itself
    assert not offenders, f"{path.relative_to(ROOT)} has literal credential(s) at line(s) {offenders}"


@pytest.mark.parametrize("path", ALGO_CONFIGS, ids=lambda p: p.parent.name)
def test_strategy_config_reads_angel_creds_from_env(path: Path):
    if path.parent.name == "example_strategy":
        pytest.skip("template uses trading.common.config, not AngelOne")
    src = path.read_text(encoding="utf-8")
    assert "_angel_creds()" in src
    assert "ANGELONE_API_KEY" in src and "ANGELONE_TOTP_SECRET" in src


def test_working_tree_has_no_broker_literal_via_git_grep():
    """Repo-wide guard (mirrors the gitleaks rule) over tracked source."""
    pat = (
        r"(clientid|apikey|api_key|mpin|totp_secret|"
        r"breeze_(api_key|secret_key|session_token))[ \t]*[:=][ \t]*['\"][A-Za-z0-9@._/-]{4,}['\"]"
    )
    r = subprocess.run(
        ["git", "grep", "-nEi", pat, "--", "trading/", ":!*.md", ":!*.example", ":!tests/"],
        cwd=ROOT, capture_output=True, text=True,
    )
    # git grep exit 1 == no matches (good); exit 0 == matches (bad)
    assert r.returncode == 1, f"tracked source still contains inline credential literals:\n{r.stdout}"
