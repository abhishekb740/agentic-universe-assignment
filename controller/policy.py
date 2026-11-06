"""Every tunable, in one place, each with the reason it has this value.

Nothing here names a variant, a segment or a scenario. Tenant preferences come from a client's
written request (stakeholders/04), and are stricter than the API contract, never looser.
"""

STEP_MINUTES = 60

# --- experiments (engagement, for welcome_message and voice)
Q_FDR = 0.10            # false discovery rate per daily look; about 70 tests a look on a 3x4 option set
Z_MIN = 2.4             # floor on top of BH: we look daily, and BH alone does not correct for peeking
MIN_LIFT = 0.03         # a 3% relative lift is the smallest worth an approver's attention
MIN_N_GLOBAL = 250      # connected calls on the arm before a workflow-wide winner may be declared
MIN_N_SEGMENT = 120     # same for one feature value; z=2.6 needs ~120 v ~500 for a 20% lift on a 0.5 base
HARM_Z = -2.5           # early stop: arm is clearly worse across the whole workflow
HARM_LIFT = -0.03
DROP_AFTER_N = 600      # successive elimination: enough data to call an arm hopeless (a 10% lift in half the
                        # population reaches z~2 by here, so it survives on DROP_MAX_SEG_Z)
DROP_MAX_SEG_Z = 1.5    # ...if no population shows even a hint of a win
DROP_MAX_ALL_Z = 0.5
ARM_MAX_SHARE = 0.20    # per untested arm: bounds the damage of an unknown variant; control stays the largest arm
ARM_SINGLE_SHARE = 0.30 # when a dim has one alternative: same exposure logic, needs a little more for power
PROMOTE_SHARE = 0.70    # winner share while other arms still run
HOLDBACK_SHARE = 0.15   # control holdback that keeps decay detectable and evidence concurrent
MAX_SEGMENT_RULES_PER_DIM = 3
MIN_HORIZON_TO_EXPLORE_DAYS = 2.0   # never start a test that cannot finish
GUARD_PICKUP_Z = -2.5   # a win on engagement is vetoed if the arm also clearly hurts pickup

# --- decay of a promoted winner
DECAY_RECENT_DAYS = 2
DECAY_REVERSAL_Z = -2.0
DECAY_DROP_Z = 2.5      # recent lift v the lift that earned the promotion
DECAY_MIN_AGE_DAYS = 3  # a promotion gets time to settle; the promotion estimate itself is optimistic
DECAY_KEEP_FRACTION = 0.4   # "faded" means most of the promoted lift is gone
DECAY_MIN_DROP = 0.05   # absolute drop in risk difference

# --- caller IDs (pickup)
DID_WINDOW_MIN = 1440
DID_FLAG_Z = -3.0
DID_FLAG_RATIO = 0.85
DID_PROBE_SHARE = 0.05  # enough for 150+ dials a day; small enough that a flagged DID costs little
DID_TRUST_DIALS = 350
DID_TRUST_Z = -1.5
DID_KNOWN_DIALS = 500   # history this large means a DID needs no probe

# --- prompt safety (hallucination)
HALLUC_Z = 3.5
HALLUC_RATIO = 2.5
HALLUC_MIN_FLAGS = 5
STRICT_PASS_RATE = 0.985    # stricter than the policy minimum: 0.97 would pass a prompt several times worse
VALIDATION_N_SCREEN = 300
VALIDATION_N_EVIDENCE = 500
REJECT_SUMMARY_WORDS = ("verbatim", "strict")   # stakeholders/04: a client said this hurts their calls

# --- scheduling
SCHED_MIN_WEIGHT = 0.25     # no zero weights: never starve a population of a whole hour
SCHED_MAX_WEIGHT = 3.0
SCHED_CHI2_PER_DF = 3.0     # interaction must be well above noise before a schedule is proposed
SCHED_MIN_DIALS = 2000

# --- governance
MAX_COHORTS = 20
REJECT_COOLDOWN_MIN = 240   # after a judgement-call rejection, wait for fresh data rather than re-ask at once
TENANT_PREFS = {
    # stakeholders/04: Kestrel's operations lead asks for at most 2 requests a day, none before 11:00.
    "kestrel_finance": {"earliest_hour": 11, "max_requests_per_24h": 2},
}
