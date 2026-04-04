from backend.security import get_password_hash, verify_password


def test_password_hash_roundtrip_for_normal_password():
    secret = "StudyAgent123"
    hashed = get_password_hash(secret)
    assert hashed.startswith("$2")
    assert verify_password(secret, hashed) is True


def test_password_hash_roundtrip_for_long_password():
    secret = "x" * 100
    hashed = get_password_hash(secret)
    assert hashed.startswith("bcrypt_sha256$")
    assert verify_password(secret, hashed) is True
