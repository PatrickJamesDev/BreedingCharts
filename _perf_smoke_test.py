"""Temporary smoke test for the SQLite row-store storage layer."""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

tmp = tempfile.mkdtemp(prefix="bc_perf_test_")

import database
database.get_data_dir = lambda: tmp          # redirect all storage to temp dir

import plant_db
plant_db._old_db_file = lambda: os.path.join(tmp, "nonexistent.json")

plant_db.reset_cache()

from models import PlantNode

N = 5000
nodes = [
    PlantNode(
        name=f"Plant {i:05d}",
        species="Rosa" if i % 2 else "Malus",
        cultivar=f"Cult {i % 50}",
        notes=f"note {i}",
        year=2000 + (i % 25),
    )
    for i in range(N)
]

t0 = time.perf_counter()
for n in nodes:
    plant_db.upsert(n)
t_insert = time.perf_counter() - t0

# Single-row update (should not rewrite everything)
t0 = time.perf_counter()
for _ in range(200):
    n = nodes[1234]
    n.name = "Renamed " + str(_)
    plant_db.upsert(n)
t_update = time.perf_counter() - t0

t0 = time.perf_counter()
all_nodes = plant_db.get_all()
t_read = time.perf_counter() - t0

t0 = time.perf_counter()
hits = plant_db.search("Plant 042")
t_search = time.perf_counter() - t0

# Persistence: flush, close, reload
plant_db._shutdown()
plant_db.reset_cache()
t0 = time.perf_counter()
reloaded = plant_db.get_all()
t_reload = time.perf_counter() - t0

# Deletes
ids = [n.id for n in reloaded[:10]]
t0 = time.perf_counter()
n_removed = plant_db.remove_many(ids)
t_remove = time.perf_counter() - t0
final = plant_db.get_all()

print(f"temp dir        : {tmp}")
print(f"insert {N:>5}      : {t_insert*1000:8.1f} ms  ({t_insert/N*1e6:6.0f} us/plant)")
print(f"update 200 rows : {t_update*1000:8.1f} ms  ({t_update/200*1e6:6.0f} us/update)")
print(f"get_all         : {t_read*1000:8.1f} ms  ({len(all_nodes)} nodes)")
print(f"search          : {t_search*1000:8.1f} ms  ({len(hits)} hits)")
print(f"reload from disk: {t_reload*1000:8.1f} ms  ({len(reloaded)} nodes)")
print(f"remove_many(10) : {t_remove*1000:8.1f} ms  ({n_removed} removed, {len(final)} remain)")

ok = (
    len(all_nodes) == N
    and len(reloaded) == N
    and n_removed == 10
    and len(final) == N - 10
    and len(hits) >= 1
    and os.path.exists(os.path.join(tmp, "plant_database.sqlite3"))
)
print("RESULT:", "PASS" if ok else "FAIL")

plant_db._shutdown()
try:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
except Exception:
    pass
sys.exit(0 if ok else 1)
