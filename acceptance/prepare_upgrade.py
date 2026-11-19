"""Prepare a real baseline library with a route, restricted grant and preserved media."""
import json, secrets, sys, time
from pathlib import Path
baseline, folder, origin, museum = sys.argv[1:]
sys.path.insert(0, baseline)
from institution import create_app
from vimeo_transfer import Source, migrate
home=Path(folder)
home.mkdir()
keys={name:secrets.token_urlsafe(32) for name in ("operator","creator","station","publisher")}
keys["institution"]="Upgrade rehearsal"
(home/"operator.json").write_text(json.dumps(keys))
data=home/"collection"
migrate(keys["institution"],data,Source(origin,None))
app=create_app(data,home/"institution.sqlite",keys["operator"],keys["institution"])
c=app.test_client()
auth={"Authorization":"Bearer "+keys["operator"]}
copy=next(w for w in c.get("/api/screening-copies",headers=auth).json if w["title"]=="Exhibition cut")
g=c.post("/api/grants",headers=auth,json=dict(work=copy["work"],version=copy["version"],origin=museum,starts=time.time()-15,ends=time.time()+7200,audience="members",agreement="fixture"))
assert g.status_code==201, g.data
route="/embed/meeting/baseline"
dest="https://station.example/videos/embed/fixture"
assert c.put("/api/routes/meeting/baseline",headers=auth,json=dict(revision="one",destination=dest,public_authorized=True)).status_code==200
assert c.put("/api/policy/moderation",headers=auth,json={"required":True}).status_code==200
(home.parent/"upgrade-proof-private.json").write_text(json.dumps(dict(grant=g.json,copy=copy,route=route,destination=dest)))
