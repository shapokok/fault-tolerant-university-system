"""transcript-service: builds transcripts from student-service data plus grades.

Also batch generation for all students, with checkpoints in FT mode.
"""
import asyncio
import logging
import os

import httpx
from fastapi import Depends, HTTPException, Path, Request

import common
import ft
import obs
import security

STUDENT_URL = os.getenv("STUDENT_URL", "http://student-service:8000")
CHECKPOINT_EVERY = 10   # students per checkpoint
STEP_DELAY_S = 0.2      # per student, so a batch takes ~10 s and can be crashed midway
STALE_JOB_S = 15        # a running job with no checkpoint for this long is resumed by someone else

student_breaker = ft.CircuitBreaker("student-service")
# The dependency check uses a health route of the gateway (no login needed, so no 401 noise).
app = common.create_app(
    "transcript-service", deps={"student-service": f"{STUDENT_URL}/health/student-service"}, breakers=[student_breaker])
SID = Path(ge=1, le=security.N_STUDENTS) if security.ON else Path()
JOB = Path(ge=1, le=2_147_483_647) if security.ON else Path()

last_good = {}  # student_id -> last full transcript (FT cache)


async def fetch_student(sid, auth):
    """auth: the caller's Authorization header, passed on to student-service."""
    async def call():
        async with common.http_client(auth=auth) as client:
            r = await client.get(f"{STUDENT_URL}/students/{sid}")
        if r.status_code >= 500:
            r.raise_for_status()
        return r

    if not common.FT_MODE:
        return await call()
    return await student_breaker.call(lambda: ft.retry(call, ft.is_transient_http))


async def grades_of(sid):
    return await common.fetch(
        """SELECT c.code, c.title, g.points
           FROM grades g JOIN courses c ON c.id = g.course_id
           WHERE g.student_id = $1 ORDER BY c.code""",
        sid,
    )


def gpa_of(grades):
    return round(sum(g["points"] for g in grades) / len(grades), 2) if grades else None


@app.get("/transcripts/{sid}")
async def transcript(request: Request, sid: int = SID, user=Depends(security.current_user)):
    await security.check_student(request, user, sid)
    return await build_transcript(sid, request.headers.get("authorization"))


async def build_transcript(sid, auth):
    try:
        r = await fetch_student(sid, auth)
    except (httpx.HTTPError, ft.CircuitOpen):
        if not common.FT_MODE:
            raise
        # Graceful degradation: student-service is down. Cached copy, else grades only.
        if sid in last_good:
            return {**last_good[sid], "partial": True, "source": "cache"}
        grades = await grades_of(sid)
        return {"student": {"id": sid}, "courses": [dict(g) for g in grades],
                "gpa": gpa_of(grades), "partial": True, "source": "db"}
    if r.status_code in (401, 403, 404):
        raise HTTPException(r.status_code, r.json().get("detail", "student-service refused"))

    grades = await grades_of(sid)
    result = {"student": r.json(), "courses": [dict(g) for g in grades],
              "gpa": gpa_of(grades), "partial": False, "source": "live"}
    last_good[sid] = result
    return result


# ---------- batch generation with checkpointing ----------

async def save(job_id, done, last_sid, finished=False):
    """One transaction: transcripts + checkpoint (last_student_id). Either both are saved or neither."""
    async with common.transaction() as conn:
        await conn.executemany(
            "INSERT INTO transcripts (job_id, student_id, gpa, partial) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT DO NOTHING", done)
        await conn.execute(
            "UPDATE transcript_jobs SET last_student_id = GREATEST(last_student_id, $2), heartbeat = now(), "
            "status = CASE WHEN $3 THEN 'done' ELSE status END, "
            "finished_at = CASE WHEN $3 THEN now() ELSE finished_at END WHERE id = $1",
            job_id, last_sid, finished)


async def run_job(job_id):
    # FT: continue after the last checkpoint. Baseline: always from the start.
    start = await common.fetchval("SELECT last_student_id FROM transcript_jobs WHERE id = $1", job_id)
    obs.log("batch_start", f"job {job_id}: starting after student {start}", job_id=job_id)
    ids = [r["id"] for r in await common.fetch("SELECT id FROM students WHERE id > $1 ORDER BY id", start)]
    done = []
    for sid in ids:
        # A job, not a user: a fresh short-lived service token per call (a job can outlive one token).
        t = await build_transcript(sid, security.service_auth("transcript-service") if security.ON else None)
        done.append((job_id, sid, t["gpa"], t["partial"]))
        await asyncio.sleep(STEP_DELAY_S)
        if common.FT_MODE and len(done) == CHECKPOINT_EVERY:
            await save(job_id, done, sid)
            obs.log("batch_checkpoint", f"job {job_id}: checkpoint at student {sid}", job_id=job_id)
            done = []
    await save(job_id, done, ids[-1] if ids else start, finished=True)
    obs.log("batch_done", f"job {job_id}: done", job_id=job_id)


@app.post("/transcripts/batch", status_code=202)
async def start_batch(request: Request, user=Depends(security.current_user)):
    await security.check_admin(request, user)
    job_id = await common.fetchval("INSERT INTO transcript_jobs DEFAULT VALUES RETURNING id")
    asyncio.create_task(run_job(job_id))
    await security.audit(request, "batch_transcripts", f"job {job_id}", "started")
    return {"job_id": job_id, "status": "running", "instance": common.INSTANCE}


@app.get("/transcripts/batch/{job_id}")
async def batch_status(request: Request, job_id: int = JOB, user=Depends(security.current_user)):
    await security.check_admin(request, user)
    row = await common.fetchrow(
        "SELECT j.*, (SELECT count(*) FROM transcripts t WHERE t.job_id = j.id) AS transcripts "
        "FROM transcript_jobs j WHERE id = $1", job_id)
    if not row:
        raise HTTPException(404, "job not found")
    return dict(row)


async def resume_loop():
    """Find jobs whose runner died (no checkpoint for STALE_JOB_S) and continue them here."""
    while True:
        await asyncio.sleep(5)
        try:
            job_id = await common.fetchval(
                "UPDATE transcript_jobs SET heartbeat = now() WHERE id = ("
                "  SELECT id FROM transcript_jobs WHERE status = 'running' "
                "  AND heartbeat < now() - make_interval(secs => $1) "
                "  ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id", float(STALE_JOB_S))
            if job_id:
                obs.log("batch_resume", f"job {job_id}: runner died, resuming from checkpoint",
                        level=logging.WARNING, job_id=job_id)
                await run_job(job_id)
        except Exception as e:
            obs.log("batch_resume_failed", f"resume loop error: {e!r}", level=logging.WARNING)


@app.on_event("startup")
async def ft_startup():
    if common.FT_MODE:
        asyncio.create_task(resume_loop())
