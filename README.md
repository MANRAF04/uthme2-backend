# uthme2-backend

Grades and restaurant menu API for the University of Thessaly. FastAPI + Celery, scraping the
university portal through an OpenVPN tunnel.

## Stack

| Piece | What it does |
| --- | --- |
| `main.py` | FastAPI app (restaurants, menus, grades, user preferences) |
| `tasks.py` | Celery worker + beat (grade sync on demand, menu scrape daily at 04:00, menu push check every 10 minutes) |
| `scraper.py`, `dine_scraper.py` | BeautifulSoup scrapers |
| `models.py`, `database.py` | SQLAlchemy models, PostgreSQL session |
| `push.py` | Shared FCM sender: Firebase app, multicast, dead-device cleanup |
| `grade_notifications.py` | Fan-out when a course gets graded, plus the once-per-exam-period ledger |
| `menu_notifications.py` | Pre-meal push with the preferred restaurant's main courses |
| `security.py` | Password hashing (scrypt) and Fernet encryption for credentials in transit |
| `uth.ovpn` | VPN profile. Gitignored, kept on the server only |

Runs as four containers built from this directory (`grades-api`, `grades-worker`,
`grades-beat`, `grades-notifier`), against `postgres-grades:5432` and `redis-grades:6379`.

## Environment

These come from the `.env` next to `docker-compose.yml`, which compose interpolates into the
service definitions. The app reads plain environment variables, never a `.env` file. See
`.env.example` for the list.

| Variable | Used by | Notes |
| --- | --- | --- |
| `DB_PASSWORD` | API, worker | Password for the `grades_user` Postgres role. Startup raises if unset |
| `CRED_ENC_KEY` | API, worker | Fernet key, **must be identical on both** |
| `BOT_USERNAME` | worker | University account for the scheduled menu scrape |
| `BOT_PASSWORD` | worker | Same |
| `FIREBASE_CREDENTIALS_FILE` | notifier | Path to the mounted service account JSON. Defaults to `/app/secrets/firebase-service-account.json` |
| `FIREBASE_CREDENTIALS_JSON` | notifier | Alternative to the file, the whole JSON key as one value. The file wins if both are present |

Generate the Fernet key with:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Push notifications

When a student refreshes and the scrape turns up a new mark on a course they had not passed
(a first mark, or a retake replacing a failing one), every other student who has not passed
that same course code is told to open the app. The mark
itself never leaves the server. The payload names the course and nothing else.

### Flow

1. `sync_user_grades` scrapes and `save_to_database` returns the not-yet-passed courses whose
   mark appeared or changed on this run.
2. A student's very first sync returns nothing. That run inserts their whole history at
   once, which is a cache fill rather than a grading event.
3. Once the tunnel is down, the worker queues `notify_graded_courses` on the
   `notifications` queue.
4. `grades-notifier` picks it up, claims the course's slot for the current exam period, looks
   up every device whose owner has that course with `passed = false`, and sends.

The split matters. `uth.ovpn` sets `redirect-gateway def1`, so while the grade worker holds
the tunnel open all of its traffic leaves through the university network. `grades-notifier`
never starts OpenVPN and never asks for `NET_ADMIN`, so FCM calls take the normal route and
a slow send cannot block the grade queue. `grades-worker` is pinned to `-Q celery` so it
cannot pick up a fan-out by accident.

### Once per exam period

`course_period_notifications` holds one row per (course, exam period). The portal exposes no
period field, so the period is derived from the date: results published Dec to Apr count as
`<year>-winter` (December rolls into the next year), May to Aug as `<year>-spring`, Sep to
Nov as `<year>-september`.

The claim is a single `INSERT ... ON CONFLICT DO NOTHING`, which takes the row lock, so two
notifiers racing on the same course cannot both send. A course nobody was waiting on rolls its
claim back rather than burning the period. A failed send releases the slot so the Celery retry
can work.

Without this, a wave of students refreshing after the first notification would each detect
their own new mark and start another round.

### Grouping

Recipients are matched on `course_code` alone. There is no department column anywhere in
the schema, and the app never sends one. Two departments reusing the same course code would
cross-notify. If that shows up in practice, the fix is to capture the department from
`studentProfiles[0]` in `scraper.py` (the profiles call is already made for the `X-Profile`
header) and add it to the join.

### Firebase setup

1. Create a Firebase project and add the Android and iOS apps.
2. *Project settings > Service accounts > Generate new private key*.
3. Put the JSON on the server at `secrets/firebase-service-account.json` inside this
   directory. It is gitignored and dockerignored, so it survives deploys and never reaches
   the image, the same treatment `uth.ovpn` gets.
4. The file has to exist before `docker compose up`. Docker creates a directory in its
   place otherwise and the notifier fails with a confusing error.

### Meal menus

Every 10 minutes Beat queues `send_menu_notifications` on the `notifications` queue. For
each restaurant that at least one student has as their preferred restaurant (with the toggle
on and a registered device), it looks at today's `Μεσημεριανό` and `Βραδινό`. The first run
inside `[start - 2h, start)` sends one push per (restaurant, day, meal), tracked in
`menu_notifications`.

- Start time is `meal_hour_from`. When the portal leaves it blank, lunch falls back to 13:00
  and dinner to 19:30 (the most common hours in the data).
- Body lists the `ΚΥΡΙΩΣ` dishes in menu order. No main courses, no push.
- When an `ΕΠΙΔΟΡΠΙΟ` item is exactly `Γλυκό`, the title switches to the sweet variant
  (`Μεσημεριανό στις 13:00, έχει Γλυκό!`).
- A meal nobody is subscribed to rolls its claim back, so a student who picks the
  restaurant before the meal starts still gets it. A failed send releases the slot and the
  next tick retries.
- Students without a preferred restaurant get nothing. `users.menu_notifications_enabled`
  (default true) is the opt-out.

Android channel is `menus`. Data payload:

```
{"type": "menu_upcoming", "restaurant_id": "...", "meal_type": "..."}
```

### Client contract

All endpoints take the same body credentials as the rest of the API.

```
POST /api/devices/register
{"username": "...", "password": "...", "fcm_token": "...", "platform": "android", "locale": "el"}

POST /api/devices/unregister
{"username": "...", "password": "...", "fcm_token": "..."}

POST /api/user/menu-notifications
{"username": "...", "password": "...", "enabled": true}
```

`register` returns the account's `menu_notifications_enabled`, so a reinstalled app picks up
the stored choice.

Register once the first sync has succeeded. Before that the user has no password hash on
file and the call returns 401. Unregister on logout, before the app clears its stored
credentials.

`fcm_token` carries the device's Firebase Installation ID (FID), which the app gets from
`FirebaseMessaging.onRegistered`. The notifier sends with `MulticastMessage(fids=...)`, so
`firebase-admin` must be 7.7.0 or newer.

A token is unique across the table and follows the handset. Registering one that already
exists moves it to the calling account, so a shared phone stops receiving the previous
student's pushes.

The message carries both a `notification` block (rendered by the OS when the app is in the
background) and a `data` block:

```
{"type": "course_graded", "course_code": "...", "course_title": "..."}
```

Notification text is picked server side from the `locale` sent at registration (`el` or
`en`, defaulting to `el`). The Android config names channel `grades`, which the app has to
create for Android 8 and up.

The Flutter client lives in `../uthme2`.

## Running

```
uvicorn main:app --host 0.0.0.0 --port 8000
celery -A tasks worker --loglevel=info -Q celery
celery -A tasks worker --loglevel=info -Q notifications
celery -A tasks beat --loglevel=info
```

The Celery worker starts OpenVPN itself, so its container needs `NET_ADMIN` and `/dev/net/tun`.

## Deployment

Pushing to `main` (or running the workflow manually) triggers `.github/workflows/deploy.yml`:

1. The runner brings up a WireGuard interface from the `WG_CONFIG` secret, joining the same
   network used for remote access.
2. It SSHes to the server over that tunnel and pipes in `.github/scripts/deploy.sh`.
3. That script refuses to run if the checkout has uncommitted changes to tracked files or if
   `uth.ovpn` is missing, then fast-forwards `main` to the pushed commit.
4. `docker compose build` and `up -d`, limited to the grades services, then dangling images are
   pruned.
5. WireGuard comes down, the runner is discarded.

SSH stays reachable only from the LAN and WireGuard. Nothing new is exposed publicly.

The checkout is the same directory the compose file builds from, so a deploy fast-forwards it
rather than hard-resetting it. Local edits are never destroyed, the deploy fails instead and
tells you to commit them. It does switch the checkout to `main` if you left it on another
branch.

### 1. Server prerequisites

- The repo cloned (or already present) at the path the compose `build:` context points to.
- `uth.ovpn` in that directory. It is gitignored, so it is invisible to git and survives every
  deploy, but a fresh clone will not have it.
- The `.env` next to `docker-compose.yml` filled in.
- The deploy user in the `docker` group.

### 2. WireGuard peer for CI

Generate a keypair for the runner:

```
wg genkey | tee ci.key | wg pubkey > ci.pub
```

Add the peer, using a free address in your subnet:

```
sudo wg set wg0 peer "$(cat ci.pub)" allowed-ips 10.8.0.50/32
sudo wg-quick save wg0
```

`wg set` applies immediately without dropping existing peers, `wg-quick save` persists it to
`/etc/wireguard/wg0.conf`.

The config that goes into the `WG_CONFIG` secret, with `ci.key` as the private key:

```
[Interface]
PrivateKey = <contents of ci.key>
Address = 10.8.0.50/32

[Peer]
PublicKey = <server public key>
Endpoint = <public host or IP>:51820
AllowedIPs = 10.8.0.1/32
PersistentKeepalive = 25
```

Two things that will bite you:

- `AllowedIPs` must list only the server address you SSH to, never `0.0.0.0/0`. A default route
  would push all runner traffic through your server and break its access to GitHub.
- No `DNS =` line. `wg-quick` needs `resolvconf` for that and the runner does not have it.

### 3. SSH key for CI

On the server:

```
ssh-keygen -t ed25519 -C "github-actions" -f ~/.ssh/gha_deploy -N ""
```

Add the public half to `~/.ssh/authorized_keys`, pinned to the CI peer address:

```
from="10.8.0.50",restrict ssh-ed25519 AAAA... github-actions
```

`from=` means the key only works from inside the tunnel, `restrict` disables port forwarding,
agent forwarding and pty allocation. Keep the private half for the secret below, then delete it
from the server.

Capture the host key from inside the LAN or tunnel, against the same address the workflow
connects to:

```
ssh-keyscan 10.8.0.1
```

### 4. GitHub secrets

*Settings > Secrets and variables > Actions*:

| Secret | Value |
| --- | --- |
| `WG_CONFIG` | The whole `[Interface]`/`[Peer]` block above |
| `SSH_HOST` | Server address inside the tunnel, e.g. `10.8.0.1` |
| `SSH_USER` | User to SSH as |
| `SSH_KEY` | `~/.ssh/gha_deploy`, whole file including the header lines |
| `SSH_KNOWN_HOSTS` | Output of the `ssh-keyscan` above |
| `DEPLOY_PATH` | Absolute path of the checkout, e.g. `/home/manraf/grades-api` |
| `COMPOSE_DIR` | Directory holding `docker-compose.yml`, e.g. `/home/manraf/server` |
| `SSH_PORT` | Optional, defaults to `22` |

Optional repository *variable* `COMPOSE_SERVICES`, space separated. It defaults to
`grades-api grades-worker grades-beat grades-notifier` in the script, which is what keeps a
shared compose file from rebuilding and recreating unrelated services.

### Notes

- Workflow logs are public on a public repo. Host and key material come from secrets so GitHub
  masks them, which is why the workflow never runs `wg show` or `ssh -v`.
- The job is guarded by `if: github.repository == 'MANRAF04/uthme2-backend'`, so pushes to forks
  do not attempt a deploy.
- `--remove-orphans` is deliberately not used. On a shared compose file it would stop every
  container that is not defined in it.
