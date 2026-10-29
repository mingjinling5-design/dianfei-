"""Explicit request/worker identity, shared without importing Django in local tools."""
from contextvars import ContextVar
from contextlib import contextmanager
import threading
import weakref

CURRENT = ContextVar('electricity_tenant', default=None)


class ScopedLock:
    def __init__(self):
        self.guard=threading.Lock();self.locks=weakref.WeakValueDictionary()
        self.default=threading.RLock();self.local=threading.local()
    def __enter__(self):
        tenant=CURRENT.get()
        if tenant:
            key=str(tenant[0].pk)
            with self.guard:
                lock=self.locks.get(key)
                if lock is None:lock=threading.RLock();self.locks[key]=lock
        else:lock=self.default
        lock.acquire()
        if not hasattr(self.local,'stack'):self.local.stack=[]
        self.local.stack.append(lock)
        return self
    def __exit__(self,*args):self.local.stack.pop().release()


@contextmanager
def tenant_scope(profile, config):
    token = CURRENT.set((profile, config))
    try: yield
    finally: CURRENT.reset(token)
