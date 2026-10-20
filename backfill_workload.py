"""Warm aggregate workload indexes once; prints counts only, never conversations."""
import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
from settings import MACHINES

ROOT = Path(__file__).resolve().parent
source = (ROOT / 'collector.py').read_text().rsplit("if __name__ == '__main__':", 1)[0]
source += '''
for attempt in range(80):
    result = workload()
    if result.get('history_complete'):
        break
print(json.dumps(result))
'''


def warm(machine):
    command = [sys.executable, '-'] if machine.get('local') else [
        'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', '-o', 'StrictHostKeyChecking=yes',
        machine['ssh'], 'python3 -']
    try:
        result = subprocess.run(command, input=source, text=True, capture_output=True, timeout=240)
        reading = json.loads(result.stdout)
        print(json.dumps({'machine': machine['name'], **reading}), flush=True)
    except Exception:
        print(json.dumps({'machine': machine['name'], 'error': 'Backfill incomplete; regular polls will continue indexing'}), flush=True)


if __name__ == '__main__':
    machines = MACHINES
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(warm, machines))
