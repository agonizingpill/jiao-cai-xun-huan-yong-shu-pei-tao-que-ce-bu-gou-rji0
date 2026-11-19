"""Budgeted, recoverable publication of differentially private statistics.

Lifecycle of one public batch
-----------------------------
1. The sample is the full set of historical baskets; its digest is the sample's
   identity.  Any pack analysed against the same digest reuses the same release
   and pays nothing, so switching class or retrying never deducts twice.
2. A new digest reserves epsilon from the single site-wide ledger while holding
   a row lock on it.  The sample is re-read under that lock, so concurrent
   publishers serialize and cannot overspend, even across processes.
3. If the ledger cannot afford a new batch, the latest *published* version is
   reused instead; the old statistic is never withdrawn.
4. The charge commits before the noise is drawn, so a crash leaves a durable,
   paid-for ``reserved`` row.  Generation is a deterministic function of the
   stored sample, epsilon and a CSPRNG nonce stored on that row, so recovery
   reproduces exactly the statistic the interrupted call would have produced --
   the charge is never repeated.
5. Baskets supplemented later change the digest and are therefore counted only
   in the next affordable batch; every class then reuses that new version.
"""

import secrets
import time

from fastapi import HTTPException
from sqlalchemy import select,update
from sqlalchemy.exc import IntegrityError

from . import dp_stats
from .constants import (
    DP_DEFAULT_RELEASE_EPSILON_MICRO,
    DP_MAX_RECOVERY_ATTEMPTS,
    DP_RESERVED_GRACE_SECONDS,
    DP_TOTAL_EPSILON_MICRO,
)
from .models import DpBudget, DpRelease, Edition, HistoricalBasket, Pack, PackLine

LEDGER_NAME='site'


def ensure_ledger(s):
    ledger=s.scalar(select(DpBudget).where(DpBudget.name==LEDGER_NAME))
    if ledger is None:
        # Two first-time publishers can race here; the unique name makes one lose.
        try:
            with s.begin_nested():
                s.add(DpBudget(name=LEDGER_NAME,total_micro=DP_TOTAL_EPSILON_MICRO,spent_micro=0))
        except IntegrityError:
            pass
        ledger=s.scalar(select(DpBudget).where(DpBudget.name==LEDGER_NAME))
    return ledger


def current_sample(s):
    rows=list(s.scalars(select(HistoricalBasket).order_by(HistoricalBasket.id)))
    return dp_stats.canonical([row.subjects for row in rows])


def latest_published(s):
    return s.scalar(select(DpRelease).where(DpRelease.state=='published').order_by(DpRelease.id.desc()))


def pack_subjects(s,pack_id):
    return sorted(set(s.scalars(select(Edition.subject).join(PackLine,PackLine.edition_id==Edition.id).where(PackLine.pack_id==pack_id))))


def public_view(release,subjects,stale):
    """Exactly what a class pack page may see: suggestions and a stat version.

    The snapshot digest is deliberately omitted: it is a commitment to the exact
    basket set and is not differentially private, so publishing it would let an
    adversary who knows all-but-one basket test that basket's membership.  No
    counts, supports, confidences, lifts, basket indices or per-basket outcomes
    are included either; those stay inside the release row.
    """
    return {
        'stat_version':release.id,
        'stale':stale,
        'suggestions':[
            {'if_subjects':rule['if_subjects'],'then_subject':rule['then_subject']}
            for rule in dp_stats.suggestions_for(release.result or {},subjects)
        ],
    }


def _lock_ledger(s):
    ledger=s.scalar(select(DpBudget).where(DpBudget.name==LEDGER_NAME).with_for_update().execution_options(populate_existing=True))
    if ledger is None:
        ensure_ledger(s)
        ledger=s.scalar(select(DpBudget).where(DpBudget.name==LEDGER_NAME).with_for_update().execution_options(populate_existing=True))
    return ledger


def _reserve_or_reuse(s):
    """Under the ledger row lock, return (release, charged, stale, snapshot)."""
    _lock_ledger(s)
    baskets=current_sample(s)
    snapshot=dp_stats.snapshot_digest(baskets)
    existing=s.scalar(select(DpRelease).where(DpRelease.snapshot==snapshot).with_for_update().execution_options(populate_existing=True))
    if existing is not None:
        return existing,False,False,snapshot
    release=DpRelease(
        snapshot=snapshot,epsilon_micro=DP_DEFAULT_RELEASE_EPSILON_MICRO,state='reserved',
        basket_payload=baskets,noise_seed=secrets.randbits(63),
        ledger_debit_micro=DP_DEFAULT_RELEASE_EPSILON_MICRO,
        attempts=0,attempted_at=int(time.time()),
    )
    try:
        with s.begin_nested():
            # Conditional debit: succeeds only if the remaining balance still
            # covers a full release.  A plain read-modify-write can lose an
            # increment where row locks are weak; this cannot overdraw.
            debited=s.execute(
                update(DpBudget)
                .where(DpBudget.name==LEDGER_NAME,
                       DpBudget.spent_micro+DP_DEFAULT_RELEASE_EPSILON_MICRO<=DpBudget.total_micro)
                .values(spent_micro=DpBudget.spent_micro+DP_DEFAULT_RELEASE_EPSILON_MICRO)
            ).rowcount
            if not debited:
                return None,False,True,snapshot
            s.add(release);s.flush()
    except IntegrityError:
        # Another publisher committed the same snapshot first; roll back our
        # debit with the savepoint and reuse their release.
        s.expire_all()
        existing=s.scalar(select(DpRelease).where(DpRelease.snapshot==snapshot).with_for_update().execution_options(populate_existing=True))
        if existing is None:raise
        return existing,False,False,snapshot
    # Make the charge durable now: a crash must not lose the debit, and a retry
    # must find this row instead of charging again.
    s.commit()
    return release,True,False,snapshot


def _finalize(s,release):
    if release.state=='published' and release.result is not None:
        return release
    try:
        release.result=dp_stats.generate(
            release.basket_payload,release.epsilon_micro/1_000_000,
            release.id,release.noise_seed,
        )
        release.state='published';release.error=None
    except Exception as error:
        release.state='failed';release.error=str(error)[:450]
        release.attempts=(release.attempts or 0)+1
        release.attempted_at=int(time.time());s.commit()
        raise
    release.attempted_at=int(time.time());s.commit()
    return release


def _propagate(s,release,stale):
    """Every already-analysed class reuses this same statistic version."""
    for pack in s.scalars(select(Pack).where(Pack.ml_state=='ready').order_by(Pack.id).with_for_update()):
        pack.recommendation=public_view(release,pack_subjects(s,pack.id),stale)
    s.flush()


def publish(s):
    """Publish or reuse the statistic for the current sample.

    Returns ``{release, charged, stale, snapshot}``.  Raises 409 only when a new
    batch is unaffordable and no earlier publication exists to fall back to.
    """
    release,charged,stale,snapshot=_reserve_or_reuse(s)
    if release is None:
        fallback=latest_published(s)
        if fallback is None:
            raise HTTPException(409,'公开统计预算不足，且暂无可沿用的旧版统计')
        _propagate(s,fallback,True);s.commit()
        return {'release':fallback,'charged':False,'stale':True,'snapshot':snapshot}
    release=_finalize(s,release)
    _propagate(s,release,stale);s.commit()
    return {'release':release,'charged':charged,'stale':stale,'snapshot':snapshot}


def attach(s,pack_id,outcome):
    pack=s.scalar(select(Pack).where(Pack.id==pack_id).with_for_update().execution_options(populate_existing=True))
    if pack is None:
        raise HTTPException(404,'业务记录不存在')
    pack.recommendation=public_view(outcome['release'],pack_subjects(s,pack_id),outcome['stale'])
    pack.ml_state='ready';pack.error=None
    s.commit()
    return pack


def state(s):
    ledger=ensure_ledger(s);s.commit()
    latest=latest_published(s)
    snapshot=dp_stats.snapshot_digest(current_sample(s))
    # The snapshot digest is an exact commitment to the protected basket set and
    # is not differentially private, so it stays server-side; only the boolean
    # freshness flag is exposed.
    return {
        'latest_stat_version':latest.id if latest else None,
        'up_to_date':bool(latest and latest.snapshot==snapshot),
        'budget':{
            'total_epsilon':ledger.total_micro/1_000_000,
            'spent_epsilon':ledger.spent_micro/1_000_000,
            'remaining_epsilon':(ledger.total_micro-ledger.spent_micro)/1_000_000,
            'next_release_epsilon':DP_DEFAULT_RELEASE_EPSILON_MICRO/1_000_000,
            'exhausted':ledger.spent_micro+DP_DEFAULT_RELEASE_EPSILON_MICRO>ledger.total_micro,
        },
    }


def retrieve(s,release_id):
    """Return a charged statistic.  Only the noisy release is ever stored."""
    release=s.get(DpRelease,release_id)
    if release is None:
        raise HTTPException(404,'统计版本不存在')
    return release


def recover(s,force=False):
    """Finish interrupted releases without repeating any budget charge."""
    now=int(time.time());done=[]
    rows=list(s.scalars(select(DpRelease).where(DpRelease.state.in_(('reserved','failed'))).order_by(DpRelease.id)))
    for release in rows:
        if (release.attempts or 0)>=DP_MAX_RECOVERY_ATTEMPTS:
            continue
        if release.state=='reserved' and not force and now-(release.attempted_at or 0)<DP_RESERVED_GRACE_SECONDS:
            continue
        try:
            done.append(_finalize(s,release).id)
        except Exception:
            continue
    current=dp_stats.snapshot_digest(current_sample(s))
    if done:
        for release in rows:
            if release.id in done and release.snapshot==current:
                _propagate(s,release,False);break
        s.commit()
    return done
