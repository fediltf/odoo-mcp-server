import json
import logging
import os
import time
import xmlrpc.client
from typing import Any

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
_logger = logging.getLogger('odoo_mcp')

# ── Static config from environment (require container restart to change) ───────
HOST = os.environ.get('MCP_HOST', '0.0.0.0')
PORT = int(os.environ.get('MCP_PORT', '8765'))
API_KEY = os.environ.get('MCP_API_KEY', '')
ODOO_URL = os.environ.get('MCP_ODOO_URL', 'http://odoo:8069')
ODOO_DB = os.environ.get('MCP_ODOO_DB', '')
ODOO_USER = os.environ.get('MCP_ODOO_USER', 'admin')
ODOO_PASSWORD = os.environ.get('MCP_ODOO_PASSWORD', '')

# How often (seconds) to re-fetch operational settings from Odoo
CONFIG_TTL_SECONDS = int(os.environ.get('MCP_CONFIG_TTL', '30'))

# ── Fallback values from env (used if Odoo is unreachable at config fetch time)
_FALLBACK_ALLOW_WRITE = os.environ.get('MCP_ALLOW_WRITE', '0') == '1'
_FALLBACK_ALLOW_EXECUTE = os.environ.get('MCP_ALLOW_EXECUTE', '0') == '1'
_raw_models = os.environ.get('MCP_ALLOWED_MODELS', '')
_FALLBACK_ALLOWED_MODELS: set[str] = {m.strip() for m in _raw_models.split(',') if m.strip()}


# ── Odoo XML-RPC client ────────────────────────────────────────────────────────
class OdooClient:
    def __init__(self):
        self._uid: int | None = None
        self._common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        self._models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

    @property
    def uid(self) -> int:
        if self._uid is None:
            self._uid = self._common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
            if not self._uid:
                raise RuntimeError('Odoo authentication failed — check MCP_ODOO_USER / MCP_ODOO_PASSWORD.')
            _logger.info('Authenticated as uid=%d', self._uid)
        return self._uid

    def execute(self, model: str, method: str, *args, **kwargs) -> Any:
        return self._models.execute_kw(
            ODOO_DB, self.uid, ODOO_PASSWORD,
            model, method, list(args), kwargs,
        )

    def search_read(self, model: str, domain: list, fields: list | None = None,
                    limit: int = 80, offset: int = 0, order: str = '') -> list[dict]:
        kw: dict[str, Any] = {'limit': limit, 'offset': offset}
        if fields:
            kw['fields'] = fields
        if order:
            kw['order'] = order
        return self.execute(model, 'search_read', domain, **kw)


odoo = OdooClient()


# ── Live config cache ──────────────────────────────────────────────────────────
class LiveConfig:
    """
    Fetches allow_write, allow_execute, and allowed_models from the
    mcp.config record in Odoo. Results are cached for CONFIG_TTL_SECONDS.

    On fetch failure the previous values are kept, so a temporary Odoo
    restart does not flip permissions back to defaults.
    """

    def __init__(self):
        self.allow_write = _FALLBACK_ALLOW_WRITE
        self.allow_execute = _FALLBACK_ALLOW_EXECUTE
        self.allowed_models = _FALLBACK_ALLOWED_MODELS
        self._fetched_at = 0.0  # force fetch on first access

    def _refresh(self):
        now = time.monotonic()
        if now - self._fetched_at < CONFIG_TTL_SECONDS:
            return  # still fresh

        try:
            records = odoo.search_read(
                'mcp.config',
                [['active', '=', True]],
                fields=['allow_write', 'allow_execute', 'allowed_model_ids'],
                limit=1,
            )
            if not records:
                _logger.warning('No active mcp.config record found in Odoo — keeping previous settings.')
                self._fetched_at = now
                return

            cfg = records[0]
            self.allow_write = bool(cfg.get('allow_write', False))
            self.allow_execute = bool(cfg.get('allow_execute', False))

            # Fetch allowed model names from the related mcp.allowed.model records
            model_ids = cfg.get('allowed_model_ids', [])
            if model_ids:
                allowed = odoo.search_read(
                    'mcp.allowed.model',
                    [['id', 'in', model_ids], ['active', '=', True]],
                    fields=['model_name'],
                )
                self.allowed_models = {r['model_name'] for r in allowed if r.get('model_name')}
            else:
                self.allowed_models = set()  # empty = all models allowed

            self._fetched_at = now
            _logger.debug(
                'Config refreshed: allow_write=%s allow_execute=%s allowed_models=%s',
                self.allow_write, self.allow_execute,
                ', '.join(self.allowed_models) if self.allowed_models else 'ALL',
            )

        except Exception as e:
            _logger.warning('Failed to refresh config from Odoo (%s) — keeping previous settings.', e)
            self._fetched_at = now  # don't hammer Odoo if it's down

    # ── Public accessors (always trigger a refresh check) ─────────────────────

    @property
    def write_enabled(self) -> bool:
        self._refresh()
        return self.allow_write

    @property
    def execute_enabled(self) -> bool:
        self._refresh()
        return self.allow_execute

    @property
    def model_whitelist(self) -> set[str]:
        self._refresh()
        return self.allowed_models

    def check_model(self, model: str) -> None:
        whitelist = self.model_whitelist
        if whitelist and model not in whitelist:
            raise PermissionError(f"Model '{model}' is not in the allowed models list.")


config = LiveConfig()

# ── MCP Server ─────────────────────────────────────────────────────────────────
mcp = Server('odoo-mcp-server')


def _ok(data: Any) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type='text', text=json.dumps(data, default=str, indent=2))]
    )


def _err(msg: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type='text', text=json.dumps({'error': msg}))],
        isError=True,
    )


@mcp.list_tools()
async def list_tools() -> ListToolsResult:
    """
    Called by the LLM client on connect.
    Returns tool list based on LIVE settings fetched from Odoo —
    no restart needed when toggling allow_write / allow_execute.
    """
    tools = [
        Tool(
            name='odoo_list_models',
            description='List available Odoo models. Filter by name with the query param.',
            inputSchema={
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'Filter models by name.'},
                },
            },
        ),
        Tool(
            name='odoo_get_fields',
            description=(
                'Get field definitions (name, type, relations) for an Odoo model. '
                'Call this before search_read to know which fields exist on the model.'
            ),
            inputSchema={
                'type': 'object',
                'required': ['model'],
                'properties': {
                    'model': {
                        'type': 'string',
                        'description': 'Odoo technical model name, e.g. res.partner',
                    },
                    'attributes': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': (
                            'Field attributes to return. '
                            'Defaults to: string, type, required, readonly, relation'
                        ),
                    },
                },
            },
        ),
        Tool(
            name='odoo_search_read',
            description=(
                'Search Odoo records using a domain filter and return selected fields. '
                'The domain parameter must be a JSON-encoded string to ensure '
                'compatibility across all LLM providers.'
            ),
            inputSchema={
                'type': 'object',
                'required': ['model'],
                'properties': {
                    'model': {
                        'type': 'string',
                        'description': 'Odoo technical model name, e.g. res.partner',
                    },
                    'domain': {
                        'type': 'string',
                        'description': (
                            'JSON-encoded Odoo domain filter. '
                            'A list of triplets [[field, operator, value], ...]. '
                            'Examples: '
                            '"[]" to return all records, '
                            '"[[\"is_company\",\"=\",true]]" for companies, '
                            '"[[\"customer_rank\",\">\",0],[\"active\",\"=\",true]]" for active customers. '
                            'Logical operators: "[\"&\", cond1, cond2]", "[\"|\" , cond1, cond2]".'
                        ),
                        'default': '[]',
                    },
                    'fields': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of field names to return. Omit to return all fields.',
                    },
                    'limit': {
                        'type': 'integer',
                        'description': 'Maximum number of records to return. Default 80.',
                    },
                    'offset': {
                        'type': 'integer',
                        'description': 'Number of records to skip (for pagination). Default 0.',
                    },
                    'order': {
                        'type': 'string',
                        'description': "Sort order, e.g. 'name asc' or 'create_date desc'.",
                    },
                },
            },
        ),
        Tool(
            name='odoo_read',
            description='Read specific Odoo records by their integer IDs.',
            inputSchema={
                'type': 'object',
                'required': ['model', 'ids'],
                'properties': {
                    'model': {'type': 'string'},
                    'ids': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                        'description': 'List of record IDs to fetch.',
                    },
                    'fields': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Field names to return. Omit for all fields.',
                    },
                },
            },
        ),
        Tool(
            name='odoo_count',
            description='Count records matching a domain without fetching any data. '
                        'Use this before search_read when you only need the total.',
            inputSchema={
                'type': 'object',
                'required': ['model'],
                'properties': {
                    'model': {'type': 'string'},
                    'domain': {
                        'type': 'string',
                        'description': 'JSON-encoded domain filter. Default "[]" = all records.',
                        'default': '[]',
                    },
                },
            },
        )
    ]

    # Write tools — only added if currently enabled in Odoo config
    if config.write_enabled:
        tools += [
            Tool(
                name='odoo_create',
                description='Create a new record in an Odoo model.',
                inputSchema={
                    'type': 'object',
                    'required': ['model', 'values'],
                    'properties': {
                        'model': {'type': 'string'},
                        'values': {
                            'type': 'string',
                            'description': (
                                'JSON-encoded object of field name to value mappings for the new record. '
                                'e.g. "{\"name\": \"Acme\", \"email\": \"info@acme.com\"}" '
                                'Always pass as a JSON string, not a raw object.'
                            ),
                        },
                    },
                },
            ),
            Tool(
                name='odoo_write',
                description='Update one or more existing Odoo records.',
                inputSchema={
                    'type': 'object',
                    'required': ['model', 'ids', 'values'],
                    'properties': {
                        'model': {'type': 'string'},
                        'ids': {
                            'type': 'array',
                            'items': {'type': 'integer'},
                            'description': 'IDs of the records to update.',
                        },
                        'values': {
                            'type': 'string',
                            'description': (
                                'JSON-encoded object of field name to new value mappings. '
                                'e.g. "{\"state\": \"done\", \"note\": \"updated\"}" '
                                'Always pass as a JSON string, not a raw object.'
                            ),
                        },
                    },
                },
            ),
            Tool(
                name='odoo_unlink',
                description='Permanently delete records from an Odoo model. This cannot be undone.',
                inputSchema={
                    'type': 'object',
                    'required': ['model', 'ids'],
                    'properties': {
                        'model': {'type': 'string'},
                        'ids': {
                            'type': 'array',
                            'items': {'type': 'integer'},
                            'description': 'IDs of the records to delete.',
                        },
                    },
                },
            ),
        ]

    # Execute tool — only added if currently enabled in Odoo config
    if config.execute_enabled:
        tools.append(Tool(
            name='odoo_execute_method',
            description=(
                'Call any public method on an Odoo model. '
                'Use for actions like confirming orders (action_confirm), '
                'validating transfers (action_validate), etc.'
            ),
            inputSchema={
                'type': 'object',
                'required': ['model', 'method'],
                'properties': {
                    'model': {'type': 'string'},
                    'method': {
                        'type': 'string',
                        'description': 'Method name to call, e.g. action_confirm',
                    },
                    'ids': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                        'description': 'Record IDs to call the method on.',
                    },
                    'args': {
                        'type': 'string',
                        'description': (
                            'JSON-encoded list of positional arguments. '
                            'e.g. "[]" for none, or "[\"value1\", 42]".'
                        ),
                        'default': '[]',
                    },
                    'kwargs': {
                        'type': 'string',
                        'description': (
                            'JSON-encoded dict of keyword arguments. '
                            'e.g. "{}" for none, or "{\"force\": true}".'
                        ),
                        'default': '{}',
                    },
                },
            },
        ))

    return ListToolsResult(tools=tools)


@mcp.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:  # noqa: C901
    try:
        # ── odoo_list_models ──────────────────────────────────────────────────
        if name == 'odoo_list_models':
            domain: list = []
            whitelist = config.model_whitelist
            if whitelist:
                domain = [('model', 'in', list(whitelist))]
            q = arguments.get('query', '')
            if q:
                domain.append(('name', 'ilike', q))
            return _ok(odoo.search_read('ir.model', domain,
                                        fields=['name', 'model', 'info'], limit=100))

        # ── odoo_get_fields ───────────────────────────────────────────────────
        elif name == 'odoo_get_fields':
            model = arguments['model']
            config.check_model(model)
            attrs = arguments.get('attributes', ['string', 'type', 'required', 'readonly', 'relation'])
            return _ok(odoo.execute(model, 'fields_get', attributes=attrs))

        # ── odoo_search_read ──────────────────────────────────────────────────
        elif name == 'odoo_search_read':
            model = arguments['model']
            config.check_model(model)
            # domain is passed as a JSON string for cross-LLM schema compatibility
            raw_domain = arguments.get('domain', '[]')
            domain = json.loads(raw_domain) if isinstance(raw_domain, str) else raw_domain
            return _ok(odoo.search_read(
                model=model,
                domain=domain,
                fields=arguments.get('fields'),
                limit=arguments.get('limit', 80),
                offset=arguments.get('offset', 0),
                order=arguments.get('order', ''),
            ))

        # ── odoo_read ─────────────────────────────────────────────────────────
        elif name == 'odoo_read':
            model = arguments['model']
            config.check_model(model)
            return _ok(odoo.execute(model, 'read', arguments['ids'],
                                    fields=arguments.get('fields')))

        # ── odoo_count ─────────────────────────────────────────────────────────
        elif name == 'odoo_count':
            model = arguments['model']
            config.check_model(model)
            raw = arguments.get('domain', '[]')
            domain = json.loads(raw) if isinstance(raw, str) else raw
            return _ok({'count': odoo.execute(model, 'search_count', domain)})

        # ── odoo_create ───────────────────────────────────────────────────────
        elif name == 'odoo_create':
            if not config.write_enabled:
                return _err(
                    'Write operations are currently disabled. Enable "Allow Write Operations" in the Odoo MCP configuration.')
            model = arguments['model']
            config.check_model(model)
            raw_values = arguments['values']
            parsed_values = json.loads(raw_values) if isinstance(raw_values, str) else raw_values
            return _ok({'id': odoo.execute(model, 'create', parsed_values)})

        # ── odoo_write ────────────────────────────────────────────────────────
        elif name == 'odoo_write':
            if not config.write_enabled:
                return _err(
                    'Write operations are currently disabled. Enable "Allow Write Operations" in the Odoo MCP configuration.')
            model = arguments['model']
            config.check_model(model)
            return _ok({'success': odoo.execute(model, 'write',
                                                arguments['ids'],
                                                json.loads(arguments['values']) if isinstance(arguments['values'],
                                                                                              str) else arguments[
                                                    'values'])})

        # ── odoo_unlink ───────────────────────────────────────────────────────
        elif name == 'odoo_unlink':
            if not config.write_enabled:
                return _err(
                    'Write operations are currently disabled. Enable "Allow Write Operations" in the Odoo MCP configuration.')
            model = arguments['model']
            config.check_model(model)
            return _ok({'success': odoo.execute(model, 'unlink', arguments['ids'])})

        # ── odoo_execute_method ───────────────────────────────────────────────
        elif name == 'odoo_execute_method':
            if not config.execute_enabled:
                return _err(
                    'Method execution is currently disabled. Enable "Allow Method Execution" in the Odoo MCP configuration.')
            model = arguments['model']
            config.check_model(model)
            # args and kwargs are passed as JSON strings for cross-LLM schema compatibility
            raw_args = arguments.get('args', '[]')
            raw_kwargs = arguments.get('kwargs', '{}')
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            parsed_kwargs = json.loads(raw_kwargs) if isinstance(raw_kwargs, str) else raw_kwargs
            return _ok(odoo.execute(
                model,
                arguments['method'],
                arguments.get('ids', []),
                *parsed_args,
                **parsed_kwargs,
            ))

        else:
            return _err(f'Unknown tool: {name}')

    except PermissionError as e:
        return _err(str(e))
    except Exception as e:
        _logger.exception('Error executing tool %s', name)
        return _err(f'{type(e).__name__}: {e}')


# ── Auth middleware ────────────────────────────────────────────────────────────
class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if API_KEY:
            auth = request.headers.get('Authorization', '')
            if auth != f'Bearer {API_KEY}':
                return Response('Unauthorized', status_code=401)
        return await call_next(request)


# ── Starlette app with SSE transport ──────────────────────────────────────────
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

if __name__ == '__main__':
    _logger.info('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    _logger.info('  Odoo MCP Server  %s:%d', HOST, PORT)
    _logger.info('  Odoo: %s  DB: %s  User: %s', ODOO_URL, ODOO_DB, ODOO_USER)
    _logger.info('  Config TTL: %ds (live-reloaded from Odoo)', CONFIG_TTL_SECONDS)
    _logger.info('  Fallback — Write: %s  Execute: %s', _FALLBACK_ALLOW_WRITE, _FALLBACK_ALLOW_EXECUTE)
    _logger.info('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    uvicorn.run(app, host=HOST, port=PORT, log_level='info')
