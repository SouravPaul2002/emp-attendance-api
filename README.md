# Employee Attendance API

## How to run it

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Start the server:
```bash
uvicorn app.main:app --port 8000
```

## Environment variables

Set these in your environment or in a `.env` file in the root folder:

- `MONGO_URI`: MongoDB connection string (example: `mongodb://localhost:27017`)
- `MONGO_DB`: database name (example: `attendance_db`)

Real environment variables take priority over values in `.env`.

## Notes for the reviewer

All application code lives in `app/main.py`.
Indexes are created automatically when the FastAPI app starts up in `lifespan`.
You can open `http://127.0.0.1:8000/docs` in a browser to see the interactive Swagger UI.
