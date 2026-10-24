"""Entry point kept for `python main.py` and `uvicorn main:app`."""

import os

from app.main import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
