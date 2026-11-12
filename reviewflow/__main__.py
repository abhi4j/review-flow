import os

import uvicorn

from reviewflow.main import create_app

if __name__ == "__main__":
    # Our JSON request log replaces uvicorn's access log; log_config=None keeps our handlers.
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_config=None,
        access_log=False,
    )
