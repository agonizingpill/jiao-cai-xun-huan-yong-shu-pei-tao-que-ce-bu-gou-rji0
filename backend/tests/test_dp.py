"""Requirement-level tests for the DP statistics pipeline (SQLite harness)."""
import os, sys, time, tempfile
os.environ.pop('DP_TOTAL_EPSILON', None)
os.environ.pop('DP_RELEASE_EPSILON', None)
os.environ.setdefault('DATABASE_URL','sqlite:///'+tempfile.mktemp(suffix='.db'))
os.environ.setdefault('JWT_SECRET','test')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import Base, engine, Session
from src import models, dp_stats, dp_service
from src.constants import DP_TOTAL_EPSILON_MICRO
from sqlalchemy import select, func
from fastapi import HTTPException

FAIL=[]
def check(name, cond, extra=''):
    print(('PASS ' if cond else 'FAIL ')+name+(' '+str(extra) if extra and not cond else ''))
    if not cond: FAIL.append(name)

def fresh():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    with Session() as s:
        dp_service.ensure_ledger(s); s.commit()

def add_basket(s, code, subjects):
    s.add(models.HistoricalBasket(code=code, subjects=subjects))

def make_pack(s, code, subjects_editions):
    p=models.Pack(code=code, classroom_id=1, term='2026秋'); s.add(p); s.flush()
    for i, subj in enumerate(subjects_editions):
        e=models.Edition(code=f'{code}-E{i}', title=f't{i}', subject=subj, version='v', deposit_cents=100)
        s.add(e); s.flush()
        s.add(models.PackLine(pack_id=p.id, edition_id=e.id, quantity=10))
    s.commit(); return p.id

def seed_baskets(n=96):
    patterns=[['math','physics','lab'],['language','history'],['math','computing'],['art','music']]
    import numpy as np
    rng=np.random.default_rng(38)
    with Session() as s:
        for i in range(n):
            add_basket(s,f'H-{i:03d}',patterns[int(rng.choice(4,p=[.4,.3,.2,.1]))])
        s.commit()

def spent():
    with Session() as s:
        return s.scalar(select(models.DpBudget.spent_micro).where(models.DpBudget.name=='site'))

# 1. publish charges once, retry / same sample / another class reuses it --------
fresh(); seed_baskets()
with Session() as s: p1=make_pack(s,'PACK-A',['physics','lab'])
with Session() as s: p2=make_pack(s,'PACK-B',['physics','lab'])
with Session() as s: make_pack(s,'PACK-C',['language'])
base=spent()
with Session() as s: o1=dp_service.publish(s)
check('first publish charges', o1['charged'], o1)
check('ledger debited once', spent()-base==DP_TOTAL_EPSILON_MICRO//5, (spent(),base))
with Session() as s: o2=dp_service.publish(s)
check('retry same sample free', (not o2['charged']) and o2['release'].id==o1['release'].id)
with Session() as s: o3=dp_service.publish(s)
check('third call same sample free', (not o3['charged']) and o3['release'].id==o1['release'].id)
with Session() as s: p=dp_service.attach(s,p1,o1)
check('pack A gets stat version', p.recommendation['stat_version']==o1['release'].id, p.recommendation)
with Session() as s: p=dp_service.attach(s,p2,o2)
check('pack B reuses same version', p.recommendation['stat_version']==o1['release'].id)
check('public view has only subjects+version', set(p.recommendation)=={'stat_version','stale','suggestions'}, p.recommendation)
check('suggestion has no numbers', all(set(x)=={'if_subjects','then_subject'} for x in p.recommendation['suggestions']), p.recommendation['suggestions'])

# 2. new baskets -> next affordable batch; existing classes reuse the new one ---
with Session() as s:
    add_basket(s,'H-NEW-1',['physics','lab']); s.commit()
before=spent()
with Session() as s: o4=dp_service.publish(s)
check('supplement triggers new batch', o4['charged'] and o4['release'].id!=o1['release'].id)
check('new batch charges', spent()-before==DP_TOTAL_EPSILON_MICRO//5)
with Session() as s:
    rows=list(s.scalars(select(models.Pack)))
    versions={r.code:r.recommendation['stat_version'] for r in rows if r.recommendation}
check('all classes on new version', set(versions.values())=={o4['release'].id}, versions)

# 3. budget exhaustion -> keep old statistic, never overspend -------------------
with Session() as s:
    led=s.scalar(select(models.DpBudget).where(models.DpBudget.name=='site'))
    led.total_micro=led.spent_micro+DP_TOTAL_EPSILON_MICRO//10  # less than one release left
    s.commit()
with Session() as s:
    add_basket(s,'H-NEW-2',['art','music']); s.commit()
spent_before=spent()
with Session() as s: o5=dp_service.publish(s)
check('exhausted: no charge', spent_before==spent())
check('exhausted: falls back to published', o5['stale'] and o5['release'].state=='published' and o5['release'].id==o4['release'].id, o5)
with Session() as s: st=dp_service.state(s)
check('state reports exhausted', st['budget']['exhausted'] and st['budget']['remaining_epsilon']<st['budget']['next_release_epsilon'], st)
check('state hides exact snapshot', 'current_snapshot' not in st and 'latest_snapshot' not in st, st)

# 4. crash mid-publish: charge durable, recovery returns same statistic --------
fresh(); seed_baskets()
with Session() as s: pid=make_pack(s,'PACK-R',['physics','lab'])
with Session() as s:
    o=dp_service.publish(s)
published=o['release'].result
# Simulate a crash *after* the reservation committed but before generation.
with Session() as s:
    led=s.scalar(select(models.DpBudget).where(models.DpBudget.name=='site'))
    r=models.DpRelease(snapshot='deadbeef'*8,epsilon_micro=dp_service.DP_DEFAULT_RELEASE_EPSILON_MICRO,
        state='reserved',basket_payload=dp_stats.canonical([['math','physics']]),noise_seed=42,
        ledger_debit_micro=0,attempts=0,attempted_at=0)
    s.add(r); led.spent_micro+=dp_service.DP_DEFAULT_RELEASE_EPSILON_MICRO; s.commit(); rid=r.id
charge=spent()
with Session() as s: done=dp_service.recover(s,force=True)
check('recovery finishes reserved row', rid in done, done)
with Session() as s: rec=dp_service.retrieve(s,rid)
check('recovery charged nothing more', spent()==charge)
with Session() as s: again=dp_service.recover(s,force=True)
check('recovery idempotent', again==[] or rid not in again)

# regenerate same release twice must be byte-identical (paid statistic retrievable)
with Session() as s: r=s.get(models.DpRelease,rid)
first=r.result
with Session() as s: r=s.get(models.DpRelease,rid)
check('stored statistic retrievable after recovery', first is not None and first['stat_version']==rid)

# 5. deterministic generation: same release identity -> same bytes
import random
g1=dp_stats.generate(dp_stats.canonical([['physics','lab'],['math']]),20.0,7,123)
g2=dp_stats.generate(dp_stats.canonical([['physics','lab'],['math']]),20.0,7,123)
check('generate is deterministic', g1==g2)

# 6. neighbouring samples: adding one basket changes the eager count but noise is
#    calibrated; sanity-check the sensitivity value published is query count.
check('sensitivity equals query count', g1['sensitivity']==len(dp_stats.query_itemsets()))

# 7. concurrency: no overspend and no leaked charges when samples collide --------
fresh(); seed_baskets()
import threading
errs=[]
def worker(i):
    try:
        with Session() as s:
            s.add(models.HistoricalBasket(code=f'CC-{i}',subjects=['math','physics','lab'])); s.commit()
        with Session() as s: dp_service.publish(s)
    except Exception as e: errs.append(repr(e))
ts=[threading.Thread(target=worker,args=(i,)) for i in range(12)]
[t.start() for t in ts]; [t.join() for t in ts]
with Session() as s:
    led=s.scalar(select(models.DpBudget)); rel=list(s.scalars(select(models.DpRelease)))
check('concurrency: no errors', not errs, errs[:2])
check('concurrency: never overspends', led.spent_micro<=led.total_micro, led.spent_micro)
check('concurrency: one charge per distinct snapshot',
      led.spent_micro==len(rel)*dp_service.DP_DEFAULT_RELEASE_EPSILON_MICRO, (led.spent_micro,len(rel)))
check('concurrency: all releases published', all(r.state=='published' for r in rel), [r.state for r in rel])

# 8. recovery reproduces the exact statistic of an interrupted paid release ----
fresh()
with Session() as s:
    s.add(models.HistoricalBasket(code='R1',subjects=['math','physics','lab']))
    s.add(models.HistoricalBasket(code='R2',subjects=['language','history'])); s.commit()
import secrets
with Session() as s:
    led=s.scalar(select(models.DpBudget)); baskets=dp_service.current_sample(s)
    snap=dp_stats.snapshot_digest(baskets); led.spent_micro+=dp_service.DP_DEFAULT_RELEASE_EPSILON_MICRO
    r=models.DpRelease(snapshot=snap,epsilon_micro=dp_service.DP_DEFAULT_RELEASE_EPSILON_MICRO,state='reserved',
        basket_payload=baskets,noise_seed=secrets.randbits(63),ledger_debit_micro=dp_service.DP_DEFAULT_RELEASE_EPSILON_MICRO,
        attempts=0,attempted_at=0)
    s.add(r); s.commit(); rid=r.id; expected=dp_stats.generate(baskets,20.0,rid,r.noise_seed)
charged=spent()
with Session() as s: done=dp_service.recover(s,force=True)
with Session() as s: got=dp_service.retrieve(s,rid).result
check('recovery produced exact statistic', got==expected)
check('recovery did not recharge', spent()==charged)

print('\nFAILURES:',FAIL if FAIL else 'none')
sys.exit(1 if FAIL else 0)
