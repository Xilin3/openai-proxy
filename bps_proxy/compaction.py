"""Adapt compact requests to BPS native compaction without synthetic summaries."""

from __future__ import annotations

import copy
import time

from bps_proxy.upstream import UpstreamError
from bps_proxy.wire import is_compaction


def compact_request(source: dict) -> dict:
    request = copy.deepcopy(source)
    items = request['input']
    if isinstance(items, str):
        items = [{'role': 'user', 'content': items}]
    if any(item.get('type') == 'compaction_trigger' for item in items[:-1]):
        raise ValueError('compaction_trigger 必须位于 input 末尾')
    if not items or items[-1].get('type') != 'compaction_trigger':
        items.append({'type': 'compaction_trigger'})
    request['input'] = items
    # Disable this request's tools without replacing the conversation's catalog.
    request['tool_choice'] = 'none'
    return request


def compact_response(response: dict) -> dict:
    if response.get('status') != 'completed':
        raise UpstreamError(502, '上游未完成上下文压缩，请重试')
    output = response.get('output')
    compacted = [item for item in output if isinstance(item, dict) and item.get('type') == 'compaction'] if isinstance(output, list) else []
    if len(compacted) != 1 or any(not isinstance(item.get('encrypted_content'), str)
                            or not item['encrypted_content'].strip() for item in compacted):
        raise UpstreamError(502, '上游未返回有效的上下文压缩结果')
    # The encrypted payload is opaque and must survive the next request unchanged.
    return {'id': response['id'], 'object': 'response.compaction',
            'created_at': response.get('created_at', int(time.time())),
            'output': output, 'usage': response.get('usage')}
