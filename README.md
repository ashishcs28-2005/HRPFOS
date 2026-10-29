# Hospital Resource and Patient Flow Optimization System

An academic MVP built with Flask and SQLite. It uses synthetic hospital data to demonstrate patient flow monitoring, resource utilization, ambulance locations, and operational recommendations.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:HRPFOS_ASHISH_PASSWORD = Read-Host "Set a local admin password"
python app.py
```

Open `http://127.0.0.1:5000` in a browser.

The first run creates `hospital.db` and seeds synthetic records. Use `flask --app app seed` to reset the sample data.

Ambulance locations are simulated. Add vehicles on the Ambulances page, choose an active patient, driver, pickup and destination, and save as Dispatched to start a trip. Fuel must be above 25%; maintenance vehicles must first be marked Available. Save as Completed to finish the trip, then Available to release the vehicle. Trip duration and fuel refill history are recorded separately. Existing dispatch audit entries remain visible; historical trips are not reconstructed from them. Restart `python app.py` after updating to apply the database additions.

Ambulance details are grouped into expandable vehicle cards. Trip statuses follow Available -> Dispatched -> On Route -> Arrived -> Completed -> Available; At Hospital is optional between Arrived and Completed. Maintenance is available from Available or Completed and returns to Available. Saving details without changing status is supported. Existing sample vehicles without trip records can finish their status sequence without creating fabricated trip history.

Trip history supports vehicle/patient search, active/completed status, and an inclusive start-date range. Export filtered trips CSV downloads every matching trip. Overdue maintenance dates are highlighted and block new dispatches; record an updated service schedule before dispatching. The simulated map scales coordinates to the filtered fleet and its numbered markers open vehicle details. It is a schematic view, without road navigation or live GPS.

The SQLite database is generated on first run and is excluded from version control, along with local logs, caches, and editor settings. This repository is an academic prototype. Login passwords come from environment variables: HRPFOS_ASHISH_PASSWORD (administrator), HRPFOS_ANKITA_PASSWORD, HRPFOS_TANMAY_PASSWORD and HRPFOS_AMAN_PASSWORD (staff). Accounts without a configured password cannot sign in. Passwords must be set again in a new terminal session. Set HRPFOS_SECRET_KEY to a private random value for sessions that persist across restarts; otherwise a fresh session key is generated at startup. Never commit credentials.
