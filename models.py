from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, DateTime, Date, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    encrypted_password = Column(String, nullable=True)
    last_updated = Column(DateTime(timezone=True), onupdate=func.now(), default=func.now())
    preferred_restaurant_id = Column(String, nullable=True)

class Grade(Base):
    __tablename__ = "grades"

    id = Column(String, primary_key=True, index=True) # Using the university's UUID
    user_id = Column(Integer, ForeignKey("users.id"))
    subject_name = Column(String, nullable=False)
    course_code = Column(String, nullable=False)
    grade = Column(Float, nullable=True)
    semester = Column(Integer, nullable=True)
    ects = Column(Integer, nullable=True)
    passed = Column(Boolean, nullable=False)
    type = Column(String, nullable=True)
    # Set on insert, bumped only when the grade/passed value actually changes.
    # This is what ordering by "latest grade" relies on.
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class Restaurant(Base):
    __tablename__ = "restaurants"
    id = Column(String, primary_key=True, index=True)
    code = Column(String, nullable=False)
    title = Column(String, nullable=False)
    city = Column(String, nullable=True)

class MenuItem(Base):
    __tablename__ = "menu_items"
    id = Column(String, primary_key=True, index=True)
    restaurant_id = Column(String, ForeignKey("restaurants.id"), index=True)
    date = Column(Date, index=True, nullable=False)
    meal_type_id = Column(String, nullable=True)
    meal_type = Column(String, nullable=False)
    meal_type_sort_order = Column(Integer, nullable=True)
    meal_hour_from = Column(String, nullable=True)
    meal_hour_to = Column(String, nullable=True)
    course_type = Column(String, nullable=False)
    course_sort_order = Column(Integer, nullable=True)
    item_sort_order = Column(Integer, nullable=True)
    food_name = Column(String, nullable=False)


class RestaurantMealType(Base):
    __tablename__ = "restaurant_meal_types"
    id = Column(String, primary_key=True, index=True)
    restaurant_id = Column(String, ForeignKey("restaurants.id"), index=True)
    meal_type_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    sort_order = Column(Integer, nullable=True)
    hour_from = Column(String, nullable=True)
    hour_to = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)


def ensure_schema_upgrades(engine):
    """Adds missing columns/tables for incremental deployments without Alembic."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE grades ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_grades_user_updated "
                "ON grades (user_id, updated_at DESC)"
            )
        )
        conn.execute(text("ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS meal_type_id VARCHAR"))
        conn.execute(text("ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS meal_type_sort_order INTEGER"))
        conn.execute(text("ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS meal_hour_from VARCHAR"))
        conn.execute(text("ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS meal_hour_to VARCHAR"))
        conn.execute(text("ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS course_sort_order INTEGER"))
        conn.execute(text("ALTER TABLE menu_items ADD COLUMN IF NOT EXISTS item_sort_order INTEGER"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS restaurant_meal_types (
                    id VARCHAR PRIMARY KEY,
                    restaurant_id VARCHAR REFERENCES restaurants(id),
                    meal_type_id VARCHAR NOT NULL,
                    title VARCHAR NOT NULL,
                    sort_order INTEGER,
                    hour_from VARCHAR,
                    hour_to VARCHAR,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_restaurant_meal_types_restaurant_id "
                "ON restaurant_meal_types (restaurant_id)"
            )
        )
