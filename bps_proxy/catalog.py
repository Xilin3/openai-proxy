"""Versioned model metadata; never invent replacement model instructions."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path

MODELS = ('gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-5.6-terra')
BUNDLED_CATALOG = Path(__file__).with_name('data') / 'model_catalog.json'
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MODEL_FIELDS = frozenset({
    'slug', 'display_name', 'description', 'default_reasoning_level',
    'supported_reasoning_levels', 'shell_type', 'visibility', 'supported_in_api',
    'priority', 'model_messages', 'base_instructions', 'include_skills_usage_instructions',
    'include_plugin_usage_instructions', 'include_apps_usage_instructions',
    'default_reasoning_summary', 'support_verbosity', 'default_verbosity',
    'apply_patch_tool_type', 'web_search_tool_type', 'truncation_policy',
    'supports_image_detail_original', 'context_window', 'max_context_window',
    'effective_context_window_percent', 'experimental_supported_tools',
    'input_modalities', 'supports_search_tool', 'supports_experimental_context',
    'supports_reasoning_summary_parameter', 'use_responses_lite',
    'node_repl_auto_review_required', 'node_repl_disabled', 'tool_mode',
    'multi_agent_version', 'multi_agent_reasoning_effort',
})


class CatalogError(ValueError):
    pass


def models_digest(models):
    raw = json.dumps(models, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def sanitized_models(document):
    if not isinstance(document, dict) or not isinstance(document.get('models'), list):
        raise CatalogError('模型目录必须包含 models 数组')
    selected = {}
    for raw in document['models']:
        if not isinstance(raw, dict) or raw.get('slug') not in MODELS:
            continue
        slug = raw['slug']
        if slug in selected:
            raise CatalogError('模型目录含重复型号')
        item = {k: copy.deepcopy(v) for k, v in raw.items() if k in MODEL_FIELDS}
        messages = item.get('model_messages')
        template = messages.get('instructions_template') if isinstance(messages, dict) else None
        if template is None and isinstance(item.get('base_instructions'), str):
            messages = dict(messages or {})
            template = item['base_instructions']
            messages['instructions_template'] = template
            item['model_messages'] = messages
        if not isinstance(template, str) or not template.strip():
            raise CatalogError('模型目录缺少完整指令模板')
        policy = item.get('truncation_policy')
        if (not isinstance(policy, dict) or policy.get('mode') not in ('bytes', 'tokens')
                or type(policy.get('limit')) is not int or policy['limit'] <= 0):
            raise CatalogError('模型目录截断策略无效')
        if type(item.get('use_responses_lite')) is not bool:
            raise CatalogError('模型目录必须明确 use_responses_lite')
        for key in ('supported_reasoning_levels', 'input_modalities', 'experimental_supported_tools'):
            if not isinstance(item.get(key), list):
                raise CatalogError('模型目录缺少必需的列表字段')
        selected[slug] = item
    if set(selected) != set(MODELS):
        raise CatalogError('模型目录必须包含四个受支持型号')
    return [selected[slug] for slug in MODELS]


@lru_cache(maxsize=4)
def _load(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise CatalogError('模型目录文件过大')
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError('无法读取有效模型目录') from exc
    models = sanitized_models(document)
    digest = models_digest(models)
    provenance = document.get('source') if isinstance(document.get('source'), dict) else {}
    if provenance.get('models_sha256') and provenance['models_sha256'] != digest:
        raise CatalogError('模型目录校验值不匹配')
    version = provenance.get('client_version', document.get('client_version', 'unknown'))
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9_.+-]{1,40}', version):
        version = 'unknown'
    return models, {'catalog_sha256': digest, 'catalog_client_version': version}


def catalog_snapshot():
    configured = os.environ.get('BPS_MODEL_CATALOG')
    path = Path(configured).expanduser() if configured else BUNDLED_CATALOG
    # Each selected file is pinned for this process. A restart explicitly reloads it.
    models, info = _load(str(path.resolve()))
    return copy.deepcopy(models), {**info, 'catalog_source': 'configured' if configured else 'bundled'}
