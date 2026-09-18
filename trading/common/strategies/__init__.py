"""Strategy interface adapters for each live algo (Phase 10).

Each module here registers one concrete Strategy for an existing live
algo under trading/algos/*. None of these adapters import anything from
their corresponding trading/algos/<Name>/ directory -- see each module's
own docstring for why: those packages use bare (non-package-qualified)
imports that rely on being run with their own directory as sys.path[0],
have real side effects at import time in some cases (dotenv credential
loading -- the exact hazard documented in Phase 5A's "real credential
leak" fix), and, most importantly, this phase's explicit scope is the
registry/interface skeleton, not a port of any algo's actual decision
logic into generate_order_intents(). See docs/phase-10-strategy-registry-
report.md for the full explanation and the recommended follow-up phase.
"""
