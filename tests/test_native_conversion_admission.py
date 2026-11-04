"""Local identity boundaries only; these fixtures do not establish scientific parity."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline/helpers'))
import local_conversion_identity as identity


class NativeAdmission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        identity.load_contracts(Path(__file__).resolve().parents[1] /
                               'pipeline/components/maxwell-native/components/common/python')

    def setUp(self):
        self.ref = dict(uri='s3://example/published/object', size=10, etag='etag', sha256='a' * 64)
        self.stage_ref = dict(id='native-stage')
        self.settings = dict(submitted={}, effective=dict(create_linked_maxwell_companion=False))
        self.stage = dict(id='native-stage', owner={'workflow_id': 'owner'},
                          settings=self.settings, signature='b' * 64)
        self.artifacts = [dict(kind=k, object=self.ref) for k in ('nwb', 'nwb_lindi', 'nwb_validation')]
        self.details = dict(native_artifacts=self.ref, native_evidence=self.ref,
            native_unit=dict(unit_id='unit', sources=[self.ref]), settings=self.settings,
            artifacts={a['kind']: a['object'] for a in self.artifacts})
        self.origin = dict(kind='modular', envelope=self.ref, stage_id=self.stage['id'])

    def seal(self, value, key='evidence_digest'):
        value[key] = identity.digest({k:v for k,v in value.items() if k != key})
        return value

    def report(self, kind):
        value = dict(document_type=kind, owner=self.stage['owner'], unit_id='unit',
            settings=self.settings, stage_signature=self.stage['signature'], artifacts=self.artifacts)
        if kind == 'maxwell_adopted_conversion_evidence':
            value.update(scientific_execution_complete=True, durable_publication_verified=True)
        elif kind == 'maxwell_reused_conversion_evidence':
            value.update(scientific_execution_complete=False, original_scientific_execution_complete=True,
                         origin=self.origin)
        return self.seal(value)

    def admit(self, basis):
        self.details['native_validation'] = self.seal(dict(schema_version=1,
            document_type='mea_verified_native_completion', basis=basis,
            native_artifacts=self.ref, native_stage=self.stage_ref, evidence=self.ref, unit_id='unit',
            owner=self.stage['owner'], settings=self.settings, artifacts=self.artifacts,
            source_current=self.ref, direct_origin=self.origin), 'digest')

    def check(self, report):
        identity.verify_native_admission(report, self.details, self.stage, self.stage_ref)

    def test_adoption_and_reuse_keep_their_distinct_execution_claims(self):
        for kind, basis in [('maxwell_adopted_conversion_evidence', 'owned_adoption'),
                            ('maxwell_reused_conversion_evidence', 'owned_reuse')]:
            self.admit(basis)
            report = self.report(kind)
            self.check(report)
            report['scientific_execution_complete'] = not report['scientific_execution_complete']
            with self.assertRaises(ValueError):
                self.check(self.seal(report))

    def test_source_unit_settings_origin_and_evidence_are_bound(self):
        report = self.report('maxwell_reused_conversion_evidence')
        for field, value in [('unit_id', 'foreign'), ('settings', {}), ('evidence', dict(self.ref, etag='changed')),
                             ('source_current', dict(self.ref, etag='changed')),
                             ('source_current', dict(self.ref, sha256='b' * 64)),
                             ('direct_origin', dict(self.origin, stage_id='foreign'))]:
            with self.subTest(field=field):
                self.admit('owned_reuse')
                self.details['native_validation'][field] = value
                self.seal(self.details['native_validation'], 'digest')
                with self.assertRaises(ValueError):
                    self.check(report)

    def test_validated_adoption_is_not_accepted_as_fresh_phase_completion(self):
        self.admit('owned_adoption')
        report = self.report('maxwell_adopted_conversion_evidence')
        self.assertFalse(identity.completed_phases(report, self.settings))
        report['durable_publication_verified'] = False
        with self.assertRaises(ValueError):
            self.check(self.seal(report))

    def test_legacy_fresh_phase_checks_stay_strict(self):
        report = dict(scientific_execution_complete=True,
            phases=[dict(phase=p, exit_code=0) for p in identity.expected_phases(self.settings)])
        self.check(report)
        report['phases'][0]['exit_code'] = False
        with self.assertRaises(ValueError):
            self.check(report)

    def test_changed_and_unknown_evidence_rejected(self):
        self.admit('owned_reuse')
        report = self.report('maxwell_reused_conversion_evidence')
        report['unexpected'] = 1
        with self.assertRaises(ValueError):
            self.check(report)
        report['document_type'] = 'unverified_completion'
        with self.assertRaises(ValueError):
            self.check(self.seal(report))


if __name__ == '__main__':
    unittest.main()
