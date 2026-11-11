# Backup and restore

## What is backed up

| Content | Where it lives | In the backup |
|---|---|---|
| Runs, equity, orders (risk decisions), fills, trades, positions, signals, risk events, paper session state (cash, pending orders, risk limits), monitoring history | PostgreSQL, database `qts` | `qts-<stamp>.sql.gz` |
| Freqtrade's own trades and orders | PostgreSQL, database `freqtrade` | same file |
| Roles and their privileges | PostgreSQL | same file (`pg_dumpall`) |
| Kill-switch state, Freqtrade risk state | volume `qts-state` | `qts-state-<stamp>.tar.gz` |
| Price history | volume `qts-data` | **no**: it is re-downloaded and checksum-verified |
| `.env` (passwords) | the host | **no**: keep your own copy somewhere safe |

## Automatic backups

The `backup-scheduler` service takes one backup 2 minutes after it starts and
then one every `BACKUP_INTERVAL_HOURS` (24). Each backup is verified after
writing (valid gzip, complete dump), the newest `BACKUP_KEEP` (14) sets are kept,
and a heartbeat is recorded: `qts health` and the daily report show when backups
stop or fail.

Files go to `./backups`, readable only by the user who owns that directory.
**They stay on the same machine**: copy them elsewhere (e.g. a nightly `rsync` or
`rclone` of `./backups`), otherwise a lost disk loses the backups too. They
contain password hashes and all trading data: treat them as confidential.

An immediate backup: `scripts/backup.sh`.

## Proving a backup works

```bash
scripts/restore_test.sh              # take a fresh backup and test it
scripts/restore_test.sh --existing   # test the newest one already in ./backups
```

Non-destructive. The dump is loaded into a throw-away PostgreSQL container and
compared with the running stack: row count and checksum of every row for runs,
equity, fills, trades, positions, risk decisions, risk events and paper state;
schema revision; the three roles and their lack of privileges; and the state
archive against the state volume. It prints what was recovered (open positions,
orders, cash, pending orders, risk limits, kill switch) and ends with
`RESTORE TEST PASSED`. CI runs it on every push.

## Restoring

```bash
scripts/restore.sh --latest --yes
scripts/restore.sh --dump backups/qts-<stamp>.sql.gz --yes
```

**Destructive**: every database is replaced by the dump and the state volume by
the matching archive. The script stops the services, restores, re-applies the
passwords from the current `.env`, runs pending migrations and starts everything
again. Price history is downloaded again by the paper trader on its next step.

On a new machine: clone the repository, put your `.env` back (or run
`scripts/setup_host.sh` for new passwords — they are re-applied to the restored
roles), copy the backup files into `./backups`, `docker compose up -d postgres`,
then `scripts/restore.sh --latest --yes`.

What is lost: anything recorded after the backup was taken — at most
`BACKUP_INTERVAL_HOURS` of data. For a daily strategy that is at most one
candle; the paper trader processes it again on its next step, because the
restored session state says where it had got to.
