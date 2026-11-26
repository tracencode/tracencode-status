# TraceNcode status

Public status page for the services behind `status.tracencode.tech`.

It probes each URL over HTTPS, stores 90 days of results in SQLite, and serves a single page plus `GET /api/status`.

| Service | URL |
| --- | --- |
| ERP | https://erp.tracencode.tech |
| Careers | https://career.tracencode.tech |
| Drive | https://drive.tracencode.tech |
| Mattermost | https://chat.tracencode.tech |

A check is **up** on HTTP 2xx/3xx, **degraded** on 4xx, and **down** on timeout, TLS failure, or 5xx. Edit `services.json` to add a host. Only `http` and `https` URLs are accepted.

## Run

```bash
python3 server.py
```

Open http://127.0.0.1:8080. The first probe starts immediately and repeats every `CHECK_INTERVAL` seconds (default 60).

| Variable | Default | |
| --- | --- | --- |
| `PORT` | `8080` | Listen port |
| `DATA_DIR` | `./data` | SQLite directory |
| `CHECK_INTERVAL` | `60` | Seconds between rounds |
| `CHECK_TIMEOUT` | `12` | Seconds per request |
| `SERVICES_FILE` | `./services.json` | Service list |

`GET /health` returns `{"ok":true}` for the status app itself.

## Render

`render.yaml` describes a free Docker web service. After the repo is on GitHub:

```bash
render services create \
  --name tracencode-status \
  --type web_service \
  --repo https://github.com/tracencode/tracencode-status \
  --runtime docker \
  --branch main \
  --plan free \
  --region oregon \
  --health-check-path /health \
  --env-var DATA_DIR=/data \
  --env-var CHECK_INTERVAL=60 \
  --confirm
```

A free instance sleeps when nobody is visiting, and the filesystem is wiped on each restart, so the 90-day history does not survive. A Starter instance with a disk mounted at `/data` stays awake and keeps that history.

Point `status.tracencode.tech` at the Render hostname with a CNAME once the service is live.

## Host at status.tracencode.tech

1. Point an A/AAAA record for `status.tracencode.tech` at the machine that will run this.
2. Run the app on that machine so the probes come from a network that can reach every service.

When nginx already owns 80/443:

```bash
docker compose up -d --build
```

The app is published on `127.0.0.1:8080`. Use `deploy/nginx.conf.example`, then:

```bash
sudo certbot --nginx -d status.tracencode.tech
```

When this host should terminate TLS itself (ports 80 and 443 free):

```bash
docker compose --profile edge up -d --build
```

Caddy fetches a certificate for `status.tracencode.tech` and proxies to the app.
