"""Read-only owned recording server; tests never launch it."""

import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if 'id' not in request:
        continue
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'owned-fixture', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'echo', 'description': 'Owned recording tool. Returns a synthetic message or error.',
                            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False},
                            'inputSchema': {'type': 'object', 'properties': {'fail': {'type': 'boolean'}},
                                            'required': ['fail']}}]}
    elif method == 'tools/call':
        failed = request['params']['arguments']['fail']
        result = {'content': [{'type': 'text', 'text': 'owned tool failure' if failed else 'owned tool success'}],
                  'isError': failed}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
