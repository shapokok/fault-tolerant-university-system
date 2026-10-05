-- 50 students, 10 courses, every student takes 4 courses and has a grade in each.
INSERT INTO students (name, program)
SELECT 'Student ' || g, (ARRAY['CS', 'SE', 'IS'])[1 + g % 3]
FROM generate_series(1, 50) g;

INSERT INTO courses (code, title, day, start_time, room)
SELECT 'CS' || (100 + g), 'Course ' || g,
       (ARRAY['Mon', 'Tue', 'Wed', 'Thu', 'Fri'])[1 + g % 5],
       make_time(9 + g % 5, 0, 0), 'R' || (100 + g)
FROM generate_series(1, 10) g;

INSERT INTO registrations (student_id, course_id)
SELECT s, 1 + (s + k * 3) % 10
FROM generate_series(1, 50) s, generate_series(0, 3) k;

INSERT INTO grades (student_id, course_id, points)
SELECT student_id, course_id, 2 + (student_id * 7 + course_id) % 3
FROM registrations;
