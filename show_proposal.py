import json, sys
base="/home/hatch/workspace/bellhaven-sync/data/"
props=json.load(open(base+"proposals.json"))
dec=json.load(open("/home/hatch/workspace/bellhaven-sync/state/decisions.json"))
pid=sys.argv[1]
p=next(x for x in props if x["proposal_id"]==pid)
print(json.dumps(p, indent=1))
print("DECIDED:" , pid in dec)
