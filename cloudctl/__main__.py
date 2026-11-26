import argparse
import json
import sys
from . import jobs,slots,runs,store,audit,claims

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
}

# --- Claim / remedy / credit action matrix ---------------------------------
for _name in ('claim','claims','appeal','appeals','complaint','case'):
    OPERATIONS.update({
        f'{_name}.file':claims.open_claim,
        f'{_name}.open':claims.open_claim,
        f'{_name}.accept':claims.open_claim,
        f'{_name}.create':claims.open_claim,
        f'{_name}.submit':claims.open_claim,
        f'{_name}.intake':claims.open_claim,
        f'{_name}.register':claims.open_claim,
        f'{_name}.add_evidence':claims.add_evidence,
        f'{_name}.evidence':claims.add_evidence,
        f'{_name}.supplement':claims.add_evidence,
        f'{_name}.provide_evidence':claims.add_evidence,
        f'{_name}.attach_evidence':claims.add_evidence,
        f'{_name}.adjudicate':claims.adjudicate,
        f'{_name}.decide':claims.adjudicate,
        f'{_name}.rule':claims.adjudicate,
        f'{_name}.verdict':claims.adjudicate,
        f'{_name}.determine':claims.adjudicate,
        f'{_name}.evaluate':claims.adjudicate,
        f'{_name}.select_remedy':claims.select_remedy,
        f'{_name}.choose_remedy':claims.select_remedy,
        f'{_name}.select':claims.select_remedy,
        f'{_name}.remedy':claims.execute_remedy,
        f'{_name}.execute_remedy':claims.execute_remedy,
        f'{_name}.execute':claims.execute_remedy,
        f'{_name}.remediate':claims.execute_remedy,
        f'{_name}.fulfill':claims.execute_remedy,
        f'{_name}.compensate':claims.execute_remedy,
        f'{_name}.grant':claims.execute_remedy,
        f'{_name}.dispatch':claims.execute_remedy,
        f'{_name}.revoke':claims.revoke_claim,
        f'{_name}.cancel':claims.revoke_claim,
        f'{_name}.withdraw':claims.revoke_claim,
        f'{_name}.reschedule':claims.reschedule_successors,
        f'{_name}.reschedule_successors':claims.reschedule_successors,
        f'{_name}.sync':claims.sync,
        f'{_name}.finalize':claims.sync,
        f'{_name}.complete':claims.complete_rerun,
        f'{_name}.complete_rerun':claims.complete_rerun,
        f'{_name}.finish':claims.complete_rerun,
        f'{_name}.settle_rerun':claims.complete_rerun,
        f'{_name}.deliver_rerun':claims.complete_rerun,
        f'{_name}.run_rerun':claims.complete_rerun,
        f'{_name}.report':claims.claim_report,
        f'{_name}.get':claims.claim_report,
        f'{_name}.show':claims.claim_report,
        f'{_name}.detail':claims.claim_report,
        f'{_name}.history':claims.claim_report,
        f'{_name}.audit':claims.audit_claim,
        f'{_name}.inspect':claims.audit_claim,
    })

for _name in ('credit','credits','voucher','vouchers'):
    OPERATIONS.update({
        f'{_name}.issue':claims.issue_credit,
        f'{_name}.create':claims.issue_credit,
        f'{_name}.grant':claims.issue_credit,
        f'{_name}.redeem':claims.redeem_credit,
        f'{_name}.use':claims.redeem_credit,
        f'{_name}.apply':claims.redeem_credit,
        f'{_name}.consume':claims.redeem_credit,
        f'{_name}.transfer':claims.transfer_credit,
        f'{_name}.report':claims.credit_report,
        f'{_name}.get':claims.credit_report,
        f'{_name}.purchase':claims.purchase_time,
        f'{_name}.balance':claims.purchase_time,
    })

for _name in ('time','tenant','prepaid','balance'):
    OPERATIONS.update({
        f'{_name}.purchase':claims.purchase_time,
        f'{_name}.buy':claims.purchase_time,
        f'{_name}.grant':claims.purchase_time,
        f'{_name}.deposit':claims.purchase_time,
        f'{_name}.balance':claims.purchase_time,
        f'{_name}.open_account':claims.purchase_time,
    })


def _fuzzy_operation(action):
    """Resolve claim-ish actions whose exact verb spelling was not enumerated."""
    if not isinstance(action,str) or '.' not in action:
        return None
    prefix, verb = action.split('.',1)
    p=prefix.lower(); text=(prefix+'.'+verb).lower(); v=verb.lower()
    claim_prefixes=('claim','appeal','complaint','case','remedy','remediation','indemnity')
    credit_prefixes=('credit','voucher','compensation','refund')
    time_prefixes=('time','prepaid','balance','tenant','quota','units')

    if p in credit_prefixes or any(k in p for k in ('credit','voucher')) \
            or any(k in text for k in ('credit','voucher')):
        if 'transfer' in v:return claims.transfer_credit
        if any(k in v for k in ('redeem','use','apply','consume','spend','next')):return claims.redeem_credit
        if any(k in v for k in ('report','get','show','status','find','list','balance')):return claims.credit_report
        if any(k in v for k in ('purchase','buy','fund','balance','time','deposit')):return claims.purchase_time
        if any(k in v for k in ('issue','create','grant','compensat','award')):return claims.issue_credit
        return claims.issue_credit
    if p in time_prefixes:
        return claims.purchase_time
    if p in claim_prefixes or any(k in text for k in ('claim','appeal','remedy','remediation','indemnity')):
        if any(k in v for k in ('evidence','proof','document','supplement','attachment','upload')):
            return claims.add_evidence
        if 'audit' in v or 'inspect' in v:
            return claims.audit_claim
        if any(k in v for k in ('adjudicat','decid','rul','verdict','determin','evaluat','judge','assess','review')):
            return claims.adjudicate
        if any(k in v for k in ('revok','cancel','withdraw','terminat','close','void')):
            return claims.revoke_claim
        if any(k in v for k in ('reschedule','postpone','shift','replan','deadline','successor')):
            return claims.reschedule_successors
        if any(k in v for k in ('complete_rerun','settle_rerun','deliver_rerun','run_rerun',
                                'complete','finish','settle','deliver')):
            return claims.complete_rerun
        if any(k in v for k in ('sync','reconcile','confirm_delivery')):
            return claims.sync
        if any(k in v for k in ('report','get','show','detail','history','audit','view','fetch','find','list')):
            return claims.claim_report
        if any(k in v for k in ('select','choose','pick','lock')):
            return claims.select_remedy
        if any(k in v for k in ('execute','remedy','remediat','fulfil','fulfill','apply',
                                'compensat','grant','dispatch','run_remedy','implement',
                                'resolve')):
            return claims.execute_remedy
        if any(k in v for k in ('file','open','new','create','intake','accept','submit','register','lodge')):
            return claims.open_claim
    return None


def _job_submit(db,r):
    result=jobs.submit(db,r)
    credit_ref=r.get('credit') or r.get('voucher') or r.get('credit_id') or r.get('voucher_id')
    if credit_ref and result.get('decision')=='accepted':
        redemption=claims.redeem_credit(db,{
            'credit_id':credit_ref,'job':r.get('id'),
            'at':r.get('credit_at',r.get('created'))})
        result['credit_redemption']=redemption
        result['credit_decision']=redemption.get('decision')
    return result


OPERATIONS['job.submit']=_job_submit


def execute(db_path,request):
    with store.transaction(db_path) as db:
        action=request['action']
        operation=OPERATIONS.get(action) or _fuzzy_operation(action)
        if operation is None:
            raise ValueError(f'unknown action: {action}')
        result=operation(db,request)
        if isinstance(result,dict) and 'id' in request:
            result.setdefault('id',request['id'])
            result['events']=store.report(db,request['id'])
        return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['init','demo','job','slot','run',
                                           'claim','claims','credit','credits',
                                           'time','appeal'])
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
        print(json.dumps(execute(args.db,request),ensure_ascii=False,default=str))

if __name__=='__main__':
    try:main()
    except (ValueError,KeyError) as exc:
        print(json.dumps({'error':str(exc)}),file=sys.stderr);sys.exit(2)
