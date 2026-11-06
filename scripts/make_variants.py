"""Generate my own validation worlds, so thresholds are not tuned to the public truth.

Same schema, different truth: option labels are permuted, seeds change, incidents move, and one world
has no effects at all (any winner there is a false positive). Public scenarios stay for the final report.
"""
import copy
import random
from pathlib import Path

import yaml

OUT = Path("scenarios_own")
OUT.mkdir(exist_ok=True)


def permute_effects(sc: dict, rng: random.Random):
    wfs = {w["id"]: w for t in sc["tenants"] for w in t["workflows"]}
    for e in sc.get("effects", []):
        w = wfs[e["workflow"]]
        opts = w["options"][e["dim"]]
        default = list(w["default_config"]["allocations"]["default"][e["dim"]])
        choices = [o for o in opts if o not in default]
        if choices:
            e["value"] = rng.choice(choices)
        if e.get("where"):
            feat = rng.choice(["lang", "age_band", "city_tier", "value_band"])
            vals = {"lang": list(w["language_mix"]), "age_band": ["18-30", "31-45", "46-60", "60+"],
                    "city_tier": ["1", "2", "3"], "value_band": ["low", "mid", "high"]}[feat]
            e["where"] = {feat: [rng.choice(vals)]}
    # one (dim, value, population) may appear once; duplicates would just compound, which is fine for a stress world


def main():
    base1 = yaml.safe_load(open("scenarios/public_01_dev.yaml", encoding="utf-8"))
    base2 = yaml.safe_load(open("scenarios/public_02_incidents.yaml", encoding="utf-8"))

    v = copy.deepcopy(base1)
    v.update(name="own_a_permuted", seed=9101)
    permute_effects(v, random.Random(1))
    yaml.safe_dump(v, open(OUT / "own_a_permuted.yaml", "w"), sort_keys=False)

    v = copy.deepcopy(base1)
    v.update(name="own_b_null", seed=9202, effects=[])
    yaml.safe_dump(v, open(OUT / "own_b_null.yaml", "w"), sort_keys=False)

    v = copy.deepcopy(base2)
    v.update(name="own_c_incidents", seed=9303)
    permute_effects(v, random.Random(3))
    for inc in v["incidents"]:
        if inc["type"] == "did_flagged":
            inc["at"] = "D4 15:00"
            inc["did"] = "D1"
        elif "prompt_version" in inc.get("patch", {}):
            inc["at"] = "D6 17:30"
        else:
            inc["at"] = "D2 12:00"
    yaml.safe_dump(v, open(OUT / "own_c_incidents.yaml", "w"), sort_keys=False)


if __name__ == "__main__":
    main()
