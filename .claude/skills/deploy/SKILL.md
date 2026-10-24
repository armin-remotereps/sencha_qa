---
name: deploy
description: Deploy Sencha QA (Punk Hazard) to the production server RRAutoTester (https://sench.remotereps.com). Use when the user asks to deploy, ship to production, or update the server.
---

# Deploy to RRAutoTester

Production runs with Docker Compose on the SSH host alias `RRAutoTester` (exact case,
`~/.ssh/config`: ubuntu@64.23.140.184, port 30001), in `~/sencha_qa`. The public
site is https://sench.remotereps.com.

Deploy what is on `origin/main`. If local `main` has unpushed commits, ask before
pushing (see the push-identity memory: push with the armin-remotereps gh token).

Use `ssh -o BatchMode=yes RRAutoTester '<command>'` for every step. Run long steps
(build, migrate) with a long timeout.

## 1. Update the code

```bash
ssh -o BatchMode=yes RRAutoTester 'cd ~/sencha_qa && git status -sb | head -3 && git pull --ff-only && git log --oneline -1'
```

If the server checkout has local changes or the pull is not a fast-forward,
stop and ask the user. Never reset or stash on the server.

## 2. Update `.env`

Compare key names only — never print `.env` values (they hold secrets):

```bash
ssh -o BatchMode=yes RRAutoTester 'cd ~/sencha_qa && echo "missing in .env:" && comm -13 <(grep -oE "^[A-Z_]+" .env | sort -u) <(grep -oE "^#? ?[A-Z_]+=" example.env | tr -d "# =" | sort -u)'
```

For each missing key, check `auto_tester/settings.py`:

- Has a safe `default` → it can stay missing.
- Has no default → it must be added.
- Needs a production value → add it.
  - `SITE_URL=https://sench.remotereps.com` (the origin is in `CSRF_TRUSTED_ORIGINS`).
  - For a secret the user must supply, ask the user. Never invent one.

Also read the newest `docs/changed/` notes for new or renamed settings and other
deploy steps (migrations with a pre-check, new workers, beat schedules).

Before editing, back the file up with `cp .env .env.bak-$(date +%Y%m%d-%H%M%S)`.
Then append the new keys with a heredoc. Leave obsolete keys in place: they are
harmless. Show the user only the lines you added.

## 3. Build

```bash
ssh -o BatchMode=yes RRAutoTester 'cd ~/sencha_qa && docker compose build api 2>&1 | tail -5'
```

All app services (api, the celery workers, beat, flower) use the `local/auto_tester`
image that `api` builds.

## 4. Migrate and collect static

If a change note asks for a check before migrating, run it first in the
running api container. For example, pipe Python into
`docker compose exec -T api python manage.py shell`. Do not use `docker compose run`:
its entrypoint does not run `manage.py`.

```bash
ssh -o BatchMode=yes RRAutoTester 'cd ~/sencha_qa && docker compose up migrate collect_static 2>&1 | grep -v VISION_WORKER | tail -25'
```

Both containers must exit with code 0. If a migration fails, stop and report;
do not continue to step 5.

## 5. Restart the stack

`up -d nginx flower` recreates the api and every celery worker and beat on the new
image, through `depends_on`. Restart nginx afterwards so it re-resolves the new api
container.

```bash
ssh -o BatchMode=yes RRAutoTester 'cd ~/sencha_qa && docker compose up -d nginx flower 2>&1 | tail -15 && docker compose restart nginx'
```

## 6. Verify

```bash
ssh -o BatchMode=yes RRAutoTester 'cd ~/sencha_qa && sleep 5 && docker compose ps --format "{{.Service}} {{.State}} {{.Status}}" && docker compose logs --since 2m api execution-celery notify-celery 2>&1 | grep -iE "error|traceback|exception" | grep -v VISION_WORKER | head'
curl -s -o /dev/null -w "%{http_code}\n" https://sench.remotereps.com/accounts/login/
```

Every service should be `running`, the log scan should be empty, and the login page
should return 200.

Report to the user:

- the deployed commit;
- the `.env` keys added and the backup file name;
- the migrations applied;
- the service status.

The Test Runner and vision worker machines are not part of this deploy. If
`controller_client/` or `vision_worker/` changed, tell the user they must update
those machines themselves.
