"""Run once to initialize the database: python init_db.py"""
from app import app, init_db

if __name__ == '__main__':
    init_db()
    print("Database initialized.")
