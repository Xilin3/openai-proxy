#!/usr/bin/env python3
"""Import an explicitly selected catalog, omitting account/cache identity."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bps_proxy.catalog import MAX_CATALOG_BYTES, CatalogError, models_digest, sanitized_models


def import_catalog(source: Path, output: Path):
    with source.open('rb') as stream:
        raw = stream.read(MAX_CATALOG_BYTES + 1)
    if len(raw) > MAX_CATALOG_BYTES:
        raise CatalogError('模型目录文件过大')
    document = json.loads(raw)
    models = sanitized_models(document)
    source_info = document.get('source') if isinstance(document.get('source'), dict) else document
    provenance = {'name': 'Codex official model catalog cache', 'models_sha256': models_digest(models)}
    version = source_info.get('client_version')
    if isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9_.+-]{1,40}', version):
        provenance['client_version'] = version
    fetched = source_info.get('fetched_at')
    if isinstance(fetched, str):
        try:
            datetime.fromisoformat(fetched.replace('Z', '+00:00'))
            provenance['fetched_at'] = fetched
        except ValueError:
            pass
    result = {'schema_version': 1, 'source': provenance, 'models': models}
    # Exclusive creation prevents an accidental replacement of a selected source.
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write(chr(10))
    return provenance


def main():
    parser = argparse.ArgumentParser(description='从指定 Codex 模型目录导入四个受支持型号')
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', required=True, type=Path, help='新建的输出文件')
    args = parser.parse_args()
    try:
        result = import_catalog(args.source, args.output)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
