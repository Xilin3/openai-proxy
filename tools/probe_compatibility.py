#!/usr/bin/env python3
"""Exercise installed Codex with synthetic credentials and a mocked BPS upstream."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bps_proxy.catalog import MODELS
from bps_proxy.server import Handler, ProxyServer
from bps_proxy.wire import CallMemory, declared_client_tools, model_catalog


def fake_token():
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return encode({'alg': 'HS256', 'typ': 'JWT'}) + '.' + encode({
        'exp': int(time.time()) + 3600, 'iat': int(time.time()), 'email': 'probe@example.invalid',
        'https://api.openai.com/auth': {'chatgpt_account_id': 'probe-account',
            'chatgpt_user_id': 'probe-user', 'chatgpt_plan_type': 'plus'},
    }) + '.cHJvYmU'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('lite', 'standard'), default='lite')
    args = parser.parse_args()
    wire_mode = args.mode
    catalog = model_catalog()
    for item in catalog:
        item['use_responses_lite'] = wire_mode == 'lite'
    codex = shutil.which('codex')
    if not codex:
        raise SystemExit('codex executable not found')
    logging.basicConfig(level=logging.ERROR)
    requests, originals, bodies, states, verified = [], [], [], {}, []

    class ObservedHandler(Handler):
        def do_GET(self):
            requests.append({'method': 'GET', 'path': self.path, 'upgrade': self.headers.get('Upgrade')})
            super().do_GET()

        def do_POST(self):
            requests.append({'method': 'POST', 'path': self.path,
                             'encoding': self.headers.get('Content-Encoding', 'identity')})
            super().do_POST()

        def _iter_relay(self, source, session, **kwargs):
            originals.append(copy.deepcopy(source))
            return super()._iter_relay(source, session, **kwargs)

    with tempfile.TemporaryDirectory(prefix='bps-codex-compat-') as directory:
        root = Path(directory)
        home = root / 'home'
        home.mkdir()
        override = root / 'instructions.txt'
        override.write_text('PROBE_EXPLICIT_INSTRUCTIONS: execute the requested local operation.')

        def upstream(session, body, **_kwargs):
            bodies.append(copy.deepcopy(body))
            model = body['model']
            state = states.setdefault(model, {})
            compact = body['input'][-1].get('type') == 'compaction_trigger'
            tokens = 50
            if compact:
                output = [{'type': 'compaction', 'id': 'cmp_probe', 'encrypted_content': 'probe-opaque-context'}]
                state['compact'] = True
            elif not state.get('native'):
                tools, origin = declared_client_tools(originals[-1])
                if not tools:
                    raise AssertionError('Fresh client request omitted its tool catalog')
                state['catalog_source'] = origin
                state['tools'] = [{'name': t['name'], 'type': t['type']} for t in tools]
                marker = root / (model + '.txt')
                script = 'from pathlib import Path; p=Path(' + repr(str(marker)) + '); p.open("a").write("BPS_TOOL_OK"); print(p.read_text())'
                import shlex
                arguments = {'cmd': 'python3 -c ' + shlex.quote(script), 'workdir': str(root), 'max_output_tokens': 300}
                executor = next((t for t in tools if t['type'] == 'custom' and t['name'] == 'exec'), None)
                if executor:
                    envelope = {'tool': 'exec', 'input': 'text(await tools.exec_command(' + json.dumps(arguments) + '));'}
                else:
                    executor = next((t for t in tools if t['type'] == 'function' and t['name'].split('.')[-1] == 'exec_command'), None)
                    if executor is None:
                        raise AssertionError('Unexpected client tools: ' + json.dumps(state['tools']))
                    envelope = {'tool': executor['name'], 'args': arguments}
                native = {'type': 'function_call', 'id': 'fc_probe', 'call_id': 'call_probe',
                          'name': 'run_officejs', 'status': 'completed',
                          'arguments': json.dumps({'summary': 'Write the temporary probe marker',
                            'code': json.dumps(envelope), 'destructive': True, 'references': [str(marker)]})}
                state.update(native=copy.deepcopy(native), metadata=copy.deepcopy(body['metadata']))
                output = [native]
            else:
                if not state.get('replayed'):
                    if state['native'] not in body['input']:
                        raise AssertionError('Full native tool item was not replayed')
                    results = [item for item in body['input'] if item.get('type') == 'function_call_output'
                               and item.get('call_id') == 'call_probe']
                    if not results or 'BPS_TOOL_OK' not in json.dumps(results):
                        raise AssertionError('Missing actual tool result')
                    if body['metadata']['turn_id'] != state['metadata']['turn_id']:
                        raise AssertionError('Tool followup changed turn_id')
                    if int(body['metadata']['agent_iteration']) <= int(state['metadata']['agent_iteration']):
                        raise AssertionError('Tool followup did not advance iteration')
                    state['replayed'] = True
                    state['tool_result'] = results
                    tokens = 210000
                elif state.get('compact'):
                    if not any(item.get('type') == 'compaction' and item.get('encrypted_content') == 'probe-opaque-context'
                               for item in body['input']):
                        raise AssertionError('Opaque compaction item lost during resume')
                    state['resumed'] = True
                output = [{'type': 'message', 'id': 'msg_probe_' + str(len(bodies)), 'role': 'assistant',
                           'status': 'completed', 'phase': 'final_answer',
                           'content': [{'type': 'output_text', 'text': 'NATIVE_PROXY_OK', 'annotations': []}]}]
            response = {'id': 'resp_probe_' + str(len(bodies)), 'object': 'response',
                        'created_at': int(time.time()), 'model': model, 'status': 'completed', 'output': output,
                        'usage': {'input_tokens': tokens, 'output_tokens': 10, 'total_tokens': tokens + 10,
                                  'input_tokens_details': {'cached_tokens': 0},
                                  'output_tokens_details': {'reasoning_tokens': 0}}}
            yield 'response.created', {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}}
            for index, item in enumerate(output):
                yield 'response.output_item.added', {'type': 'response.output_item.added', 'output_index': index, 'item': item}
                yield 'response.output_item.done', {'type': 'response.output_item.done', 'output_index': index, 'item': item}
            yield 'response.completed', {'type': 'response.completed', 'response': response}

        server = ProxyServer(('127.0.0.1', 0), CallMemory())
        server.RequestHandlerClass = ObservedHandler
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        url = 'http://127.0.0.1:' + str(server.server_port) + '/v1'
        (home / 'config.toml').write_text('openai_base_url = ' + json.dumps(url) + chr(10))
        token = fake_token()
        (home / 'auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {
            'id_token': token, 'access_token': token, 'refresh_token': 'probe-not-a-real-refresh-token',
            'account_id': 'probe-account'}}))
        env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'TMPDIR') if key in os.environ}
        env.update({'HOME': str(root), 'CODEX_HOME': str(home), 'NO_PROXY': '127.0.0.1,localhost'})

        def run(arguments):
            result = subprocess.run([codex, '-a', 'never', '-c', 'features.apps=false',
                                     '-c', 'features.remote_plugin=false', *arguments], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=90)
            if result.returncode or 'NATIVE_PROXY_OK' not in result.stdout:
                raise RuntimeError('Codex fixture failed: ' + result.stderr[-2500:] + result.stdout[-2500:])
            if 'failed to refresh' in result.stderr.lower():
                raise RuntimeError('Model catalog refresh failed')
            return [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]

        thread.start()
        try:
            with patch('bps_proxy.server.load_session', return_value=None), patch(
                    'bps_proxy.server.iter_events', side_effect=upstream), patch(
                    'bps_proxy.server.model_catalog', return_value=catalog):
                for model in MODELS:
                    start = len(originals)
                    args = ['exec', '--json', '--skip-git-repo-check', '-s', 'workspace-write', '-m', model]
                    if model == 'gpt-5.6-luna':
                        args += ['-c', 'model_instructions_file=' + json.dumps(str(override))]
                    first = run(args + ['Write the requested probe marker once using a local tool, then reply NATIVE_PROXY_OK.'])
                    marker = root / (model + '.txt')
                    if not marker.exists():
                        raise AssertionError('Marker missing: ' + json.dumps(states[model].get('tool_result')) + json.dumps(first)[-3000:])
                    thread_id = next(item['thread_id'] for item in first if item.get('type') == 'thread.started')
                    run(['exec', 'resume', thread_id, '-m', model, '--json', '--skip-git-repo-check', 'Reply NATIVE_PROXY_OK again.'])
                    if (root / (model + '.txt')).read_text() != 'BPS_TOOL_OK':
                        raise AssertionError('Tool execution did not produce exactly one marker')
                    state = states[model]
                    if not all(state.get(key) for key in ('replayed', 'compact', 'resumed')):
                        raise AssertionError('Incomplete tool/compaction flow for ' + model)
                    expected_origin = 'additional_tools' if wire_mode == 'lite' else 'top_level'
                    if state['catalog_source'] != expected_origin:
                        raise AssertionError('CLI did not exercise the selected wire mode')
                    if model == 'gpt-5.6-luna' and 'PROBE_EXPLICIT_INSTRUCTIONS' not in json.dumps(originals[start]):
                        raise AssertionError('Explicit model_instructions_file was not preserved')
                    verified.append({'model': model, 'catalog_source': state['catalog_source'],
                                     'tool_executed_once': True, 'native_replay': True, 'compaction_roundtrip': True})
                    print(json.dumps({'verified': verified[-1]}), flush=True)
            if not any(item['path'].split('?')[0].endswith('/models') for item in requests):
                raise RuntimeError('Codex did not fetch the model catalog')
            if not any(item.get('encoding') == 'zstd' for item in requests):
                raise RuntimeError('Codex did not send a compressed request')
            if not any(item.get('upgrade') == 'websocket' for item in requests):
                raise RuntimeError('Codex did not exercise WebSocket fallback')
            if any('tools' in b or any(i.get('type') == 'additional_tools' for i in b['input']) for b in bodies):
                raise AssertionError('Client tool schemas leaked to native upstream')
            print(json.dumps({'ok': True, 'mode': wire_mode, 'codex': subprocess.check_output([codex, '--version'], text=True).strip(),
                              'model_requests': len(bodies), 'verified': verified, 'explicit_instructions_preserved': True,
                              'zstd_and_websocket_fallback': True, 'real_upstream_called': False}, indent=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    main()
