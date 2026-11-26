"""Remote license vendor gateway.

The vendor service is the source of truth for software-seat tokens. Local
SQLite rows only mirror vendor receipts; deleting a local row never frees a
seat at the vendor. The gateway enforces request idempotency (double-start and
power-loss replay must not double-charge a seat) and can be made unreachable
to exercise the short-outline reconciliation paths.
"""
import threading

_GATEWAYS = {}
_REGISTRY_LOCK = threading.Lock()


class ServiceUnavailable(Exception):
    """Raised when the license service cannot be reached in time."""


class LicenseGateway:
    """Gateway contract used by :mod:`cloudctl.licenses`."""

    def borrow(self, entitlement_id, job, generation, request_id, at):
        raise NotImplementedError

    def release(self, token, request_id, at):
        raise NotImplementedError

    def verify(self, entitlement_id, token):
        raise NotImplementedError

    def pending_receipts(self, entitlement_id):
        raise NotImplementedError


class FakeLicenseService(LicenseGateway):
    """In-process vendor stand-in holding the authoritative seat ledger."""

    def __init__(self, offline=False):
        self.lock = threading.RLock()
        self.offline = offline
        # entitlement_id -> seat capacity (mirrors registered contracts)
        self.capacity = {}
        # token -> dict(entitlement, job, generation, request_id, status)
        self.tokens = {}
        # request_id -> receipt dict (idempotency for borrow and release)
        self.requests = {}
        # receipts the vendor sent whose delivery got lost; verified later
        self.pending = {}
        self._counter = 0
        # queued faults: list of callables(service, method, kwargs)->bool
        self.faults = []

    def _next_id(self, prefix):
        self._counter += 1
        return f'{prefix}-{self._counter}'

    def set_offline(self, offline):
        with self.lock:
            self.offline = offline

    def register_capacity(self, entitlement_id, seats):
        with self.lock:
            self.capacity[entitlement_id] = seats

    def _maybe_fault(self, method, kwargs):
        while self.faults:
            predicate = self.faults.pop(0)
            if predicate(self, method, kwargs):
                raise ServiceUnavailable(f'injected fault on {method}')

    def active_count(self, entitlement_id):
        return sum(1 for t in self.tokens.values()
                   if t['entitlement'] == entitlement_id and t['status'] == 'held')

    def borrow(self, entitlement_id, job, generation, request_id, at):
        with self.lock:
            self._maybe_fault('borrow', dict(entitlement=entitlement_id,
                                             request_id=request_id))
            if self.offline:
                raise ServiceUnavailable('license service offline')
            duplicate = self.requests.get(request_id)
            if duplicate is not None:
                # Replay of the same borrow request returns the original
                # receipt; the seat is never charged twice.
                return dict(duplicate)
            if entitlement_id not in self.capacity:
                return {'receipt_id': self._next_id('rcp'),
                        'op': 'borrow', 'status': 'rejected',
                        'entitlement': entitlement_id, 'token': None,
                        'request_id': request_id, 'reason': 'unknown_entitlement'}
            if self.active_count(entitlement_id) >= self.capacity[entitlement_id]:
                return {'receipt_id': self._next_id('rcp'),
                        'op': 'borrow', 'status': 'rejected',
                        'entitlement': entitlement_id, 'token': None,
                        'request_id': request_id, 'reason': 'no_capacity'}
            token = f'tok-{entitlement_id}-{self._next_id("t")}'
            receipt = {'receipt_id': self._next_id('rcp'), 'op': 'borrow',
                       'status': 'granted', 'entitlement': entitlement_id,
                       'token': token, 'job': job, 'generation': generation,
                       'request_id': request_id, 'at': at}
            self.tokens[token] = {'entitlement': entitlement_id, 'job': job,
                                  'generation': generation,
                                  'request_id': request_id, 'status': 'held',
                                  'granted_at': at}
            self.requests[request_id] = receipt
            return dict(receipt)

    def release(self, token, request_id, at):
        with self.lock:
            self._maybe_fault('release', dict(token=token, request_id=request_id))
            if self.offline:
                raise ServiceUnavailable('license service offline')
            duplicate = self.requests.get(request_id)
            if duplicate is not None:
                return dict(duplicate)
            record = self.tokens.get(token)
            if record is None:
                receipt = {'receipt_id': self._next_id('rcp'), 'op': 'release',
                           'status': 'unknown_token', 'token': token,
                           'request_id': request_id, 'at': at}
            else:
                prior = record['status']
                record['status'] = 'released'
                receipt = {'receipt_id': self._next_id('rcp'), 'op': 'release',
                           'status': 'released' if prior == 'held' else 'already_released',
                           'token': token, 'request_id': request_id, 'at': at}
            self.requests[request_id] = receipt
            return dict(receipt)

    def verify(self, entitlement_id, token):
        with self.lock:
            self._maybe_fault('verify', dict(entitlement=entitlement_id, token=token))
            if self.offline:
                raise ServiceUnavailable('license service offline')
            record = self.tokens.get(token)
            if record is None:
                return {'entitlement': entitlement_id, 'token': token,
                        'status': 'unknown'}
            return {'entitlement': entitlement_id, 'token': token,
                    'status': record['status'], 'job': record['job'],
                    'generation': record['generation']}

    def pending_receipts(self, entitlement_id):
        with self.lock:
            self._maybe_fault('pending', dict(entitlement=entitlement_id))
            if self.offline:
                raise ServiceUnavailable('license service offline')
            return [dict(r) for r in self.pending.get(entitlement_id, [])]

    # --- test/vendor-side helpers -------------------------------------------------
    def vendor_reclaim(self, token, at, lose_confirmation=True):
        """Vendor reclaims a seat (e.g. expiry sweep). The confirmation may be
        lost in transit, leaving an open return-pending record locally."""
        with self.lock:
            record = self.tokens.get(token)
            if record is None:
                return None
            record['status'] = 'released'
            receipt = {'receipt_id': self._next_id('rcp'), 'op': 'release',
                       'status': 'vendor_reclaimed', 'token': token,
                       'request_id': record['request_id'], 'at': at}
            if lose_confirmation:
                self.pending.setdefault(record['entitlement'], []).append(dict(receipt))
            return dict(receipt)

    def vendor_correct_double_charge(self, entitlement_id, token, at):
        """Vendor admits a duplicate hold and refunds it."""
        with self.lock:
            record = self.tokens.get(token)
            if record is not None:
                record['status'] = 'released'
            return {'receipt_id': self._next_id('rcp'), 'op': 'correction',
                    'status': 'double_charge_refund', 'entitlement': entitlement_id,
                    'token': token, 'at': at}

    def queue_fault(self, predicate):
        self.faults.append(predicate)

    def snapshot(self):
        with self.lock:
            return {'capacity': dict(self.capacity),
                    'tokens': {k: dict(v) for k, v in self.tokens.items()},
                    'pending': {k: [dict(x) for x in v]
                                for k, v in self.pending.items()}}


def register_gateway(db_path, gateway):
    with _REGISTRY_LOCK:
        _GATEWAYS[db_path] = gateway
    return gateway


def get_gateway(db):
    """Return the gateway bound to this database, defaulting to a fake vendor."""
    from .store import db_path
    key = db_path(db)
    with _REGISTRY_LOCK:
        gateway = _GATEWAYS.get(key)
        if gateway is None:
            gateway = FakeLicenseService()
            _GATEWAYS[key] = gateway
        return gateway


def reset_gateways():
    with _REGISTRY_LOCK:
        _GATEWAYS.clear()
