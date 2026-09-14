from __future__ import annotations
import asyncio
import logging
import os
import time
from .models import train_cex,train_scanner

LOG = logging.getLogger(__name__)


class ProcessLock:
    """OS lock automatically released on crash. Prevent duplicate venue engines."""
    def __init__(self,root,name):
        self.path,self.handle = root/(name+".lock"),None

    def __enter__(self):
        self.handle = self.path.open("a+b")
        try:
            if os.name=="nt":
                import msvcrt
                self.handle.seek(0)
                self.handle.write(b"0")
                self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError(f"Another process is already using {self.path.name}") from None
        return self

    def __exit__(self,*exc):
        if self.handle:
            self.handle.close()


def maintain(cfg,store,fetcher,bootstrap=False):
    # Separate lock permits scanner/trader commands to share a DB with fetch.
    with ProcessLock(store.root,"maintenance"):
        if bootstrap:
            fetcher.bootstrap()
        fetcher.follow_cohort()
        last = store.get("models:last_attempt",0)
        if time.time()-last>86400:
            train_cex(cfg,store)
            train_scanner(cfg,store)
            store.set("models:last_attempt",time.time())
        # Retain candle history for training. Raw depth/chain logs are never stored.
        cutoff = time.time()-35*86400
        with store.transaction() as db:
            db.execute("DELETE FROM scans WHERE ts<?",(cutoff,))
            db.execute("DELETE FROM events WHERE ts<?",(cutoff,))
            # Keep the equity path for full-experiment drawdown and forward
            # validation. Deleting old marks can make a bad experiment look good.
            db.execute("DELETE FROM outbox WHERE ts<? AND status='sent'",(cutoff,))
        store.set("heartbeat:maintenance",time.time())


async def maintenance_loop(cfg,store,fetcher):
    while True:
        # services() already handled the optional startup bootstrap. In
        # particular, bootstrap_on_start=False must not download immediately.
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(maintain,cfg,store,fetcher,True)
        except Exception as exc:
            LOG.warning("Maintenance incomplete: %s",exc)


async def scanner_loop(scanner,cfg):
    while True:
        try:
            await asyncio.to_thread(scanner.run_once)
        except Exception as exc:
            LOG.warning("Scanner temporarily unavailable: %s",exc)
            scanner.store.event("scanner_error",{"type":type(exc).__name__})
        await asyncio.sleep(cfg["scanner"]["poll_s"])
