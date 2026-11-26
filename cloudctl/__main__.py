import argparse
import json
import sys
from . import jobs,slots,runs,store,audit

OPERATIONS={
    'job.submit':jobs.submit,
    'job.revise':lambda db,r:jobs.revise(db,r['id'],r['deadline'],r['expected'],r.get('at')),
    'job.cancel':lambda db,r:jobs.cancel(db,r['id'],r['at'],r['reason']),
    'node.register':slots.register_node,
    'slot.register':slots.register,
    'slot.lease':lambda db,r:slots.lease(db,r['id'],r['slot'],r['at'],r['expires'],r.get('preempt',False)),
    'slot.heartbeat':lambda db,r:slots.heartbeat(db,r['slot'],r['at'],r['healthy']),
    'slot.repay':lambda db,r:slots.repay(db,r['id'],r['slot'],r['at']),
    'slot.release':lambda db,r:slots.release(db,r['id'],r['at'],r.get('reason','operator')),
    'run.start':lambda db,r:runs.start(db,r['id'],r['generation'],r['at']),
    'run.settle':lambda db,r:runs.settle(db,r['id'],r['generation'],r['at'],r.get('digest'),r.get('success',True),r.get('content')),
    'run.finalize':lambda db,r:runs.finalize(db,r['id'],r['generation'],r['at']),
    'run.retention':lambda db,r:runs.retention(db,r['id'],r['expires'],r['hold'],r.get('reason','')),
    'run.purge':lambda db,r:runs.purge(db,r['at']),
    'job.audit':lambda db,r:audit.inspect(db,r['id']),
}

def execute(db_path,request):
    with store.transaction(db_path) as db:
        action=request['action']
        if action not in OPERATIONS:raise ValueError(f'unknown action: {action}')
        result=OPERATIONS[action](db,request)
        subject=request.get('id',request.get('node'))
        if subject:result['events']=store.report(db,subject)
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
