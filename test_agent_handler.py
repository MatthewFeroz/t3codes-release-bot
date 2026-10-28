import json
import unittest

from agent_handler import AgentHandler, AgentHandlerError, configured, parse_message


class FakePost:
    """Records JSON-RPC messages and answers like Agent Handler's MCP endpoint."""

    def __init__(self, tool_result, sse=False, error=None):
        self.tool_result = tool_result
        self.sse = sse
        self.error = error
        self.calls = []

    def __call__(self, url, headers, payload):
        self.calls.append((url, dict(headers), payload))
        if 'id' not in payload:
            return {'Content-Type': 'application/json'}, ''
        if payload['method'] == 'initialize':
            result = {'protocolVersion': '2025-06-18', 'capabilities': {}}
        elif self.error:
            message = {'jsonrpc': '2.0', 'id': payload['id'], 'error': {'code': -32602, 'message': self.error}}
            return {'Content-Type': 'application/json', 'Mcp-Session-Id': 'abc'}, json.dumps(message)
        else:
            result = self.tool_result
        message = {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}
        if self.sse:
            return {'content-type': 'text/event-stream', 'mcp-session-id': 'abc'}, f'event: message\ndata: {json.dumps(message)}\n\n'
        return {'Content-Type': 'application/json', 'Mcp-Session-Id': 'abc'}, json.dumps(message)


def text_result(value, is_error=False):
    return {'content': [{'type': 'text', 'text': json.dumps(value) if not isinstance(value, str) else value}],
            'isError': is_error}


class AgentHandlerTests(unittest.TestCase):
    def test_initializes_then_calls_the_tool_with_the_session(self):
        post = FakePost(text_result({'number': 12}))
        ah = AgentHandler('key-123456789', 'pack', 'user', post=post)
        self.assertEqual(ah.call('github__create_issue', {'owner': 'o', 'repo': 'r'}), {'number': 12})
        methods = [payload['method'] for _, _, payload in post.calls]
        self.assertEqual(methods, ['initialize', 'notifications/initialized', 'tools/call'])
        url, headers, payload = post.calls[-1]
        self.assertEqual(url, 'https://ah-api.merge.dev/api/v1/tool-packs/pack/registered-users/user/mcp')
        self.assertEqual(headers['Authorization'], 'Bearer key-123456789')
        self.assertEqual(headers['Mcp-Session-Id'], 'abc')
        self.assertEqual(payload['params'], {'name': 'github__create_issue', 'arguments': {'input': {'owner': 'o', 'repo': 'r'}}})
        ah.call('github__get_issue', {})
        self.assertEqual([payload['method'] for _, _, payload in post.calls].count('initialize'), 1)

    def test_reads_an_event_stream_reply(self):
        ah = AgentHandler('key', 'pack', 'user', post=FakePost(text_result({'number': 3}), sse=True))
        self.assertEqual(ah.call('github__get_issue', {}), {'number': 3})

    def test_prefers_structured_content(self):
        result = {'content': [{'type': 'text', 'text': 'ignored'}], 'structuredContent': {'number': 9}}
        ah = AgentHandler('key', 'pack', 'user', post=FakePost(result))
        self.assertEqual(ah.call('github__get_issue', {}), {'number': 9})

    def test_tool_error_is_loud(self):
        ah = AgentHandler('key', 'pack', 'user', post=FakePost(text_result('Label does not exist', is_error=True)))
        with self.assertRaisesRegex(AgentHandlerError, 'Label does not exist'):
            ah.call('github__create_issue', {})

    def test_json_rpc_error_is_loud(self):
        ah = AgentHandler('key', 'pack', 'user', post=FakePost(None, error='tool not in pack'))
        with self.assertRaisesRegex(AgentHandlerError, 'tool not in pack'):
            ah.call('github__create_issue', {})

    def test_http_error_text_does_not_leak_the_key(self):
        def post(url, headers, payload):
            raise AgentHandlerError('Agent Handler HTTP 403: bad key secret-key-value')
        ah = AgentHandler('secret-key-value', 'pack', 'user', post=post)
        with self.assertRaises(AgentHandlerError) as caught:
            ah.call('github__create_issue', {})
        self.assertNotIn('secret-key-value', str(caught.exception))

    def test_missing_response_id_is_an_error(self):
        with self.assertRaises(AgentHandlerError):
            parse_message('application/json', json.dumps({'jsonrpc': '2.0', 'id': 99, 'result': {}}), 1)

    def test_configured_needs_all_three_values(self):
        self.assertFalse(configured({'AGENT_HANDLER_API_KEY': 'k'}))
        self.assertTrue(configured({
            'AGENT_HANDLER_API_KEY': 'k', 'AGENT_HANDLER_TOOL_PACK_ID': 'p', 'AGENT_HANDLER_REGISTERED_USER_ID': 'u',
        }))


if __name__ == '__main__':
    unittest.main()
