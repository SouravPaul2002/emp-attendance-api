# Employee Attendance & Analytics API

A FastAPI + MongoDB backend service implementing attendance tracking (punch-in, punch-out, regularization) and analytics pipelines (monthly summaries, department aggregation, late-comers competition leaderboard, and daily moving average trend).

Built according to `PROBLEM_STATEMENT.docx` and `openapi.yaml` (v2.0.0).

---

## Deliverables

- `app/main.py`: Complete application code with all endpoints and startup database indexing.
- `REVIEW.md`: Detailed audit table listing all defects identified in the starter code, symptoms, and fixes.
- `DECISIONS.md`: Architectural decisions regarding indexes, punch-in race concurrency, rank ties, zero-log headcount, and scaling to 100x data.
- `requirements.txt`: Minimal dependencies for running the service.

---

## Local Setup

### 1. Environment & Dependencies
```bash
python -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows:
venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` and set your MongoDB connection details:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(Environment variables take precedence over `.env`).*

### 3. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Run Server
```bash
uvicorn app.main:app --port 8000
```
API documentation will be available at `http://127.0.0.1:8000/docs`.

---

## Key Features & Guarantees

- **Indexes at Startup**: Automatically creates unique compound index on `("emp_code", "date")`, unique on `("emp_code")`, and compound indexes supporting sorting and pagination without in-memory sorts.
- **Race Condition Safety**: Uses atomic MongoDB updates (`find_one_and_update`) and unique constraint handling (`DuplicateKeyError` -> `409 Conflict`) for concurrent punch-ins, punch-outs, and employee creations.
- **Rules R1 – R10 Compliance**: Correct IST calendar boundary calculations, overnight shift date alignment, 10-minute grace window for late calculation, `>= 30 min` overtime threshold, and half-up rounding.
- **Database-Level Analytics**: Analytics pipelines (`$densify`, `$setWindowFields`, competition `$rank`, and cross-collection `$lookup`) run directly within MongoDB.
- **Diagnostics (`/admin/explain/{endpoint}`)**: Returns execution plans using `IXSCAN` without full collection scans (`COLLSCAN`).
