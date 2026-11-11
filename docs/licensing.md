# Licensing

## Why there is no `LICENSE` file yet

The repository is private and personal. Without a licence file the default is
"all rights reserved", which is the correct state until a deliberate choice is
made. That choice depends on the points below and should be made before the code
is shared with anyone or offered as a service.

## Dependency licences (checked with `pip-licenses` on the installed versions)

| Package | Licence | Used by |
|---|---|---|
| numpy, pandas, scikit-learn, httpx | BSD | core / ai |
| pyarrow, quantstats | Apache-2.0 | core / reporting |
| pydantic, pydantic-settings, SQLAlchemy, typer, PyYAML, ccxt, lightgbm, optuna, shap | MIT | core / ai / freqtrade |
| structlog | MIT or Apache-2.0 | core |
| matplotlib | PSF-style (permissive) | reporting |
| psycopg (+ binary) | **LGPL-3.0** | core (database driver) |
| **freqtrade**, **technical** | **GPL-3.0** | optional `freqtrade` extra only |
| TA-Lib (Python wrapper) | BSD-2-Clause (metadata missing on PyPI) | pulled in by freqtrade |
| pytest, pytest-cov, ruff, mypy | MIT | development only |
| PostgreSQL | PostgreSQL Licence (permissive) | service |
| Grafana | **AGPL-3.0** | service, used unmodified |

## What this means in practice

- **Permissive licences (MIT/BSD/Apache/PSF):** no constraints beyond keeping
  notices when redistributing.
- **psycopg (LGPL-3.0):** used as an unmodified library through its public API;
  this does not impose the LGPL on the platform code.
- **Freqtrade (GPL-3.0):** the only file that imports Freqtrade is
  `freqtrade/user_data/strategies/qts_trend_following.py`; the `qts` package never
  does (enforced by a test), and Freqtrade is an optional extra. Private use and
  running it as a service do not trigger GPL obligations, because the GPL (unlike
  the AGPL) is triggered by *distribution*. If the adapter, or an image that
  bundles Freqtrade with it, were ever distributed to third parties, that
  combination would have to be offered under GPL-3.0.
- **Grafana (AGPL-3.0):** used unmodified as a separate service. The dashboard
  JSON is configuration, not a modification of Grafana. Offering a *modified*
  Grafana to users over a network would require publishing those modifications.

For a future SaaS: keep the core independent from Freqtrade (as it is now), and
get proper legal advice before distributing anything — this document is an
engineering summary, not legal advice.
