from app import app, db

# This script creates the database file and all necessary tables.
# Run this once after deleting your old 'database.db' file.

with app.app_context():
    print("Creating database tables (User and Round)...")
    db.create_all()
    print("Database tables created successfully!")