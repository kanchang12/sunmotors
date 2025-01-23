from flask import Flask, render_template, request, jsonify
from dataclasses import dataclass
from typing import List, Dict
from openai import OpenAI
import json
import os

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
Format your response as:

Recommended Cars:
1. [Car Make & Model] - £[Price]
   Key Features: [List relevant features for the query]
   Why This Car: [Brief explanation]

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
        
        # Check if the response contains a list of cars
        if result.lower().startswith('recommended cars:'):
            # Convert to table format
            lines = result.split('\n')
            table = "| Car | Price | Key Features |\n|-----|-------|---------------|\n"
            for line in lines[1:]:
                if line.strip() and line.startswith(tuple('0123456789')):
                    parts = line.split('-')
                    car_info = parts[0].strip()
                    price = parts[1].split('£')[1].split('Key')[0].strip()
                    features = line.split('Key Features:')[1].split('Why This Car:')[0].strip()
                    table += f"| {car_info} | £{price} | {features} |\n"
            return table
        
        return result
        
    except Exception as e:
        return f"Error processing query: {str(e)}"

# Flask application
app = Flask(__name__)

# Load car data and initialize search system
def load_car_data():
    try:
        with open('vehicles_data.json', 'r') as f:
            return json.load(f)['data']  # Note: accessing the 'data' key
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []

# Initialize the search system
car_data = load_car_data()
openai_api_key = os.getenv("OPENAI_API_KEY") 
search_system = CarSearchSystem(car_data, openai_api_key)

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
