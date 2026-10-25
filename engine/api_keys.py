"""
Persistent API Key Store for DLNE Server.

Provides secure Bearer token authentication (dlne_sk_...) for VS Code extensions,
automated coding agents, and external API consumers.
"""

import os
import json
import time
import uuid
import secrets
import hashlib
import hmac
import threading
from typing import Dict, List, Optional, Tuple, Any

from engine.config import get_base_dir
from engine.logger import log_system, log_warning, log_error


def _api_keys_file_path() -> str:
    config_dir = os.path.join(get_base_dir(), "config")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "api_keys.json")


class ApiKeyManager:
    """Manages creation, persistent storage, and constant-time verification of API keys."""

    def __init__(self, file_path: Optional[str] = None):
        self._file_path = file_path
        self._lock = threading.Lock()

    @property
    def file_path(self) -> str:
        return self._file_path or _api_keys_file_path()

    def _load_data_locked(self) -> Dict[str, Any]:
        path = self.file_path
        if not os.path.exists(path):
            return {"keys": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict) or "keys" not in data:
                    return {"keys": []}
                return data
        except Exception as e:
            log_error(f"Failed to read API keys from {path}: {e}")
            return {"keys": []}

    def _save_data_locked(self, data: Dict[str, Any]) -> None:
        path = self.file_path
        tmp_path = f"{path}.tmp.{uuid.uuid4().hex}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            log_error(f"Failed to save API keys to {path}: {e}")
            raise

    def generate_key(self, name: str = "vscode-client") -> Tuple[str, Dict[str, Any]]:
        """
        Generate a new API key.
        Returns:
            Tuple of (raw_token, key_metadata).
            Note: raw_token is only returned once and is never stored in plaintext.
        """
        raw_token = f"dlne_sk_{secrets.token_hex(24)}"
        salt = secrets.token_hex(16)
        hashed = hashlib.sha256(f"{salt}:{raw_token}".encode("utf-8")).hexdigest()
        key_id = f"key_{uuid.uuid4().hex[:12]}"
        now = time.time()

        key_record = {
            "id": key_id,
            "name": name,
            "prefix": raw_token[:14] + "...",
            "hashed_key": hashed,
            "salt": salt,
            "created_at": now,
            "last_used_at": None,
            "revoked": False,
        }

        with self._lock:
            data = self._load_data_locked()
            data["keys"].append(key_record)
            self._save_data_locked(data)

        log_system(f"Created new API key '{name}' (ID: {key_id})")
        return raw_token, {
            "id": key_record["id"],
            "name": key_record["name"],
            "prefix": key_record["prefix"],
            "created_at": key_record["created_at"],
        }

    def verify_key(self, raw_token: str) -> Optional[Dict[str, Any]]:
        """
        Verify a raw bearer token against active stored keys in constant time.
        Returns key metadata if valid, None otherwise.
        """
        if not raw_token or not raw_token.startswith("dlne_sk_"):
            return None

        with self._lock:
            data = self._load_data_locked()
            matching_key: Optional[Dict[str, Any]] = None

            for key in data.get("keys", []):
                if key.get("revoked", False):
                    continue
                salt = key.get("salt", "")
                expected_hash = key.get("hashed_key", "")
                computed = hashlib.sha256(f"{salt}:{raw_token}".encode("utf-8")).hexdigest()

                if hmac.compare_digest(expected_hash, computed):
                    matching_key = key
                    break

            if matching_key:
                matching_key["last_used_at"] = time.time()
                try:
                    self._save_data_locked(data)
                except Exception:
                    pass
                return {
                    "id": matching_key["id"],
                    "name": matching_key["name"],
                    "prefix": matching_key["prefix"],
                    "created_at": matching_key["created_at"],
                    "last_used_at": matching_key["last_used_at"],
                }

        return None

    def list_keys(self) -> List[Dict[str, Any]]:
        """List all keys with safe metadata (no salts or hashes)."""
        with self._lock:
            data = self._load_data_locked()
            return [
                {
                    "id": k["id"],
                    "name": k["name"],
                    "prefix": k["prefix"],
                    "created_at": k["created_at"],
                    "last_used_at": k.get("last_used_at"),
                    "revoked": k.get("revoked", False),
                }
                for k in data.get("keys", [])
            ]

    def revoke_key(self, key_id: str) -> bool:
        """Revoke an API key by ID."""
        with self._lock:
            data = self._load_data_locked()
            found = False
            for k in data.get("keys", []):
                if k.get("id") == key_id and not k.get("revoked", False):
                    k["revoked"] = True
                    found = True
                    break
            if found:
                self._save_data_locked(data)
                log_system(f"Revoked API key ID: {key_id}")
                return True
            return False


# Global singleton instance
api_key_manager = ApiKeyManager()


# ======================================================================================
# Operator (admin) key
# ======================================================================================
#
# One long-lived key for whoever runs the network (in practice BarrkBOT, on the same host).
# It is not an API key for inference: it opens /v1/admin/*. The coordinator stores only the
# SHA-256 of it (ADMIN_KEY_HASH); the key itself is printed once, when it is minted.

ADMIN_KEY_PREFIX = "dlne_ak_"


def mint_admin_key() -> str:
    """A fresh random operator key (256 bits of entropy behind a recognisable prefix)."""
    return ADMIN_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_admin_key(key: str) -> str:
    """The value stored as ADMIN_KEY_HASH for ``key``. A fast hash is right for a random key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

