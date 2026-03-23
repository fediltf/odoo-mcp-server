# Odoo MCP Server

A lightweight [Model Context Protocol](https://modelcontextprotocol.io) server that connects any LLM — Claude, Gemini, GPT-4, and more — to your Odoo instance.

Runs as a single Docker container. No changes to Odoo required.

---

## How It Works

```
LLM Client  ──MCP/SSE──►  odoo-mcp-server  ──XML-RPC──►  Odoo
```

The server exposes Odoo data and actions as **tools** the LLM can call. Permissions are controlled from the Odoo UI and reload live — no container restart needed.

For a full breakdown of the server logic and scenario examples, see [`odoo_mcp_breakdown.md`](./odoo_mcp_breakdown.md).

---

## Quick Start

```bash
git clone https://github.com/fediltf/odoo-mcp-server.git
cd odoo-mcp-server
cp .env.example .env   # fill in your Odoo details
docker compose up --build -d
```

The SSE endpoint will be live at `http://localhost:8765/sse`.

---

## Configuration

Edit `.env` before starting the container.

| Variable | Required | Description |
|---|---|---|
| `MCP_API_KEY` | Yes (prod) | Bearer token for client auth. Empty = no auth |
| `MCP_ODOO_URL` | Yes | Full URL of your Odoo instance |
| `MCP_ODOO_DB` | Yes | Odoo database name |
| `MCP_ODOO_USER` | Yes | Odoo user login |
| `MCP_ODOO_PASSWORD` | Yes | Odoo user password |
| `MCP_ALLOW_WRITE` | No | `1` to enable create/write/delete tools |
| `MCP_ALLOW_EXECUTE` | No | `1` to enable method execution |
| `MCP_ALLOWED_MODELS` | No | Comma-separated model whitelist. Empty = all models |

---

## Available Tools

| Tool | When Available |
|---|---|
| `odoo_list_models` | Always |
| `odoo_get_fields` | Always |
| `odoo_search_read` | Always |
| `odoo_read` | Always |
| `odoo_create` | `MCP_ALLOW_WRITE=1` |
| `odoo_write` | `MCP_ALLOW_WRITE=1` |
| `odoo_unlink` | `MCP_ALLOW_WRITE=1` |
| `odoo_execute_method` | `MCP_ALLOW_EXECUTE=1` |

---

## Connecting a Client

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

async with MultiServerMCPClient({
    "odoo": {
        "url": "http://localhost:8765/sse",
        "transport": "sse",
        "headers": {"Authorization": "Bearer your_api_key"},
    }
}) as client:
    tools = await client.get_tools()
```

Works with any MCP-compatible client. The LangChain example above uses `langchain-mcp-adapters`.

---

## Security

- Always set `MCP_API_KEY` in production
- Use a dedicated Odoo user with minimal permissions — avoid `admin`
- Set `MCP_ALLOWED_MODELS` to limit what data the LLM can access
- Keep `MCP_ALLOW_WRITE` and `MCP_ALLOW_EXECUTE` off unless needed
- Put the server behind a TLS-terminating reverse proxy (nginx, Caddy) in production
