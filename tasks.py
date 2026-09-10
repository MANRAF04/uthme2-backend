import os
import time
import subprocess
from celery import Celery
from celery.schedules import crontab
from dine_scraper import fetch_menus
from scraper import fetch_grades
from database import SessionLocal, engine
import models
import security
from models import Grade, User, Restaurant, MenuItem, RestaurantMealType
from datetime import datetime, timezone

models.Base.metadata.create_all(bind=engine)
models.ensure_schema_upgrades(engine)

# Connect Celery to the Redis container
app = Celery('grades_tasks', broker='redis://redis-grades:6379/0',backend='redis://redis-grades:6379/0')

# --- CELERY BEAT SCHEDULE ---
# This tells Celery to run the task every day at 4:00 AM.
app.conf.beat_schedule = {
    'daily-menu-scrape': {
        'task': 'tasks.sync_university_menus',
        'schedule': crontab(hour=4, minute=0), 
    },
}
app.conf.timezone = 'Europe/Athens'

@app.task(bind=True)
def sync_university_menus(self):
    """
    Opens VPN, uses YOUR bot credentials to scrape all menus, and saves them.
    """
    bot_user = os.getenv("BOT_USERNAME")
    bot_pass = os.getenv("BOT_PASSWORD")
    
    if not bot_user or not bot_pass:
        print("[Cron Error] BOT_USERNAME or BOT_PASSWORD missing from environment!")
        return "Failed: Missing Credentials"

    auth_file = "/tmp/bot_auth.txt"
    vpn_process = None

    try:
        security.write_secret_file(auth_file, f"{bot_user}\n{bot_pass}\n")

        print("[Cron] Starting VPN for daily menu scrape...")
        vpn_process = subprocess.Popen(
            ["openvpn", "--config", "uth.ovpn", "--auth-user-pass", auth_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Wait for tun0... (Use the exact same wait loop from your grade task)
        time.sleep(10) # Simplified for brevity, use your existing tun0 check loop

        print("[Cron] VPN Connected. Fetching menus...")
        scrape_result = fetch_menus(bot_user, bot_pass)
        
        if scrape_result.get("status") == "success":
            save_menus_to_db(
                scrape_result["restaurants"],
                scrape_result["meal_types"],
                scrape_result["menu_items"],
            )
            return "Menus updated successfully!"
            
    except Exception as e:
        print(f"[Cron Error] {e}")
    finally:
        if vpn_process:
            vpn_process.terminate()
        if os.path.exists(auth_file):
            os.remove(auth_file)

def save_menus_to_db(restaurants_data, meal_types_data, menu_items_data):
    db = SessionLocal()
    
    # 1. Save Restaurants FIRST
    try:
        for r in restaurants_data:
            existing_r = db.query(Restaurant).filter(Restaurant.id == r["id"]).first()
            if not existing_r:
                db.add(Restaurant(id=r["id"], code=r["code"], title=r["title"], city=r["city"]))
        db.commit()
        print(f"[DB] Successfully saved {len(restaurants_data)} restaurants.")
    except Exception as e:
        db.rollback()
        print(f"\n[DB CRASH - Restaurants] {e}\n")

    # 2. Save Meal Type Schedules
    try:
        active_ids = set()
        added_count = 0
        updated_count = 0

        for mt in meal_types_data:
            active_ids.add(mt["id"])
            existing_mt = db.query(RestaurantMealType).filter(RestaurantMealType.id == mt["id"]).first()

            if existing_mt:
                existing_mt.restaurant_id = mt["restaurant_id"]
                existing_mt.meal_type_id = mt["meal_type_id"]
                existing_mt.title = mt["title"]
                existing_mt.sort_order = mt["sort_order"]
                existing_mt.hour_from = mt["hour_from"]
                existing_mt.hour_to = mt["hour_to"]
                existing_mt.is_active = mt["is_active"]
                updated_count += 1
            else:
                db.add(RestaurantMealType(**mt))
                added_count += 1

        if active_ids:
            db.query(RestaurantMealType).filter(RestaurantMealType.id.notin_(active_ids)).update(
                {"is_active": False},
                synchronize_session=False,
            )

        db.commit()
        print(f"[DB] Successfully saved {added_count} meal types and updated {updated_count} existing ones.")
    except Exception as e:
        db.rollback()
        print(f"\n[DB CRASH - Meal Types] {e}\n")

    # 3. Save Menu Items
    try:
        seen_ids = set()
        added_count = 0
        updated_count = 0
        for m in menu_items_data:
            # Prevent duplicate ID crash!
            if m["id"] in seen_ids:
                continue
            seen_ids.add(m["id"])
            
            existing_m = db.query(MenuItem).filter(MenuItem.id == m["id"]).first()
            if existing_m:
                existing_m.restaurant_id = m["restaurant_id"]
                existing_m.date = m["date"]
                existing_m.meal_type_id = m["meal_type_id"]
                existing_m.meal_type = m["meal_type"]
                existing_m.meal_type_sort_order = m["meal_type_sort_order"]
                existing_m.meal_hour_from = m["meal_hour_from"]
                existing_m.meal_hour_to = m["meal_hour_to"]
                existing_m.course_type = m["course_type"]
                existing_m.course_sort_order = m["course_sort_order"]
                existing_m.item_sort_order = m["item_sort_order"]
                existing_m.food_name = m["food_name"]
                updated_count += 1
            else:
                db.add(MenuItem(**m))
                added_count += 1
                
        db.commit()
        print(f"[DB] Successfully saved {added_count} new menu items and updated {updated_count} existing ones.")
    except Exception as e:
        db.rollback()
        print(f"\n[DB CRASH - Menu Items] {e}\n")
    finally:
        db.close()

@app.task(bind=True, max_retries=3)
def sync_user_grades(self, user_id: int, username: str, encrypted_password: str):
    """
    Spins up OpenVPN, runs the scraper, saves to DB, and tears down the VPN.

    The password arrives encrypted (see security.encrypt_secret); it is decrypted
    here in memory and is never written to a durable store.
    """
    try:
        password = security.decrypt_secret(encrypted_password)
    except Exception:
        return {"status": "error", "message": "Server misconfiguration: credential decryption failed."}

    auth_file = f"/tmp/auth_{user_id}.txt"
    vpn_process = None

    try:
        # 1. Create the temporary auth file for OpenVPN (owner-readable only)
        security.write_secret_file(auth_file, f"{username}\n{password}\n")

        # 2. Start OpenVPN in the background
        # Note: In Docker we run as root, so no 'sudo' is needed.
        print(f"[Worker] Starting VPN for user {username}...")
        vpn_process = subprocess.Popen(
            ["openvpn", "--config", "uth.ovpn", "--auth-user-pass", auth_file],
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )

        # 3. Wait for the VPN network interface (tun0) to appear
        vpn_connected = False
        for _ in range(20):  # Wait up to 20 seconds
            if os.path.exists('/sys/class/net/tun0'):
                vpn_connected = True
                break
            time.sleep(1)

        if not vpn_connected:
            return {"status": "error", "message": "VPN Connection Timeout."}

        # Give the Linux routing table 2 seconds to update after tun0 appears
        time.sleep(2)
        print(f"[Worker] VPN Connected. Scraping grades for {username}...")

        # 4. Run your exact scraper function
        scrape_result = fetch_grades(username, password)

        if "error" in scrape_result:
            return {"status": "error", "message": scrape_result["error"]}

        # 5. Save the data to PostgreSQL
        save_to_database(user_id, scrape_result["data"], password)
        
        return {"status": "success", "message": f"Successfully synced {len(scrape_result['data'])} grades."}

    except Exception as e:
        return {"status": "error", "message": f"Worker crashed: {str(e)}"}

    finally:
        # 6. CRITICAL CLEANUP: Always kill the VPN and delete the password
        print(f"[Worker] Cleaning up VPN for {username}...")
        if vpn_process:
            vpn_process.terminate()
            vpn_process.wait() # Ensure it actually dies
        
        if os.path.exists(auth_file):
            os.remove(auth_file)


def save_to_database(user_id: int, parsed_grades: list, password: str):
    """
    Upserts (Updates or Inserts) grades into PostgreSQL.
    """
    db = SessionLocal()
    try:
        for item in parsed_grades:
            # Check if this exact grade already exists
            existing_grade = db.query(Grade).filter(Grade.id == item["id"]).first()
            
            if existing_grade:
                # Only stamp updated_at when the value truly changed (a new grade
                # appeared or a teacher fixed a mistake). Bumping it every scrape
                # would make "latest grade" meaningless.
                changed = False
                if existing_grade.grade != item["grade"]:
                    existing_grade.grade = item["grade"]
                    changed = True
                if existing_grade.passed != item["passed"]:
                    existing_grade.passed = item["passed"]
                    changed = True
                if changed:
                    existing_grade.updated_at = datetime.now(timezone.utc)
            else:
                # Insert a new grade record
                new_grade = Grade(
                    id=item["id"],
                    user_id=user_id,
                    subject_name=item["title"],
                    course_code=item["code"],
                    grade=item["grade"],
                    semester=item["semester"],
                    ects=item["ects"],
                    passed=item["passed"],
                    type=item["type"]
                )
                db.add(new_grade)
        
        # Update the user's last_updated timestamp
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.last_updated = datetime.utcnow()
            user.encrypted_password = security.hash_password(password)

        db.commit()
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()
