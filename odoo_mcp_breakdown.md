# Odoo MCP Server — Code Breakdown

> A deep-dive into `mcp_server.py`: architecture, each logical component, and real-world usage scenarios.

---

## Table of Contents

1. [What Is This Server?](#1-what-is-this-server)
2. [Configuration Layer](#2-configuration-layer)
3. [OdooClient — The XML-RPC Bridge](#3-odoodlient--the-xml-rpc-bridge)
4. [LiveConfig — Dynamic Permission Reloading](#4-liveconfig--dynamic-permission-reloading)
5. [MCP Tools — The API Surface](#5-mcp-tools--the-api-surface)
   - [Read Tools (always available)](#read-tools-always-available)
   - [Write Tools (gated)](#write-tools-gated)
   - [Execute Tool (gated)](#execute-tool-gated)
6. [Auth Middleware](#6-auth-middleware)
7. [Transport Layer — SSE over Starlette](#7-transport-layer--sse-over-starlette)
8. [Error Handling Strategy](#8-error-handling-strategy)
9. [End-to-End Scenarios](#9-end-to-end-scenarios)
10. [Architecture Diagram](#10-architecture-diagram)

---

## 1. What Is This Server?

This is an **MCP (Model Context Protocol) server** that sits between an AI assistant (the LLM client) and an **Odoo** instance. It exposes Odoo's data and business logic as a set of **tools** that the LLM can call — like fetching records, creating entries, or triggering workflow actions.

```
LLM Client (e.g. Claude)
        │
        │  MCP over SSE (HTTP)
        ▼
 odoo-mcp-server  ◄──── ApiKeyMiddleware
        │
        │  XML-RPC
        ▼
   Odoo Instance
```

The server uses **Server-Sent Events (SSE)** as the MCP transport, meaning the LLM opens a persistent HTTP connection and receives streamed tool responses.

---

## 2. Configuration Layer

```python
HOST          = os.environ.get('MCP_HOST', '0.0.0.0')
PORT          = int(os.environ.get('MCP_PORT', '8765'))
API_KEY       = os.environ.get('MCP_API_KEY', '')
ODOO_URL      = os.environ.get('MCP_ODOO_URL', 'http://odoo:8069')
ODOO_DB       = os.environ.get('MCP_ODOO_DB', '')
ODOO_USER     = os.environ.get('MCP_ODOO_USER', 'admin')
ODOO_PASSWORD = os.environ.get('MCP_ODOO_PASSWORD', '')
CONFIG_TTL_SECONDS = int(os.environ.get('MCP_CONFIG_TTL', '30'))
```

**Two tiers of configuration:**

| Tier | Source | When Applied |
|------|--------|--------------|
| **Static** | Environment variables | At container start (requires restart to change) |
| **Operational** | Odoo `mcp.config` record | Live-reloaded every `CONFIG_TTL_SECONDS` seconds |

**Fallback values** exist for the operational settings. If Odoo is temporarily unreachable, the server keeps the last known permissions rather than defaulting to a restrictive or permissive state:

```python
_FALLBACK_ALLOW_WRITE   = os.environ.get('MCP_ALLOW_WRITE', '0') == '1'
_FALLBACK_ALLOW_EXECUTE = os.environ.get('MCP_ALLOW_EXECUTE', '0') == '1'
_FALLBACK_ALLOWED_MODELS: set[str] = {m.strip() for m in _raw_models.split(',') if m.strip()}
```

> **Scenario:** Odoo restarts for a maintenance window. The MCP server doesn't crash or lock out the LLM — it continues operating with the last cached permissions until Odoo comes back.

---

## 3. OdooClient — The XML-RPC Bridge

```python
class OdooClient:
    def __init__(self):
        self._uid: int | None = None
        self._common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        self._models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
```

Odoo exposes two XML-RPC endpoints:

| Endpoint | Purpose |
|----------|---------|
| `/xmlrpc/2/common` | Authentication (`authenticate`) |
| `/xmlrpc/2/object` | Data operations (`execute_kw`) |

### Lazy Authentication

```python
@property
def uid(self) -> int:
    if self._uid is None:
        self._uid = self._common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
```

The client authenticates **on first use**, not at startup. This avoids race conditions when Odoo is still booting during container start.

### `execute` and `search_read`

```python
def execute(self, model: str, method: str, *args, **kwargs) -> Any:
    return self._models.execute_kw(
        ODOO_DB, self.uid, ODOO_PASSWORD,
        model, method, list(args), kwargs,
    )

def search_read(self, model, domain, fields=None, limit=80, offset=0, order=''):
    ...
    return self.execute(model, 'search_read', domain, **kw)
```

`search_read` is a convenience wrapper. `execute` is the raw gateway to **any** Odoo model method.

> **Scenario:** The LLM calls `odoo_search_read` on `sale.order` with a domain filter. This maps to `odoo.execute('sale.order', 'search_read', [[...]], fields=[...])`, which becomes a single XML-RPC call to Odoo.

---

## 4. LiveConfig — Dynamic Permission Reloading

```python
class LiveConfig:
    def _refresh(self):
        now = time.monotonic()
        if now - self._fetched_at < CONFIG_TTL_SECONDS:
            return  # still fresh
        # fetch from mcp.config in Odoo ...
```

This class polls an `mcp.config` record in Odoo (at most every `CONFIG_TTL_SECONDS` seconds) to determine three live settings:

| Setting | Effect |
|---------|--------|
| `allow_write` | Enables `odoo_create`, `odoo_write`, `odoo_unlink` |
| `allow_execute` | Enables `odoo_execute_method` |
| `allowed_model_ids` | Restricts which Odoo models the LLM can access |

### Model Whitelisting

```python
def check_model(self, model: str) -> None:
    whitelist = self.model_whitelist
    if whitelist and model not in whitelist:
        raise PermissionError(f"Model '{model}' is not in the allowed models list.")
```

If `allowed_models` is **empty**, all models are accessible. If it contains entries, only those are allowed. This check is called before every tool operation.

> **Scenario:** An admin adds `res.partner` and `sale.order` to the Odoo whitelist. Within 30 seconds (one TTL), the MCP server picks up the change. Any LLM request targeting `account.move` now returns a permission error — no server restart needed.

---

## 5. MCP Tools — The API Surface

Tools are declared in `list_tools()` and handled in `call_tool()`. The list is **dynamic** — write and execute tools only appear when their respective permissions are enabled.

### Read Tools (always available)

#### `odoo_list_models`

Lists available Odoo models. If a model whitelist is active, only whitelisted models are returned.

```python
# Example LLM call
{ "tool": "odoo_list_models", "arguments": { "query": "sale" } }

# Returns models like:
# sale.order, sale.order.line, sale.report, ...
```

**Scenario:** The LLM doesn't know what models exist. It calls `odoo_list_models` first to discover `sale.order`, then proceeds to query it.

---

#### `odoo_get_fields`

Returns the field schema of a model — names, types, whether they are required or readonly, and relational targets.

```python
# Example LLM call
{ "tool": "odoo_get_fields", "arguments": { "model": "res.partner" } }

# Returns:
# { "name": { "type": "char", "required": true, ... },
#   "email": { "type": "char", ... },
#   "country_id": { "type": "many2one", "relation": "res.country" } }
```

**Scenario:** Before searching partner records, the LLM inspects fields to know that `customer_rank` (not `is_customer`) is the correct field for filtering customers.

---

#### `odoo_search_read`

The primary read tool. Searches records using an Odoo domain and returns selected fields.

The `domain` parameter is a **JSON-encoded string** (not a native JSON array) to ensure consistent handling across different LLM providers that may serialize arguments differently.

```python
# Example LLM call — find active customers
{
  "tool": "odoo_search_read",
  "arguments": {
    "model": "res.partner",
    "domain": "[[\"customer_rank\",\">\",0],[\"active\",\"=\",true]]",
    "fields": ["name", "email", "phone"],
    "limit": 20,
    "order": "name asc"
  }
}
```

**Scenario:** A user asks "Show me our top customers." The LLM calls `odoo_search_read` on `res.partner` ordered by `customer_rank desc`, paginating with `limit` and `offset` if needed.

---

#### `odoo_read`

Reads specific records by their integer IDs — useful as a follow-up when IDs are already known.

```python
# Example LLM call
{
  "tool": "odoo_read",
  "arguments": {
    "model": "sale.order",
    "ids": [42, 43],
    "fields": ["name", "state", "amount_total"]
  }
}
```

**Scenario:** The LLM extracted order IDs from a previous search and now needs specific field values from those exact records without re-running a search.

---

### Write Tools (gated)

These tools only appear in the tool list when `allow_write = True` in the Odoo config. Even if called directly, they re-check the live permission at execution time.

#### `odoo_create`

Creates a new record. The `values` field is a **JSON-encoded string**.

```python
{
  "tool": "odoo_create",
  "arguments": {
    "model": "res.partner",
    "values": "{\"name\": \"Acme Corp\", \"email\": \"hello@acme.com\", \"is_company\": true}"
  }
}
# Returns: { "id": 1042 }
```

**Scenario:** A user dictates a new customer to the AI assistant. The LLM creates the partner record directly in Odoo.

---

#### `odoo_write`

Updates one or more existing records.

```python
{
  "tool": "odoo_write",
  "arguments": {
    "model": "sale.order",
    "ids": [42],
    "values": "{\"note\": \"Priority shipment requested\"}"
  }
}
# Returns: { "success": true }
```

**Scenario:** A sales rep tells the AI "add a priority note to order SO042." The LLM finds the order ID, then calls `odoo_write` to patch the `note` field.

---

#### `odoo_unlink`

Permanently deletes records. The description explicitly warns: *"This cannot be undone."*

```python
{
  "tool": "odoo_unlink",
  "arguments": {
    "model": "res.partner",
    "ids": [99]
  }
}
# Returns: { "success": true }
```

**Scenario:** A user asks the AI to remove a test contact created during a demo. The LLM confirms the ID and calls `odoo_unlink`. An admin should carefully consider whether to enable write operations in production.

---

### Execute Tool (gated)

#### `odoo_execute_method`

Calls any public Python method on an Odoo model. This unlocks business workflow actions.

Both `args` and `kwargs` are JSON-encoded strings for cross-provider compatibility.

```python
# Confirm a sale order
{
  "tool": "odoo_execute_method",
  "arguments": {
    "model": "sale.order",
    "method": "action_confirm",
    "ids": [42],
    "args": "[]",
    "kwargs": "{}"
  }
}
```

```python
# Validate a stock picking
{
  "tool": "odoo_execute_method",
  "arguments": {
    "model": "stock.picking",
    "method": "button_validate",
    "ids": [17],
    "args": "[]",
    "kwargs": "{}"
  }
}
```

**Scenario:** A warehouse manager tells the AI "confirm order SO042 and validate its delivery." The LLM chains two tool calls: `action_confirm` on `sale.order`, then `button_validate` on the related `stock.picking`.

---

## 6. Auth Middleware

```python
class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if API_KEY:
            auth = request.headers.get('Authorization', '')
            if auth != f'Bearer {API_KEY}':
                return Response('Unauthorized', status_code=401)
        return await call_next(request)
```

Every HTTP request passes through this middleware. If `MCP_API_KEY` is set (non-empty), the request must include:

```
Authorization: Bearer <your-api-key>
```

If `MCP_API_KEY` is empty, auth is **disabled** — suitable for local/dev environments behind a private network.

> **Scenario:** The MCP server is exposed on a shared network. Setting `MCP_API_KEY=supersecret123` ensures only configured LLM clients with the correct bearer token can call tools.

---

## 7. Transport Layer — SSE over Starlette

```python
sse = SseServerTransport('/messages/')

async def handle_sse(request: Request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp.run(streams[0], streams[1], mcp.create_initialization_options())

app = Starlette(
    routes=[
        Route('/sse', endpoint=handle_sse),
        Mount('/messages/', app=sse.handle_post_message),
    ],
    middleware=[Middleware(ApiKeyMiddleware)],
)
```

Two HTTP routes are registered:

| Route | Purpose |
|-------|---------|
| `GET /sse` | LLM client opens a persistent SSE connection for receiving events |
| `POST /messages/` | LLM client sends tool call requests |

This bidirectional pattern (SSE for server→client push, POST for client→server messages) is the standard MCP SSE transport.

---

## 8. Error Handling Strategy

```python
@mcp.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    try:
        # ... tool logic
    except PermissionError as e:
        return _err(str(e))
    except Exception as e:
        _logger.exception('Error executing tool %s', name)
        return _err(f'{type(e).__name__}: {e}')
```

Errors are **never raised to the transport layer**. Instead, they are returned as structured `CallToolResult` objects with `isError=True`:

```python
def _err(msg: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type='text', text=json.dumps({'error': msg}))],
        isError=True,
    )
```

This means the LLM receives the error message as readable text and can decide how to respond to the user — rather than the connection failing silently.

> **Scenario:** The LLM tries to write to `account.move` but it's not whitelisted. It gets back `{"error": "Model 'account.move' is not in the allowed models list."}` and can tell the user "I don't have permission to modify accounting entries."

---

## 9. End-to-End Scenarios

### Scenario A: Read-only Q&A assistant

**Config:** `allow_write=False`, `allow_execute=False`, `allowed_models={res.partner, sale.order}`

1. User: *"How many confirmed orders do we have this month?"*
2. LLM calls `odoo_search_read` on `sale.order` with domain `[["state","=","sale"],["date_order",">=","2025-03-01"]]`
3. LLM counts records and answers the user.
4. No write tools appear in the tool list — no risk of accidental mutation.

---

### Scenario B: Customer onboarding assistant

**Config:** `allow_write=True`, `allow_execute=False`, `allowed_models={res.partner}`

1. User provides a new customer's details.
2. LLM calls `odoo_get_fields` to confirm required fields on `res.partner`.
3. LLM calls `odoo_create` with name, email, phone, and `is_company=true`.
4. Odoo returns the new partner ID; LLM confirms to the user.
5. Unlink and execute are unavailable — the assistant can create but not delete or trigger workflows.

---

### Scenario C: Order fulfillment assistant

**Config:** `allow_write=True`, `allow_execute=True`, `allowed_models={sale.order, stock.picking}`

1. User: *"Confirm order SO099 and mark its delivery as done."*
2. LLM calls `odoo_search_read` on `sale.order` with domain `[["name","=","SO099"]]` → gets ID `99`.
3. LLM calls `odoo_execute_method`: `action_confirm` on `sale.order`, ids=[99].
4. LLM calls `odoo_search_read` on `stock.picking` with domain `[["sale_id","=",99]]` → gets picking ID `55`.
5. LLM calls `odoo_execute_method`: `button_validate` on `stock.picking`, ids=[55].
6. LLM reports success to the user.

---

### Scenario D: Permission toggled mid-session

**Config:** Admin unchecks `allow_write` in the Odoo MCP config UI.

1. Within 30 seconds, `LiveConfig._refresh()` picks up the change.
2. The next `list_tools()` call from the LLM no longer includes write tools.
3. Even if the LLM caches old tool definitions, any write call returns:
   `{"error": "Write operations are currently disabled."}`
4. No server restart required.

---

## 10. Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    LLM Client (Claude)                  │
│                                                         │
│  GET /sse ──────────────────────────────────────────►  │
│  POST /messages/ ◄──────────────────────────────────►  │
└──────────────────────────┬──────────────────────────────┘
                           │  HTTP + ApiKeyMiddleware
                           │
┌──────────────────────────▼──────────────────────────────┐
│                   Starlette App (SSE)                   │
│                                                         │
│  ┌──────────────┐    ┌──────────────────────────────┐  │
│  │ list_tools() │    │       call_tool()            │  │
│  │              │    │  ┌─────────────────────────┐ │  │
│  │  Reads live  │    │  │ check model whitelist   │ │  │
│  │  config to   │    │  │ check write/exec perms  │ │  │
│  │  build tool  │    │  │ delegate to OdooClient  │ │  │
│  │  list        │    │  └─────────────────────────┘ │  │
│  └──────────────┘    └──────────────────────────────┘  │
│                                                         │
│  ┌──────────────────────────────────────────────────┐  │
│  │                  LiveConfig                      │  │
│  │  Polls mcp.config every CONFIG_TTL_SECONDS       │  │
│  │  allow_write · allow_execute · allowed_models    │  │
│  └──────────────────────────────────────────────────┘  │
│                                                         │
│  ┌──────────────────────────────────────────────────┐  │
│  │                  OdooClient                      │  │
│  │  XML-RPC /xmlrpc/2/common  (auth)                │  │
│  │  XML-RPC /xmlrpc/2/object  (data)                │  │
│  └──────────────────────────┬─────────────────────┘   │
└──────────────────────────────┼──────────────────────────┘
                               │  XML-RPC
                               ▼
                    ┌──────────────────┐
                    │   Odoo Instance  │
                    │  (res.partner,   │
                    │   sale.order,    │
                    │   mcp.config…)   │
                    └──────────────────┘
```

---

*Generated from `mcp_server.py` — Odoo MCP Server.*
