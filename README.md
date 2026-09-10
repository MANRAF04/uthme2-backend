# uthme2-backend

Grades and restaurant menu API for the University of Thessaly. FastAPI + Celery, scraping the
university portal through an OpenVPN tunnel.

## Stack

| Piece | What it does |
| --- | --- |
| `main.py` | FastAPI app (restaurants, menus, grades, user preferences) |
| `tasks.py` | Celery worker + beat (grade sync on demand, menu scrape daily at 04:00) |
| `scraper.py`, `dine_scraper.py` | BeautifulSoup scrapers |
| `models.py`, `database.py` | SQLAlchemy models, PostgreSQL session |
| `security.py` | Password hashing (scrypt) and Fernet encryption for credentials in transit |
| `uth.ovpn` | VPN profile. Gitignored, kept on the server only |

Runs as three containers built from this directory (`grades-api`, `grades-worker`,
`grades-beat`), against `postgres-grades:5432` and `redis-grades:6379`.

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

Generate the Fernet key with:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Running

```
uvicorn main:app --host 0.0.0.0 --port 8000
celery -A tasks worker --loglevel=info
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
`grades-api grades-worker grades-beat` in the script, which is what keeps a shared compose file
from rebuilding and recreating unrelated services.

### Notes

- Workflow logs are public on a public repo. Host and key material come from secrets so GitHub
  masks them, which is why the workflow never runs `wg show` or `ssh -v`.
- The job is guarded by `if: github.repository == 'MANRAF04/uthme2-backend'`, so pushes to forks
  do not attempt a deploy.
- `--remove-orphans` is deliberately not used. On a shared compose file it would stop every
  container that is not defined in it.
