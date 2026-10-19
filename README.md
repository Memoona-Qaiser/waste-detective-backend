# Resource Waste Detective (Backend)

AI-powered resource waste detection for buildings (electricity and water).
It does not only flag unusual readings. It answers: can we show a resource is
being wasted, how much money is lost, what action reduces it, and did that
action actually work?

## Key principles
- Excess consumption is NOT confirmed waste. Only inspection plus measured
  improvement confirms it.
- Estimated and verified savings are always shown separately.
- If there is not enough comparable data after an action, verified savings = Rs 0.

## Important note
**Data is simulated; testing on real meters or the LEAD dataset is the next phase.**

## Tech stack
FastAPI, pandas, numpy, scikit-learn, sqlite. Optional: Claude API for explanations.

## How to run
```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
API docs: http://127.0.0.1:8000/docs

## Optional: AI explanations
```powershell
pip install anthropic
$env:ANTHROPIC_API_KEY="your-key-here"
```
Without a key, a template explanation is used. Never commit API keys.

## Sample CSV files
`sample_good.csv`, `sample_bad.csv`, `sample_wrong.csv` are for testing the upload page.

## Frontend
See the waste-detective frontend repository.

## Known limitations
- All data is simulated and dates are hard-coded.
- Roles (Manager / Technician / Auditor) are not real authentication.
- Water tariff (Rs 200/m3) is an assumption.
- Verification does not adjust for weather or occupancy.