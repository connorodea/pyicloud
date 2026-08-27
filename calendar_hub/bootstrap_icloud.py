"""One-time interactive iCloud 2FA bootstrap for the persistent cookie volume."""

from __future__ import annotations

from getpass import getpass

from calendar_hub.config import Settings


def main() -> None:
    settings = Settings()
    if not settings.pyicloud_apple_id:
        raise SystemExit("Set PYICLOUD_APPLE_ID before running the bootstrap")
    try:
        password = settings.read_secret(settings.pyicloud_password_file)
    except RuntimeError:
        password = getpass("Apple Account password: ")

    from pyicloud import PyiCloudService

    api = PyiCloudService(
        settings.pyicloud_apple_id,
        password,
        cookie_directory=settings.pyicloud_cookie_directory,
        china_mainland=settings.pyicloud_china_mainland,
    )
    if api.requires_2fa:
        code = input("Enter the verification code shown on your Apple device: ").strip()
        if not api.validate_2fa_code(code):
            raise SystemExit("The iCloud verification code was rejected")
        if not api.is_trusted_session and not api.trust_session():
            raise SystemExit("The iCloud session could not be trusted")
    elif api.requires_2sa:
        devices = api.trusted_devices
        for index, device in enumerate(devices):
            label = device.get("deviceName") or device.get("phoneNumber") or "device"
            print(f"{index}: {label}")
        selected = int(input("Choose a trusted device: ").strip() or "0")
        device = devices[selected]
        if not api.send_verification_code(device):
            raise SystemExit("Apple could not send a verification code")
        code = input("Enter the verification code: ").strip()
        if not api.validate_verification_code(device, code):
            raise SystemExit("The iCloud verification code was rejected")
    api.calendar.events()
    print("iCloud calendar authentication is ready.")


if __name__ == "__main__":
    main()
