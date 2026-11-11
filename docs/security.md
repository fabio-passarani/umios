# Security

| Topic | What is in place |
|---|---|
| Credentials in Git | none; `.env`, keys, state, data, backups and reports are git-ignored; CI runs a secret scan (gitleaks) on the full history |
| `.env.example` | placeholders only |
| API keys | not needed for backtest or paper trading; the settings fields exist but are unused. Held as `SecretStr` (masked in `repr`/dumps) |
| Logging | structlog processor redacts values of sensitive keys (`*key*`, `*secret*`, `*token*`, `*password*`, …) and `user:password@` inside URLs; database errors are re-raised without the driver message |
| Configuration | strict validation, unknown keys rejected, leverage not configurable |
| Live trading | disabled by default, double opt-in required, and still refused because no live broker exists; the Freqtrade adapter refuses to load outside backtest/dry-run |
| Broker requests | brokers accept only single-use, HMAC-signed risk approvals, valid for one minute and (for entries) only at the approved price |
| Downloads | SHA-256 checksum verified for every archive |
| Network exposure | every Docker port is bound to `127.0.0.1`; Grafana sign-up and anonymous access disabled |
| Database | Grafana connects with a read-only role; application errors never echo the DSN |
| Containers | run as a non-root user; configs mounted read-only |
| Backups | `scripts/backup.sh`, files created with `umask 077` |
| Dependencies | Dependabot for pip, Docker and GitHub Actions; exact audited versions in `requirements-lock.txt` |

## Before ever considering real money (not part of this MVP)

- exchange API keys with trading-only permissions, **withdrawals disabled**, IP allow-list;
- secrets in a secret manager or Docker secrets instead of `.env`;
- TLS and authentication in front of Grafana/Freqtrade if the host is not local;
- reconciliation between the exchange account and the internal ledger;
- an explicit, written decision to enable it — the software will not do it by itself.
