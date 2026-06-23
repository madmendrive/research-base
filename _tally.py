import json, time
from pathlib import Path
from scripts import kb
from datetime import datetime, timezone
from collections import Counter
DATA = Path('data')
c = kb.connect()
# wait until the (re)queued transcripts finish
for _ in range(45):
    rem = c.execute("select count(*) from jobs where id between 1424 and 1430 and status in ('queued','running')").fetchone()[0]
    if rem == 0:
        break
    time.sleep(30)
# re-enable the deferred backlog so it resumes now
now = datetime.now(timezone.utc).isoformat(timespec='seconds')
n = c.execute("update jobs set available_at=? where status='queued' and id not between 1421 and 1430 and available_at > ?", (now, now)).rowcount
c.commit()
print(f"re-enabled {n} backlog/reindex jobs\n")

def find_author(stem):
    for cat in ('Macro', 'Semis'):
        base = DATA / cat / 'authors'
        if base.is_dir():
            for note in base.glob(f'*/notes/*{stem}*'):
                if note.suffix == '.txt':
                    return f'{cat}/{note.parent.parent.name}'
    return '(not found)'

rows = c.execute("select id,status,payload_json from jobs where id between 1421 and 1430 order by id").fetchall()
print("=== FINAL TRANSCRIPT TALLY ===")
print(dict(Counter(r[1] for r in rows)))
for r in rows:
    fn = Path(json.loads(r[2])['path']).stem
    loc = find_author(fn) if r[1] == 'succeeded' else r[1]
    print(f"  {fn.replace('transcript-','')[:46]:48} -> {loc}")
