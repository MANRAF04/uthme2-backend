"""Push fan-out for newly graded courses.

When one student's scrape turns up a new mark on a course they had not passed,
every other student in the same department who has not passed that course code is
told to open the app, at most once per course per department per exam period.
Course codes are only unique within a department. A student whose department is
not known yet (no sync since it started being stored) matches any department. The grade value
itself never leaves the server: the payload names the course and nothing else.
"""

from datetime import date

from sqlalchemy import text

import push
from database import SessionLocal

_TEXTS = {
    "el": {
        "title": "Νέα βαθμολογία",
        "body": "Το μάθημα {course} βαθμολογήθηκε. Άνοιξε την εφαρμογή για να δεις τον βαθμό σου.",
    },
    "en": {
        "title": "New grade posted",
        "body": "{course} has been graded. Open the app to see your grade.",
    },
}

# ON CONFLICT DO NOTHING still takes the row lock, so a second notifier racing on
# the same course and period blocks until the first commits or rolls back.
_CLAIM_COURSE_SQL = text(
    """
    INSERT INTO course_period_notifications (course_code, department_code, exam_period)
    VALUES (:course_code, :department_code, :exam_period)
    ON CONFLICT (course_code, department_code, exam_period) DO NOTHING
    RETURNING course_code
    """
)

_RECIPIENTS_SQL = text(
    """
    SELECT DISTINCT d.fcm_token, d.locale
    FROM devices d
    JOIN grades g ON g.user_id = d.user_id
    JOIN users u ON u.id = d.user_id
    WHERE g.course_code = :course_code
      AND g.passed = false
      AND d.user_id <> :trigger_user_id
      AND (
          CAST(:department_code AS VARCHAR) IS NULL
          OR u.department_code IS NULL
          OR u.department_code = :department_code
      )
    """
)


def current_exam_period(today: date | None = None) -> str:
    """Buckets the date into the exam period whose results are being published.

    The portal exposes no period field, so this follows the academic calendar:
    winter results land Dec to Apr, spring results May to Aug, September
    resits Sep to Nov. December counts toward the following year's winter.
    """
    today = today or date.today()
    if today.month == 12:
        return f"{today.year + 1}-winter"
    if today.month <= 4:
        return f"{today.year}-winter"
    if today.month <= 8:
        return f"{today.year}-spring"
    return f"{today.year}-september"


def _localized(language: str, course_title: str) -> tuple[str, str]:
    strings = _TEXTS.get(language, _TEXTS[push.DEFAULT_LANGUAGE])
    return strings["title"], strings["body"].format(course=course_title)


def _department_of(db, user_id: int) -> str | None:
    return db.execute(
        text("SELECT department_code FROM users WHERE id = :user_id"), {"user_id": user_id}
    ).scalar()


def _claim_key(course_code: str, department_code: str | None) -> dict:
    return {
        "course_code": course_code,
        "department_code": department_code or "",
        "exam_period": current_exam_period(),
    }


def _claim_and_load(db, course_code: str, department_code: str | None, trigger_user_id: int):
    """Claim this period's slot and read the waiting devices in one transaction.

    Returns (claimed, fids_by_language). When nobody is waiting the claim is
    rolled back, otherwise a course graded for a single student would burn its
    period without a push ever going out.
    """
    try:
        claimed = db.execute(_CLAIM_COURSE_SQL, _claim_key(course_code, department_code)).first()
        if claimed is None:
            db.rollback()
            return False, {}

        rows = db.execute(
            _RECIPIENTS_SQL,
            {
                "course_code": course_code,
                "department_code": department_code,
                "trigger_user_id": trigger_user_id,
            },
        ).all()
        if not rows:
            db.rollback()
            return True, {}

        db.commit()
    except Exception:
        db.rollback()
        raise

    return True, push.group_by_language(rows)


def _release_course(db, course_code: str, department_code: str | None) -> None:
    """Drop this period's slot so a failed fan-out can be retried."""
    try:
        db.execute(
            text(
                "DELETE FROM course_period_notifications "
                "WHERE course_code = :course_code "
                "AND department_code = :department_code "
                "AND exam_period = :exam_period"
            ),
            _claim_key(course_code, department_code),
        )
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[Notify] Could not release slot for {course_code}: {e}")


def notify_course_graded(trigger_user_id: int, courses: list[dict]) -> dict:
    """Fan out one push per course to everyone still waiting on it."""
    notified = {}
    skipped = []

    db = SessionLocal()
    try:
        department_code = _department_of(db, trigger_user_id)
        for course in courses:
            course_code = course.get("code")
            course_title = course.get("title") or course_code
            if not course_code:
                continue

            claimed, fids_by_language = _claim_and_load(
                db, course_code, department_code, trigger_user_id
            )
            if not claimed:
                skipped.append(course_code)
                continue
            if not fids_by_language:
                notified[course_code] = 0
                continue

            delivered = 0
            dead: list[str] = []
            try:
                for language, fids in fids_by_language.items():
                    title, body = _localized(language, course_title)
                    for batch in push.chunk(fids):
                        batch_delivered, batch_dead = push.send_multicast(
                            batch,
                            title=title,
                            body=body,
                            data={
                                "type": "course_graded",
                                "course_code": course_code,
                                "course_title": course_title,
                            },
                            channel_id="grades",
                            label=f"{course_code} ({language})",
                        )
                        delivered += batch_delivered
                        dead.extend(batch_dead)
            except Exception:
                if delivered == 0:
                    _release_course(db, course_code, department_code)
                raise

            push.delete_devices(db, dead)
            notified[course_code] = delivered
    finally:
        db.close()

    return {"notified": notified, "skipped_already_notified": skipped}
