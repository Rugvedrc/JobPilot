"""Start FresherPilot: python main.py -> http://localhost:8000"""
from fresherpilot.server import app, run  # noqa: F401  (app: for `uvicorn main:app`)

if __name__ == "__main__":
    run()
