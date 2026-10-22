
import os
import requests
import firebase_admin

from firebase_admin import auth, credentials


def initialize_firebase():
    """Initialize Firebase Admin SDK once."""
    if firebase_admin._apps:
        return

    service_account_path = os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT",
        r"D:\neet27\serviceAccountKey.json",
    )

    if not os.path.isfile(service_account_path):
        raise RuntimeError(
            "Firebase service-account JSON was not found. "
            "Set FIREBASE_SERVICE_ACCOUNT to its path."
        )

    credential = credentials.Certificate(service_account_path)
    firebase_admin.initialize_app(credential)


def firebase_email_password_login(email: str, password: str) -> dict:
    """
    Authenticate using Firebase Email/Password REST API.
    Return the verified Firebase user claims.
    """
    api_key = os.environ.get("FIREBASE_WEB_API_KEY")

    if not api_key:
        raise RuntimeError("FIREBASE_WEB_API_KEY is not configured.")

    response = requests.post(
        "https://identitytoolkit.googleapis.com/v1/"
        f"accounts:signInWithPassword?key={api_key}",
        json={
            "email": email.strip(),
            "password": password,
            "returnSecureToken": True,
        },
        timeout=15,
    )

    if response.status_code != 200:
        # Do not expose detailed authentication errors to the user.
        raise ValueError("Invalid email or password.")

    payload = response.json()
    id_token = payload.get("idToken")

    if not id_token:
        raise ValueError("Firebase did not return an ID token.")

    # Verify signature, issuer, audience, expiry, and token validity.
    decoded_token = auth.verify_id_token(
        id_token,
        check_revoked=True,
    )

    return decoded_token


def is_authorized_admin(uid: str) -> bool:
    """
    Only UIDs explicitly listed in FIREBASE_ADMIN_UIDS are admins.
    Separate multiple UIDs with commas.
    """
    allowed_uids = {
        item.strip()
        for item in os.environ.get("FIREBASE_ADMIN_UIDS", "").split(",")
        if item.strip()
    }

    return bool(uid and uid in allowed_uids)


def verify_admin_session_token(id_token: str) -> dict:
    """Verify a Firebase ID token and enforce the admin UID allowlist."""
    initialize_firebase()

    claims = auth.verify_id_token(id_token, check_revoked=True)
    uid = claims.get("uid", "")

    if not is_authorized_admin(uid):
        raise PermissionError("This account is not authorized as an admin.")

    return claims
