"""Call Merge Agent Handler tools over MCP.

Agent Handler holds the GitHub credential and records every call in its Tool
Call Logs, so the review issues for triage replies are opened, read, and
closed through it rather than with a token in this repository. MCP here is
JSON-RPC 2.0 over HTTP. The reply is either JSON or a short event stream.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

from bot import redact

AH_API = 'https://ah-api.merge.dev/api/v1'
AH_SECRET_NAMES = ('AGENT_HANDLER_API_KEY', 'AGENT_HANDLER_TOOL_PACK_ID', 'AGENT_HANDLER_REGISTERED_USER_ID')
PROTOCOL_VERSION = '2025-06-18'


class AgentHandlerError(RuntimeError):
    pass


def configured(env=None):
    env = os.environ if env is None else env
    return all((env.get(name) or '').strip() for name in AH_SECRET_NAMES)


def post_json(url, headers, payload, timeout=60):
    """POST a JSON-RPC message. Returns (headers, body text)."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return dict(response.headers), response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        raise AgentHandlerError(f'Agent Handler HTTP {exc.code}: {body[:500]}') from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AgentHandlerError(f'Network request to Agent Handler failed ({getattr(exc, "reason", exc)}).') from None


def parse_message(content_type, body, request_id):
    """Return the JSON-RPC response with ``request_id`` from a JSON or SSE body."""
    if 'text/event-stream' in (content_type or ''):
        candidates = []
        for line in body.splitlines():
            if line.startswith('data:'):
                data = line[5:].strip()
                if data:
                    candidates.append(json.loads(data))
    else:
        candidates = [json.loads(body)] if body.strip() else []
    for message in candidates:
        if isinstance(message, dict) and message.get('id') == request_id:
            return message
    raise AgentHandlerError('Agent Handler returned no response for the request.')


class AgentHandler:
    def __init__(self, api_key, tool_pack_id, registered_user_id, post=post_json):
        self.url = f'{AH_API}/tool-packs/{tool_pack_id}/registered-users/{registered_user_id}/mcp'
        self.api_key = api_key
        self.post = post
        self.session_id = None
        self.request_id = 0

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(*(env[name].strip() for name in AH_SECRET_NAMES))

    def _send(self, method, params=None, notify=False):
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
            'User-Agent': 't3codes-release-bot',
        }
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        if not notify:
            self.request_id += 1
            payload['id'] = self.request_id
        try:
            response_headers, body = self.post(self.url, headers, payload)
        except AgentHandlerError as exc:
            raise AgentHandlerError(redact(str(exc), (self.api_key,))) from None
        lowered = {key.lower(): value for key, value in response_headers.items()}
        self.session_id = lowered.get('mcp-session-id') or self.session_id
        if notify:
            return None
        message = parse_message(lowered.get('content-type'), body, self.request_id)
        if message.get('error'):
            raise AgentHandlerError(f'Agent Handler {method} failed: {message["error"].get("message")}')
        return message.get('result') or {}

    def _initialize(self):
        if self.session_id is not None:
            return
        self._send('initialize', {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {},
            'clientInfo': {'name': 't3codes-release-bot', 'version': '1'},
        })
        self.session_id = self.session_id or ''
        self._send('notifications/initialized', notify=True)

    def call(self, name, tool_input):
        """Call one tool, such as ``github__create_issue``, and return its JSON result."""
        self._initialize()
        result = self._send('tools/call', {'name': name, 'arguments': {'input': tool_input}})
        text = next((item.get('text') or '' for item in result.get('content') or [] if item.get('type') == 'text'), '')
        if result.get('isError'):
            raise AgentHandlerError(f'{name} failed: {text[:300]}')
        if isinstance(result.get('structuredContent'), dict):
            return result['structuredContent']
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {'text': text}
