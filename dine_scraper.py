import requests
from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo


ATHENS_TZ = ZoneInfo("Europe/Athens")
UTC_TZ = ZoneInfo("UTC")


def epoch_ms_to_athens_date(epoch_ms):
    """Converts a Unix epoch timestamp in milliseconds to a calendar date in Athens."""
    raw_value = int(epoch_ms)

    # Guard for APIs that sometimes return seconds instead of milliseconds.
    if abs(raw_value) < 100_000_000_000:
        raw_value *= 1000

    dt_utc = datetime.fromtimestamp(raw_value / 1000.0, tz=UTC_TZ)
    dt_athens = dt_utc.astimezone(ATHENS_TZ)

    print(
        "[Scraper] Timestamp conversion "
        f"raw={raw_value} utc={dt_utc.isoformat()} athens={dt_athens.isoformat()} "
        f"utc_date={dt_utc.date()} athens_date={dt_athens.date()}"
    )

    return dt_athens.date()

def fetch_menus(username, password):
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    
    try:
        # 1. CAS Login
        print("[Scraper] Fetching CAS login page...")
        login_url = "https://cas.uth.gr/login?service=https%3A%2F%2Fsw-dine.uth.gr%2Flogin%2Fcas"
        login_page = session.get(login_url, timeout=10)
        soup = BeautifulSoup(login_page.text, 'html.parser')
        
        execution_val = soup.find('input', {'name': 'execution'})['value']
        lt_val = soup.find('input', {'name': 'lt'})['value']
        
        payload = {
            'username': username,
            'password': password,
            'execution': execution_val,
            'lt': lt_val,
            '_eventId': 'submit',
            'submitForm': 'Login'
        }
        
        print("[Scraper] Submitting CAS login...")
        session.post(login_url, data=payload, timeout=10)
        
        # 2. Get the CSRF Token from the dashboard HTML
        print("[Scraper] Accessing dashboard for CSRF token...")
        dash_response = session.get("https://sw-dine.uth.gr/student/restaurant/", timeout=10)
        
        if "cas.uth.gr" in dash_response.url:
            raise Exception("CAS Login Failed! Check BOT_PASSWORD.")

        dash_soup = BeautifulSoup(dash_response.text, 'html.parser')
        csrf_token_tag = dash_soup.find('meta', {'name': '_csrf'})
        csrf_token = csrf_token_tag['content'] if csrf_token_tag else ""
        
        # We need the CSRF token to ask the API for the Profile token
        session.headers.update({
            'X-CSRF-TOKEN': csrf_token,
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest'
        })
        
        # 3. Call the Profiles API (Your brilliant discovery!)
        print("[Scraper] Calling Profiles API for X-Profile token...")
        profile_response = session.get("https://sw-dine.uth.gr/api/person/profiles", timeout=10)
        profile_data = profile_response.json()
        
        profile_token = ""
        # Extract from studentProfiles (or teacherProfiles just in case)
        if profile_data.get("studentProfiles") and len(profile_data["studentProfiles"]) > 0:
            profile_token = profile_data["studentProfiles"][0].get("id")
        elif profile_data.get("teacherProfiles") and len(profile_data["teacherProfiles"]) > 0:
            profile_token = profile_data["teacherProfiles"][0].get("id")
            
        if not profile_token:
             raise Exception(f"Failed to find X-Profile token in API response: {profile_data}")
             
        print(f"[Scraper] Success! Profile Token: {profile_token}")

        # Add the final piece to our headers
        session.headers.update({
            'X-Profile': profile_token
        })
        
        # 4. Fetch Restaurants API
        print("[Scraper] Fetching restaurants API...")
        rest_response = session.get("https://sw-dine.uth.gr/feign/student/restaurant", timeout=10)
        
        if "application/json" not in rest_response.headers.get("Content-Type", ""):
            raise Exception(f"API returned HTML instead of JSON! Status: {rest_response.status_code}.")
            
        restaurants_data = rest_response.json()
        
        parsed_restaurants = []
        all_meal_types = []
        all_menu_items = []
        
        print(f"[Scraper] Found {len(restaurants_data)} restaurants. Fetching menus...")
        for rest in restaurants_data:
            parsed_restaurants.append({
                "id": rest["id"],
                "code": rest["code"],
                "title": rest["title"],
                "city": rest.get("city", "")
            })

            meal_type_schedule_by_id = {}
            meal_type_url = f"https://sw-dine.uth.gr/feign/student/restaurant/{rest['id']}/meal_type"
            meal_type_response = session.get(meal_type_url, timeout=10)

            if meal_type_response.status_code == 200 and "application/json" in meal_type_response.headers.get("Content-Type", ""):
                meal_type_rows = meal_type_response.json()
                for meal_type_row in meal_type_rows:
                    meal_type_data = meal_type_row.get("mealTypeId", {})
                    meal_type_id = meal_type_data.get("id")

                    if not meal_type_id:
                        continue

                    row = {
                        "id": meal_type_row.get("id"),
                        "restaurant_id": rest["id"],
                        "meal_type_id": meal_type_id,
                        "title": meal_type_data.get("title", "Meal"),
                        "sort_order": meal_type_data.get("sortOrder"),
                        "hour_from": meal_type_row.get("hourFrom"),
                        "hour_to": meal_type_row.get("hourTo"),
                        "is_active": bool(meal_type_row.get("isActive", True)),
                    }

                    if row["id"]:
                        all_meal_types.append(row)

                    meal_type_schedule_by_id[meal_type_id] = row
            
            menu_url = f"https://sw-dine.uth.gr/feign/student/restaurant/{rest['id']}/menu"
            menu_response = session.get(menu_url, timeout=10)
            
            if menu_response.status_code == 200 and "application/json" in menu_response.headers.get("Content-Type", ""):
                menu_weeks = menu_response.json()
                for week in menu_weeks:
                    for item in week.get("menuItems", []):
                        item_date = epoch_ms_to_athens_date(item["menuDate"])
                        meal_type_data = item.get("mealTypeId", {})
                        course_type_data = item.get("courseTypeId", {})
                        meal_data = item.get("mealId", {})

                        meal_type_id = meal_type_data.get("id")
                        meal_window = meal_type_schedule_by_id.get(meal_type_id, {})
                        
                        all_menu_items.append({
                            "id": item["id"],
                            "restaurant_id": rest["id"],
                            "date": item_date,
                            "meal_type_id": meal_type_id,
                            "meal_type": meal_type_data.get("title", "Meal"),
                            "meal_type_sort_order": meal_type_data.get("sortOrder")
                                if meal_type_data.get("sortOrder") is not None
                                else meal_window.get("sort_order"),
                            "meal_hour_from": meal_window.get("hour_from"),
                            "meal_hour_to": meal_window.get("hour_to"),
                            "course_type": course_type_data.get("title", "Dish"),
                            "course_sort_order": course_type_data.get("sortOrder"),
                            "item_sort_order": item.get("sortOrder"),
                            "food_name": meal_data.get("title", "Unknown food")
                        })
                        
        print(f"[Scraper] Success! Extracted {len(all_menu_items)} menu items.")
        return {
            "status": "success",
            "restaurants": parsed_restaurants,
            "meal_types": all_meal_types,
            "menu_items": all_menu_items
        }
        
    except Exception as e:
        print(f"[Scraper Exception] {str(e)}")
        return {"status": "error", "message": str(e)}