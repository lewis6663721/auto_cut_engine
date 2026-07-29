# AutoCut Engine Deployment

## 1. Server Requirements

- Linux server with Docker Engine and Docker Compose plugin
- Ports `80` and `22` open
- Recommended: 4 CPU / 8 GB RAM / 100 GB disk or higher for video rendering
- Remotion 真渲染需要 Node.js / npm。Docker 镜像会自动安装并执行 `remotion_runtime/npm ci`；裸机运行时需手动执行 `cd remotion_runtime && npm install`。

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
- Fill `TRANSFER_API_KEY`, `OPENAI_API_KEY`, `DASHSCOPE_API_KEY`, or `SEDANCE_API_KEY` if AI features should call remote models.
- Users can also configure provider keys from `/account`; server-side env keys are still useful as shared defaults.

ASR fallback:

- The project includes `faster-whisper` in `requirements.txt`.
- The first local ASR run downloads the selected Whisper model weights, so keep enough disk space and allow outbound access to the model host.
- If remote ASR and local ASR are both unavailable, the task still produces placeholder subtitles so the workflow does not break.

Custom effect assets:

- User-uploaded effect packages are stored under `media/effects`.
- Remotion zip uploads require `manifest.json` and a valid `entry` such as `src/Root.tsx`.
- Lottie JSON and LUT `.cube` files are stored and listed first; rendering adapters can be enabled incrementally.

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
