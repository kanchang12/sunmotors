from flask import Flask, render_template, request, jsonify
from typing import List, Dict
from openai import OpenAI
import json
import os
import requests
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CarSearchSystem:
    def __init__(self, data: List[Dict], openai_api_key: str):
        self.data = data
        self.client = OpenAI(api_key=openai_api_key)
        self.available_cars = self._process_inventory()
        
    def _process_inventory(self) -> str:
        """Create a formatted string of all available cars with their details"""
        inventory = []
        for car in self.data:
            v = car['vehicle']
            car_details = (
                f"Car ID: {car['uniqueId']}\n"
                f"Make & Model: {v['heading']}\n"
                f"Price: £{v['price']}\n"
                f"Year: {v['vehicleRegistrationYear']}\n"
                f"Mileage: {v['miles']} miles\n"
                f"Color: {v['exteriorColor']}\n"
                "---"
            )
            inventory.append(car_details)
        
        return "\n".join(inventory)
    
    def _create_system_prompt(self) -> str:
        return f"""You are a car dealership assistant. Below is our current inventory of cars:

{self.available_cars}

Based on the user's query, recommend the most suitable cars from our inventory ONLY.
If we don't have cars that match the specific requirements, apologize and explain what we do have that comes closest.

These are the way you should answer:

Hi, thank you. Yes you can see these cars. /or
Sorry, we don't have that now but we have a very close alternative. However these cars. /or
If the customer asks, follow up questions, based on the car inventory, you can find the data in your training data set and answer.
So if the customer asks a specific thing about a car, and that is not available in the json, you will check your vast training knowledge and answer.

The recommended cars between two cars please give two line breaks
Recommended Cars:
1. [Car Make & Model] - £[Price]
   Key Features: [List relevant features for the query]
   Why This Car: [Brief explanation]
line break - line break
2. [Next car if applicable]
   ...

If no suitable cars are found, explain why and suggest alternatives from our inventory."""

    def search(self, query_text: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": self._create_system_prompt()},
                    {"role": "user", "content": query_text}
                ],
                temperature=0.7,
                max_tokens=250
            )
            
            result = response.choices[0].message.content
            return result
    
        except Exception as e:
            return f"I apologize, but I couldn't process your query. Here are some used cars we have available that might interest you. Our inventory includes various models from compact city cars to spacious SUVs, with prices ranging from budget-friendly to premium options. Feel free to ask about specific requirements like budget, size, or fuel type."

def fetch_api_data():
    """Fetch car data from the Sun Motors API"""
    url = "https://api.sunmotors.co.uk/marketcheck/vehicles"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"API request failed: {e}")
        return None

def save_json_data(data):
    """Save API response to JSON file with timestamp"""
    if data:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = "vehicles_data.json"
        
        if os.path.exists(filename):
            backup_filename = f"vehicles_data_backup_{timestamp}.json"
            try:
                os.rename(filename, backup_filename)
            except Exception as e:
                logger.error(f"Error creating backup: {e}")
        
        try:
            with open(filename, 'w') as f:
                json.dump(data, f, indent=4)
            logger.info(f"Data saved successfully at {timestamp}")
            return True
        except Exception as e:
            logger.error(f"Error saving data: {e}")
            return False
    return False

def load_car_data():
    try:
        with open('vehicles_data.json', 'r') as f:
            return json.load(f)['data']  # Note: accessing the 'data' key
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []

def update_data(search_system):
    """Fetch new data and update search system"""
    logger.info("Starting data update...")
    new_data = fetch_api_data()
    if new_data and save_json_data(new_data):
        search_system.data = new_data['data']
        search_system.available_cars = search_system._process_inventory()
        logger.info("Search system updated with new data")
    else:
        logger.error("Failed to update data")

# Flask application
app = Flask(__name__)

# Load car data and initialize search system
car_data = load_car_data()
openai_api_key = os.getenv("OPENAI_API_KEY") 
search_system = CarSearchSystem(car_data, openai_api_key)

# Setup scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(
    func=lambda: update_data(search_system),
    trigger="interval",
    minutes=60,
    id='update_car_data'
)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search', methods=['POST'])
def search():
    try:
        data = request.get_json()
        query = data.get('query')
        
        if not query:
            return jsonify({'error': 'No query provided'}), 400
        
        result = search_system.search(query)
        return jsonify({'results': result})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
