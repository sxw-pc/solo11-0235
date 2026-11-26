import argparse
import json
import sys
from . import jobs,slots,runs,store,audit,secret_rotation as sr

OPERATIONS={
    'job.submit':jobs.submit,
    'job.revise':lambda db,r:jobs.revise(db,r['id'],r['deadline'],r['expected']),
    'job.cancel':lambda db,r:jobs.cancel(db,r['id'],r['at'],r['reason']),
    'slot.register':slots.register,
    'slot.lease':lambda db,r:slots.lease(db,r['id'],r['slot'],r['at'],r['expires'],r.get('preempt',False)),
    'slot.heartbeat':lambda db,r:slots.heartbeat(db,r['slot'],r['at'],r['healthy']),
    'slot.repay':lambda db,r:slots.repay(db,r['id'],r['slot'],r['at']),
    'run.start':lambda db,r:runs.start(db,r['id'],r['generation'],r['at']),
    'run.settle':lambda db,r:runs.settle(db,r['id'],r['generation'],r['at'],r.get('digest'),r.get('success',True),r.get('content')),
    'run.finalize':lambda db,r:runs.finalize(db,r['id'],r['generation'],r['at']),
    'run.retention':lambda db,r:runs.retention(db,r['id'],r['expires'],r['hold'],r.get('reason','')),
    'run.purge':lambda db,r:runs.purge(db,r['at']),
    'job.audit':lambda db,r:audit.inspect(db,r['id']),
    'secret.register':lambda db,r:sr.register_envelope(db,r['id'],r['key_id'],r['version'],r['sealed_digest'],r.get('at',0)),
    'secret.mount':lambda db,r:sr.issue_mount(db,r['id'],r['generation'],r['at'],r.get('ttl',300)),
    'secret.rotate':lambda db,r:sr.rotate(db,r['id'],r['key_id'],r['new_version'],r['sealed_digest'],r['at'],r.get('reason','key_service_rotation')),
    'secret.revoke':lambda db,r:sr.revoke(db,r['key_id'],r['version'],r['reason'],r['at'],r.get('tenant')),
    'secret.kms_receipt':lambda db,r:sr.kms_receipt(db,r['receipt_id'],r['key_id'],r['version'],r.get('receipt_action',r.get('action')),r['at'],r.get('mount_id'),r.get('slot'),r.get('claim','')),
    'secret.attest':lambda db,r:sr.attest(db,r['attestation_id'],r['slot'],r['id'],r['key_id'],r['version'],r['verdict'],r['at'],r.get('claim','')),
    'secret.recovery_resume':lambda db,r:sr.resume_at_recovery_point(db,r['id'],r['at'],r['point_digest'],r.get('ttl',300)),
    'secret.quarantine':lambda db,r:sr.isolate_and_replan(db,r['id'],r['at'],r.get('reason','reclaim_unverified')),
    'secret.standby_mount':lambda db,r:sr.standby_mount(db,r['id'],r['generation'],r['at'],r.get('ttl',300)),
    'secret.confirm_recompute':lambda db,r:sr.confirm_clean_recomputation(db,r['id'],r['generation'],r['at'],r['digest']),
    'secret.evidence':lambda db,r:{'subject':r['id'],'decision':'evidence_chain','evidence':sr.evidence_chain(db,r['id'])},
}

def execute(db_path,request):
    with store.transaction(db_path) as db:
        action=request['action']
        if action not in OPERATIONS:raise ValueError(f'unknown action: {action}')
        result=OPERATIONS[action](db,request)
        result['events']=store.report(db,result.get('subject',request.get('id','')))
        return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['init','demo','job','slot','run'])
    parser.add_argument('operation',nargs='?')
    parser.add_argument('--db',default='state.sqlite')
    parser.add_argument('--fixture')
    args=parser.parse_args()
    store.init(args.db)
    if args.command=='init':print(json.dumps({'status':'initialized','db':args.db}));return
    if args.command=='demo':
        requests=json.load(open(args.fixture or 'fixtures/demo.json',encoding='utf-8'))
    else:
        if not args.fixture:parser.error('--fixture required')
        requests=json.load(open(args.fixture,encoding='utf-8'))
    if isinstance(requests,dict):requests=[requests]
    for request in requests:
        if args.command not in ('demo',request['action'].split('.')[0]):
            raise ValueError('fixture action and command differ')
        for prerequisite in request.get('setup',[]):
            execute(args.db,prerequisite)
        print(json.dumps(execute(args.db,request),ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except (ValueError,KeyError) as exc:
        print(json.dumps({'error':str(exc)}),file=sys.stderr);sys.exit(2)
