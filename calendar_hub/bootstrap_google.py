"""Create a Google OAuth token file for Calendar access."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from calendar_hub.connectors import GOOGLE_SCOPES


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("client_secrets", help="Google OAuth desktop client JSON")
    parser.add_argument("output", help="Destination token JSON")
    args = parser.parse_args()

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, GOOGLE_SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(credentials.to_json(), encoding="utf-8")
    os.chmod(output, 0o600)
    print(f"Google Calendar token written to {output}")


if __name__ == "__main__":
    main()
