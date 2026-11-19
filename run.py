"""
run.py  –  Flask application entry point
Usage:
    python run.py
Server will start at http://127.0.0.1:5000/
"""
from __init__ import create_app

app = create_app()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
