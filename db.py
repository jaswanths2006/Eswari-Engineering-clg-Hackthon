"""MongoDB Atlas connection and small helpers. Documents keep an integer "id" field
(from the counters collection) so URLs and forms can use simple numeric ids."""
import os

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient, ReturnDocument

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

OFFLINE = False


def _connect():
    """MongoDB Atlas when reachable; otherwise an in-memory database (offline demo mode), so the app
    still opens on networks that block Atlas or before your IP is on the Atlas access list."""
    global OFFLINE
    uri = os.environ.get("MONGODB_URI")
    if uri and os.environ.get("FORCE_OFFLINE") != "1":
        try:
            c = MongoClient(uri, serverSelectionTimeoutMS=8000)
            c.admin.command("ping")
            return c
        except Exception as e:  # unreachable network, IP not allowed, bad credentials…
            print(f"\n[db] MongoDB Atlas unreachable ({str(e)[:90]}…)")
    import mongomock
    OFFLINE = True
    print("[db] OFFLINE MODE: using an in-memory database with demo data (changes are not saved to Atlas).\n")
    return mongomock.MongoClient()


client = _connect()
db = client[os.environ.get("MONGODB_DB", "campusverse")]

NO_ID = {"_id": 0}


def next_id(coll):
    doc = db.counters.find_one_and_update({"_id": coll}, {"$inc": {"seq": 1}}, upsert=True,
                                          return_document=ReturnDocument.AFTER)
    return doc["seq"]


def find(coll, flt=None, sort=None, limit=0):
    cur = db[coll].find(flt or {}, NO_ID)
    if sort:
        cur = cur.sort(sort)
    if limit:
        cur = cur.limit(limit)
    return list(cur)


def find_one(coll, flt):
    return db[coll].find_one(flt, NO_ID)


def insert(coll, doc):
    doc = dict(doc, id=next_id(coll))
    db[coll].insert_one(dict(doc))  # insert a copy so the caller's dict doesn't get an _id
    return doc["id"]


def update(coll, flt, fields):
    db[coll].update_one(flt, {"$set": fields})


def count(coll, flt=None):
    return db[coll].count_documents(flt or {})


def ensure_indexes():
    db.users.create_index("username", unique=True)
    db.users.create_index("id", unique=True)
    db.attendance.create_index([("student_id", ASCENDING), ("subject_id", ASCENDING)])
    db.marks.create_index("student_id")
    db.applications.create_index([("job_id", ASCENDING), ("student_id", ASCENDING)], unique=True)
    db.quizzes.create_index([("note_id", ASCENDING), ("student_id", ASCENDING)])
    db.tickets.create_index("student_id")
    db.chat_messages.create_index("student_id")
    db.grievances.create_index("token", unique=True)
    db.enrollments.create_index([("student_id", ASCENDING), ("subject_id", ASCENDING)], unique=True)
    db.offerings.create_index("subject_id")
