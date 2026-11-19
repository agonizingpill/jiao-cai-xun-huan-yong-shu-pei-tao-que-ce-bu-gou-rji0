import os

ROLES=('operator','supervisor','viewer')
COPY_CONDITIONS=('pending','usable','repair','lost')
PACK_STATES=('planned','partial','allocated','cancelled')
ASSIGNMENT_STATES=('reserved','issued','returned','lost')
RELEASE_STATES=('reserved','publishing','published','failed')

# Differential privacy -----------------------------------------------------------
# Neighbouring data sets differ by one added or removed historical course basket.
# Every statistic is a count of baskets containing a fixed itemset, so each query
# has sensitivity 1.  The itemset universe is fixed independently of the data so
# that the set of published queries never depends on what the data contains.
SUBJECT_UNIVERSE=('art','computing','history','lab','language','math','music','physics')
DP_MAX_ITEMSET=3
DP_MIN_SUPPORT=0.15
DP_MIN_CONFIDENCE=0.6
DP_MIN_LIFT=1.0
DP_MECHANISM='laplace-fixed-universe-v1'
# Both an itemset and its antecedent must clear this multiple of the public noise
# scale before producing a rule.  Epsilon and the query count are public, so this
# is post-processing and does not alter the privacy guarantee.
DP_COUNT_THRESHOLD_SCALE=4.0


def _float_env(name,default):
    try:return float(os.getenv(name,default))
    except (TypeError,ValueError):return float(default)


def _micro_env(name,default_epsilon):
    # The site-wide budget is kept in integer micro-epsilon so debits are exact.
    return max(1,int(round(_float_env(name,default_epsilon)*1_000_000)))

DP_EPSILON_UNIT_MICRO=1_000_000
DP_TOTAL_EPSILON_MICRO=_micro_env('DP_TOTAL_EPSILON',100.0)
DP_DEFAULT_RELEASE_EPSILON_MICRO=_micro_env('DP_RELEASE_EPSILON',20.0)
DP_MIN_RELEASE_EPSILON_MICRO=5*DP_EPSILON_UNIT_MICRO
DP_MAX_RELEASE_EPSILON_MICRO=40*DP_EPSILON_UNIT_MICRO

# A durable budget reservation left behind by a crash is retried after this grace.
DP_RESERVED_GRACE_SECONDS=int(_float_env('DP_RESERVED_GRACE_SECONDS',20))
DP_MAX_RECOVERY_ATTEMPTS=int(_float_env('DP_MAX_RECOVERY_ATTEMPTS',3))
