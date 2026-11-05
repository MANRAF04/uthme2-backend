"""Shared FCM plumbing for every push the notifier sends.

Runs only on the `notifications` Celery queue, on a worker that never brings up
the VPN. The tunnel is configured with `redirect-gateway def1`, so an FCM call
made while it is up would be routed through the university network.
"""

import json
import os
from typing import Iterable

from models import Device

FCM_BATCH_SIZE = 500
DEFAULT_LANGUAGE = "el"
_DEFAULT_CREDENTIALS_FILE = "/app/secrets/firebase-service-account.json"

_firebase_app = None


def language_of(locale: str | None) -> str:
    return (locale or DEFAULT_LANGUAGE).split("-")[0].split("_")[0].lower()


def group_by_language(rows) -> dict[str, list[str]]:
    """(fid, locale) rows -> {language: [fid, ...]}."""
    by_language: dict[str, list[str]] = {}
    for fid, locale in rows:
        by_language.setdefault(language_of(locale), []).append(fid)
    return by_language


def chunk(items: list, size: int = FCM_BATCH_SIZE) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def delete_devices(db, fids: list[str]) -> None:
    if not fids:
        return
    db.query(Device).filter(Device.fcm_token.in_(fids)).delete(
        synchronize_session=False
    )
    db.commit()
    print(f"[Push] Dropped {len(fids)} dead device(s).")


def send_multicast(
    fids: list[str],
    *,
    title: str,
    body: str,
    data: dict[str, str],
    channel_id: str,
    label: str,
) -> tuple[int, list[str]]:
    """Sends one multicast (max FCM_BATCH_SIZE). Returns (delivered, dead_fids)."""
    from firebase_admin import messaging

    message = messaging.MulticastMessage(
        fids=fids,
        notification=messaging.Notification(title=title, body=body),
        data=data,
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(channel_id=channel_id),
        ),
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
        ),
    )

    response = messaging.send_each_for_multicast(message, app=_get_firebase_app())

    dead = []
    rejected = []
    for fid, result in zip(fids, response.responses):
        if result.success:
            continue
        error = result.exception
        if _is_dead_device(error):
            dead.append(fid)
        elif _is_invalid_argument(error):
            rejected.append(fid)
        else:
            print(f"[Push] Send failed for one device on {label}: {error}")

    # A whole batch rejected as invalid means the message is malformed, not that
    # every phone vanished. Deleting on that would empty the device table.
    if rejected and len(rejected) < len(fids):
        dead.extend(rejected)
    elif rejected:
        print(
            f"[Push] Entire {label} batch rejected as invalid. "
            "Treating as a message-level fault and keeping the devices."
        )

    print(
        f"[Push] {label}: {response.success_count} sent, "
        f"{response.failure_count} failed, {len(dead)} dead."
    )
    return response.success_count, dead


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


def _is_dead_device(error) -> bool:
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
