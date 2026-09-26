"""Loopback Responses server with bounded image retries and strict completion."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from bps_proxy.auth import AuthError, load_session
from bps_proxy.images import Pictures
from bps_proxy.upstream import UpstreamError, iter_events
from bps_proxy.tool_policy import ToolSelectionError, missing_call_message, missing_call_reason, requires_tool_call
from bps_proxy.wire import (CallMemory, DEFAULT_MODEL, MODEL_ALIASES, ProtocolError, StreamRewriter,
                            conversation_identity, continue_message, model_catalog, office_stub, prepare_body)

log = logging.getLogger('bps_proxy')
MAX_OFFICE_HOPS = 3
MAX_NO_CALL_RETRIES = 1
MAX_IMAGE_RETRIES = 4
MAX_REQUEST_BYTES = 32 * 1024 * 1024
REQUEST_TIMEOUT = 30
MAX_CONCURRENT_REQUESTS = 16
TERMINALS = {'response.completed', 'response.failed', 'response.incomplete'}


def is_loopback(host: str) -> bool:
    if host == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or bool(getattr(address, 'ipv4_mapped', None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], memory: CallMemory) -> None:
        if not is_loopback(address[0]):
            raise ValueError('proxy must bind to a loopback address')
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
        self.memory = memory
        self.pictures = Pictures()
        self.admission = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(REQUEST_TIMEOUT)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server: ProxyServer
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt: str, *args: Any) -> None:
        # Request paths and headers can contain client-provided secrets.
        route = getattr(self, "path", "").partition("?")[0].rstrip("/") or "/"
        if route not in ("/health", "/v1/health", "/models", "/v1/models", "/responses", "/v1/responses"):
            route = "other"
        method = getattr(self, "command", None)
        if method not in ("GET", "POST", "OPTIONS", "HEAD"):
            method = "other"
        log.info("local request request_id=%s method=%s route=%s status=%s",
                 self._request_id(), method, route, args[1] if len(args) > 1 else "-")

    def _request_id(self):
        if not hasattr(self, "_trace_id"):
            self._trace_id = uuid.uuid4().hex[:12]
        return self._trace_id

    def _error(self, status: int, message: str, kind: str = 'invalid_request_error') -> None:
        self._json(status, {'error': {'message': message, 'type': kind}})

    def _guard(self) -> bool:
        hosts = self.headers.get_all('Host') or []
        valid = len(hosts) == 1 and is_loopback(self.client_address[0]) and self.headers.get('Origin') is None
        if valid:
            try:
                host = urlparse('http://' + hosts[0])
                valid = bool(host.hostname and is_loopback(host.hostname) and not host.username
                             and not host.password and not host.path and not host.query and not host.fragment
                             and (host.port is None or host.port == self.server.server_address[1]))
            except ValueError:
                valid = False
        if not valid:
            log.warning("request rejected request_id=%s source=local_guard host_count=%s origin_present=%s peer_loopback=%s",
                        self._request_id(), len(hosts), self.headers.get("Origin") is not None, is_loopback(self.client_address[0]))
            self._error(403, '仅接受本机客户端请求')
        return valid

    def _body_length(self):
        if self.headers.get('Transfer-Encoding') is not None:
            self._error(400, '不接受分块上传，请提供 Content-Length')
            return None
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._error(400, 'Content-Length 无效或缺失')
            return None
        if len(lengths[0]) > 12 or int(lengths[0]) > MAX_REQUEST_BYTES:
            self._error(413, '请求体超过 32 MiB')
            return None
        if self.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            self._error(415, '请发送未压缩的 JSON 请求体')
            return None
        return int(lengths[0])

    def handle_expect_100(self):
        if not self._guard() or self._body_length() is None:
            return False
        return super().handle_expect_100()

    def do_GET(self) -> None:
        if not self._guard():
            return
        path = urlparse(self.path).path.rstrip('/') or '/'
        if path in ('/health', '/v1/health'):
            self._json(200, {'ok': True})
        elif path in ('/v1/models', '/models'):
            models = model_catalog()
            self._json(200, {'object': 'list', 'data': models, 'models': models})
        else:
            self._error(404, 'not found')

    def do_POST(self) -> None:
        if not self._guard():
            return
        if urlparse(self.path).path.rstrip('/') not in ('/v1/responses', '/responses'):
            self._error(404, 'not found')
            return
        length = self._body_length()
        if length is None:
            return
        if not self.server.admission.acquire(blocking=False):
            self._error(503, '代理正在处理其他请求，请稍后重试', 'server_error')
            return
        try:
            self._post(length)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            log.info('client disconnected')
        finally:
            self.server.admission.release()

    def _post(self, length: int) -> None:
        try:
            raw = self.rfile.read(length)
        except socket.timeout:
            self._error(408, '读取请求体超时')
            return
        if len(raw) != length:
            self._error(400, '请求体不完整')
            return
        try:
            source = json.loads(raw.decode('utf-8'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non-finite number')))
            self._validate(source)
        except (UnicodeError, ValueError, RecursionError) as exc:
            message = str(exc) if type(exc) is ValueError else '请求体不是有效 JSON'
            self._error(400, message)
            return
        try:
            session = load_session()
        except AuthError as exc:
            self._error(401, str(exc), 'authentication_error')
            return
        stream = source.get('stream', False)
        started = False
        started_at = time.monotonic()
        terminal = "none"
        try:
            with closing(self._iter_relay(source, session)) as relay:
                # Resolve HTTP rejections and upload failures before committing SSE headers.
                first = next(relay)
                if first[0] in TERMINALS:
                    terminal = first[0]
                if stream:
                    self._start_sse()
                    started = True
                    self._write_event(*first)
                    for event, payload in relay:
                        if event in TERMINALS:
                            terminal = event
                        self._write_event(event, payload)
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                else:
                    final = first[1].get('response') if first[0] in TERMINALS else None
                    for event, payload in relay:
                        if event in TERMINALS:
                            terminal = event
                            final = payload.get('response')
                    if not isinstance(final, dict):
                        raise UpstreamError(502, '上游没有返回完整响应')
                    self._json(200, final)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            terminal = "client_disconnected"
            raise
        except Exception as exc:
            terminal = "response.failed"
            error_kind = 'server_error'
            if isinstance(exc, ToolSelectionError):
                status, message, error_kind = 400, str(exc), 'invalid_request_error'
                log.warning("request rejected request_id=%s source=client_tools reason=no_declared_tools",
                            self._request_id())
            elif isinstance(exc, UpstreamError):
                status, message = exc.status, exc.message
                log.warning("request failed request_id=%s source=upstream status=%s stream_started=%s",
                            self._request_id(), status, started)
            else:
                status, message = 502, '代理处理请求失败'
                log.error('relay failed exception_type=%s', type(exc).__name__)
            if started:
                self._write_event('response.failed', {'type': 'response.failed', 'response': {
                    'status': 'failed', 'output': [], 'error': {'message': message, 'code': 'proxy_error'}}})
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            else:
                self._error(status, message, error_kind)
        finally:
            log.info("relay ended request_id=%s terminal=%s elapsed_ms=%s",
                     self._request_id(), terminal, int((time.monotonic() - started_at) * 1000))

    @staticmethod
    def _validate(source):
        if not isinstance(source, dict):
            raise ValueError('请求体必须是对象')
        if not isinstance(source.get('input'), (str, list)):
            raise ValueError('input 必须是字符串或消息数组')
        if isinstance(source['input'], list) and any(not isinstance(item, dict) for item in source['input']):
            raise ValueError('input 数组中的每一项必须是对象')
        if 'stream' in source and not isinstance(source['stream'], bool):
            raise ValueError('stream 必须是布尔值')
        if source.get('previous_response_id') or source.get('conversation') or source.get('background'):
            raise ValueError('请发送完整对话历史，不支持后台请求或服务端续接')

    def _iter_relay(self, source: dict, session: Any):
        account = getattr(session, 'account_id', '')
        memory = self.server.memory
        if account:
            memory = memory.scoped(account + ':' + conversation_identity(source))
        pictures = getattr(self.server, 'pictures', None) or Pictures()
        tools = memory.bind_tools(source)
        log.info("relay start request_id=%s tools_present=%s effective_tools=%s",
                 self._request_id(), "tools" in source, len(tools))
        if requires_tool_call(source) and not tools:
            raise ToolSelectionError('请求要求调用工具，但没有可用的已声明工具')
        working = json.loads(json.dumps(source))
        response_id = None
        sequence = 0
        prior_output = []
        fresh = set()
        bump = False
        seen_created = False
        office_retries = 0
        no_call_retries = 0
        instructions = source.get('instructions') if isinstance(source.get('instructions'), str) else ''

        def emit(event, payload):
            nonlocal response_id, sequence
            payload = _hide_excel_instructions(payload, instructions)
            payload = dict(payload)
            response = payload.get('response')
            if isinstance(response, dict):
                response_id = response_id or response.get('id') or 'resp_' + uuid.uuid4().hex
                payload['response'] = {**response, 'id': response_id}
            if 'response_id' in payload and response_id:
                payload['response_id'] = response_id
            if isinstance(payload.get('output_index'), int):
                payload['output_index'] += len(prior_output)
            payload['type'] = event
            payload['sequence_number'] = sequence
            sequence += 1
            return event, payload

        def failed(message, code='proxy_error'):
            return emit('response.failed', {'response': {'id': response_id or 'resp_' + uuid.uuid4().hex,
                        'object': 'response', 'status': 'failed', 'output': list(prior_output),
                        'error': {'code': code, 'message': message}}})

        for hop in range(MAX_OFFICE_HOPS + MAX_NO_CALL_RETRIES + 1):
            terminal = None
            for attempt in range(MAX_IMAGE_RETRIES + 1):
                sent = pictures.rewrite(working, session, fresh=fresh)
                fresh.update(sent.uploaded)
                body = prepare_body(sent.body, memory, identity_source=source, bump=bump)
                bump = True
                metadata = body['metadata']
                known_models = {item["id"] for item in model_catalog()}
                requested_model = source.get("model")
                requested_model = requested_model.strip() if isinstance(requested_model, str) else DEFAULT_MODEL
                requested_model = requested_model or DEFAULT_MODEL
                if requested_model not in known_models | MODEL_ALIASES.keys():
                    requested_model = "unlisted"
                model = body["model"] if body["model"] in known_models else "unlisted"
                log.info("upstream start request_id=%s requested_model=%s model=%s turn_id=%s iteration=%s hop=%s image_attempt=%s",
                         self._request_id(), requested_model, model, metadata["turn_id"], metadata["agent_iteration"], hop, attempt)
                rewriter = StreamRewriter(tools, memory, turn_id=metadata['turn_id'],
                                          iteration=int(metadata['agent_iteration']), hop=hop,
                                          parallel=source.get('parallel_tool_calls') is not False)
                received = False
                upstream = None
                try:
                    upstream = iter_events(session, body)
                    for event, payload in upstream:
                        received = True
                        if event == 'error':
                            error = payload.get('error')
                            message = error.get('message') if isinstance(error, dict) else payload.get('message')
                            raise UpstreamError(502, message if isinstance(message, str) else '上游返回错误')
                        for name, rewritten in rewriter.handle(event, payload):
                            if name in TERMINALS:
                                terminal = (name, rewritten)
                            elif name == 'response.created':
                                if not seen_created:
                                    seen_created = True
                                    yield emit(name, rewritten)
                            else:
                                yield emit(name, rewritten)
                        if terminal:
                            break
                    if terminal is None:
                        yield failed('上游连接在响应完成前中断，请重试')
                        return
                    break
                except ProtocolError as exc:
                    log.warning("request failed request_id=%s source=protocol reason=%s", self._request_id(), str(exc))
                    yield failed(str(exc))
                    return
                except UpstreamError as exc:
                    plan = pictures.retry_plan(exc.status, sent) if not received and attempt < MAX_IMAGE_RETRIES else None
                    if plan == 'reupload':
                        pictures.forget(account, sent.reused)
                        log.info('retrying pictures with renewed attachments')
                        continue
                    if plan == 'upload':
                        kind = 'message' if 'message' in sent.inline else min(sent.inline)
                        pictures.refuse_inline(account, {kind})
                        log.info('retrying inline pictures as attachments kind=%s', kind)
                        continue
                    if received:
                        yield failed(exc.message)
                        return
                    raise
                finally:
                    close = getattr(upstream, 'close', None)
                    if close is not None:
                        close()
            if terminal is None:
                raise UpstreamError(502, '图片重试次数已用尽')
            name, payload = terminal
            response = dict(payload['response'])
            output = response.get('output', [])
            log.info("relay output request_id=%s terminal=%s client_calls=%s rejected_calls=%s no_call_retries=%s",
                     self._request_id(), name, len(rewriter.client_calls), len(rewriter.office_calls), no_call_retries)
            if name == 'response.completed' and rewriter.office_calls and not rewriter.client_calls:
                if office_retries >= MAX_OFFICE_HOPS:
                    yield failed('工具调用格式连续无效，客户端未执行这些调用，请重试')
                    return
                office_retries += 1
                working = self._continue_office(working, rewriter.office_calls, tools, memory, rewriter, output)
                prior_output.extend(output)
                continue
            reason = (missing_call_reason(source, tools, output)
                      if name == 'response.completed' and not rewriter.client_calls else None)
            if reason:
                log.warning("missing client call request_id=%s reason=%s retry=%s limit=%s",
                            self._request_id(), reason, no_call_retries, MAX_NO_CALL_RETRIES)
                prior_output.extend(output)
                if no_call_retries >= MAX_NO_CALL_RETRIES:
                    yield failed('模型未能完成客户端工具调用，请重试', reason)
                    return
                no_call_retries += 1
                working = self._continue_without_call(working, output, missing_call_message(reason, tools))
                continue
            response['output'] = prior_output + output
            yield emit(name, {**payload, 'response': response})
            return

    def _continue_without_call(self, source, output, message):
        continued = json.loads(json.dumps(source))
        items = continued.get('input')
        if isinstance(items, str):
            items = [{'role': 'user', 'content': items}]
        continued['input'] = list(items or []) + output + [{'role': 'developer', 'content': message}]
        return continued

    def _continue_office(self, source, calls, tools, memory, rewriter, output):
        continued = json.loads(json.dumps(source))
        items = continued.get('input')
        if isinstance(items, str):
            items = [{'role': 'user', 'content': items}]
        items = list(items or [])
        items.extend(output)
        seen = set()
        for call in calls:
            call_id = call.get('call_id')
            if not isinstance(call_id, str) or not call_id or call_id in seen:
                continue
            seen.add(call_id)
            rejection = rewriter.rejections.get(call_id)
            items.append(call)
            items.append({'type': 'function_call_output', 'call_id': call_id,
                          'output': office_stub(call, tools, rejection.reason if rejection else None)})
            memory.remember(call, turn_id=rewriter.turn_id, iteration=rewriter.iteration)
        items.append({'role': 'developer', 'content': continue_message(tools)})
        continued['input'] = items
        return continued

    def _json(self, status: int, payload: dict) -> None:
        self.close_connection = True
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()

    def _write_event(self, event: str, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        self.wfile.write(f'event: {event}\ndata: {data}\n\n'.encode('utf-8'))
        self.wfile.flush()


def _hide_excel_instructions(payload: dict, instructions: str) -> dict:
    response = payload.get('response')
    if not isinstance(response, dict) or 'instructions' not in response:
        return payload
    return {**payload, 'response': {**response, 'instructions': instructions}}


def serve(host: str, port: int, memory: CallMemory) -> None:
    server = ProxyServer((host, port), memory)
    log.info('listening on loopback port=%s default_model=%s', port, DEFAULT_MODEL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
