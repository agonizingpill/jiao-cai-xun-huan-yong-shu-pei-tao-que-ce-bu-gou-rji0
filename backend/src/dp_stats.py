"""Differentially private course-basket statistics.

Threat model
------------
The protected unit is one historical course basket.  Two data sets are
neighbours when exactly one basket is added or removed.  We publish only noisy
*counts* of baskets containing each fixed itemset, so every count query has
sensitivity 1 and the whole query vector has L1 sensitivity equal to the number
of queries.  One Laplace vector with scale ``queries / epsilon`` therefore gives
an ``epsilon``-differentially-private release; every statistic (support,
confidence, lift, suggestions) is post-processing of that single release.

The query universe is fixed by ``SUBJECT_UNIVERSE`` and ``DP_MAX_ITEMSET`` and
never depends on the data, so the set of published queries cannot leak which
subjects exist.  Each public batch costs its own epsilon, and successive batches
compose (basic composition), which is what the shared budget accounts for.
"""

import hashlib
import itertools
import json
import math
import random

from .constants import (
    DP_COUNT_THRESHOLD_SCALE,
    DP_MAX_ITEMSET,
    DP_MIN_CONFIDENCE,
    DP_MIN_LIFT,
    DP_MIN_SUPPORT,
    SUBJECT_UNIVERSE,
)


def query_itemsets():
    """Fixed, data-independent itemsets whose counts are published."""
    out=[()]
    for size in range(1,DP_MAX_ITEMSET+1):
        out.extend(itertools.combinations(SUBJECT_UNIVERSE,size))
    return out


def canonical(baskets):
    return sorted([sorted({str(x).strip().lower() for x in basket}) for basket in baskets])


def snapshot_digest(baskets):
    payload=json.dumps(canonical(baskets),ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def _laplace(rng,scale):
    u=rng.random()-0.5
    return -scale*math.copysign(math.log(1.0-2.0*abs(u)),u)


def noisy_counts(baskets,epsilon,rng):
    """One epsilon-DP count release over the fixed itemset universe.

    ``rng`` must be a private, data-independent random source (a CSPRNG-seeded
    generator).  Noise magnitude depends only on the public epsilon and the
    fixed query count, never on the data.
    """
    clean=canonical(baskets)
    sets=[set(basket) for basket in clean]
    itemsets=query_itemsets()
    scale=len(itemsets)/epsilon
    counts={}
    for key in itemsets:
        exact=len(sets) if not key else sum(set(key)<=basket for basket in sets)
        counts[key]=max(0.0,exact+_laplace(rng,scale))
    return counts,scale


def derive_rules(counts,scale):
    total=max(1.0,counts[()])
    threshold=DP_COUNT_THRESHOLD_SCALE*scale
    rules=[]
    for itemset,count in counts.items():
        if len(itemset)<2 or count<threshold:continue
        support=count/total
        for rhs in itemset:
            lhs=tuple(x for x in itemset if x!=rhs)
            denominator=counts.get(lhs,0.0)
            single=counts.get((rhs,),0.0)
            if denominator<threshold or single<=0:continue
            confidence=count/denominator
            lift=confidence/(single/total)
            if support<DP_MIN_SUPPORT or confidence<DP_MIN_CONFIDENCE or lift<=DP_MIN_LIFT:continue
            rules.append({
                'if_subjects':list(lhs),
                'then_subject':rhs,
                'support':round(min(1.0,support),4),
                'confidence':round(min(1.0,confidence),4),
                'lift':round(lift,4),
            })
    rules.sort(key=lambda r:(-r['lift'],-r['confidence'],r['then_subject'],r['if_subjects']))
    return rules


def generate(baskets,epsilon,stat_version,noise_seed):
    """Deterministic public statistic for one budgeted release.

    ``noise_seed`` is drawn from a CSPRNG when the release is reserved and stored
    on the row, never from the data.  Generation is therefore a pure function of
    (sample, epsilon, release identity, stored nonce), so recomputing an
    interrupted release reproduces it byte-for-byte without new randomness and
    without any data-dependent seed.

    The exact sample digest is intentionally *not* part of the result: it is a
    commitment to the protected basket set and is not differentially private.
    """
    rng=random.Random(noise_seed)
    counts,scale=noisy_counts(baskets,epsilon,rng)
    item_frequency={key[0]:round(value) for key,value in counts.items() if len(key)==1}
    return {
        'mechanism':'Laplace over a fixed itemset universe (add/remove-one basket)',
        'stat_version':stat_version,
        'epsilon':round(epsilon,6),
        'sensitivity':len(query_itemsets()),
        'min_support':DP_MIN_SUPPORT,
        'min_confidence':DP_MIN_CONFIDENCE,
        'min_lift':DP_MIN_LIFT,
        'count_threshold':round(DP_COUNT_THRESHOLD_SCALE*scale,4),
        'basket_count':round(counts[()]),
        'noisy_item_frequency':item_frequency,
        'rules':derive_rules(counts,scale),
    }


def suggestions_for(result,subjects):
    """Post-processing: filter published rules for one pack's current subjects."""
    current={str(x).strip().lower() for x in subjects}
    best={}
    for rule in result.get('rules',[]):
        if not set(rule['if_subjects'])<=current:continue
        if rule['then_subject'] in current:continue
        chosen=rule['then_subject']
        if chosen not in best or (rule['lift'],rule['confidence'])>(best[chosen]['lift'],best[chosen]['confidence']):
            best[chosen]=rule
    return [best[key] for key in sorted(best)]
