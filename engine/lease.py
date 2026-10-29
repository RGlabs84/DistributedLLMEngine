"""
Inference Lease Manager for DLNE.

Guarantees that active inference requests, agent tool sessions, and in-flight file edits
hold an active lease, mathematically preventing the coordinator from tearing down or
swapping models mid-edit or mid-stream.
"""

import time
import threading
import uuid
import asyncio
from typing import Dict, List, Optional, Any


class InferenceLease:
    """Represents a single claimed execution lease."""

    def __init__(
        self,
        lease_id: str,
        task_type: str,
        timeout: float,
        manager: "InferenceLeaseManager",
        client_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.lease_id = lease_id
        self.task_type = task_type
        self.timeout = timeout
        self.client_id = client_id or "unknown"
        self.created_at = time.time()
        self.expires_at = self.created_at + timeout
        self.metadata = metadata or {}
        self._manager = manager
        self._released = False

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def touch(self, extra_seconds: Optional[float] = None) -> None:
        """Extend lease duration if a long multi-step agent task is progressing."""
        if not self._released:
            extension = extra_seconds if extra_seconds is not None else self.timeout
            self.expires_at = time.time() + extension

    def release(self) -> None:
        """Release this lease back to the manager."""
        if not self._released:
            self._released = True
            self._manager._remove_lease(self.lease_id)

    async def __aenter__(self) -> "InferenceLease":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    def __enter__(self) -> "InferenceLease":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


class InferenceLeaseManager:
    """
    Tracks in-flight leases across chat, completion, and agent edit streams.
    Thread-safe and async-safe.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._leases: Dict[str, InferenceLease] = {}

    def _prune_expired_locked(self) -> None:
        now = time.time()
        expired = [lid for lid, lease in self._leases.items() if now > lease.expires_at]
        for lid in expired:
            del self._leases[lid]

    def acquire(
        self,
        task_type: str = "chat",
        timeout: float = 120.0,
        client_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> InferenceLease:
        """Acquire a new inference lease."""
        lease_id = f"lease_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._prune_expired_locked()
            lease = InferenceLease(
                lease_id=lease_id,
                task_type=task_type,
                timeout=timeout,
                manager=self,
                client_id=client_id,
                metadata=metadata,
            )
            self._leases[lease_id] = lease
            return lease

    def _remove_lease(self, lease_id: str) -> None:
        with self._lock:
            self._leases.pop(lease_id, None)

    def release(self, lease_id: str) -> bool:
        """Explicitly release a lease by its ID."""
        with self._lock:
            lease = self._leases.pop(lease_id, None)
            if lease:
                lease._released = True
                return True
            return False

    def is_locked(self) -> bool:
        """True if there are active, unexpired leases guarding running models."""
        with self._lock:
            self._prune_expired_locked()
            return len(self._leases) > 0

    def active_count(self, task_type: Optional[str] = None) -> int:
        """Return the count of active, unexpired leases (of one task type, if given)."""
        with self._lock:
            self._prune_expired_locked()
            if task_type is None:
                return len(self._leases)
            return sum(1 for lease in self._leases.values() if lease.task_type == task_type)

    def active_leases_summary(self) -> List[Dict[str, Any]]:
        """Return summary of in-flight tasks for telemetry/status bar."""
        with self._lock:
            self._prune_expired_locked()
            return [
                {
                    "lease_id": lease.lease_id,
                    "task_type": lease.task_type,
                    "client_id": lease.client_id,
                    "elapsed_seconds": round(time.time() - lease.created_at, 2),
                    "remaining_seconds": max(0.0, round(lease.expires_at - time.time(), 2)),
                    "metadata": lease.metadata,
                }
                for lease in self._leases.values()
            ]

    def public_summary(self) -> List[Dict[str, Any]]:
        """
        The in-flight tasks as /v1/cluster/status shows them to anyone: each one's kind and timing,
        nothing that says whose it is. ``client_id`` is the caller's IP address and ``metadata`` the
        caller's own, so neither leaves this process; until 2.4.9 the public status carried both.
        """
        return [{"task_type": item["task_type"], "elapsed_seconds": item["elapsed_seconds"],
                 "remaining_seconds": item["remaining_seconds"]}
                for item in self.active_leases_summary()]

    async def wait_for_idle(self, timeout: float = 30.0, poll_interval: float = 0.5) -> bool:
        """
        Asynchronously wait until active_count() is 0.
        Returns True if idle was achieved, False if timed out.
        """
        start = time.time()
        while time.time() - start < timeout:
            if not self.is_locked():
                return True
            await asyncio.sleep(poll_interval)
        return not self.is_locked()


# Global singleton instance
lease_manager = InferenceLeaseManager()

