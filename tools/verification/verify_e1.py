import os,sys,time,json,base64,tempfile,random
sys.path.insert(0,os.getcwd())
from PyQt6.QtWidgets import QApplication
app=QApplication([])
import database, plant_db, fileio
from models import PlantNode, LineageTree
TMP=tempfile.mkdtemp(); database._data_dir=TMP; plant_db.reset_cache()
print("orjson available:", fileio.orjson is not None)
# G: truncation safety
good=LineageTree(name="Precious")
for i in range(5): n=PlantNode(name=f"x{i}"); good.nodes[n.id]=n
fp=os.path.join(TMP,"Precious.plf"); database.save_lineage(good,fp); before=open(fp,"rb").read()
list(good.nodes.values())[3].color=object()
ok=database.save_lineage(good,fp)
after=open(fp,"rb").read()
print("G unserialisable value: save_lineage ->",ok,"| old file byte-identical:",before==after,"| stray temp files:",[f for f in os.listdir(TMP) if f.endswith(".tmp")])
# round trip + sidecar + settings
list(good.nodes.values())[3].color="#fff"
print("round trip ok:", database.save_lineage(good,fp) and len(database.load_lineage(fp).nodes)==5)
# speed of atomic save on a 46MB lineage
tr=LineageTree(name="Big")
for i in range(300):
    n=PlantNode(name=f"p{i}"); n.thumb=base64.b64encode(os.urandom(120_000)).decode(); tr.nodes[n.id]=n
bp=os.path.join(TMP,"Big.plf")
t=time.perf_counter(); database.save_lineage(tr,bp); a=(time.perf_counter()-t)*1000
print(f"save_lineage 46 MB (atomic, {'orjson' if fileio.orjson else 'streamed json'}): {a:.0f} ms")
# F25 search
N=5000
for i in range(N): plant_db.upsert(PlantNode(name=f"Plant {i:05d}",species=f"Sp{i%40} alba",cultivar=f"C{i%20}"))
plant_db.search("plant 042")
b=1e9
for _ in range(5):
    t=time.perf_counter(); r=plant_db.search("plant 042"); b=min(b,time.perf_counter()-t)
print(f"search('plant 042') @5000 warm: {b*1000:.2f} ms ({len(r)} hits)")
# stale-haystack correctness after rename
p=r[0]; p.name="Zzyzx renamed"; plant_db.upsert(p)
print("search sees renamed plant:", [x.name for x in plant_db.search("zzyzx")]==["Zzyzx renamed"], "| old name gone:", p.id not in [x.id for x in plant_db.search("plant 042")])
os._exit(0)
