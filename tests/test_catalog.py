import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bps_proxy.catalog import BUNDLED_CATALOG, CatalogError, catalog_snapshot, models_digest
from bps_proxy.wire import model_catalog
from tools.import_model_catalog import import_catalog


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(BUNDLED_CATALOG.read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'catalog.json'

    def write(self, document):
        self.path.write_text(json.dumps(document))
        return patch.dict(os.environ, {'BPS_MODEL_CATALOG': str(self.path)})

    def test_bundled_templates_match_source_and_are_not_stubbed(self):
        with patch.dict(os.environ, {}, clear=True):
            result = model_catalog()
        originals = {m['slug']: m for m in self.document['models']}
        for item in result:
            original = originals[item['slug']]
            self.assertEqual(item['model_messages'], original['model_messages'])
            self.assertEqual(item['base_instructions'], original['model_messages']['instructions_template'])
            self.assertGreater(len(item['base_instructions']), 10000)
            self.assertEqual(item['truncation_policy'], {'mode': 'tokens', 'limit': 10000})
            self.assertTrue(item['use_responses_lite'])
        self.assertEqual(models_digest(self.document['models']), self.document['source']['models_sha256'])

    def test_configured_source_is_pinned_and_returned_values_are_copied(self):
        document = copy.deepcopy(self.document)
        document.pop('source')
        document['identity'] = {'private': 'must-not-be-exposed'}
        for item in document['models']:
            item['account_id'] = 'must-not-be-exposed'
            item['model_messages']['instructions_template'] = 'Explicit fixture instructions.'
        with self.write(document):
            first, info = catalog_snapshot()
            self.assertEqual(info['catalog_source'], 'configured')
            first[0]['model_messages']['instructions_template'] = 'mutated'
            self.path.write_text('broken')
            second, _ = catalog_snapshot()
            self.assertEqual(second[0]['model_messages']['instructions_template'], 'Explicit fixture instructions.')
            self.assertNotIn('must-not-be-exposed', json.dumps(second))

    def test_invalid_source_does_not_fall_back_to_short_instructions(self):
        for index, mutate in enumerate((
                lambda d: d['models'][0].pop('model_messages'),
                lambda d: d['models'][0].pop('use_responses_lite'),
                lambda d: d['models'][0].update(truncation_policy={'mode': 'tokens', 'limit': 0}),
                lambda d: d['source'].update(models_sha256='invalid'))):
            with self.subTest(index=index):
                document = copy.deepcopy(self.document)
                mutate(document)
                self.path = Path(self.temp.name) / (str(index) + '.json')
                with self.write(document), self.assertRaises(CatalogError):
                    model_catalog()

    def test_import_removes_identity_and_refuses_overwriting(self):
        document = copy.deepcopy(self.document)
        document['identity'] = 'private-account'
        document['source']['account_id'] = 'private-account'
        document['models'][0]['account_id'] = 'private-account'
        self.path.write_text(json.dumps(document))
        output = self.path.with_name('snapshot.json')
        info = import_catalog(self.path, output)
        saved = json.loads(output.read_text())
        self.assertNotIn('private-account', output.read_text())
        self.assertEqual(saved['models'], self.document['models'])
        self.assertEqual(info['models_sha256'], models_digest(saved['models']))
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            import_catalog(self.path, output)
        self.assertEqual(output.read_bytes(), before)
