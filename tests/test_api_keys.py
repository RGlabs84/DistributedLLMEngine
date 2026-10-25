import os
import tempfile
import pytest
from engine.api_keys import ApiKeyManager


def test_api_key_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        keys_file = os.path.join(tmpdir, "api_keys.json")
        mgr = ApiKeyManager(file_path=keys_file)

        assert mgr.list_keys() == []

        # Generate a key
        raw_key, meta = mgr.generate_key("vscode-dev")
        assert raw_key.startswith("dlne_sk_")
        assert meta["name"] == "vscode-dev"

        # Verify key
        verified = mgr.verify_key(raw_key)
        assert verified is not None
        assert verified["id"] == meta["id"]
        assert verified["last_used_at"] is not None

        # Verify invalid keys
        assert mgr.verify_key("dlne_sk_invalid") is None
        assert mgr.verify_key("some_other_token") is None
        assert mgr.verify_key("") is None

        # List keys
        keys = mgr.list_keys()
        assert len(keys) == 1
        assert keys[0]["id"] == meta["id"]
        assert not keys[0]["revoked"]

        # Revoke key
        assert mgr.revoke_key(meta["id"]) is True
        assert mgr.verify_key(raw_key) is None

        keys_after = mgr.list_keys()
        assert keys_after[0]["revoked"] is True

