from flask import Flask, render_template, request, jsonify
from typing import List, Dict
import openai
import json
import os
import requests
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CarSearchSystem:
    def __init__(self, data: List[Dict], openai_api_key: str):
        self.data = data
        openai.api_key = openai_api_key
        self.available_cars = self._process_inventory()

    def _process_inventory(self) -> str:
        """Create a formatted string of all available cars with their details."""
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
        """Create a detailed system prompt for the conversational AI."""
        return f"""
You are a friendly and professional car dealership assistant. Below is our current inventory of cars:

{self.available_cars}

When the user asks for car recommendations, respond in the following structured and friendly format:

1. Greet the user warmly and acknowledge their query. For example:
   - "Hi, thank you for your query! I'd love to help you find the perfect car."
   - "You're in luck! We have some great options for you."
   - "Thanks for reaching out! Let me show you what we have."

2. Use the following format for recommendations:

   **Recommended Cars:**

   1. [Car Make & Model] - £[Price]
      Key Features: [Relevant features based on query]
      Why This Car: [Brief explanation about the recommendation]

   2. [Next car, if applicable]

   Example:

   Hi, thank you for your query! I'd love to help you find the perfect car. Based on what you're looking for:

   **Recommended Cars:**

   1. MINI HATCH - £11,495  
      Key Features: Compact size, fuel-efficient, stylish design  
      Why This Car: The MINI HATCH is perfect for young drivers looking for affordability and fun driving.

   2. AUDI A4 - £16,995  
      Key Features: Reliable, spacious, safe  
      Why This Car: The AUDI A4 offers luxury, performance, and a comfortable interior.

3. If no suitable cars match the user’s query:
   - Apologize politely and explain why.
   - Suggest alternatives or invite the user to refine their search.

Always ensure responses are clear, friendly, and formatted with proper line breaks.
"""

    def search(self, query_text: str) -> str:
        """Handle user queries using OpenAI's GPT model."""
        try:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": self._create_system_prompt()},
                    {"role": "user", "content": query_text}
                ],
                temperature=0.7,
                max_tokens=500
            )

            # Capture the response and ensure formatting
            return response.choices[0].message.content

        except Exception as e:
            logger.error(f"Error during search: {e}")
            return (
                "Hi there! Thank you for reaching out. It sounds like you're looking for a great car—I’d love to help with that!\n\n"
                "Unfortunately, I’m having a bit of trouble accessing the exact options for you right now. But don’t worry—we have a fantastic range of cars, "
                "and I’m sure there’s something here that’s just right for you!\n\n"
                "For instance, we have compact, fuel-efficient cars like the MINI HATCH and reliable sedans like the AUDI A4. "
                "If you share a bit more about your budget or specific preferences, I’d be happy to refine the recommendations and help you further!"
            )



    def search(self, query_text: str) -> str:
        """Handle user queries using OpenAI's GPT model."""
        try:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": self._create_system_prompt()},
                    {"role": "user", "content": query_text}
                ],
                temperature=0.7,
                max_tokens=500
            )

            # Capture the response and ensure formatting
            raw_response = response.choices[0].message.content
            return self._format_response(raw_response)

        except Exception as e:
            logger.error(f"Error during search: {e}")
            return (
                "I'm sorry, I couldn't process your query at the moment. Here are some cars from our inventory you might like. "
                "Feel free to ask me again with specific requirements!"
            )

    def _format_response(self, raw_response: str) -> str:
        """Ensure proper formatting of AI responses with clear line breaks."""
        lines = raw_response.split("\n")
        formatted_lines = []
        for line in lines:
            # Add line breaks before numbered car recommendations
            if line.strip().startswith("1.") or line.strip().startswith("2."):
                formatted_lines.append("\n\n" + line.strip())
            else:
                formatted_lines.append(line.strip())
        return "\n".join(formatted_lines)


def fetch_api_data():
    """Fetch car data from the Sun Motors API."""
    url = "https://api.sunmotors.co.uk/marketcheck/vehicles"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"API request failed: {e}")
        return None


def save_json_data(data):
    """Save API response to a JSON file with a timestamp."""
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
    """Load car data from a JSON file."""
    try:
        with open('vehicles_data.json', 'r') as f:
            return json.load(f)['data']  # Note: accessing the 'data' key
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []


def update_data(search_system):
    """Fetch new data and update the search system."""
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
    """Render the homepage."""
    return render_template('index.html')


@app.route('/api/search', methods=['POST'])
def search():
    """Handle search requests from the frontend."""
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
