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

Services expected on the docker network: `postgres-grades:5432` and `redis-grades:6379`.

## Environment

Copy `.env.example` to `.env` and fill it in:

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
   network you use for remote access.
2. It SSHes to the server over that tunnel and pipes in `.github/scripts/deploy.sh`.
3. The script fetches, hard-resets the checkout to the pushed commit, runs
   `docker compose build` and `up -d`, then prunes dangling images.
4. WireGuard comes down, runner is discarded.

SSH stays reachable only from the LAN and WireGuard. Nothing new is exposed publicly.

### 1. Server checkout

```
git clone https://github.com/MANRAF04/uthme2-backend.git /opt/uthme2-backend
cd /opt/uthme2-backend
cp .env.example .env    # then fill it in
```

Copy the VPN profile in, once. It is gitignored, and `COPY . .` in the Dockerfile picks it up
from the build context, so it has to sit next to the source:

```
scp uth.ovpn <user>@<server>:/opt/uthme2-backend/uth.ovpn
```

The deploy refuses to build if it is missing, rather than shipping an image that fails at the
first scrape. Being untracked, it survives every deploy.

The repo is public, so no deploy key is needed for pulls. `.env` is gitignored and survives the
deploy reset, since only tracked files are reset.

Make sure the deploy user can talk to docker (`usermod -aG docker <user>`) and that
`docker compose ps` works from the directory holding `docker-compose.yml`.

### 2. WireGuard peer for CI

Generate a keypair for the runner, anywhere:

```
wg genkey | tee ci.key | wg pubkey > ci.pub
```

Add the peer on the WireGuard server, using a free address in your subnet:

```
sudo wg set wg0 peer "$(cat ci.pub)" allowed-ips 10.8.0.50/32
sudo wg-quick save wg0
```

`wg set` applies immediately without dropping existing peers, `wg-quick save` writes it to
`/etc/wireguard/wg0.conf` so it survives a restart.

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

`from=` means the key is only usable from inside the tunnel, `restrict` disables port forwarding,
agent forwarding and pty allocation. Keep the private half for the secret below, then delete it
from the server.

Capture the host key, running this from inside the LAN or WireGuard, against the same address the
workflow connects to:

```
ssh-keyscan -p 22 10.8.0.1
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
| `SSH_PORT` | Optional, defaults to `22` |
| `DEPLOY_PATH` | Absolute path of the clone, e.g. `/opt/uthme2-backend` |
| `COMPOSE_DIR` | Optional, directory holding `docker-compose.yml` if it is not `DEPLOY_PATH` |

Optional repository *variable* `COMPOSE_SERVICES`: space separated service names to limit the
build and restart (e.g. `api worker beat`). Empty means the whole stack.

### Notes

- Workflow logs are public on a public repo. Host and key material come from secrets so GitHub
  masks them, which is why the workflow never runs `wg show` or `ssh -v`.
- The job is guarded by `if: github.repository == 'MANRAF04/uthme2-backend'`, so pushes to forks
  do not attempt a deploy.
- The deploy runs `git checkout -B main <pushed sha>`, so uncommitted edits to tracked files on
  the server are discarded. Untracked files (`.env`, `uth.ovpn`, volumes) are left alone.
- `--remove-orphans` is deliberately not used, so containers outside the compose file are safe.
