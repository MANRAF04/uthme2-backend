from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Import our custom modules
import models
import security
from database import SessionLocal, engine
from tasks import sync_user_grades, app as celery_app
from celery.result import AsyncResult

# Create database tables if they don't exist yet
models.Base.metadata.create_all(bind=engine)
models.ensure_schema_upgrades(engine)

app = FastAPI()

ATHENS_TZ = ZoneInfo("Europe/Athens")
UTC_TZ = ZoneInfo("UTC")

def serialize_grade(g):
    return {
        "id": g.id,
        "title": g.subject_name,
        "code": g.course_code,
        "semester": g.semester,
        "ects": g.ects,
        "grade": g.grade,
        "passed": g.passed,
        "type": g.type,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
    }

# Dependency to get the DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Define what Flutter sends us
class LoginRequest(BaseModel):
    username: str
    password: str
    force_refresh: Optional[bool] = False  # Flutter can set this to True for "Pull to Refresh"

class PreferenceRequest(BaseModel):
    username: str
    password: str
    restaurant_id: str

@app.get("/api/restaurants")
def get_restaurants(db: Session = Depends(get_db)):
    """Returns the list of all available restaurants."""
    restaurants = db.query(models.Restaurant).all()
    return {"status": "success", "data": [{"id": r.id, "title": r.title, "city": r.city} for r in restaurants]}

@app.get("/api/restaurants/{restaurant_id}/menu")
def get_restaurant_menu(
    restaurant_id: str, 
    start: Optional[str] = None, 
    end: Optional[str] = None, 
    db: Session = Depends(get_db)
):
    """Returns menu items for a specific week. Defaults to the current week."""
    
    today = datetime.now(ATHENS_TZ).date()
    
    # --- SMART PAGINATION LOGIC ---
    if not start or not end:
        # If no dates provided, snap to the current week (Monday to Sunday)
        # Python's weekday() is 0 for Monday, 6 for Sunday
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
    else:
        # If Flutter asks for a specific week, parse those dates safely
        try:
            monday = datetime.strptime(start, "%Y-%m-%d").date()
            sunday = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    # ------------------------------

    meal_types = (
        db.query(models.RestaurantMealType)
        .filter(models.RestaurantMealType.restaurant_id == restaurant_id)
        .order_by(
            models.RestaurantMealType.sort_order,
            models.RestaurantMealType.title,
        )
        .all()
    )

    meal_type_by_id = {m.meal_type_id: m for m in meal_types}

    menu_items = db.query(models.MenuItem)\
                   .filter(models.MenuItem.restaurant_id == restaurant_id)\
                   .filter(models.MenuItem.date >= monday)\
                   .filter(models.MenuItem.date <= sunday)\
                   .order_by(
                        models.MenuItem.date,
                        models.MenuItem.meal_type_sort_order,
                        models.MenuItem.item_sort_order,
                        models.MenuItem.course_sort_order,
                        models.MenuItem.meal_type,
                        models.MenuItem.food_name,
                    )\
                   .all()
                   
    data = [{
        "id": m.id,
        "date": m.date.isoformat(),
        "meal_type_id": m.meal_type_id,
        "meal_type": m.meal_type,
        "meal_type_sort_order": m.meal_type_sort_order if m.meal_type_sort_order is not None else (
            meal_type_by_id[m.meal_type_id].sort_order if m.meal_type_id in meal_type_by_id else None
        ),
        "meal_hour_from": m.meal_hour_from if m.meal_hour_from else (
            meal_type_by_id[m.meal_type_id].hour_from if m.meal_type_id in meal_type_by_id else None
        ),
        "meal_hour_to": m.meal_hour_to if m.meal_hour_to else (
            meal_type_by_id[m.meal_type_id].hour_to if m.meal_type_id in meal_type_by_id else None
        ),
        "course_type": m.course_type,
        "course_sort_order": m.course_sort_order,
        "item_sort_order": m.item_sort_order,
        "food_name": m.food_name
    } for m in menu_items]

    meal_type_data = [
        {
            "id": m.id,
            "meal_type_id": m.meal_type_id,
            "title": m.title,
            "sort_order": m.sort_order,
            "hour_from": m.hour_from,
            "hour_to": m.hour_to,
            "is_active": m.is_active,
        }
        for m in meal_types
    ]
    
    return {
        "status": "success", 
        # Send back the exact range so Flutter knows what week it is currently looking at!
        "range": {"start": monday.isoformat(), "end": sunday.isoformat()}, 
        "meal_types": meal_type_data,
        "data": data
    }

@app.post("/api/user/preference")
def set_user_preference(req: PreferenceRequest, db: Session = Depends(get_db)):
    """Allows the Flutter app to save the user's preferred restaurant."""
    user = db.query(models.User).filter(models.User.username == req.username).first()

    if not user or not security.verify_password(req.password, user.encrypted_password):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Upgrade legacy/unsalted hashes now that we know the password is correct.
    if security.needs_rehash(user.encrypted_password):
        user.encrypted_password = security.hash_password(req.password)

    user.preferred_restaurant_id = req.restaurant_id
    db.commit()
    
    return {"status": "success", "message": "Preference saved"}

@app.post("/api/get-grades")
def get_grades_endpoint(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == req.username).first()
    
    if not user:
        user = models.User(username=req.username)
        db.add(user)
        db.commit()
        db.refresh(user)

    # Check the incoming password against the salted hash on file.
    password_matches = security.verify_password(req.password, user.encrypted_password)

    # Upgrade legacy/unsalted hashes once we have confirmed the password.
    if password_matches and security.needs_rehash(user.encrypted_password):
        user.encrypted_password = security.hash_password(req.password)
        db.commit()

    cached_grades = db.query(models.Grade).filter(models.Grade.user_id == user.id).all()
    
    if cached_grades and not req.force_refresh and password_matches:
        data_list = [serialize_grade(g) for g in cached_grades]

        return {
            "status": "success",
            "source": "database",
            "data": data_list
        }
    else:
        # The plaintext password is encrypted before it touches the broker; the
        # worker decrypts it in memory and never persists it.
        task = sync_user_grades.delay(
            user.id, req.username, security.encrypt_secret(req.password)
        )
        return {
            "status": "processing",
            "source": "celery_queue",
            "task_id": task.id,
            "message": "Grades are being fetched from the university. Please poll the task status."
        }

@app.get("/api/task-status/{task_id}")
def get_task_status(task_id: str):
    task_result = AsyncResult(task_id, app=celery_app)
    
    if task_result.state == 'PENDING':
        return {"status": "processing", "message": "Waiting in queue..."}
    elif task_result.state == 'STARTED':
        return {"status": "processing", "message": "VPN connected, scraping in progress..."}
    elif task_result.state == 'SUCCESS':
        # --- THE FIX ---
        # Look inside the result. If our scraper explicitly returned an error, 
        # tell Flutter it failed so it stops polling!
        res = task_result.result
        if isinstance(res, dict) and res.get("status") == "error":
            return {"status": "failed", "message": res.get("message")}
            
        return {"status": "completed", "result": res}
        # ---------------
    elif task_result.state == 'FAILURE':
        return {"status": "failed", "message": str(task_result.info)}
    else:
        return {"status": task_result.state}
