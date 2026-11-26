import hashlib
import tempfile
import unittest
from pathlib import Path

from cloudctl import residency, store
from cloudctl.__main__ import execute


class ResidencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'residency.sqlite')
        store.init(self.db)
        self.t = 0

    def tearDown(self):
        self.temp.cleanup()

    def clock(self, step=1):
        self.t += step
        return self.t

    def call(self, action, **kw):
        return execute(self.db, {'action': action, **kw})

    def decision(self, action, **kw):
        return self.call(action, **kw)['decision']

    # -- catalog helpers ----------------------------------------------------

    def dataset(self, did, origin, category, fields, content):
        return self.decision('residency.dataset', id=did, origin=origin,
                             category=category, fields=fields, content=content,
                             at=self.clock())

    def license(self, did, version, scope, signers):
        signatures = {s: residency.signature_of(did, version, scope, s) for s in signers}
        return self.decision('residency.license', dataset=did, version=version,
                             scope=scope, signers=signers, signatures=signatures,
                             at=self.clock())

    def node(self, node, jurisdiction, controlled=False, categories=()):
        return self.decision('residency.node', node=node, jurisdiction=jurisdiction,
                             controlled=controlled, categories=list(categories),
                             at=self.clock())

    def slot(self, sid, node):
        return self.decision('slot.register', id=sid, node=node, cpu=8, at=self.clock())

    def job(self, jid, parents=()):
        return self.decision('job.submit', id=jid, tenant='t', deadline=100000,
                             duration=20, cpu=2, created=0, parents=list(parents))

    def manifest(self, mid, jid, entries):
        return self.decision('residency.manifest', id=mid, job=jid,
                             entries=entries, at=self.clock())

    def deliver_ungoverned(self, jid, slot_id, content):
        gen = self.lease_generation(jid, slot_id)
        self.assertEqual(self.decision('run.start', id=jid, generation=gen,
                                       at=self.clock()), 'running')
        digest = 'sha256:' + hashlib.sha256(content.encode()).hexdigest()
        self.assertEqual(self.decision('run.settle', id=jid, generation=gen,
                                       at=self.clock(), digest=digest,
                                       content=content), 'delivered')

    def lease_generation(self, jid, slot_id):
        result = self.call('slot.lease', id=jid, slot=slot_id, at=self.clock(),
                           expires=99000)
        self.assertEqual(result['decision'], 'leased', result)
        return result['evidence']['generation']

    # -- base world: EU raw customer data P + NA public parameters Q --------

    P_FIELDS = ['amount', 'customer_id', 'email']
    Q_FIELDS = ['param_key', 'param_value']

    def base_catalog(self, p_version='v1', q_version='v1'):
        self.dataset('P', 'EU', 'raw_personal', self.P_FIELDS, 'raw-eu-customer-rows')
        self.dataset('Q', 'NA', 'public_params', self.Q_FIELDS, 'na-public-parameters')
        p_scope = {
            'processing_locations': ['EU'],
            'node_categories': ['eu_standard'],
            'allowed_fields': self.P_FIELDS,
            'controlled_environment': False,
            'deletion_within_days': 7,
            'required_signers': ['customer'],
            'egress': [{'classification': 'deid_stats',
                        'recipient_jurisdictions': ['EU', 'NA'],
                        'requires_verified_transform': True}],
        }
        q_scope = {
            'processing_locations': ['EU', 'NA'],
            'node_categories': ['eu_standard', 'na_standard', 'na_controlled'],
            'allowed_fields': self.Q_FIELDS,
            'controlled_environment': False,
            'deletion_within_days': 30,
            'required_signers': ['customer'],
            'egress': [{'classification': 'public_params',
                        'recipient_jurisdictions': ['EU', 'NA'],
                        'requires_verified_transform': False}],
        }
        self.assertEqual(self.license('P', p_version, p_scope, ['customer']),
                         'license_granted')
        self.assertEqual(self.license('Q', q_version, q_scope, ['customer']),
                         'license_granted')
        self.node('node-e', 'EU', categories=['eu_standard'])
        self.node('node-n', 'NA', controlled=False, categories=['na_standard'])
        self.node('node-nc', 'NA', controlled=True, categories=['na_controlled'])
        self.slot('E', 'node-e')
        self.slot('N', 'node-n')
        self.slot('NC', 'node-nc')

    def parent_jobs(self):
        # submit the whole family first: the existing scheduler only accepts
        # children whose parents are still in the accepted state
        self.job('jp')
        self.job('jq')

    def deliver_parents(self):
        self.deliver_ungoverned('jp', 'E', 'parent-p-bytes')
        self.deliver_ungoverned('jq', 'E', 'parent-q-bytes')

    def job_j_with_manifest(self, mid, jid='J', p_version='v1', q_version='v1'):
        self.job(jid, parents=['jp', 'jq'])
        self.deliver_parents()
        return self.manifest(mid, jid, [
            {'dataset': 'P', 'version': p_version},
            {'dataset': 'Q', 'version': q_version},
        ])

    def stats_dataset_and_proof(self):
        p_digest = residency.digest_of('raw-eu-customer-rows')
        self.dataset('P-stats', 'EU', 'deidentified_stats',
                     ['amount_sum', 'region'], 'stats-bytes-without-customer-fields')
        dropped = ['customer_id', 'email']
        proof = {
            'input_digest': p_digest,
            'dropped_fields': dropped,
            'method': 'aggregation',
            'proof_digest': residency.digest_of(
                {'input_digest': p_digest, 'dropped_fields': dropped,
                 'method': 'aggregation'}),
        }
        return proof

    # ----------------------------------------------------------------------
    # Acceptance path 1: EU execution, real de-identification, stats to NA
    # ----------------------------------------------------------------------

    def test_path1_eu_execution_deidentified_stats_egress(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')

        # the NA slot cannot take the raw EU customer data under v1
        denied = self.call('slot.lease', id='J', slot='N', at=self.clock(), expires=99000)
        self.assertEqual(denied['decision'], 'residency_jurisdiction_denied')
        self.assertEqual(denied['evidence']['allowed'], ['EU'])

        gen = self.lease_generation('J', 'E')
        self.assertEqual(self.decision('run.start', id='J', generation=gen,
                                       at=self.clock()), 'running')

        # raw inputs, staging and recovery points stay inside the EU
        self.assertEqual(self.decision('residency.copy', job='J', generation=gen,
                                       dataset='P', location='node-e', kind='staging',
                                       fields=self.P_FIELDS, at=self.clock()),
                         'copy_authorized')
        self.assertEqual(self.decision('residency.copy', job='J', generation=gen,
                                       dataset='P', location='node-e', kind='recovery',
                                       fields=self.P_FIELDS, at=self.clock()),
                         'copy_authorized')

        # real new bytes + verifiable transform proof; digest rename is rejected
        proof = self.stats_dataset_and_proof()
        self.assertEqual(self.decision(
            'residency.transform', job='J', generation=gen, input='P',
            output='P-stats', ttype='deidentify', proof=proof,
            output_content='stats-bytes-without-customer-fields',
            at=self.clock()), 'transform_verified')

        stats_digest = residency.digest_of('stats-bytes-without-customer-fields')
        self.assertEqual(self.decision(
            'residency.egress', job='J', generation=gen, recipient='na-analytics',
            recipient_jurisdiction='NA', classification='deid_stats',
            digest=stats_digest, at=self.clock()), 'egress_authorized')

        report = 'eu-derived-report'
        report_digest = 'sha256:' + hashlib.sha256(report.encode()).hexdigest()
        result = self.call('run.settle', id='J', generation=gen, at=self.clock(),
                           digest=report_digest, content=report)
        self.assertEqual(result['decision'], 'delivered')
        # receipt pins the exact approved manifest and license versions
        receipt = result['evidence']['residency']
        self.assertEqual(receipt['manifest'], 'm1')
        self.assertEqual(
            sorted((x['dataset'], x['version']) for x in receipt['licenses']),
            [('P', 'v1'), ('Q', 'v1')])

        audit = self.call('residency.audit', id='J')
        self.assertEqual(audit['decision'], 'compliant')
        self.assertEqual(audit['evidence']['violations'], [])
        self.assertEqual(len(audit['evidence']['egress']), 1)

    # ----------------------------------------------------------------------
    # Acceptance path 2: cross-border license, controlled NA environment,
    # full traceable report
    # ----------------------------------------------------------------------

    def test_path2_cross_border_license_controlled_full_report(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        self.assertEqual(self.call('slot.lease', id='J', slot='N', at=self.clock(),
                                   expires=99000)['decision'],
                         'residency_jurisdiction_denied')

        # cross-border license requires both customer and regulator signatures
        v2_scope = {
            'processing_locations': ['EU', 'NA'],
            'node_categories': ['eu_standard', 'na_controlled'],
            'allowed_fields': self.P_FIELDS,
            'controlled_environment': True,
            'controlled_environment_required': True,
            'deletion_within_days': 14,
            'required_signers': ['customer', 'regulator'],
            'egress': [
                {'classification': 'deid_stats',
                 'recipient_jurisdictions': ['EU', 'NA'],
                 'requires_verified_transform': True},
                {'classification': 'controlled_report',
                 'recipient_jurisdictions': ['NA'],
                 'requires_verified_transform': True},
            ],
        }
        self.assertEqual(self.license('P', 'v2', v2_scope, ['customer']),
                         'license_pending_signature')
        # a late/incomplete signature still authorizes nothing
        self.assertEqual(self.manifest('m2-pending', 'J', [
            {'dataset': 'P', 'version': 'v2'}, {'dataset': 'Q', 'version': 'v1'}]),
            'manifest_rejected')
        self.assertEqual(self.call('slot.lease', id='J', slot='N', at=self.clock(),
                                   expires=99000)['decision'],
                         'residency_jurisdiction_denied')

        self.assertEqual(self.decision(
            'residency.sign', dataset='P', version='v2', signer='regulator',
            signature=residency.signature_of('P', 'v2', v2_scope, 'regulator'),
            at=self.clock()), 'license_granted')
        self.assertEqual(self.manifest('m2', 'J', [
            {'dataset': 'P', 'version': 'v2'}, {'dataset': 'Q', 'version': 'v1'}]),
            'manifest_approved')
        # even with the cross-border license, an ordinary NA node is rejected;
        # the controlled execution environment is mandatory
        self.assertEqual(self.call('slot.lease', id='J', slot='N', at=self.clock(),
                                   expires=99000)['decision'],
                         'residency_node_category_denied')

        gen = self.lease_generation('J', 'NC')
        self.assertEqual(self.decision('run.start', id='J', generation=gen,
                                       at=self.clock()), 'running')
        # designated fields into the controlled environment are licensed
        self.assertEqual(self.decision('residency.copy', job='J', generation=gen,
                                       dataset='P', location='node-nc', kind='staging',
                                       fields=self.P_FIELDS, at=self.clock()),
                         'copy_authorized')

        p_digest = residency.digest_of('raw-eu-customer-rows')
        self.dataset('P-full', 'NA', 'controlled_report', self.P_FIELDS,
                     'full-report-new-bytes')
        proof = {
            'input_digest': p_digest,
            'dropped_fields': [],
            'method': 'controlled_extract',
            'proof_digest': residency.digest_of(
                {'input_digest': p_digest, 'dropped_fields': [],
                 'method': 'controlled_extract'}),
        }
        self.assertEqual(self.decision(
            'residency.transform', job='J', generation=gen, input='P',
            output='P-full', ttype='controlled_report', proof=proof,
            output_content='full-report-new-bytes', at=self.clock()),
            'transform_verified')
        full_digest = residency.digest_of('full-report-new-bytes')
        self.assertEqual(self.decision(
            'residency.egress', job='J', generation=gen, recipient='na-customer',
            recipient_jurisdiction='NA', classification='controlled_report',
            digest=full_digest, at=self.clock()), 'egress_authorized')
        # the controlled report may not be sent back into the EU under v2
        self.assertEqual(self.decision(
            'residency.egress', job='J', generation=gen, recipient='eu-archive',
            recipient_jurisdiction='EU', classification='controlled_report',
            digest=full_digest, at=self.clock()), 'egress_denied')

        result = self.call('run.settle', id='J', generation=gen, at=self.clock(),
                           digest='sha256:' + hashlib.sha256(b'final').hexdigest(),
                           content='final')
        self.assertEqual(result['decision'], 'delivered')
        versions = {(x['dataset'], x['version'])
                    for x in result['evidence']['residency']['licenses']}
        self.assertEqual(versions, {('P', 'v2'), ('Q', 'v1')})

    # ----------------------------------------------------------------------
    # 1. conflicting parent restrictions: intersection, not broadest union
    # ----------------------------------------------------------------------

    def test_conflicting_parent_restrictions_leave_no_legal_location(self):
        self.base_catalog()
        # a second parameter set licensed for NA processing only
        self.dataset('R', 'NA', 'regional_params', ['k'], 'na-only-params')
        r_scope = {
            'processing_locations': ['NA'],
            'node_categories': ['na_standard'],
            'allowed_fields': ['k'],
            'required_signers': ['customer'],
            'egress': [],
        }
        self.assertEqual(self.license('R', 'v1', r_scope, ['customer']),
                         'license_granted')
        self.parent_jobs()
        self.job('K', parents=['jp', 'jq'])
        result = self.call('residency.manifest', id='mk', job='K', at=self.clock(),
                           entries=[{'dataset': 'P', 'version': 'v1'},
                                    {'dataset': 'R', 'version': 'v1'}])
        self.assertEqual(result['decision'], 'manifest_conflicting_restrictions')
        self.assertEqual(result['evidence']['effective_permissions']
                         ['processing_locations'], [])
        # neither datacenter can run the job while the conflict stands
        for slot_id in ('E', 'N'):
            self.assertEqual(self.call('slot.lease', id='K', slot=slot_id,
                                       at=self.clock(), expires=99000)['decision'],
                             'residency_jurisdiction_denied')

    # ----------------------------------------------------------------------
    # 2. signature arriving late cannot authorize earlier processing
    # ----------------------------------------------------------------------

    def test_late_signature_does_not_launder_earlier_raw_processing(self):
        self.base_catalog()
        self.parent_jobs()
        v2_scope = {
            'processing_locations': ['EU', 'NA'],
            'node_categories': ['na_controlled'],
            'allowed_fields': self.P_FIELDS,
            'controlled_environment': True,
            'deletion_within_days': 14,
            'required_signers': ['customer', 'regulator'],
            'egress': [],
        }
        self.assertEqual(self.license('P', 'v2', v2_scope, []),
                         'license_pending_signature')
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        gen = self.lease_generation('J', 'E')
        self.assertEqual(self.decision('run.start', id='J', generation=gen,
                                       at=self.clock()), 'running')
        # running raw P in NA before the license exists is a violation
        bad = self.call('residency.copy', job='J', generation=gen, dataset='P',
                        location='node-n', kind='staging', fields=self.P_FIELDS,
                        at=self.clock())
        self.assertEqual(bad['decision'], 'residency_copy_denied')
        self.assertIn('violation', bad['evidence'])

        # the signature arrives afterwards and genuinely becomes effective...
        for signer in ('customer', 'regulator'):
            self.decision('residency.sign', dataset='P', version='v2', signer=signer,
                          signature=residency.signature_of('P', 'v2', v2_scope, signer),
                          at=self.clock())
        lic = store.row  # noqa
        with store.transaction(self.db) as db:
            self.assertEqual(
                store.row(db, 'SELECT status FROM rlicenses WHERE dataset=? AND version=?',
                          ('P', 'v2'))['status'], 'granted')
            # ...but the earlier illegal processing is still pinned
            self.assertEqual(
                store.rows(db, 'SELECT * FROM rviolations WHERE job=?', ('J',))[0]
                ['reason'], 'jurisdiction_denied')

    # ----------------------------------------------------------------------
    # 3. revocation mid-run stops future processing; delivered artifacts keep
    #    point-in-time evidence and inherit deletion duties
    # ----------------------------------------------------------------------

    def test_revocation_stops_future_processing_but_preserves_delivery_evidence(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        gen = self.lease_generation('J', 'E')
        self.assertEqual(self.decision('run.start', id='J', generation=gen,
                                       at=self.clock()), 'running')
        self.assertEqual(self.decision('residency.copy', job='J', generation=gen,
                                       dataset='P', location='node-e', kind='staging',
                                       fields=self.P_FIELDS, at=self.clock()),
                         'copy_authorized')

        revoke_at = self.clock(10)
        result = self.call('residency.revoke', dataset='P', version='v1',
                           at=revoke_at, reason='customer_withdrawal',
                           deletion_within_days=5)
        self.assertEqual(result['decision'], 'license_revoked')
        self.assertEqual(result['evidence']['active_leases_released'], ['J'])

        with store.transaction(self.db) as db:
            self.assertEqual(
                store.row(db, 'SELECT status FROM leases WHERE job=?', ('J',))
                ['status'], 'released')
        # further processing, restaging and redistribution are blocked
        self.assertEqual(self.decision('run.start', id='J', generation=gen,
                                       at=self.clock()), 'not_leased')
        self.assertEqual(self.call('slot.lease', id='J', slot='E', at=self.clock(),
                                   expires=99000)['decision'],
                         'residency_license_blocked')
        self.assertEqual(self.decision('residency.copy', job='J', generation=gen,
                                       dataset='P', location='node-e', kind='recovery',
                                       fields=self.P_FIELDS, at=self.clock()),
                         'residency_license_blocked')

    def test_revocation_after_delivery_keeps_receipt_and_opens_deletion_duty(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        gen = self.lease_generation('J', 'E')
        self.decision('run.start', id='J', generation=gen, at=self.clock())
        report = 'delivered-report'
        digest = 'sha256:' + hashlib.sha256(report.encode()).hexdigest()
        self.assertEqual(self.decision('run.settle', id='J', generation=gen,
                                       at=self.clock(), digest=digest,
                                       content=report), 'delivered')

        result = self.call('residency.revoke', dataset='P', version='v1',
                           at=self.clock(), reason='regulator_order',
                           deletion_within_days=9)
        # the lawful delivery remains lawful; its license evidence survives
        self.assertEqual(result['evidence']['delivered_evidence_preserved'], ['J'])
        self.assertEqual(len(result['evidence']['deletion_duties']), 1)
        with store.transaction(self.db) as db:
            self.assertEqual(
                store.row(db, 'SELECT status FROM artifacts WHERE job=?', ('J',))
                ['status'], 'delivered')
            receipt = store.row(db, 'SELECT * FROM regress WHERE job=?', ('J',))
            self.assertIn('v1', receipt['licenses'])
            duty = store.row(db, 'SELECT * FROM rdeletion_duties WHERE job=?', ('J',))
            self.assertEqual(duty['status'], 'open')
        # redistribution after revocation is refused
        self.assertEqual(self.call('residency.egress', job='J', generation=gen,
                                   recipient='na-late', recipient_jurisdiction='NA',
                                   classification='deid_stats',
                                   digest='sha256:abc', at=self.clock())['decision'],
                         'egress_unverified_bytes')

    # ----------------------------------------------------------------------
    # 4. cross-datacenter recovery points are processing locations too
    # ----------------------------------------------------------------------

    def test_cross_datacenter_recovery_point_is_denied_and_recorded(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        gen = self.lease_generation('J', 'E')
        self.decision('run.start', id='J', generation=gen, at=self.clock())
        result = self.call('residency.copy', job='J', generation=gen, dataset='P',
                           location='node-n', kind='recovery', fields=self.P_FIELDS,
                           at=self.clock())
        self.assertEqual(result['decision'], 'residency_copy_denied')
        self.assertEqual(result['evidence']['reason'], 'jurisdiction_denied')
        with store.transaction(self.db) as db:
            row = store.row(db, 'SELECT * FROM rviolations WHERE job=?', ('J',))
            self.assertEqual(row['kind'], 'recovery')
            self.assertEqual(row['jurisdiction'], 'NA')
            copy = store.row(db, 'SELECT * FROM rcopies WHERE id=?',
                             (result['evidence']['copy'],))
            self.assertEqual(copy['authorized'], 0)

    # ----------------------------------------------------------------------
    # 5. deleted illegal temporary copy still leaves immutable evidence
    # ----------------------------------------------------------------------

    def test_deleted_illegal_copy_keeps_immutable_violation_and_blocks_delivery(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        gen = self.lease_generation('J', 'E')
        self.decision('run.start', id='J', generation=gen, at=self.clock())
        bad = self.call('residency.copy', job='J', generation=gen, dataset='P',
                        location='node-n', kind='staging', fields=self.P_FIELDS,
                        at=self.clock())
        copy_id = bad['evidence']['copy']
        self.assertEqual(self.decision('residency.copy_delete', copy=copy_id,
                                       at=self.clock()), 'copy_deleted')
        with store.transaction(self.db) as db:
            self.assertEqual(
                store.row(db, 'SELECT status FROM rcopies WHERE id=?', (copy_id,))
                ['status'], 'deleted')
            violations = store.rows(db, 'SELECT * FROM rviolations WHERE job=?',
                                    ('J',))
            self.assertEqual(len(violations), 1)
            before = violations[0]['id']

        # even a later cross-border license cannot erase the old violation
        v2_scope = {
            'processing_locations': ['EU', 'NA'],
            'node_categories': ['na_controlled'],
            'allowed_fields': self.P_FIELDS,
            'controlled_environment': True,
            'deletion_within_days': 14,
            'required_signers': ['customer'],
            'egress': [],
        }
        self.assertEqual(self.license('P', 'v2', v2_scope, ['customer']),
                         'license_granted')
        self.assertEqual(self.manifest('m2', 'J', [
            {'dataset': 'P', 'version': 'v2'}, {'dataset': 'Q', 'version': 'v1'}]),
            'manifest_approved')
        report = 'report'
        settle = self.call('run.settle', id='J', generation=gen, at=self.clock(),
                           digest='sha256:' + hashlib.sha256(report.encode()).hexdigest(),
                           content=report)
        self.assertEqual(settle['decision'], 'residency_violation_present')
        with store.transaction(self.db) as db:
            self.assertEqual(
                store.row(db, 'SELECT id FROM rviolations WHERE job=?', ('J',))['id'],
                before)
            self.assertIsNone(
                store.row(db, 'SELECT * FROM artifacts WHERE job=?', ('J',)))
        self.assertEqual(self.call('residency.audit', id='J')['decision'],
                         'non_compliant')

    # ----------------------------------------------------------------------
    # 6. old-generation receipts cannot deliver against a swapped manifest
    # ----------------------------------------------------------------------

    def test_stale_generation_receipt_rejected_after_manifest_swap(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        gen = self.lease_generation('J', 'E')
        self.decision('run.start', id='J', generation=gen, at=self.clock())
        v2_scope = {
            'processing_locations': ['EU', 'NA'],
            'node_categories': ['eu_standard', 'na_controlled'],
            'allowed_fields': self.P_FIELDS,
            'controlled_environment': True,
            'deletion_within_days': 14,
            'required_signers': ['customer'],
            'egress': [],
        }
        self.assertEqual(self.license('P', 'v2', v2_scope, ['customer']),
                         'license_granted')
        self.assertEqual(self.manifest('m2', 'J', [
            {'dataset': 'P', 'version': 'v2'}, {'dataset': 'Q', 'version': 'v1'}]),
            'manifest_approved')
        report = 'report'
        # the generation-1 attempt is pinned to m1 forever; it cannot settle on m2
        result = self.call('run.settle', id='J', generation=gen, at=self.clock(),
                           digest='sha256:' + hashlib.sha256(report.encode()).hexdigest(),
                           content=report)
        self.assertEqual(result['decision'], 'residency_stale_generation_receipt')
        with store.transaction(self.db) as db:
            self.assertIsNone(
                store.row(db, 'SELECT * FROM artifacts WHERE job=?', ('J',)))

    # ----------------------------------------------------------------------
    # 7. public parameters can be reused where their own license allows
    # ----------------------------------------------------------------------

    def test_public_parameters_reused_under_their_own_license(self):
        self.base_catalog()
        # a NA-only job built purely from the public NA parameters
        self.job('na-job')
        self.assertEqual(self.manifest('mq', 'na-job', [
            {'dataset': 'Q', 'version': 'v1'}]), 'manifest_approved')
        gen = self.lease_generation('na-job', 'N')
        self.assertEqual(self.decision('run.start', id='na-job', generation=gen,
                                       at=self.clock()), 'running')
        self.assertEqual(self.decision('residency.copy', job='na-job',
                                       generation=gen, dataset='Q',
                                       location='node-n', kind='staging',
                                       fields=self.Q_FIELDS, at=self.clock()),
                         'copy_authorized')
        # the same dataset still cannot carry P's raw fields into another job
        self.parent_jobs()
        self.job('J', parents=['jp', 'jq'])
        self.assertEqual(self.manifest('m1', 'J', [
            {'dataset': 'P', 'version': 'v1'}, {'dataset': 'Q', 'version': 'v1'}]),
            'manifest_approved')
        self.assertEqual(self.call('slot.lease', id='J', slot='N', at=self.clock(),
                                   expires=99000)['decision'],
                         'residency_jurisdiction_denied')

    # ----------------------------------------------------------------------
    # de-identification mechanics: new bytes and verifiable proof
    # ----------------------------------------------------------------------

    def test_deidentification_rejects_digest_rename_and_bad_proof(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        gen = self.lease_generation('J', 'E')
        self.decision('run.start', id='J', generation=gen, at=self.clock())
        p_digest = residency.digest_of('raw-eu-customer-rows')

        # "new" dataset whose bytes are identical to the raw input
        self.dataset('fake-stats', 'EU', 'deidentified_stats',
                     ['region'], 'raw-eu-customer-rows')
        proof = {
            'input_digest': p_digest, 'dropped_fields': ['customer_id', 'email'],
            'method': 'aggregation',
            'proof_digest': residency.digest_of(
                {'input_digest': p_digest,
                 'dropped_fields': ['customer_id', 'email'],
                 'method': 'aggregation'})}
        result = self.call('residency.transform', job='J', generation=gen,
                           input='P', output='fake-stats', ttype='deidentify',
                           proof=proof, output_content='raw-eu-customer-rows',
                           at=self.clock())
        self.assertEqual(result['decision'], 'transform_unverified')
        self.assertIn('output_identical_to_input', result['evidence']['problems'])
        # and it must not be egressable as de-identified statistics
        self.assertEqual(self.call('residency.egress', job='J', generation=gen,
                                   recipient='na-x', recipient_jurisdiction='NA',
                                   classification='deid_stats',
                                   digest=p_digest, at=self.clock())['decision'],
                         'egress_unverified_bytes')

        # a real transform with a tampered proof digest also fails
        self.dataset('real-stats', 'EU', 'deidentified_stats', ['region'],
                     'genuine-stats')
        bad_proof = dict(proof, proof_digest='sha256:deadbeef')
        result = self.call('residency.transform', job='J', generation=gen,
                           input='P', output='real-stats', ttype='deidentify',
                           proof=bad_proof, output_content='genuine-stats',
                           at=self.clock())
        self.assertEqual(result['decision'], 'transform_unverified')
        self.assertIn('proof_digest_invalid', result['evidence']['problems'])

    # ----------------------------------------------------------------------
    # control-plane power loss: restart must re-derive the same basis
    # ----------------------------------------------------------------------

    def test_restart_after_power_loss_reuses_same_manifest_and_blocks_swap(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        gen = self.lease_generation('J', 'E')
        self.decision('run.start', id='J', generation=gen, at=self.clock())

        # control plane comes back: replaying start is idempotent and still
        # verifies against the same manifest/license versions
        self.assertEqual(self.decision('run.start', id='J', generation=gen,
                                       at=self.clock()), 'idempotent')
        v2_scope = {
            'processing_locations': ['EU', 'NA'],
            'node_categories': ['eu_standard', 'na_controlled'],
            'allowed_fields': self.P_FIELDS,
            'controlled_environment': True,
            'deletion_within_days': 14,
            'required_signers': ['customer'],
            'egress': [],
        }
        self.assertEqual(self.license('P', 'v2', v2_scope, ['customer']),
                         'license_granted')
        self.assertEqual(self.manifest('m2', 'J', [
            {'dataset': 'P', 'version': 'v2'}, {'dataset': 'Q', 'version': 'v1'}]),
            'manifest_approved')
        # the old generation cannot be replayed against the new basis
        self.assertEqual(self.decision('run.start', id='J', generation=gen,
                                       at=self.clock()),
                         'residency_manifest_generation_mismatch')
        with store.transaction(self.db) as db:
            pinned = store.row(db, 'SELECT * FROM rattempts WHERE job=? AND generation=?',
                               ('J', gen))
            self.assertEqual(pinned['manifest'], 'm1')

    # ----------------------------------------------------------------------
    # field-level license limits and audit wiring
    # ----------------------------------------------------------------------

    def test_fields_outside_license_create_violation(self):
        self.base_catalog()
        self.parent_jobs()
        self.assertEqual(self.job_j_with_manifest('m1'), 'manifest_approved')
        gen = self.lease_generation('J', 'E')
        self.decision('run.start', id='J', generation=gen, at=self.clock())
        # narrow license for P that only permits the amount field
        narrow = {
            'processing_locations': ['EU'],
            'node_categories': ['eu_standard'],
            'allowed_fields': ['amount'],
            'controlled_environment': False,
            'deletion_within_days': 7,
            'required_signers': ['customer'],
            'egress': [],
        }
        self.assertEqual(self.license('P', 'v3', narrow, ['customer']),
                         'license_granted')
        self.assertEqual(self.manifest('m3', 'J', [
            {'dataset': 'P', 'version': 'v3'}, {'dataset': 'Q', 'version': 'v1'}]),
            'manifest_approved')
        result = self.call('residency.copy', job='J', generation=gen, dataset='P',
                           location='node-e', kind='staging',
                           fields=['amount', 'email'], at=self.clock())
        self.assertEqual(result['decision'], 'residency_copy_denied')
        self.assertEqual(result['evidence']['reason'], 'fields_outside_license')

        # the standard job audit carries the residency evidence alongside
        audit = self.call('job.audit', id='J')
        self.assertIn('residency', audit['evidence'])
        self.assertFalse(audit['evidence']['residency']['compliant'])
        self.assertEqual(len(audit['evidence']['residency']['violations']), 1)

    def test_slot_registration_can_bind_jurisdiction_directly(self):
        self.dataset('Q', 'NA', 'public_params', self.Q_FIELDS, 'na-public-parameters')
        q_scope = {
            'processing_locations': ['NA'],
            'node_categories': ['na_standard'],
            'allowed_fields': self.Q_FIELDS,
            'required_signers': ['customer'],
            'egress': [],
        }
        self.license('Q', 'v1', q_scope, ['customer'])
        self.assertEqual(self.decision(
            'slot.register', id='S', node='direct-node', cpu=4, at=self.clock(),
            jurisdiction='NA', categories=['na_standard']), 'ready')
        self.job('z')
        self.assertEqual(self.manifest('mz', 'z', [
            {'dataset': 'Q', 'version': 'v1'}]), 'manifest_approved')
        self.assertEqual(self.lease_generation('z', 'S'), 1)


if __name__ == '__main__':
    unittest.main()
