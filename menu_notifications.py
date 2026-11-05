"""Push the main courses of the user's preferred restaurant two hours before a meal.

Celery Beat runs this every few minutes. Each (restaurant, day, meal) pushes at
most once, through the menu_notifications ledger. A meal is announced on the
first run inside [start - 2h, start), so a missed tick only delays the push.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

import push
from database import SessionLocal
from models import MenuItem

ATHENS_TZ = ZoneInfo("Europe/Athens")
LEAD_TIME = timedelta(hours=2)

MAIN_COURSE = "ΚΥΡΙΩΣ"
DESSERT = "ΕΠΙΔΟΡΠΙΟ"
SWEET_DESSERT = "γλυκό"

# Fallback start times are the most common hours in the scraped data, used when
# the portal leaves a meal's hours blank.
_MEALS = {
    "Μεσημεριανό": {"key": "lunch", "fallback_start": time(13, 0)},
    "Βραδινό": {"key": "dinner", "fallback_start": time(19, 30)},
}

_TEXTS = {
    "el": {
        "lunch": "Μεσημεριανό",
        "dinner": "Βραδινό",
        "title": "{meal} στις {start}",
        "title_sweet": "{meal} στις {start}, έχει Γλυκό!",
    },
    "en": {
        "lunch": "Lunch",
        "dinner": "Dinner",
        "title": "{meal} at {start}",
        "title_sweet": "{meal} at {start}, with sweet dessert!",
    },
}

_SUBSCRIBED_RESTAURANTS_SQL = text(
    """
    SELECT DISTINCT u.preferred_restaurant_id
    FROM users u
    JOIN devices d ON d.user_id = u.id
    WHERE u.preferred_restaurant_id IS NOT NULL
      AND u.menu_notifications_enabled
    """
)

# ON CONFLICT DO NOTHING still takes the row lock, so two notifiers racing on
# the same meal cannot both send.
_CLAIM_MEAL_SQL = text(
    """
    INSERT INTO menu_notifications (restaurant_id, date, meal_type)
    VALUES (:restaurant_id, :date, :meal_type)
    ON CONFLICT (restaurant_id, date, meal_type) DO NOTHING
    RETURNING restaurant_id
    """
)

_RELEASE_MEAL_SQL = text(
    """
    DELETE FROM menu_notifications
    WHERE restaurant_id = :restaurant_id AND date = :date AND meal_type = :meal_type
    """
)

_RECIPIENTS_SQL = text(
    """
    SELECT DISTINCT d.fcm_token, d.locale
    FROM devices d
    JOIN users u ON u.id = d.user_id
    WHERE u.preferred_restaurant_id = :restaurant_id
      AND u.menu_notifications_enabled
    """
)


@dataclass
class UpcomingMeal:
    restaurant_id: str
    meal_type: str
    starts_at: datetime
    main_courses: list[str] = field(default_factory=list)
    has_sweet_dessert: bool = False

    @property
    def slot(self) -> dict:
        return {
            "restaurant_id": self.restaurant_id,
            "date": self.starts_at.date(),
            "meal_type": self.meal_type,
        }


def send_upcoming_menu_notifications(now: datetime | None = None) -> dict:
    now = now or datetime.now(ATHENS_TZ)
    sent = {}

    db = SessionLocal()
    try:
        restaurant_ids = [row[0] for row in db.execute(_SUBSCRIBED_RESTAURANTS_SQL)]
        if not restaurant_ids:
            return {"sent": sent}

        for meal in _load_meals(db, restaurant_ids, now):
            if not (meal.starts_at - LEAD_TIME <= now < meal.starts_at):
                continue
            if not meal.main_courses:
                continue

            label = f"{meal.restaurant_id} {meal.slot['date']} {meal.meal_type}"
            try:
                sent[label] = _notify_meal(db, meal, label)
            except Exception as e:
                print(f"[MenuPush] Failed for {label}: {e}")
    finally:
        db.close()

    return {"sent": sent}


def _load_meals(db, restaurant_ids: list[str], now: datetime) -> list[UpcomingMeal]:
    items = (
        db.query(MenuItem)
        .filter(MenuItem.restaurant_id.in_(restaurant_ids))
        .filter(MenuItem.date == now.date())
        .filter(MenuItem.meal_type.in_(_MEALS.keys()))
        .order_by(MenuItem.course_sort_order, MenuItem.item_sort_order)
        .all()
    )

    meals: dict[tuple[str, str], UpcomingMeal] = {}
    for item in items:
        key = (item.restaurant_id, item.meal_type)
        meal = meals.get(key)
        if meal is None:
            meal = UpcomingMeal(
                restaurant_id=item.restaurant_id,
                meal_type=item.meal_type,
                starts_at=_meal_start(item, now),
            )
            meals[key] = meal

        food_name = item.food_name.strip()
        if item.course_type == MAIN_COURSE and food_name not in meal.main_courses:
            meal.main_courses.append(food_name)
        elif item.course_type == DESSERT and food_name.lower() == SWEET_DESSERT:
            meal.has_sweet_dessert = True

    return list(meals.values())


def _meal_start(item: MenuItem, now: datetime) -> datetime:
    start = _parse_hour(item.meal_hour_from) or _MEALS[item.meal_type]["fallback_start"]
    return datetime.combine(now.date(), start, tzinfo=ATHENS_TZ)


def _parse_hour(raw: str | None) -> time | None:
    if not raw:
        return None
    try:
        hours, minutes = raw.strip().split(":")[:2]
        return time(int(hours), int(minutes))
    except ValueError:
        return None


def _notify_meal(db, meal: UpcomingMeal, label: str) -> int:
    fids_by_language = _claim_and_load(db, meal)
    if not fids_by_language:
        return 0

    delivered = 0
    dead: list[str] = []
    try:
        for language, fids in fids_by_language.items():
            title = _title(language, meal)
            body = ", ".join(meal.main_courses)
            for batch in push.chunk(fids):
                batch_delivered, batch_dead = push.send_multicast(
                    batch,
                    title=title,
                    body=body,
                    data={
                        "type": "menu_upcoming",
                        "restaurant_id": meal.restaurant_id,
                        "meal_type": meal.meal_type,
                    },
                    channel_id="menus",
                    label=f"{label} ({language})",
                )
                delivered += batch_delivered
                dead.extend(batch_dead)
    except Exception:
        if delivered == 0:
            _release_meal(db, meal)
        raise

    push.delete_devices(db, dead)
    return delivered


def _claim_and_load(db, meal: UpcomingMeal) -> dict[str, list[str]]:
    """Claim the meal's slot and read its recipients in one transaction.

    An already claimed meal, or one nobody is subscribed to, returns {}. The
    latter rolls the claim back so a student who picks this restaurant before
    the meal starts still gets the push.
    """
    try:
        if db.execute(_CLAIM_MEAL_SQL, meal.slot).first() is None:
            db.rollback()
            return {}

        rows = db.execute(
            _RECIPIENTS_SQL, {"restaurant_id": meal.restaurant_id}
        ).all()
        if not rows:
            db.rollback()
            return {}

        db.commit()
    except Exception:
        db.rollback()
        raise

    return push.group_by_language(rows)


def _release_meal(db, meal: UpcomingMeal) -> None:
    """Drop the slot so the next Beat tick can retry a failed send."""
    try:
        db.execute(_RELEASE_MEAL_SQL, meal.slot)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[MenuPush] Could not release slot for {meal.restaurant_id}: {e}")


def _title(language: str, meal: UpcomingMeal) -> str:
    strings = _TEXTS.get(language, _TEXTS[push.DEFAULT_LANGUAGE])
    template = strings["title_sweet"] if meal.has_sweet_dessert else strings["title"]
    return template.format(
        meal=strings[_MEALS[meal.meal_type]["key"]],
        start=meal.starts_at.strftime("%H:%M"),
    )
