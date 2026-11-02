"""Run model-written CadQuery in a one-shot worker subprocess with a
wall-clock limit: the read loop's quick build for code mode. The worker
(worker.py) is the same program the final build uses, so a frozen
desktop app needs no new entry point; here it is started directly rather
than through the job registry (the read thread holds no conversion slot
and must not wait for one) and killed with its process group when it
overruns. The worker itself also stops the script after the same limit
when it can (a Python loop); this outer limit catches a kernel call that
never gives the interpreter back."""
import json
import os
import subprocess

from . import jobs

GRACE_S = 15          # seconds past the worker's own limit before it is killed from outside


def run_quick(params, out_dir, timeout, drawing_path=None):
    """Run the worker on `params` (tool blueprint, code, units, quick) in
    `out_dir`; the result.json contents, or a dict with ok False and a
    readable 'error' when it overran or crashed."""
    os.makedirs(out_dir, exist_ok=True)
    params_path = os.path.join(out_dir, 'params.json')
    result_path = os.path.join(out_dir, 'result.json')
    try:
        os.remove(result_path)
    except OSError:
        pass
    with open(params_path, 'w') as f:
        json.dump(params, f)
    with open(os.path.join(out_dir, 'log.txt'), 'wb') as logf:
        proc = subprocess.Popen(
            jobs._worker_cmd() + [drawing_path or os.devnull, os.path.join(out_dir, 'output.step'),
                                  params_path, result_path],
            stdout=logf, stderr=subprocess.STDOUT, cwd=jobs._worker_cwd(), **jobs._own_group())
        try:
            # the worker stops the script itself after `timeout` when it
            # can; this outer limit is for a kernel call that never yields
            proc.wait(timeout=timeout + GRACE_S)
        except subprocess.TimeoutExpired:
            jobs._kill_tree(proc, hard=True)
            proc.wait()
            return {'ok': False, 'failure': 'timeout',
                    'error': f'The script ran longer than {timeout:.0f} s and was stopped (an endless '
                             'loop, or a boolean the kernel cannot finish).'}
    try:
        with open(result_path) as f:
            res = json.load(f)
    except (OSError, ValueError):
        res = None
    if not isinstance(res, dict):
        tail = ''
        try:
            with open(os.path.join(out_dir, 'log.txt'), errors='replace') as f:
                lines = [ln.rstrip() for ln in f.read().splitlines() if ln.strip()]
            tail = ' | '.join(lines[-6:])
        except OSError:
            pass
        return {'ok': False, 'failure': 'crashed',
                'error': 'The script crashed the geometry kernel before it could report an error.'
                         + (f' The log ends: {tail}' if tail else '')}
    return res
