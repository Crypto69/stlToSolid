"""Everything that touches the engine happens in a worker subprocess, so a
geometry-kernel crash or a mesh that eats all the memory costs that process
and never the server. Two ways in:

- `call`: one short question, answered in seconds; waits for no other job.
- `start_job` / `job_state`: a conversion or build, through the job registry
  (one at a time, cancel, log), whose finished files are copied to the output
  folder. Finishing is idempotent and recorded in the job's folder, so a job
  a restarted server recovers from disk still delivers its files.
"""
import json
import os
import subprocess
import tempfile
import threading

from . import files as F

# what a run leaves in its job folder -> the name it gets next to the part
DELIVERABLES = (('output.step', '{stem}.step', 'step'),
                ('output_fusion.py', '{stem}_fusion.py', 'fusion_script'),
                ('output_fusion_bfill.py', '{stem}_fusion_bfill.py', 'fusion_boundary_fill_script'),
                ('output.py', '{stem}_cadquery.py', 'cadquery_script'),
                ('code.py', '{stem}_cadquery.py', 'cadquery_script'),
                ('recipe.json', '{stem}_recipe.json', 'recipe'),
                ('render_front.png', '{stem}_render_front.png', 'render_front'),
                ('render_top.png', '{stem}_render_top.png', 'render_top'),
                ('render_right.png', '{stem}_render_right.png', 'render_right'),
                ('render_iso.png', '{stem}_render_iso.png', 'render_iso'))
# result.json keys a model does not need to read (they repeat the request, or
# are viewer data measured in megabytes)
_NOISE = {'params', 'preview', 'preview_map', 'renders', 'freeform', 'recipe', 'per_feature',
          'has_script', 'has_bfill_script', 'has_fusion_script', 'has_step'}
LOG_TAIL = 15
_deliver_lock = threading.Lock()


class CallFailed(RuntimeError):
    """A worker question that failed; the message is a readable sentence."""


def _tail(path, n=LOG_TAIL):
    try:
        with open(path, errors='replace') as f:
            return [ln.rstrip() for ln in f.read().splitlines() if ln.strip()][-n:]
    except OSError:
        return []


def call(cfg, tool, in_path, params=None, out_path=None, timeout=300.0):
    """Ask the worker one question; the result dict, or CallFailed."""
    from stl_to_solid.service import jobs
    os.makedirs(cfg.jobs_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cfg.jobs_dir, prefix='call-') as d:
        params_path = os.path.join(d, 'params.json')
        result_path = os.path.join(d, 'result.json')
        log_path = os.path.join(d, 'log.txt')
        with open(params_path, 'w') as f:
            json.dump(dict(params or {}, tool=tool), f)
        with open(log_path, 'wb') as logf:
            proc = subprocess.Popen(
                jobs._worker_cmd() + [in_path, out_path or os.path.join(d, 'output'), params_path, result_path],
                stdout=logf, stderr=subprocess.STDOUT, cwd=jobs._worker_cwd(),
                **jobs.worker_stdin(), **jobs._own_group())
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                jobs._kill_tree(proc, hard=True)
                proc.wait()
                raise CallFailed(f'{tool} took longer than {timeout:.0f} s and was stopped.') from None
            finally:
                if proc.stdin is not None:
                    proc.stdin.close()
        try:
            with open(result_path) as f:
                res = json.load(f)
        except (OSError, ValueError):
            note = jobs._killed_by(code)
            tail = ' | '.join(_tail(log_path, 6))
            raise CallFailed((note or {}).get('message') or
                             f'{tool} stopped before it could report a result.'
                             + (f' The log ends: {tail}' if tail else '')) from None
        if not res.get('ok'):
            raise CallFailed(res.get('error') or f'{tool} failed.')
        for k in ('ok', 'error', 'failure', 'mode', 'metrics', 'output_stats', 'params'):
            res.pop(k, None)
        return res


def start_job(cfg, params, stem, out_dir, in_path=None):
    """Queue a conversion or a build; the job id. `in_path` is copied into
    the job's folder so the job does not depend on the original staying put."""
    import shutil
    from stl_to_solid.service import jobs
    job_id, d = jobs.new_job()
    if in_path:
        # the part's own name: the STEP's product (the body's name in CAD) comes from it
        input_name = stem + os.path.splitext(in_path)[1].lower()
        shutil.copyfile(in_path, os.path.join(d, input_name))
        with jobs._lock:
            jobs._jobs[job_id]['input'] = input_name
    with open(os.path.join(d, 'mcp.json'), 'w') as f:
        json.dump({'stem': stem, 'out_dir': out_dir, 'delivered': None}, f)
    jobs.start(job_id, os.path.basename(in_path) if in_path else stem, params)
    return job_id


def _deliver(cfg, job_id):
    """Copy a finished job's files next to the part, once. Returns {kind: path}."""
    from stl_to_solid.service import jobs
    d = jobs.job_dir(job_id)
    meta_path = os.path.join(d, 'mcp.json')
    with _deliver_lock:                # two status calls must not copy twice
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return {}
        if meta.get('delivered') is not None:
            return meta['delivered']
        out = {}
        os.makedirs(meta['out_dir'], exist_ok=True)
        for name, target, kind in DELIVERABLES:
            src = os.path.join(d, name)
            if os.path.exists(src) and kind not in out:
                out[kind] = F.copy_fresh(src, meta['out_dir'], target.format(stem=meta['stem']))
        meta['delivered'] = out
        with open(meta_path, 'w') as f:
            json.dump(meta, f)
        return out


def _report(result):
    if not isinstance(result, dict):
        return {}
    return {k: v for k, v in result.items() if k not in _NOISE and k not in ('ok', 'error') and v is not None}


def job_state(cfg, job_id):
    """The job's state for a tool's answer, delivering files once it is done.
    None for a job this server has never heard of."""
    from stl_to_solid.service import jobs
    st = jobs.public_state(job_id)
    if st is None:
        return None
    lines = [ln for ln in (st.get('log') or '').splitlines() if ln.strip()]
    res = st.get('result') if isinstance(st.get('result'), dict) else None
    status = st['status']
    done = status in ('done', 'error', 'cancelled')
    ok = bool(res and res.get('ok')) and status == 'done'
    out = {'job_id': job_id, 'status': status, 'finished': done, 'ok': ok,
           'error': None if ok or not res else res.get('error'),
           'log_tail': lines[-LOG_TAIL:], 'files': {}, 'file_urls': {}, 'report': {}}
    if done and res:
        out['report'] = _report(res)
    if ok:
        # the report says all of it now, and the log's paths are scratch ones
        out['log_tail'] = []
        delivered = _deliver(cfg, job_id)
        out['files'] = {k: F.public(cfg, p) for k, p in delivered.items()}
        out['file_urls'] = {k: u for k, u in ((k, F.url_for(cfg, p)) for k, p in delivered.items()) if u}
    return out


def stop_all():
    """Cancel every job still going, when the server shuts down."""
    from stl_to_solid.service import jobs
    with jobs._lock:
        live = [j['id'] for j in jobs._jobs.values() if j.get('status') in ('queued', 'running')]
    for job_id in live:
        jobs.cancel(job_id)


