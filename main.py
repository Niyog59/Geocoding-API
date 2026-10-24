"""Entry point kept for `python main.py` and `uvicorn main:app`."""

import os

from app.main import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")  # noqa: S104 - containers must listen on all interfaces
    uvicorn.run(app, host=host, port=int(os.getenv("PORT", "8000")))
