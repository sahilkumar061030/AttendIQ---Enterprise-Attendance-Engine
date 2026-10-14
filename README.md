# Employee Attendance & Analytics API

FastAPI backend with MongoDB 6.0+ storing employee attendance, punch events, and analytical rollups.

## Setup & Run

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables:**
   Set `MONGO_URI` and `MONGO_DB` in your environment or in `.env` (system environment variables take priority):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed Database:**
   ```bash
   python sample_seed.py
   ```

4. **Run Server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

## Assumptions & Design Choices

1. **Timezone & Instants (R1):** Shift schedules and attendance dates operate strictly in Indian Standard Time (IST, UTC+05:30). All API timestamps are transmitted as epoch milliseconds and truncated to whole seconds upon receipt and persistence.
2. **Overnight Shifts (R1):** An overnight shift is recognized when `shift_end <= shift_start`. Punches occurring before `shift_end` are attributed to the shift that started on the previous calendar day.
3. **Grace Period & Late Calculation (R2):** Punches occurring strictly more than 10 minutes past shift start are late. The late duration is computed as the floor of elapsed minutes from shift start.
4. **Rounding (R4, R8):** Standard half-up arithmetic (`ROUND_HALF_UP`) is used for durations, percentages, and moving averages instead of banker's rounding.
5. **Competition Ranking (Leaderboard):** Tied employees share identical ranks while skipping subsequent ranks (1, 2, 2, 4). The `limit` query parameter filters records with `rank <= limit`, allowing tie-breakers to return more than `limit` rows when ties occur at the cutoff boundary.
6. **Headcount & Department Summary (R9):** Aggregation starts from the `employees` collection filtered by `joined_on <= end_of_month` to ensure employees with zero attendance records are included in department headcount.
