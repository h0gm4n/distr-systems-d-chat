import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, List

# Maximum number of messages to retain
MAX_MESSAGES = 100

logger = logging.getLogger("state_machine")


class ChatState:
    """Very simple chat state machine.

    Stores committed chat messages as a list and appends them to a JSONL file.
    
    Implements a retention policy: only the latest MAX_MESSAGES are kept.
    Since all RAFT nodes apply the same commands in the same order, the
    retention policy is applied deterministically and consistently across
    all nodes without requiring special leader coordination.
    """

    def __init__(self, path: str = "chat_log.jsonl"):
        self.path = path
        self._log: List[Dict] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    self._log.append(json.loads(line))
                except Exception:
                    continue
        # Backfill missing timestamps for older chat messages
        changed = False
        now = time.time()
        for msg in self._log:
            if isinstance(msg, dict) and msg.get("type") == "chat" and "timestamp" not in msg:
                # prefer any existing 'ts' field if present
                if "ts" in msg:
                    msg["timestamp"] = msg["ts"]
                else:
                    msg["timestamp"] = now
                changed = True
        if changed:
            # persist updated log with backfilled timestamps
            self._persist()
        # Apply retention policy on load to handle any pre-existing overflow
        if len(self._log) > MAX_MESSAGES:
            trimmed_count = len(self._log) - MAX_MESSAGES

            self._log = self._log[-MAX_MESSAGES:]
            self._persist()

            logger.info(
                "Applied retention policy on load: trimmed %d old messages, kept %d",
                trimmed_count, len(self._log)
            )

    async def apply(self, command: Dict) -> None:
        """Apply a committed command to the state.
        
        The retention policy is applied after each command. Since RAFT
        guarantees all nodes apply the same commands in the same order,
        all nodes will trim at the same point and maintain identical state.
        """
        # Handle room_clear command: remove all chat messages for the specified room
        if command.get("type") == "room_clear":
            room = command.get("room", "general")
            self._log = [
                m for m in self._log
                if not (m.get("type") == "chat" and m.get("room", "general") == room)
            ]
            # Append the room_clear event itself so clients can react to it
            self._log.append(command)
            self._persist()
            logger.info("Cleared all messages in room '%s'", room)
            return

        # Ensure chat messages include a stable timestamp field for clients
        if command.get("type") == "chat":
            # prefer existing explicit 'timestamp' or 'ts' field, otherwise set now
            if "timestamp" not in command:
                if "ts" in command:
                    command["timestamp"] = command["ts"]
                else:
                    command["timestamp"] = datetime.now().strftime('%H:%M')

        self._log.append(command)
        
        # Retention policy: keep only the latest MAX_MESSAGES
        if len(self._log) > MAX_MESSAGES:
            trimmed_count = len(self._log) - MAX_MESSAGES
            self._log = self._log[-MAX_MESSAGES:]
            self._persist()
            logger.debug(
                "Retention policy triggered: trimmed %d old messages, kept %d",
                trimmed_count, MAX_MESSAGES
            )
        else:
            # Just append the new message (no trimming needed)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(command, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _persist(self) -> None:
        """Persist the current log to disk atomically.
        
        Uses a temp file + rename pattern to ensure atomic writes,
        preventing file corruption on crashes.
        """
        try:
            temp_path = self.path + ".tmp"

            with open(temp_path, "w", encoding="utf-8") as f:
                for msg in self._log:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")

            os.replace(temp_path, self.path)
        except Exception as e:
            logger.warning("Failed to persist chat log: %s", e)

    def all_messages(self) -> List[Dict]:
        return list(self._log)
    
    def message_count(self) -> int:
        """Return the current number of stored messages."""
        return len(self._log)