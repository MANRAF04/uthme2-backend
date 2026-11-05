"""Push fan-out for newly graded courses.

When one student's scrape turns up a new mark on a course they had not passed,
every other student who has not passed that course code is told to open the app,
at most once per course per exam period. The grade value
itself never leaves the server: the payload names the course and nothing else.

Everything here runs on the `notifications` Celery queue, on a worker that never
brings up the VPN. The tunnel is configured with `redirect-gateway def1`, so an
FCM call made while it is up would be routed through the university network.
"""

import json
import os
from datetime import date
from typing import Iterable

from sqlalchemy import text

from database import SessionLocal
from models import Device

_FCM_BATCH_SIZE = 500
_DEFAULT_LOCALE = "el"
_DEFAULT_CREDENTIALS_FILE = "/app/secrets/firebase-service-account.json"

_firebase_app = None

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
    INSERT INTO course_period_notifications (course_code, exam_period)
    VALUES (:course_code, :exam_period)
    ON CONFLICT (course_code, exam_period) DO NOTHING
    RETURNING course_code
    """
)

_RECIPIENTS_SQL = text(
    """
    SELECT DISTINCT d.fcm_token, d.locale
    FROM devices d
    JOIN grades g ON g.user_id = d.user_id
    WHERE g.course_code = :course_code
      AND g.passed = false
      AND d.user_id <> :trigger_user_id
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


def _load_credentials():
    from firebase_admin import credentials

    path = os.getenv("FIREBASE_CREDENTIALS_FILE", _DEFAULT_CREDENTIALS_FILE)
    if os.path.exists(path):
        return credentials.Certificate(path)

    raw = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if raw:
        return credentials.Certificate(json.loads(raw))

    raise RuntimeError(
        f"No Firebase service account found. Mount one at {path} or set "
        "FIREBASE_CREDENTIALS_JSON to the contents of the JSON key."
    )


def _get_firebase_app():
    global _firebase_app
    if _firebase_app is None:
        import firebase_admin

        _firebase_app = firebase_admin.initialize_app(_load_credentials())
    return _firebase_app


def _localized(locale: str | None, course_title: str) -> tuple[str, str]:
    lang = (locale or _DEFAULT_LOCALE).split("-")[0].split("_")[0].lower()
    strings = _TEXTS.get(lang, _TEXTS[_DEFAULT_LOCALE])
    return strings["title"], strings["body"].format(course=course_title)


def _chunk(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _claim_and_load(db, course_code: str, trigger_user_id: int):
    """Claim this period's slot and read the waiting devices in one transaction.

    Returns (claimed, tokens_by_locale). When nobody is waiting the claim is
    rolled back, otherwise a course graded for a single student would burn its
    period without a push ever going out.
    """
    try:
        claimed = db.execute(
            _CLAIM_COURSE_SQL,
            {"course_code": course_code, "exam_period": current_exam_period()},
        ).first()
        if claimed is None:
            db.rollback()
            return False, {}

        rows = db.execute(
            _RECIPIENTS_SQL,
            {"course_code": course_code, "trigger_user_id": trigger_user_id},
        ).all()
        if not rows:
            db.rollback()
            return True, {}

        db.commit()
    except Exception:
        db.rollback()
        raise

    by_locale: dict[str, list[str]] = {}
    for fcm_token, locale in rows:
        lang = (locale or _DEFAULT_LOCALE).split("-")[0].split("_")[0].lower()
        by_locale.setdefault(lang, []).append(fcm_token)
    return True, by_locale


def _release_course(db, course_code: str) -> None:
    """Drop this period's slot so a failed fan-out can be retried."""
    try:
        db.execute(
            text(
                "DELETE FROM course_period_notifications "
                "WHERE course_code = :course_code AND exam_period = :exam_period"
            ),
            {"course_code": course_code, "exam_period": current_exam_period()},
        )
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[Notify] Could not release slot for {course_code}: {e}")


def _delete_tokens(db, tokens: list[str]) -> None:
    if not tokens:
        return
    db.query(Device).filter(Device.fcm_token.in_(tokens)).delete(
        synchronize_session=False
    )
    db.commit()
    print(f"[Notify] Dropped {len(tokens)} dead FCM tokens.")


def _send_batch(tokens: list[str], locale: str, course_code: str, course_title: str):
    """Sends one multicast. Returns (delivered_count, dead_tokens)."""
    from firebase_admin import messaging

    title, body = _localized(locale, course_title)
    message = messaging.MulticastMessage(
        fids=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={
            "type": "course_graded",
            "course_code": course_code,
            "course_title": course_title,
        },
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(channel_id="grades"),
        ),
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
        ),
    )

    response = messaging.send_each_for_multicast(message, app=_get_firebase_app())

    dead = []
    rejected = []
    for token, result in zip(tokens, response.responses):
        if result.success:
            continue
        error = result.exception
        if _is_dead_token(error):
            dead.append(token)
        elif _is_invalid_argument(error):
            rejected.append(token)
        else:
            print(f"[Notify] Send failed for one token on {course_code}: {error}")

    # A whole batch rejected as invalid means the message is malformed, not that
    # every phone vanished. Deleting on that would empty the device table.
    if rejected and len(rejected) < len(tokens):
        dead.extend(rejected)
    elif rejected:
        print(
            f"[Notify] Entire {course_code} ({locale}) batch rejected as invalid. "
            "Treating as a message-level fault and keeping the tokens."
        )

    print(
        f"[Notify] {course_code} ({locale}): {response.success_count} sent, "
        f"{response.failure_count} failed, {len(dead)} dead."
    )
    return response.success_count, dead


def _is_dead_token(error) -> bool:
    from firebase_admin import exceptions, messaging

    return isinstance(
        error,
        (
            messaging.UnregisteredError,
            messaging.SenderIdMismatchError,
            exceptions.NotFoundError,
        ),
    )


def _is_invalid_argument(error) -> bool:
    from firebase_admin import exceptions

    return isinstance(error, exceptions.InvalidArgumentError)


def notify_course_graded(trigger_user_id: int, courses: list[dict]) -> dict:
    """Fan out one push per course to everyone still waiting on it."""
    notified = {}
    skipped = []

    db = SessionLocal()
    try:
        for course in courses:
            course_code = course.get("code")
            course_title = course.get("title") or course_code
            if not course_code:
                continue

            claimed, tokens_by_locale = _claim_and_load(db, course_code, trigger_user_id)
            if not claimed:
                skipped.append(course_code)
                continue
            if not tokens_by_locale:
                notified[course_code] = 0
                continue

            delivered = 0
            dead: list[str] = []
            try:
                for locale, tokens in tokens_by_locale.items():
                    for batch in _chunk(tokens, _FCM_BATCH_SIZE):
                        batch_delivered, batch_dead = _send_batch(
                            batch, locale, course_code, course_title
                        )
                        delivered += batch_delivered
                        dead.extend(batch_dead)
            except Exception:
                if delivered == 0:
                    _release_course(db, course_code)
                raise

            _delete_tokens(db, dead)
            notified[course_code] = delivered
    finally:
        db.close()

    return {"notified": notified, "skipped_already_notified": skipped}
