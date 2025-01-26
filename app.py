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
        self.client = openai.OpenAI(api_key=openai_api_key)
        self.available_cars = self._process_inventory()
        self.chat_history = []
        self.car_makes = self._get_unique_makes()  # Get list of car makes in inventory

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

    def _get_unique_makes(self) -> List[str]:
        """Extract unique car makes from inventory."""
        makes = set()
        for car in self.data:
            make = car['vehicle']['heading'].split()[0]
            makes.add(make.upper())
        return list(makes)

    def _is_general_car_query(self, query: str) -> bool:
        """Check if the query is about general car knowledge rather than inventory."""
        # Look for comparison words or general car topics
        general_indicators = ['better', 'worse', 'vs', 'versus', 'compare', 'difference', 
                            'reliable', 'safety', 'brand', 'manufacturer', 'opinion']
        query_words = query.lower().split()
        
        # Check if query contains general indicators or multiple car makes
        mentioned_makes = sum(1 for make in self.car_makes if make.lower() in query.lower())
        return any(word in query_words for word in general_indicators) or mentioned_makes > 1

    def _create_system_prompt(self, is_general: bool) -> str:
        """Create appropriate system prompt based on query type."""
        if is_general:
            return """You are a knowledgeable car expert. Provide brief, accurate answers about car-related topics.
                     Keep responses concise and factual. Use comparisons when relevant."""
        else:
            return f"""You are a car dealership assistant. Here's our inventory:
    {self.available_cars}
    
    You should:
    1. Check inventory for exact matches
    2. If no exact matches, suggest alternatives
    3. Keep responses brief and friendly
    
    Format your response like this:
    "I found [car details] that matches your requirements. Please provide the following details of the car in the chat: The car name, availability, Price Condition and Location in a tabular format" OR
    "While I don't have an exact match, here are some alternatives: [car suggestions]"
    
    Never include the instruction numbers (1., 2., etc.) in your response."""

    def search(self, query_text: str) -> str:
        """Handle user queries using OpenAI's GPT model."""
        try:
            # Add user's message to chat history
            self.chat_history.append({"role": "user", "content": query_text})
            if len(self.chat_history) > 4:
                self.chat_history = self.chat_history[-4:]

            # Determine if this is a general car query
            is_general = self._is_general_car_query(query_text)
            
            messages = [
                {"role": "system", "content": self._create_system_prompt(is_general)}
            ] + self.chat_history

            response = self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0.7,
                max_tokens=500
            )

            result = response.choices[0].message.content
            self.chat_history.append({"role": "assistant", "content": result})
            
            return result

        except Exception as e:
            logger.error(f"Error during search: {e}")
            return "I apologize, but I'm having trouble processing your request. Could you please try again?"

    def _format_response(self, raw_response: str) -> str:
        """Ensure proper formatting of AI responses with clear line breaks."""
        lines = raw_response.split("\n")
        formatted_lines = []
        for line in lines:
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
