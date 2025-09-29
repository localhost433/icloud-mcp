# Deployment / Public HTTPS

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

## ngrok

```bash
brew install ngrok/ngrok/ngrok
ngrok config add-authtoken <YOUR_TOKEN>
ngrok http 8000
```

Use the printed https URL: `https://<random>.ngrok.io/mcp`
> Note: free ngrok URLs rotates.

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

---

## Connect to ChatGPT

1. ChatGPT -> **Settings -> Connectors -> Add custom connector**
2. Enter your MCP endpoint (`https://…/mcp`) and save
3. In a chat, select this connector and call tools:
   * “Use **icloud-caldav** to `list_calendars`”
   * “Create an event tomorrow 15:00–15:30 on calendar URL `<…>`”
   * “Update event UID `<…>` to 16:00–16:20 and rename to ‘Study Session’”

> Availability of custom connectors depends on plan (please check their policy online). If the menu isn’t visible, upgrade to a plan that supports Connectors.
