import json
import os
from datetime import datetime

def save_fema_response(address, response_data):
    os.makedirs("audit/fema_responses", exist_ok=True)

    filename = (
        datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        + ".json"
    )

    path = os.path.join(
        "audit/fema_responses",
        filename
    )

    with open(path, "w") as f:
        json.dump(response_data, f, indent=2)

    return path
