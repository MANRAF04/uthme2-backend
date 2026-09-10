import requests
from bs4 import BeautifulSoup
import json
import re

def fetch_grades(username, password):
    session = requests.Session()
    
    # ADD THIS: Disguise the script as a normal web browser
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    
    # 1. URLs
    login_url = "https://cas.uth.gr/login?service=https%3A%2F%2Fsis-web.uth.gr%2Flogin%2Fcas"
    dashboard_url = "https://sis-web.uth.gr/student/grades/list_diploma"
    api_url = "https://sis-web.uth.gr/feign/student/grades/diploma"
    
    # --- STEP 2: CAS LOGIN HANDSHAKE ---
    try:
        response = session.get(login_url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        lt_token = soup.find('input', {'name': 'lt'})['value']
        execution_token = soup.find('input', {'name': 'execution'})['value']
        
        payload = {
            'username': username,
            'password': password,
            'lt': lt_token,
            'execution': execution_token,
            '_eventId': 'submit',
            'submitForm': 'Login'
        }
        
        login_response = session.post(login_url, data=payload, allow_redirects=True)
        if "cas.uth.gr/login" in login_response.url or "Invalid credentials" in login_response.text:
            return {"error": "Invalid University Credentials"}
            
    except Exception as e:
        return {"error": f"Login failed: {str(e)}"}

    # --- STEP 3: EXTRACT SECURITY TOKENS VIA API ---
    try:
        # 1. Fetch the root dashboard just to get the CSRF token from the meta tags
        dash_response = session.get("https://sis-web.uth.gr/")
        dash_soup = BeautifulSoup(dash_response.text, 'html.parser')
        
        csrf_token_tag = dash_soup.find('meta', {'name': '_csrf'})
        csrf_token = csrf_token_tag['content'] if csrf_token_tag else ""
        
        if not csrf_token:
            return {"error": "Could not find X-CSRF-TOKEN in the main page HTML."}

        # 2. Update headers to mimic a browser AJAX request
        session.headers.update({
            'X-CSRF-TOKEN': csrf_token,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest'
        })
        
        # 3. Hit the profiles API to get your exact X-Profile ID
        profiles_url = "https://sis-web.uth.gr/api/person/profiles"
        profiles_response = session.get(profiles_url)
        
        if 'application/json' not in profiles_response.headers.get('Content-Type', ''):
            return {
                "error": "Profiles API rejected us and returned HTML.",
                "html_snippet": profiles_response.text[:1000]
            }
            
        profiles_data = profiles_response.json()
        student_profiles = profiles_data.get("studentProfiles", [])
        
        if not student_profiles:
            return {"error": "API returned no student profiles for this user."}
            
        # Extract the profile ID from the first active student profile
        profile_token = student_profiles[0].get("id")
        
        # 4. Lock in the final headers needed for the Grades API
        referer_url = f"https://sis-web.uth.gr/student/grades/list_diploma?p={profile_token}"
        
        session.headers.update({
            'X-Profile': profile_token,
            'Referer': referer_url
        })
        
    except Exception as e:
        return {"error": f"Failed during Step 3 Token Extraction: {str(e)}"}

    # --- STEP 4: FETCH THE JSON GRADES ---
    try:
        # Now we hit the actual JSON API
        grades_response = session.get(api_url)
        
        # Catch redirect to HTML
        if 'application/json' not in grades_response.headers.get('Content-Type', ''):
            return {
                "error": "API refused the token and redirected to HTML.",
                "current_url": grades_response.url,
                "html_snippet": grades_response.text[:1500] 
            }

        grades_response.raise_for_status()
        raw_json = grades_response.json()
        
        clean_data = parse_and_clean_grades(raw_json)
        
        return {
            "status": "success",
            "data": clean_data
        }
    except Exception as e:
        return {"error": f"Failed to fetch JSON API: {str(e)}"}


def parse_and_clean_grades(raw_json):
    """
    Takes the massive JSON from the university and extracts only what the app needs.
    """
    clean_grades = []
    
    for item in raw_json:
        # Note on Grades: Based on your JSON, a grade of 7.9/10 is represented as 0.79.
        # We multiply by 10 to make it readable for the student.
        raw_grade = item.get("grade")
        display_grade = round(raw_grade * 10, 2) if isinstance(raw_grade, float) and raw_grade <= 1.0 else raw_grade
        
        course = {
            "id": item.get("id"),
            "title": item.get("title"),
            "code": item.get("courseCode"),
            "semester": item.get("studentSemester"),
            "ects": item.get("ects"),
            "grade": display_grade,
            "passed": item.get("isPassed"),
            "type": item.get("typeId", {}).get("title", "Unknown") # e.g. "Υποχρεωτικό"
        }
        clean_grades.append(course)
        
    # Sort the grades by semester, then by title
    clean_grades.sort(key=lambda x: (x['semester'] or 0, x['title']))
    return clean_grades
