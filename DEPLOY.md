# Deployment / Public HTTPS

The MCP server listens on `HOST:PORT` (default `0.0.0.0:8000`) and serves:

- MCP JSON-RPC: `POST /mcp`
- Health check: `GET /health`

> Keep this private or fronted by auth; it has write access to your iCloud Calendar.
> Set `MCP_AUTH_TOKEN` (shell or `.env`) to require a token on every route except `/health`; without it the server logs a warning when `HOST` is not loopback.

## Cloudflare Tunnel

Prereq: a domain on Cloudflare.

```bash
# Install & login
brew install cloudflared
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create icloud-mcp       # note UUID output

# Route hostname to tunnel
cloudflared tunnel route dns icloud-mcp mcp.yourdomain.com

# Configure ingress (replace <UUID> and home dir)
cat > ~/.cloudflared/config.yml <<'YAML'
tunnel: <UUID_FROM_CREATE>
credentials-file: /Users/<you>/.cloudflared/<UUID_FROM_CREATE>.json
ingress:
  - hostname: mcp.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
YAML

# Run the tunnel
cloudflared tunnel run icloud-mcp
```

MCP URL: `https://mcp.yourdomain.com/mcp`  
Health: `https://mcp.yourdomain.com/health`

## ngrok

```bash
brew install ngrok/ngrok/ngrok
ngrok config add-authtoken <YOUR_TOKEN>

# In another terminal, run the MCP server with a shared secret:
export MCP_AUTH_TOKEN="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
python server.py
```

Then expose it:

```bash
ngrok http 8000
```

Use the printed https URL:

- MCP: `https://<random>.ngrok.io/mcp`
- Health: `https://<random>.ngrok.io/health` (does **not** require the token)

### MCP auth header

Configure your MCP client / ChatGPT custom connector to send:

- Header: `X-Auth-Token`
- Value: `<same value as MCP_AUTH_TOKEN>`

`Authorization: Bearer <MCP_AUTH_TOKEN>` is accepted too, for clients that only send bearer tokens. Requests without a matching token get `401`.

## VPS + Caddy (auto-TLS) or Nginx (manual TLS)

On a remote box running your server on `0.0.0.0:8000`, add a reverse proxy:

**Caddy**

```bash
# /etc/caddy/Caddyfile
mcp.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
# then: sudo systemctl reload caddy
```

MCP URL: `https://mcp.yourdomain.com/mcp`  
Health: `https://mcp.yourdomain.com/health`

---

## Deep Research mode (read-only)

You can run the same container/server in a read-only profile for Deep Research:

```bash
# Local
DR_PROFILE=1 python server.py

# Docker
docker run --rm \
  -p 8000:8000 \
  -e APPLE_ID=you@example.com \
  -e ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx \
  -e MCP_AUTH_TOKEN=<random secret> \
  -e DR_PROFILE=1 \
  -e SCAN_DAYS=1095 \
  icloud-mcp
```

In this mode the MCP surface only exposes:

- `search(query)` -> list of matching events
- `fetch(ids)` -> raw `text/calendar` ICS blobs

No write tools are registered.

---

## Connect to ChatGPT

1. ChatGPT -> **Settings -> Connectors -> Add custom connector**
2. Enter your MCP endpoint (`https://…/mcp`) and save.
3. In a chat, select this connector and call tools, for example:
   * “Use **icloud-caldav** to `list_calendars`.”
   * “Create an event tomorrow 15:00–15:30 on calendar URL `<…>` at *Bobst Library*.”
   * “Update event UID `<…>` to 16:00–16:20 and move it to ‘Study Room B’.”

> Availability of custom connectors depends on plan; if the menu isn’t visible, check OpenAI’s current plan requirements.
