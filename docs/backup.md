# Backup and moving to a new server

## What a backup contains

Under ⚙️ Settings → Backup an admin downloads everything as one file (`kombuese-backup-YYYY-MM-DD.tar.gz`): recipes,
cards, week plan, meal prep, shopping list, freezer, cupboard, household settings, accounts, AI run history and all
images, with a consistent snapshot of the database and a `manifest.json` (version, date, counts).

With several households the file holds all of them: household 1 at the top level as before, every
further household in `households/<id>/`. Only instance admins (admins of household 1) download and import backups.
Backups from older versions import as household 1.

Left out: sign-in sessions, and by default all stored credentials. The AI settings travel without the API key;
Bring!, Yazio, Telegram and ntfy are set up again on the new server. Tick **Include the credentials** to take them along:
they are then encrypted with a one-time key that is part of the file, so treat such a file like a password. The
server's own `secret.key` never leaves the data directory. Accounts travel with their password hashes, API tokens as
hashes only (they keep working on the new server).

## Moving

Install Kombüse on the new server, open it and choose **Restore backup** on the first page instead of creating an
account, then sign in with your old account.

On an installation that is already set up, Settings → Backup → Restore replaces all data with the backup; you stay
signed in with your current account. Before every restore the server writes a backup of the current data to
`backups/` in the data directory (the last three are kept).

The archive is checked first: only expected files, no links or `..` paths, size limit `KOMBUESE_BACKUP_MAX_MB`
(default 2048). Backups of older versions are migrated as on a normal start; backups of a newer version are refused –
update first.

## Command line

For a cron job, or when the browser upload is too large:

```sh
docker exec kombuese python /app/tools/admin.py export - > kombuese-backup.tar.gz           # add --secrets for credentials
docker exec -i kombuese python /app/tools/admin.py import - --yes < kombuese-backup.tar.gz   # then: docker restart kombuese
```

A nightly backup that keeps two weeks:

```sh
0 3 * * * docker exec kombuese python /app/tools/admin.py export - > /backups/kombuese-$(date +\%F).tar.gz && find /backups -name 'kombuese-*.tar.gz' -mtime +14 -delete
```

Copying the whole data directory while the container is stopped works as well. It contains `secret.key`, so the copy
includes the credentials unless `KOMBUESE_SECRET_KEY` is set.
