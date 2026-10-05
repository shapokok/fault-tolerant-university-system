"""student-service: students, course registration, timetable (with cache)."""
from fastapi import HTTPException
from pydantic import BaseModel

import common

app = common.create_app("student-service")

timetable_cache = {}  # student_id -> classes, last value read from the DB (FT mode)


class Registration(BaseModel):
    student_id: int
    course_id: int


@app.get("/students/{sid}")
async def get_student(sid: int):
    row = await common.fetchrow("SELECT id, name, program FROM students WHERE id = $1", sid)
    if not row:
        raise HTTPException(404, "student not found")
    return dict(row)


@app.post("/registrations", status_code=201)
async def register(r: Registration):
    await common.execute(
        "INSERT INTO registrations (student_id, course_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        r.student_id, r.course_id,
    )
    timetable_cache.pop(r.student_id, None)  # timetable changed, do not serve the old one
    return {"student_id": r.student_id, "course_id": r.course_id, "status": "registered"}


@app.get("/timetable/{sid}")
async def timetable(sid: int):
    try:
        rows = await common.fetch(
            """SELECT c.code, c.title, c.day, c.start_time, c.room
               FROM registrations r JOIN courses c ON c.id = r.course_id
               WHERE r.student_id = $1 ORDER BY array_position(ARRAY['Mon','Tue','Wed','Thu','Fri'], c.day), c.start_time""",
            sid,
        )
    except common.CONN_ERRORS:
        # Graceful degradation: DB unreachable, serve the last known timetable.
        if common.FT_MODE and sid in timetable_cache:
            return {"student_id": sid, "source": "cache", "classes": timetable_cache[sid]}
        raise
    classes = [dict(r) for r in rows]
    timetable_cache[sid] = classes
    return {"student_id": sid, "source": "db", "classes": classes}
