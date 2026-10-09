"""
Load sample documents into MongoDB.
Run from the repository root:
    python sample_seed.py

Reads MONGO_URI and MONGO_DB from environment (or .env).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.environ.get("MONGO_DB",  "attendance_db")

client = MongoClient(MONGO_URI)
db     = client[MONGO_DB]


def parse_extended_json(obj):
    """Recursively convert MongoDB Extended JSON $date fields to Python datetimes."""
    if isinstance(obj, dict):
        if "$date" in obj and len(obj) == 1:
            return datetime.fromisoformat(obj["$date"].replace("Z", "+00:00")).replace(tzinfo=timezone.utc).replace(tzinfo=None)
        return {k: parse_extended_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [parse_extended_json(i) for i in obj]
    return obj


def seed_collection(collection_name: str, filename: str) -> None:
    path = Path("sample_data") / filename
    with open(path) as f:
        raw = json.load(f)
    docs = [parse_extended_json(d) for d in raw]

    col = db[collection_name]
    col.delete_many({})  # clear existing sample data
    result = col.insert_many(docs)
    print(f"  {collection_name}: inserted {len(result.inserted_ids)} documents")


if __name__ == "__main__":
    print(f"Seeding database '{MONGO_DB}' at {MONGO_URI}...")
    seed_collection("employees",      "employees.json")
    seed_collection("attendance_logs", "attendance_logs.json")
    print("Done.")
