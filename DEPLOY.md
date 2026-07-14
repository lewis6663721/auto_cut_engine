# AutoCut Engine Deployment

## 1. Server Requirements

- Linux server with Docker Engine and Docker Compose plugin
- Ports `80` and `22` open
- Recommended: 4 CPU / 8 GB RAM / 100 GB disk or higher for video rendering

Install Docker on Ubuntu:

```bash
curl -fsSL https://get.docker.com | bash
systemctl enable --now docker
```

## 2. Configure Production Env

```bash
cp .env.production.example .env.production
```

Edit `.env.production`:

- Replace `MYSQL_ROOT_PASSWORD`
- Replace `SECRET_KEY`
- Fill `TRANSFER_API_KEY` if AI analysis should call the remote model

## 3. Deploy From Local Machine

```bash
chmod +x scripts/deploy.sh
SERVER_HOST=your.server.ip SERVER_USER=root scripts/deploy.sh
```

Optional variables:

```bash
SERVER_PORT=22
REMOTE_DIR=/opt/auto_cut_engine
```

## 4. Common Commands On Server

```bash
cd /opt/auto_cut_engine
docker compose ps
docker compose logs -f web
docker compose logs -f worker
docker compose restart
docker compose down
```

The compose stack publishes the web container only on `127.0.0.1:18080` by default.
Change `AUTOCUT_HOST_PORT` in `.env.production` if this port is already used.
Use the host Nginx to proxy public traffic to that local port.

Example host Nginx server:

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 5g;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 5. Verify

```bash
curl http://your.server.ip/healthz
```

Then open:

```text
http://your.server.ip
```

Default accounts are seeded on first start:

- `admin` / `admin123`
- `demo` / `demo123`
